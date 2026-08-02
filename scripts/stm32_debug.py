"""通过 Serial Hub HTTP API 调试 STM32 串口（需先启动 Hub）。

示例：
    .venv\\Scripts\\python.exe scripts\\stm32_debug.py list
    .venv\\Scripts\\python.exe scripts\\stm32_debug.py send "hello stm32"
    .venv\\Scripts\\python.exe scripts\\stm32_debug.py read --seconds 10
    .venv\\Scripts\\python.exe scripts\\stm32_debug.py session
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def hub_base() -> str:
    return os.environ.get("SERIAL_MCP_HUB", "http://127.0.0.1:8080").rstrip("/")


def request(method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict[str, Any]:
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
    except urllib.error.URLError as e:
        raise SystemExit(
            f"无法连接 Serial Hub ({hub_base()}): {e.reason}\n"
            "请先运行: .\\scripts\\start-hub.bat"
        ) from e


def print_items(items: list[dict]) -> None:
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
        print(f"{label} {it.get('msg', '')}", flush=True)


def cmd_list(_: argparse.Namespace) -> None:
    resp = request("GET", "/api/ports")
    if not resp.get("ok"):
        raise SystemExit(resp.get("error") or resp)
    ports = resp.get("ports") or []
    if not ports:
        print("未找到串口")
        return
    for p in ports:
        print(f"{p.get('device')} - {p.get('description', '')}")


def cmd_connect(args: argparse.Namespace) -> None:
    resp = request(
        "POST",
        "/api/connect",
        {"port": args.port, "baudrate": args.baudrate, "encoding": args.encoding},
    )
    print(resp.get("message") or resp, flush=True)
    if not resp.get("ok"):
        raise SystemExit(1)


def cmd_send(args: argparse.Namespace) -> None:
    resp = request("POST", "/api/write", {"data": args.message, "source": "LLM"})
    print(resp.get("message") or resp, flush=True)
    if not resp.get("ok"):
        raise SystemExit(1)


def cmd_read(args: argparse.Namespace) -> None:
    cursor = 0
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        resp = request("GET", "/api/read", query={"cursor": str(cursor), "limit": "200"})
        if not resp.get("ok"):
            raise SystemExit(resp.get("error") or resp)
        items = resp.get("items") or []
        if items:
            print_items(items)
            cursor = int(resp.get("next_cursor") or cursor)
        time.sleep(0.2)


def cmd_session(args: argparse.Namespace) -> None:
    cmd_connect(args)
    print("已连接 Hub。输入 send <内容>、read 或 quit。", flush=True)
    cursor = 0
    while True:
        line = input("stm32> ").strip()
        command, _, payload = line.partition(" ")
        if command in {"quit", "exit", "q"}:
            return
        if command == "send" and payload:
            resp = request("POST", "/api/write", {"data": payload, "source": "LLM"})
            print(resp.get("message") or resp, flush=True)
        elif command == "read":
            resp = request("GET", "/api/read", query={"cursor": str(cursor), "limit": "200"})
            items = resp.get("items") or []
            if not items:
                print("无新数据", flush=True)
            else:
                print_items(items)
                cursor = int(resp.get("next_cursor") or cursor)
        else:
            print("用法：send <内容> | read | quit", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过 Serial Hub 调试 STM32 串口")
    parser.add_argument("command", choices=["list", "send", "read", "session"])
    parser.add_argument("message", nargs="?", help="send 命令要发送的文本")
    parser.add_argument("--port", default="COM1")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--encoding", default="gbk", choices=["utf-8", "gbk", "gb18030"])
    parser.add_argument("--seconds", type=float, default=10, help="read 命令持续读取秒数")
    parser.add_argument(
        "--hub",
        default=os.environ.get("SERIAL_MCP_HUB", "http://127.0.0.1:8080"),
        help="Hub 地址，默认 http://127.0.0.1:8080",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    os.environ["SERIAL_MCP_HUB"] = args.hub.rstrip("/")
    if args.command == "send" and not args.message:
        raise SystemExit('send 命令需要提供消息，例如：send "hello stm32"')

    # 健康检查
    health = request("GET", "/api/health")
    if not health.get("ok"):
        raise SystemExit(f"Hub 不可用: {health}")

    if args.command == "list":
        cmd_list(args)
    elif args.command == "send":
        cmd_connect(args)
        cmd_send(args)
    elif args.command == "read":
        cmd_connect(args)
        cmd_read(args)
    elif args.command == "session":
        cmd_session(args)


if __name__ == "__main__":
    main()
