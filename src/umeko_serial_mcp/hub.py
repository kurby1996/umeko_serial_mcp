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
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import serial
import serial.tools.list_ports
import websockets

SUPPORTED_ENCODINGS = ("utf-8", "gbk", "gb18030")
DEFAULT_HTTP_PORT = int(os.environ.get("SERIAL_MCP_HTTP_PORT", "8080"))
DEFAULT_HOST = os.environ.get("SERIAL_MCP_HOST", "127.0.0.1")
BUFFER_MAX = int(os.environ.get("SERIAL_MCP_BUFFER_MAX", "5000"))


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
    def append_log(self, source: str, msg: str) -> dict[str, Any]:
        with self.buffer_cv:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "ts": time.time(),
                "source": source,
                "msg": msg,
            }
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
        limit = max(1, min(int(limit or 200), 1000))
        with self.lock:
            items = list(self.buffer)[-limit:]
        return items

    # ---------- serial ops ----------
    def connect(self, port: str, baudrate: int = 115200, encoding: str = "") -> str:
        with self.lock:
            enc = encoding.strip() if encoding else self.serial_encoding
            self.reset_decoder(enc)
            if self.active_serial and self.active_serial.is_open:
                self._stop_reader_unlocked()
                try:
                    self.active_serial.close()
                except Exception:
                    pass
            try:
                self.active_serial = serial.Serial(port, int(baudrate), timeout=0)
            except Exception as e:
                self.status["connected"] = False
                self.status["port"] = None
                msg = f"连接失败: {e}"
                self._append_unlocked("SYSTEM", msg)
                self._broadcast_status_unlocked()
                return msg
            self.status["connected"] = True
            self.status["port"] = port
            self.status["baudrate"] = int(baudrate)
            self.status["encoding"] = self.serial_encoding
            self._start_reader_unlocked()
            msg = f"已连接到 {port} @ {baudrate}，编码 {self.serial_encoding}"
            self._append_unlocked("SYSTEM", msg)
            self._broadcast_status_unlocked()
            return msg

    def close(self) -> str:
        with self.lock:
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

    def write(self, data: str, source: str = "LLM") -> str:
        with self.lock:
            if not self.active_serial or not self.active_serial.is_open:
                return "串口未打开"
            try:
                payload = data if data.endswith("\n") else data + "\r\n"
                self.active_serial.write(self.encode_text(payload))
                text = data.strip("\r\n")
                src = source if source in ("LLM", "USER_OVERRIDE") else "LLM"
                self._append_unlocked(src, text)
                return "发送成功"
            except Exception as e:
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
            return dict(self.status)

    # ---------- internal unlocked helpers (caller holds lock) ----------
    def _append_unlocked(self, source: str, msg: str) -> dict[str, Any]:
        self._seq += 1
        entry = {
            "seq": self._seq,
            "ts": time.time(),
            "source": source,
            "msg": msg,
        }
        self.buffer.append(entry)
        self.buffer_cv.notify_all()
        # broadcast outside strict serial critical section is ok; schedule
        threading.Thread(target=self.broadcast_log, args=(entry,), daemon=True).start()
        return entry

    def _broadcast_status_unlocked(self) -> None:
        st = dict(self.status)
        threading.Thread(target=self.broadcast_status, args=(st,), daemon=True).start()

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

    def _read_loop(self) -> None:
        while self.read_running:
            ser = self.active_serial
            if ser and ser.is_open:
                try:
                    data = ser.read(4096)
                    if data:
                        text = self.decode_bytes(data)
                        if text:
                            self.append_log("HARDWARE", text)
                    else:
                        time.sleep(0.05)
                except Exception as e:
                    self.append_log("SYSTEM", f"串口读取异常: {e}")
                    time.sleep(0.2)
            else:
                time.sleep(0.2)

    # ---------- websocket broadcast ----------
    def broadcast_log(self, entry: dict[str, Any]) -> None:
        if not self.ws_loop or not self.ws_clients:
            return
        payload = json.dumps(
            {"source": entry["source"], "msg": entry["msg"], "seq": entry["seq"], "ts": entry["ts"]},
            ensure_ascii=False,
        )

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
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "role": "serial-hub",
                    "http_port": hub.http_port,
                    "ws_port": hub.ws_port,
                    "host": hub.host,
                },
            )
            return
        if path == "/api/status":
            _json_response(self, 200, {"ok": True, "status": hub.get_status()})
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
            if path == "/api/close":
                msg = hub.close()
                _json_response(self, 200, {"ok": True, "message": msg, "status": hub.get_status()})
                return
            if path == "/api/write":
                data = body.get("data")
                if data is None:
                    data = body.get("payload", "")
                source = body.get("source") or "LLM"
                msg = hub.write(str(data), source=str(source))
                ok = msg == "发送成功"
                _json_response(self, 200, {"ok": ok, "message": msg, "status": hub.get_status()})
                return
            if path == "/api/encoding":
                encoding = body.get("encoding") or "utf-8"
                msg = hub.set_encoding(str(encoding))
                _json_response(self, 200, {"ok": True, "message": msg, "status": hub.get_status()})
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
        # 连接后：状态 + 串口列表 + 最近日志（网页刷新不丢近期内容）
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
                hub.write(data.get("payload", ""), source="USER_OVERRIDE")
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
    print(f"  网页:  http://{display_host}:{hub.http_port}", flush=True)
    print(f"  API:   http://{display_host}:{hub.http_port}/api/health", flush=True)
    print(f"  WS:    ws://{display_host}:{hub.ws_port}", flush=True)
    print(f"  默认编码: {hub.serial_encoding}", flush=True)
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
