"""
Serial Hub：常驻进程，独占串口，服务网页 + MCP HTTP API。

- HTTP  : 默认 127.0.0.1:8080  （页面 + REST）
- WS    : 默认 127.0.0.1:8081  （实时日志）
- 环形缓冲：网页发送、AI 发送、下位机数据共用，MCP 按 cursor 拉取
"""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import pathlib
import sys
import threading
import time
import traceback
import queue
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import serial
import serial.tools.list_ports
import websockets

from umeko_serial_mcp.version import HUB_API_PATHS, HUB_FEATURES, HUB_VERSION

SUPPORTED_ENCODINGS = ("utf-8", "gbk", "gb18030")
DEFAULT_HTTP_PORT = int(os.environ.get("SERIAL_MCP_HTTP_PORT", "8080"))
DEFAULT_HOST = os.environ.get("SERIAL_MCP_HOST", "127.0.0.1")
BUFFER_MAX = int(os.environ.get("SERIAL_MCP_BUFFER_MAX", "5000"))
AUTO_RECONNECT = os.environ.get("SERIAL_MCP_AUTO_RECONNECT", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
TX_QUEUE_SIZE = int(os.environ.get("SERIAL_MCP_TX_QUEUE", "256"))


def default_encoding() -> str:
    env = os.environ.get("SERIAL_MCP_ENCODING", "").strip().lower()
    if env in ("utf-8", "utf8", "gbk", "gb18030", "gb2312", "cp936", "936"):
        return normalize_encoding(env)
    if sys.platform == "win32":
        return "gbk"
    return "utf-8"


def normalize_encoding(encoding: str) -> str:
    value = (encoding or "utf-8").strip().lower().replace("_", "-")
    aliases = {
        "utf8": "utf-8",
        "utf-8": "utf-8",
        "gbk": "gbk",
        "936": "gbk",
        "cp936": "gbk",
        "gb2312": "gb18030",
        "gb18030": "gb18030",
    }
    normalized = aliases.get(value)
    if normalized is None:
        raise ValueError(f"不支持的编码: {encoding}，可选: {', '.join(SUPPORTED_ENCODINGS)}")
    return normalized


class SerialHub:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.active_serial: serial.Serial | None = None
        self.serial_encoding = default_encoding()
        self.serial_decoder = codecs.getincrementaldecoder(self.serial_encoding)(errors="replace")
        self.status = {
            "connected": False,
            "port": None,
            "baudrate": 115200,
            "encoding": self.serial_encoding,
            "reconnecting": False,
            "version": HUB_VERSION,
        }
        self._seq = 0
        self.buffer: deque[dict[str, Any]] = deque(maxlen=BUFFER_MAX)
        self.buffer_cv = threading.Condition(self.lock)
        self.read_thread: threading.Thread | None = None
        self.read_running = False
        self.ws_clients: set = set()
        self.ws_loop: asyncio.AbstractEventLoop | None = None
        self.ws_ready = threading.Event()
        self.http_port = DEFAULT_HTTP_PORT
        self.ws_port = DEFAULT_HTTP_PORT + 1
        self.host = DEFAULT_HOST

        # 上次成功连接参数（自动重连用）
        self.last_connect: dict[str, Any] = {
            "port": None,
            "baudrate": 115200,
            "encoding": self.serial_encoding,
        }
        self.auto_reconnect = AUTO_RECONNECT
        self._user_closed = True  # 用户主动断开后不自动重连
        self._reconnect_thread: threading.Thread | None = None
        self._reconnect_stop = threading.Event()

        # 发送队列：串行化写串口，避免网页周期 + MCP 交错无序
        self._tx_queue: queue.Queue = queue.Queue(maxsize=max(16, TX_QUEUE_SIZE))
        self._tx_thread = threading.Thread(target=self._tx_worker, name="serial-tx", daemon=True)
        self._tx_thread.start()

    # ---------- ports ----------
    def enumerate_ports(self) -> list[dict]:
        ports = serial.tools.list_ports.comports()
        result = []
        for p in sorted(ports, key=lambda x: x.device):
            result.append(
                {
                    "device": p.device,
                    "description": p.description or "",
                    "hwid": getattr(p, "hwid", "") or "",
                }
            )
        return result

    def ports_text(self) -> str:
        ports = self.enumerate_ports()
        if not ports:
            return "未找到串口"
        return "\n".join(f"{p['device']} - {p['description']}" for p in ports)

    # ---------- encoding ----------
    def reset_decoder(self, encoding: str | None = None) -> None:
        if encoding is not None:
            self.serial_encoding = normalize_encoding(encoding)
        self.serial_decoder = codecs.getincrementaldecoder(self.serial_encoding)(errors="replace")
        self.status["encoding"] = self.serial_encoding

    def decode_bytes(self, data: bytes) -> str:
        return self.serial_decoder.decode(data, final=False)

    def encode_text(self, text: str) -> bytes:
        return text.encode(self.serial_encoding, errors="replace")

    # ---------- log buffer ----------
    def append_log(self, source: str, msg: str, *, hex_msg: str | None = None) -> dict[str, Any]:
        with self.buffer_cv:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "ts": time.time(),
                "source": source,
                "msg": msg,
            }
            if hex_msg is not None:
                entry["hex"] = hex_msg
            self.buffer.append(entry)
            self.buffer_cv.notify_all()
        self.broadcast_log(entry)
        return entry

    def read_since(self, cursor: int = 0, limit: int = 200) -> dict[str, Any]:
        """返回 seq > cursor 的条目；不消费、不清空（多客户端可各自拉）。"""
        limit = max(1, min(int(limit or 200), 1000))
        cursor = int(cursor or 0)
        with self.lock:
            items = [e for e in self.buffer if e["seq"] > cursor][:limit]
            next_cursor = items[-1]["seq"] if items else cursor
            latest = self._seq
        return {
            "items": items,
            "next_cursor": next_cursor,
            "latest_seq": latest,
            "count": len(items),
        }

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        # 导出时可能需要接近整缓冲；上限与 BUFFER_MAX 对齐
        limit = max(1, min(int(limit or 200), BUFFER_MAX))
        with self.lock:
            items = list(self.buffer)[-limit:]
        return items

    def clear_logs(self) -> int:
        """Clear the shared log ring without resetting the monotonic sequence."""
        with self.buffer_cv:
            cleared = len(self.buffer)
            self.buffer.clear()
            # Keep _seq monotonic so existing MCP cursors can safely read logs
            # appended after a clear without replaying pre-clear entries.
            self.buffer_cv.notify_all()
        self.broadcast_event({"type": "logs_cleared", "cleared": cleared})
        return cleared

    # ---------- serial ops ----------
    def connect(
        self,
        port: str,
        baudrate: int = 115200,
        encoding: str = "",
        *,
        from_reconnect: bool = False,
    ) -> str:
        with self.lock:
            if not from_reconnect:
                # 用户/MCP/网页主动连接：取消后台重连
                self._reconnect_stop.set()
            self._user_closed = False
            enc = encoding.strip() if encoding else self.serial_encoding
            self.reset_decoder(enc)
            if self.active_serial and self.active_serial.is_open:
                self._stop_reader_unlocked()
                try:
                    self.active_serial.close()
                except Exception:
                    pass
                self.active_serial = None
            try:
                self.active_serial = serial.Serial(port, int(baudrate), timeout=0)
            except Exception as e:
                self.status["connected"] = False
                self.status["port"] = None
                if not from_reconnect:
                    self.status["reconnecting"] = False
                msg = f"连接失败: {e}"
                self._append_unlocked("SYSTEM", msg)
                self._broadcast_status_unlocked()
                return msg
            self.last_connect = {
                "port": port,
                "baudrate": int(baudrate),
                "encoding": self.serial_encoding,
            }
            self.status["connected"] = True
            self.status["port"] = port
            self.status["baudrate"] = int(baudrate)
            self.status["encoding"] = self.serial_encoding
            self.status["reconnecting"] = False
            self._start_reader_unlocked()
            msg = f"已连接到 {port} @ {baudrate}，编码 {self.serial_encoding}"
            self._append_unlocked("SYSTEM", msg)
            self._broadcast_status_unlocked()
            return msg

    def close(self) -> str:
        with self.lock:
            self._user_closed = True
            self._reconnect_stop.set()
            self.status["reconnecting"] = False
            if self.active_serial and self.active_serial.is_open:
                port_name = self.status.get("port") or "未知"
                self._stop_reader_unlocked()
                try:
                    self.active_serial.close()
                except Exception:
                    pass
                self.active_serial = None
                self.status["connected"] = False
                self.status["port"] = None
                self.reset_decoder(self.serial_encoding)
                msg = f"已断开串口 {port_name}"
                self._append_unlocked("SYSTEM", msg)
                self._broadcast_status_unlocked()
                return msg
            self.status["connected"] = False
            self._broadcast_status_unlocked()
            return "串口未打开"

    @staticmethod
    def parse_hex_string(text: str) -> bytes:
        """解析 HEX 字符串：支持空格/逗号/0x 前缀，如 '01 0A FF' 或 '010AFF'。"""
        cleaned = (text or "").replace(",", " ").replace("0x", " ").replace("0X", " ")
        parts = cleaned.split()
        if not parts:
            # 无空格连续十六进制
            hex_only = "".join(ch for ch in cleaned if ch in "0123456789abcdefABCDEF")
            if not hex_only:
                raise ValueError("HEX 内容为空")
            if len(hex_only) % 2 != 0:
                raise ValueError("HEX 长度必须为偶数")
            return bytes.fromhex(hex_only)
        out = bytearray()
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if len(p) % 2 != 0:
                raise ValueError(f"无效 HEX 段: {p}")
            out.extend(bytes.fromhex(p))
        if not out:
            raise ValueError("HEX 内容为空")
        return bytes(out)

    @staticmethod
    def eol_bytes(eol: str) -> bytes:
        mapping = {
            "none": b"",
            "": b"",
            "cr": b"\r",
            "lf": b"\n",
            "crlf": b"\r\n",
            "\r": b"\r",
            "\n": b"\n",
            "\r\n": b"\r\n",
        }
        key = (eol or "crlf").strip().lower()
        if key not in mapping:
            raise ValueError(f"不支持的结尾: {eol}，可选 none/cr/lf/crlf")
        return mapping[key]

    @staticmethod
    def checksum_bytes(data: bytes, algo: str) -> bytes:
        algo = (algo or "none").strip().lower()
        if algo in ("none", "", "off"):
            return b""
        if algo in ("sum8", "sum", "checksum8"):
            return bytes([sum(data) & 0xFF])
        if algo in ("xor", "xor8"):
            x = 0
            for b in data:
                x ^= b
            return bytes([x & 0xFF])
        if algo in ("crc16", "crc16_modbus", "modbus"):
            crc = 0xFFFF
            for b in data:
                crc ^= b
                for _ in range(8):
                    if crc & 1:
                        crc = (crc >> 1) ^ 0xA001
                    else:
                        crc >>= 1
            # Modbus：低字节在前
            return bytes([crc & 0xFF, (crc >> 8) & 0xFF])
        raise ValueError(f"不支持的校验: {algo}，可选 none/sum8/xor/crc16_modbus")

    @staticmethod
    def bytes_to_hex_display(data: bytes) -> str:
        return " ".join(f"{b:02X}" for b in data)

    def write(
        self,
        data: str,
        source: str = "LLM",
        *,
        mode: str = "text",
        eol: str = "crlf",
        checksum: str = "none",
    ) -> str:
        """
        发送串口数据（经发送队列串行执行）。
        mode: text | hex
        eol: none | cr | lf | crlf  （追加在校验之后）
        checksum: none | sum8 | xor | crc16_modbus  （对正文计算，追加在 EOL 前）
        """
        reply: queue.Queue = queue.Queue(maxsize=1)
        job = {
            "kind": "write",
            "data": data,
            "source": source,
            "mode": mode,
            "eol": eol,
            "checksum": checksum,
            "reply": reply,
        }
        try:
            self._tx_queue.put(job, timeout=2.0)
        except queue.Full:
            return "发送失败: 发送队列已满"
        try:
            return reply.get(timeout=10.0)
        except queue.Empty:
            return "发送失败: 队列处理超时"

    def _tx_worker(self) -> None:
        while True:
            job = self._tx_queue.get()
            if job is None:
                break
            try:
                if job.get("kind") == "write":
                    msg = self._write_now(
                        job.get("data", ""),
                        source=str(job.get("source") or "LLM"),
                        mode=str(job.get("mode") or "text"),
                        eol=str(job.get("eol") if job.get("eol") is not None else "crlf"),
                        checksum=str(job.get("checksum") or "none"),
                    )
                    reply = job.get("reply")
                    if reply is not None:
                        try:
                            reply.put_nowait(msg)
                        except Exception:
                            pass
            except Exception as e:
                reply = job.get("reply")
                if reply is not None:
                    try:
                        reply.put_nowait(f"发送失败: {e}")
                    except Exception:
                        pass
            finally:
                self._tx_queue.task_done()

    def _write_now(
        self,
        data: str,
        source: str = "LLM",
        *,
        mode: str = "text",
        eol: str = "crlf",
        checksum: str = "none",
    ) -> str:
        with self.lock:
            if not self.active_serial or not self.active_serial.is_open:
                return "串口未打开"
            try:
                mode_n = (mode or "text").strip().lower()
                if mode_n == "hex":
                    body = self.parse_hex_string(str(data))
                else:
                    body = self.encode_text(str(data))

                cs = self.checksum_bytes(body, checksum)
                ending = self.eol_bytes(eol)
                frame = body + cs + ending
                if not frame:
                    return "发送失败: 空数据"
                self.active_serial.write(frame)

                if mode_n == "hex":
                    shown = self.bytes_to_hex_display(frame)
                    if cs:
                        shown += f"  [校验 {checksum}]"
                else:
                    shown = str(data)
                    if cs:
                        shown += f" +{self.bytes_to_hex_display(cs)}"
                    if ending == b"\r\n":
                        shown += " <CRLF>"
                    elif ending == b"\r":
                        shown += " <CR>"
                    elif ending == b"\n":
                        shown += " <LF>"

                src = source if source in ("LLM", "USER_OVERRIDE") else "LLM"
                self._append_unlocked(src, shown)
                return "发送成功"
            except Exception as e:
                # 写失败可能是端口掉了，触发重连
                self._handle_serial_error_unlocked(f"写串口异常: {e}")
                return f"发送失败: {e}"

    def set_encoding(self, encoding: str) -> str:
        with self.lock:
            self.reset_decoder(encoding)
            msg = f"串口编码已切换为 {self.serial_encoding}（后续收发生效）"
            self._append_unlocked("SYSTEM", msg)
            self._broadcast_status_unlocked()
            return f"串口编码已设置为 {self.serial_encoding}"

    def get_status(self) -> dict[str, Any]:
        with self.lock:
            st = dict(self.status)
            st["version"] = HUB_VERSION
            st["auto_reconnect"] = self.auto_reconnect
            st["tx_queue_size"] = self._tx_queue.qsize()
            return st

    def health_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "role": "serial-hub",
            "version": HUB_VERSION,
            "features": list(HUB_FEATURES),
            "api": list(HUB_API_PATHS),
            "http_port": self.http_port,
            "ws_port": self.ws_port,
            "host": self.host,
            "buffer_max": BUFFER_MAX,
            "auto_reconnect": self.auto_reconnect,
            "status": self.get_status(),
        }

    def text_to_hex(self, text: str, encoding: str = "") -> str:
        enc = normalize_encoding(encoding or self.serial_encoding)
        data = str(text).encode(enc, errors="replace")
        return self.bytes_to_hex_display(data)

    def hex_to_text(self, hex_str: str, encoding: str = "") -> str:
        enc = normalize_encoding(encoding or self.serial_encoding)
        data = self.parse_hex_string(str(hex_str))
        return data.decode(enc, errors="replace")

    # ---------- internal unlocked helpers (caller holds lock) ----------
    def _append_unlocked(
        self, source: str, msg: str, *, hex_msg: str | None = None
    ) -> dict[str, Any]:
        self._seq += 1
        entry = {
            "seq": self._seq,
            "ts": time.time(),
            "source": source,
            "msg": msg,
        }
        if hex_msg is not None:
            entry["hex"] = hex_msg
        self.buffer.append(entry)
        self.buffer_cv.notify_all()
        # 直接调度到 WS 事件循环，不再每条日志新建线程
        self.broadcast_log(entry)
        return entry

    def _broadcast_status_unlocked(self) -> None:
        st = dict(self.status)
        st["version"] = HUB_VERSION
        self.broadcast_status(st)

    def _start_reader_unlocked(self) -> None:
        self._stop_reader_unlocked()
        self.read_running = True
        self.read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.read_thread.start()

    def _stop_reader_unlocked(self) -> None:
        self.read_running = False
        t = self.read_thread
        self.read_thread = None
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)

    def _handle_serial_error_unlocked(self, reason: str) -> None:
        """读/写异常时关闭句柄并视配置启动自动重连。"""
        try:
            if self.active_serial:
                try:
                    self.active_serial.close()
                except Exception:
                    pass
            self.active_serial = None
        except Exception:
            pass
        self.status["connected"] = False
        self._append_unlocked("SYSTEM", reason)
        self._broadcast_status_unlocked()
        if self.auto_reconnect and not self._user_closed and self.last_connect.get("port"):
            self._start_reconnect_thread()

    def _start_reconnect_thread(self) -> None:
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_stop.clear()
        self.status["reconnecting"] = True
        self._broadcast_status_unlocked()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, name="serial-reconnect", daemon=True
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        delay = 1.0
        port = self.last_connect.get("port")
        baud = int(self.last_connect.get("baudrate") or 115200)
        enc = self.last_connect.get("encoding") or self.serial_encoding
        self.append_log("SYSTEM", f"串口异常，将自动重连 {port}（可 close 取消）...")
        while not self._reconnect_stop.is_set() and not self._user_closed:
            if not port:
                break
            # 已恢复则退出
            with self.lock:
                if self.active_serial and self.active_serial.is_open:
                    self.status["reconnecting"] = False
                    self._broadcast_status_unlocked()
                    return
            msg = self.connect(
                str(port), baud, str(enc) if enc else "", from_reconnect=True
            )
            if msg.startswith("已连接"):
                self.append_log("SYSTEM", f"自动重连成功: {port}")
                return
            self.append_log("SYSTEM", f"重连失败，{delay:.1f}s 后重试: {msg}")
            if self._reconnect_stop.wait(delay):
                break
            delay = min(delay * 1.5, 15.0)
        with self.lock:
            self.status["reconnecting"] = False
            self._broadcast_status_unlocked()

    def _read_loop(self) -> None:
        while self.read_running:
            ser = self.active_serial
            if ser and ser.is_open:
                try:
                    data = ser.read(4096)
                    if data:
                        text = self.decode_bytes(data)
                        hex_msg = self.bytes_to_hex_display(data)
                        # 文本可能为空（纯二进制），仍记录 HEX
                        self.append_log("HARDWARE", text if text else hex_msg, hex_msg=hex_msg)
                    else:
                        time.sleep(0.05)
                except Exception as e:
                    # 端口拔出等
                    with self.lock:
                        self.read_running = False
                        self._handle_serial_error_unlocked(f"串口读取异常: {e}")
                    break
            else:
                time.sleep(0.2)

    # ---------- websocket broadcast ----------
    def broadcast_log(self, entry: dict[str, Any]) -> None:
        if not self.ws_loop or not self.ws_clients:
            return
        payload_obj = {
            "source": entry["source"],
            "msg": entry["msg"],
            "seq": entry["seq"],
            "ts": entry["ts"],
        }
        if entry.get("hex") is not None:
            payload_obj["hex"] = entry["hex"]
        payload = json.dumps(payload_obj, ensure_ascii=False)

        def _send() -> None:
            dead = []
            for ws in list(self.ws_clients):
                try:
                    asyncio.create_task(ws.send(payload))
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.ws_clients.discard(ws)

        try:
            self.ws_loop.call_soon_threadsafe(_send)
        except Exception:
            pass

    def broadcast_status(self, status: dict[str, Any] | None = None) -> None:
        if not self.ws_loop or not self.ws_clients:
            return
        st = status if status is not None else self.get_status()
        payload = json.dumps({"type": "status", **st}, ensure_ascii=False)

        def _send() -> None:
            websockets.broadcast(self.ws_clients, payload)

        try:
            self.ws_loop.call_soon_threadsafe(_send)
        except Exception:
            pass

    def broadcast_event(self, event: dict[str, Any]) -> None:
        if not self.ws_loop or not self.ws_clients:
            return
        payload = json.dumps(event, ensure_ascii=False)

        def _send() -> None:
            websockets.broadcast(self.ws_clients, payload)

        try:
            self.ws_loop.call_soon_threadsafe(_send)
        except Exception:
            pass


