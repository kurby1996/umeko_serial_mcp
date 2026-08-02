"""统一版本与能力声明（Hub / MCP / 网页握手用）。"""

# 与 pyproject.toml version 保持同步
HUB_VERSION = "0.3.0"

# 网页/MCP 期望的最低 Hub 版本（主.次 比较用字符串，要求完整实现下列 features）
MIN_HUB_VERSION = "0.3.0"

# Hub 能力列表；客户端可检测缺失项并提示重启
HUB_FEATURES = (
    "convert",
    "write_options",
    "ring_buffer",
    "auto_reconnect",
    "send_queue",
    "health_version",
)

# HTTP API 路径清单（文档与握手）
HUB_API_PATHS = (
    "/api/health",
    "/api/status",
    "/api/ports",
    "/api/read",
    "/api/recent",
    "/api/connect",
    "/api/close",
    "/api/write",
    "/api/encoding",
    "/api/convert",
)
