"""Small MCP client for repeatable STM32 serial debugging.

Run with the project's virtual environment, for example:
    .venv\\Scripts\\python.exe scripts\\stm32_debug.py list
    .venv\\Scripts\\python.exe scripts\\stm32_debug.py send "hello stm32"
    .venv\\Scripts\\python.exe scripts\\stm32_debug.py read --seconds 10
    .venv\\Scripts\\python.exe scripts\\stm32_debug.py session
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


def server_command() -> str:
    configured = os.environ.get("SERIAL_MCP_COMMAND")
    if configured:
        return configured
    local = ROOT / ".venv" / "Scripts" / "start-serial-mcp.exe"
    if local.exists():
        return str(local)
    found = shutil.which("start-serial-mcp")
    if found:
        return found
    raise RuntimeError(
        "找不到 serial-mcp。请先创建项目虚拟环境，或设置 SERIAL_MCP_COMMAND。"
    )


def text_result(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        value = getattr(item, "text", None)
        if value:
            parts.append(value)
    if parts:
        return "\n".join(parts)
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured.get("result") is not None:
        return str(structured["result"])
    return str(result)


async def connect(session: ClientSession, args: argparse.Namespace) -> None:
    result = await session.call_tool("connect_port", {
        "port": args.port,
        "baudrate": args.baudrate,
        "encoding": args.encoding,
    })
    print(text_result(result), flush=True)


async def run(args: argparse.Namespace) -> None:
    # Listing ports is read-only and does not need to claim the dashboard ports.
    server_args = ["--no-auto-ui"] if args.command == "list" else []
    params = StdioServerParameters(command=server_command(), args=server_args)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            if args.command == "list":
                result = await session.call_tool("list_ports", {})
                print(text_result(result))
                return

            await connect(session, args)

            if args.command == "send":
                result = await session.call_tool("write_data", {"data": args.message})
                print(text_result(result))
                return

            if args.command == "read":
                deadline = asyncio.get_running_loop().time() + args.seconds
                while asyncio.get_running_loop().time() < deadline:
                    result = await session.call_tool("read_data", {})
                    message = text_result(result).strip()
                    if message and message not in {"无数据", "串口未打开"}:
                        print(message, flush=True)
                    await asyncio.sleep(0.2)
                return

            print("已连接。输入 send <内容>、read 或 quit。", flush=True)
            while True:
                line = await asyncio.to_thread(input, "stm32> ")
                command, _, payload = line.strip().partition(" ")
                if command in {"quit", "exit", "q"}:
                    return
                if command == "send" and payload:
                    result = await session.call_tool("write_data", {"data": payload})
                    print(text_result(result), flush=True)
                elif command == "read":
                    result = await session.call_tool("read_data", {})
                    print(text_result(result), flush=True)
                else:
                    print("用法：send <内容> | read | quit", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过 serial-mcp 调试 STM32 串口")
    parser.add_argument("command", choices=["list", "send", "read", "session"])
    parser.add_argument("message", nargs="?", help="send 命令要发送的文本")
    parser.add_argument("--port", default="COM1")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--encoding", default="gbk", choices=["utf-8", "gbk", "gb18030"])
    parser.add_argument("--seconds", type=float, default=10, help="read 命令持续读取秒数")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "send" and not args.message:
        raise SystemExit("send 命令需要提供消息，例如：send \"hello stm32\"")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