# 全局单例（Hub 进程内）
hub = SerialHub()


def _json_response(handler: BaseHTTPRequestHandler, code: int, obj: Any) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


class HubHTTPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # 精简日志
        sys.stderr.write("[hub-http] %s\n" % (format % args))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html", "/dashboard"):
            self._serve_dashboard()
            return
        if path == "/api/health":
            _json_response(self, 200, hub.health_payload())
            return
        if path == "/api/status":
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "version": HUB_VERSION,
                    "features": list(HUB_FEATURES),
                    "status": hub.get_status(),
                },
            )
            return
        if path == "/api/ports":
            _json_response(self, 200, {"ok": True, "ports": hub.enumerate_ports()})
            return
        if path == "/api/read":
            cursor = int((qs.get("cursor") or ["0"])[0] or 0)
            limit = int((qs.get("limit") or ["200"])[0] or 200)
            data = hub.read_since(cursor=cursor, limit=limit)
            _json_response(self, 200, {"ok": True, **data, "status": hub.get_status()})
            return
        if path == "/api/recent":
            limit = int((qs.get("limit") or ["200"])[0] or 200)
            _json_response(self, 200, {"ok": True, "items": hub.recent(limit), "status": hub.get_status()})
            return

        _json_response(self, 404, {"ok": False, "error": f"unknown path: {path}"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        body = _read_json_body(self)

        try:
            if path == "/api/connect":
                port = (body.get("port") or "").strip()
                baud = int(body.get("baudrate") or 115200)
                encoding = body.get("encoding") or ""
                if not port:
                    _json_response(self, 400, {"ok": False, "error": "缺少 port"})
                    return
                msg = hub.connect(port, baud, encoding)
                ok = msg.startswith("已连接")
                # 业务失败也返回 HTTP 200，由 ok 字段区分（便于脚本与 MCP 客户端）
                _json_response(self, 200, {"ok": ok, "message": msg, "status": hub.get_status()})
                return
            if path == "/api/clear-logs":
                cleared = hub.clear_logs()
                _json_response(self, 200, {"ok": True, "cleared": cleared, "status": hub.get_status()})
                return
            if path == "/api/close":
                msg = hub.close()
                _json_response(self, 200, {"ok": True, "message": msg, "status": hub.get_status()})
                return
            if path == "/api/write":
                data = body.get("data")
                if data is None:
                    data = body.get("payload", "")
                source = body.get("source") or "LLM"
                mode = body.get("mode") or "text"
                eol = body.get("eol") if body.get("eol") is not None else "crlf"
                checksum = body.get("checksum") or "none"
                msg = hub.write(
                    str(data),
                    source=str(source),
                    mode=str(mode),
                    eol=str(eol),
                    checksum=str(checksum),
                )
                ok = msg == "发送成功"
                _json_response(self, 200, {"ok": ok, "message": msg, "status": hub.get_status()})
                return
            if path == "/api/encoding":
                encoding = body.get("encoding") or "utf-8"
                msg = hub.set_encoding(str(encoding))
                _json_response(self, 200, {"ok": True, "message": msg, "status": hub.get_status()})
                return
            if path == "/api/convert":
                # direction: to_hex | to_text
                direction = (body.get("direction") or "").strip().lower()
                encoding = body.get("encoding") or hub.serial_encoding
                data = body.get("data")
                if data is None:
                    data = body.get("text") or body.get("hex") or ""
                try:
                    if direction in ("to_hex", "hex", "encode"):
                        out = hub.text_to_hex(str(data), str(encoding))
                        _json_response(
                            self,
                            200,
                            {"ok": True, "direction": "to_hex", "result": out, "encoding": normalize_encoding(encoding)},
                        )
                        return
                    if direction in ("to_text", "text", "decode"):
                        out = hub.hex_to_text(str(data), str(encoding))
                        _json_response(
                            self,
                            200,
                            {"ok": True, "direction": "to_text", "result": out, "encoding": normalize_encoding(encoding)},
                        )
                        return
                    _json_response(self, 200, {"ok": False, "error": "direction 需为 to_hex 或 to_text"})
                    return
                except Exception as conv_err:
                    _json_response(self, 200, {"ok": False, "error": str(conv_err)})
                    return
        except Exception as e:
            _json_response(self, 500, {"ok": False, "error": str(e)})
            return

        _json_response(self, 404, {"ok": False, "error": f"unknown path: {path}"})

    def _serve_dashboard(self) -> None:
        html_path = pathlib.Path(__file__).with_name("dashboard.html")
        default_enc = hub.get_status().get("encoding") or default_encoding()
        html = (
            html_path.read_text(encoding="utf-8")
            .replace("{WS_PORT}", str(hub.ws_port))
            .replace("{DEFAULT_ENCODING}", str(default_enc))
            .replace("{HTTP_PORT}", str(hub.http_port))
            .replace("{HUB_VERSION}", HUB_VERSION)
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


async def _ws_handler(websocket) -> None:
    hub.ws_clients.add(websocket)
    try:
        # 连接后：健康/版本 + 状态 + 串口列表 + 最近日志
        await websocket.send(
            json.dumps({"type": "health", **hub.health_payload()}, ensure_ascii=False)
        )
        await websocket.send(json.dumps({"type": "status", **hub.get_status()}, ensure_ascii=False))
        await websocket.send(json.dumps({"type": "ports", "ports": hub.enumerate_ports()}, ensure_ascii=False))
        recent = hub.recent(200)
        if recent:
            await websocket.send(
                json.dumps({"type": "history", "items": recent}, ensure_ascii=False)
            )
        async for message in websocket:
            try:
                data = json.loads(message)
            except Exception:
                continue
            action = data.get("action")
            if action == "write":
                payload = data.get("payload", "")
                opts = data.get("options") or {}
                # 兼容扁平字段
                mode = opts.get("mode") or data.get("mode") or "text"
                eol = opts.get("eol") if opts.get("eol") is not None else data.get("eol", "crlf")
                checksum = opts.get("checksum") or data.get("checksum") or "none"
                result = hub.write(
                    payload,
                    source="USER_OVERRIDE",
                    mode=str(mode),
                    eol=str(eol),
                    checksum=str(checksum),
                )
                if not result.startswith("发送成功"):
                    hub.append_log("SYSTEM", result)
            elif action == "connect":
                payload = data.get("payload") or {}
                port = payload.get("port", "")
                if port:
                    hub.connect(port, int(payload.get("baudrate") or 115200), payload.get("encoding") or "")
            elif action == "close":
                hub.close()
            elif action == "list_ports":
                await websocket.send(
                    json.dumps({"type": "ports", "ports": hub.enumerate_ports()}, ensure_ascii=False)
                )
            elif action == "set_encoding":
                payload = data.get("payload") or {}
                try:
                    hub.set_encoding(payload.get("encoding") or hub.serial_encoding)
                except Exception as e:
                    hub.append_log("SYSTEM", f"设置编码失败: {e}")
            elif action == "get_status":
                await websocket.send(json.dumps({"type": "status", **hub.get_status()}, ensure_ascii=False))
            elif action == "ping":
                await websocket.send(json.dumps({"type": "pong", "ts": time.time()}, ensure_ascii=False))
    finally:
        hub.ws_clients.discard(websocket)


def _run_ws(host: str, ws_port: int) -> None:
    async def serve() -> None:
        # ping_interval 保持长连接，避免代理/空闲断开
        async with websockets.serve(
            _ws_handler,
            host,
            ws_port,
            ping_interval=20,
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
        ):
            hub.ws_loop = asyncio.get_running_loop()
            hub.ws_ready.set()
            print(f"[hub] WebSocket listening on {host}:{ws_port}", flush=True)
            await asyncio.Future()

    try:
        asyncio.run(serve())
    except Exception:
        traceback.print_exc()
        hub.ws_ready.clear()


def _run_http(host: str, http_port: int) -> None:
    server = ThreadingHTTPServer((host, http_port), HubHTTPHandler)
    print(f"[hub] HTTP listening on http://{host}:{http_port}", flush=True)
    server.serve_forever()


def run_hub(host: str | None = None, http_port: int | None = None) -> None:
    """阻塞运行 Hub（常驻）。"""
    hub.host = host or DEFAULT_HOST
    hub.http_port = int(http_port or DEFAULT_HTTP_PORT)
    hub.ws_port = hub.http_port + 1

    display_host = "127.0.0.1" if hub.host in ("0.0.0.0", "::") else hub.host
    print("=" * 56, flush=True)
    print("  Umeko Serial Hub  (常驻串口中枢)", flush=True)
    print(f"  版本:  {HUB_VERSION}", flush=True)
    print(f"  能力:  {', '.join(HUB_FEATURES)}", flush=True)
    print(f"  网页:  http://{display_host}:{hub.http_port}", flush=True)
    print(f"  API:   http://{display_host}:{hub.http_port}/api/health", flush=True)
    print(f"  WS:    ws://{display_host}:{hub.ws_port}", flush=True)
    print(f"  默认编码: {hub.serial_encoding}", flush=True)
    print(f"  自动重连: {'开' if hub.auto_reconnect else '关'}", flush=True)
    print("  改代码后必须重启本进程才会生效", flush=True)
    print("  Codex MCP 请指向同一 Hub，不要再单独 open COM", flush=True)
    print("  按 Ctrl+C 退出", flush=True)
    print("=" * 56, flush=True)

    threading.Thread(target=_run_ws, args=(hub.host, hub.ws_port), daemon=True).start()
    if not hub.ws_ready.wait(timeout=3.0):
        print("[hub] 警告: WebSocket 未在 3s 内就绪", file=sys.stderr, flush=True)

    try:
        _run_http(hub.host, hub.http_port)
    except KeyboardInterrupt:
        print("\n[hub] 已退出", flush=True)
        hub.close()


def run_hub_cli() -> None:
    """console_scripts: start-serial-hub"""
    host = os.environ.get("SERIAL_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("SERIAL_MCP_HTTP_PORT", "8080"))
    run_hub(host=host, http_port=port)
