"""
MCP 瘦客户端：所有串口操作转发到常驻 Serial Hub 的 HTTP API。
不在本进程 open COM，因此可与网页共用同一串口与日志缓冲。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP

from umeko_serial_mcp.version import HUB_FEATURES, HUB_VERSION, MIN_HUB_VERSION

mcp = FastMCP("SerialPortMCP")

DEFAULT_HUB = os.environ.get("SERIAL_MCP_HUB", "http://127.0.0.1:8080").rstrip("/")

# 每个 MCP 进程自己的读取游标（不消费 Hub 缓冲，多客户端可并存）
_read_cursor = 0


def hub_base() -> str:
    return os.environ.get("SERIAL_MCP_HUB", DEFAULT_HUB).rstrip("/")


def _request(method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict[str, Any]:
    url = hub_base() + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8")
            obj = json.loads(raw) if raw else {}
            if obj:
                return obj
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {e.reason}"}
    except urllib.error.URLError as e:
        return {
            "ok": False,
            "error": (
                f"无法连接 Serial Hub ({hub_base()}): {e.reason}。"
                "请先在本机启动 Hub：start-serial-hub 或 "
                "python -m umeko_serial_mcp --hub"
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _fmt_fail(resp: dict[str, Any]) -> str:
    return resp.get("message") or resp.get("error") or str(resp)


def _parse_ver(v: str) -> tuple[int, ...]:
    parts = []
    for p in (v or "0").split("."):
        try:
            parts.append(int("".join(ch for ch in p if ch.isdigit()) or "0"))
        except ValueError:
            parts.append(0)
    return tuple(parts or (0,))


def _check_hub_health(health: dict[str, Any]) -> str:
    """返回握手提示文本（可能为空）。"""
    if not health.get("ok"):
        return ""
    notes: list[str] = []
    ver = str(health.get("version") or "")
    if not ver:
        notes.append(
            f"Hub 未报告 version（可能是旧进程）。客户端期望 >= {MIN_HUB_VERSION}。"
            "请重启 Hub：scripts\\start-hub.bat"
        )
    elif _parse_ver(ver) < _parse_ver(MIN_HUB_VERSION):
        notes.append(
            f"Hub 版本过旧: {ver} < {MIN_HUB_VERSION}。请重启 Hub 加载新代码。"
        )
    feats = health.get("features") or []
    if feats:
        missing = [f for f in HUB_FEATURES if f not in feats]
        if missing:
            notes.append(f"Hub 缺少能力: {', '.join(missing)}。请重启 Hub。")
    else:
        # 旧 health 无 features
        notes.append("Hub 未声明 features，建议重启 Hub 以启用 convert/自动重连/发送队列。")
    return "\n".join(notes)


@mcp.tool()
def start_monitor_ui(http_port: int = 8080) -> str:
    """检查 Hub/监控面板是否已启动（健康检查 + 版本握手）。"""
    resp = _request("GET", "/api/health")
    if resp.get("ok"):
        base = hub_base()
        warn = _check_hub_health(resp)
        lines = [
            f"Hub 已在线 v{resp.get('version') or '?'}。请打开: {base}/",
            f"API: {base}/api/health",
            f"MCP 客户端版本期望: {HUB_VERSION} / min Hub {MIN_HUB_VERSION}",
            "请保持 start-serial-hub 运行；改代码后必须重启 Hub。",
        ]
        if warn:
            lines.append("⚠️ " + warn.replace("\n", "\n⚠️ "))
        return "\n".join(lines)
    return _fmt_fail(resp)


@mcp.tool()
def list_ports() -> str:
    """扫描本机串口（经 Hub）。"""
    resp = _request("GET", "/api/ports")
    if not resp.get("ok"):
        return _fmt_fail(resp)
    ports = resp.get("ports") or []
    if not ports:
        return "未找到串口"
    return "\n".join(f"{p.get('device')} - {p.get('description', '')}" for p in ports)


@mcp.tool()
def connect_port(port: str, baudrate: int = 115200, encoding: str = "") -> str:
    """连接指定串口（经 Hub，与网页共用）。encoding 为空时使用 Hub 默认（Windows 多为 gbk）。"""
    resp = _request(
        "POST",
        "/api/connect",
        {"port": port, "baudrate": baudrate, "encoding": encoding},
    )
    return resp.get("message") or _fmt_fail(resp)


@mcp.tool()
def close_port() -> str:
    """断开当前串口（经 Hub）。"""
    resp = _request("POST", "/api/close", {})
    return resp.get("message") or _fmt_fail(resp)


@mcp.tool()
def set_encoding(encoding: str = "utf-8") -> str:
    """设置串口文本编码：utf-8 / gbk / gb18030。"""
    resp = _request("POST", "/api/encoding", {"encoding": encoding})
    return resp.get("message") or _fmt_fail(resp)


@mcp.tool()
def write_data(data: str) -> str:
    """向串口写入数据（标记为 AI 发送，网页显示 [AI Agent 发出]）。"""
    resp = _request("POST", "/api/write", {"data": data, "source": "LLM"})
    return resp.get("message") or _fmt_fail(resp)


@mcp.tool()
def read_data(limit: int = 200) -> str:
    """
    读取 Hub 日志缓冲中的新消息（网页发送 + 下位机 + 系统），不清空，不与其它客户端冲突。
    使用本 MCP 进程游标增量读取。
    """
    global _read_cursor
    resp = _request("GET", "/api/read", query={"cursor": str(_read_cursor), "limit": str(limit)})
    if not resp.get("ok"):
        return _fmt_fail(resp)
    items = resp.get("items") or []
    _read_cursor = int(resp.get("next_cursor") or _read_cursor)
    if not items:
        st = resp.get("status") or {}
        conn = "已连接" if st.get("connected") else "未连接"
        port = st.get("port") or "-"
        return f"无新数据（串口{conn}: {port}，cursor={_read_cursor}）"
    lines = []
    for it in items:
        src = it.get("source")
        if src == "USER_OVERRIDE":
            label = "[用户/网页]"
        elif src == "HARDWARE":
            label = "[下位机]"
        elif src == "LLM":
            label = "[AI]"
        else:
            label = "[系统]"
        lines.append(f"{label} {it.get('msg', '')}")
    return "\n".join(lines)


@mcp.tool()
def hub_status() -> str:
    """查询 Serial Hub 版本、能力与串口状态。"""
    health = _request("GET", "/api/health")
    if not health.get("ok"):
        return _fmt_fail(health)
    st = health.get("status") or {}
    # 兼容仅 status 接口
    if not st:
        resp = _request("GET", "/api/status")
        st = (resp.get("status") or {}) if resp.get("ok") else {}
    warn = _check_hub_health(health)
    lines = [
        f"Hub: {hub_base()}",
        f"version={health.get('version') or st.get('version') or '?'}",
        f"features={','.join(health.get('features') or []) or '-'}",
        f"connected={st.get('connected')} reconnecting={st.get('reconnecting')} "
        f"port={st.get('port')} baud={st.get('baudrate')} encoding={st.get('encoding')}",
        f"auto_reconnect={health.get('auto_reconnect', st.get('auto_reconnect'))} "
        f"tx_queue={st.get('tx_queue_size', '?')}",
        f"client_expect={HUB_VERSION} min_hub={MIN_HUB_VERSION}",
    ]
    if warn:
        lines.append("WARN: " + warn.replace("\n", " | "))
    return "\n".join(lines)


def run_mcp() -> None:
    print(f"[mcp] Serial MCP client {HUB_VERSION}, Hub = {hub_base()}", file=sys.stderr, flush=True)
    health = _request("GET", "/api/health")
    if health.get("ok"):
        print(
            f"[mcp] Hub online v{health.get('version') or '?'}: {hub_base()}",
            file=sys.stderr,
            flush=True,
        )
        warn = _check_hub_health(health)
        if warn:
            print(f"[mcp] WARN: {warn}", file=sys.stderr, flush=True)
    else:
        print(
            f"[mcp] Hub offline: {_fmt_fail(health)}",
            file=sys.stderr,
            flush=True,
        )
    mcp.run(transport="stdio")
