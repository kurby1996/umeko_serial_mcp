"""
入口：
  --hub   启动常驻 Serial Hub（网页 + API + 串口）
  默认    启动 MCP 瘦客户端（给 Codex 用，转发到 Hub）
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Umeko Serial Hub / MCP thin client",
    )
    parser.add_argument(
        "--hub",
        action="store_true",
        dest="hub",
        help="启动常驻 Serial Hub（网页监控 + HTTP API，独占串口）",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=int(os.environ.get("SERIAL_MCP_HTTP_PORT", "8080")),
        help="Hub HTTP 端口（默认 8080，WebSocket=端口+1）",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=os.environ.get("SERIAL_MCP_HOST", "127.0.0.1"),
        help="Hub 监听地址（默认 127.0.0.1；局域网可设 0.0.0.0）",
    )
    parser.add_argument(
        "--hub-url",
        type=str,
        default=os.environ.get("SERIAL_MCP_HUB", ""),
        help="MCP 模式连接的 Hub 地址，如 http://127.0.0.1:8080",
    )
    args, _unknown = parser.parse_known_args(argv)

    if args.hub:
        from umeko_serial_mcp.hub import run_hub

        run_hub(host=args.host, http_port=args.http_port)
        return

    # MCP 瘦客户端
    if args.hub_url:
        os.environ["SERIAL_MCP_HUB"] = args.hub_url.rstrip("/")
    elif "SERIAL_MCP_HUB" not in os.environ:
        host = args.host if args.host not in ("0.0.0.0", "::") else "127.0.0.1"
        os.environ["SERIAL_MCP_HUB"] = f"http://{host}:{args.http_port}"

    from umeko_serial_mcp.mcp_bridge import run_mcp

    run_mcp()


if __name__ == "__main__":
    main()
