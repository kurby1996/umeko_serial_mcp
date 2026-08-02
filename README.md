# Umeko Serial MCP（Hub 架构）

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT">
  <img src="https://img.shields.io/badge/Version-0.3.0-orange" alt="Version 0.3.0">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Platform">
</p>

<p align="center">
  <b>常驻 Serial Hub 独占串口 + Web 监控；Codex / 其它 AI 通过 MCP 瘦客户端共用同一 COM 与日志。</b>
</p>

![Dashboard](assets/image-1.png)

---

## 架构

```text
start-hub.bat  →  Serial Hub（常驻，只起一个）
                    ├─ 网页  http://127.0.0.1:8080
                    ├─ WS    :8081（长连）
                    ├─ HTTP API
                    └─ 独占 COM + 环形日志

Codex  →  start-serial-mcp.exe（瘦客户端，不 open COM）
              └─ 访问 Hub /api/*
```

**正确用法：先起 Hub，再开 Codex / 网页。不要用两个进程分别抢同一 COM。**

---

## 快速开始（Windows）

### 1. 安装

```powershell
cd <项目目录>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .
```

### 2. 启动 Hub（保持窗口不关）

```powershell
.\scripts\start-hub.bat
```

浏览器打开：http://127.0.0.1:8080  

选串口、编码（中文常用 **GBK**）→ 连接。

可选开机自启：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-hub-autostart.ps1
```

### 3. 配置 Codex

`%USERPROFILE%\.codex\config.toml`：

```toml
[mcp_servers.serial-mcp]
command = 'F:\AI\串口MCP\umeko_serial_mcp\.venv\Scripts\start-serial-mcp.exe'

[mcp_servers.serial-mcp.env]
SERIAL_MCP_HUB = "http://127.0.0.1:8080"
```

路径改成你机器上的实际位置，然后**重启 Codex**。

### 4. 在 Codex 里

```text
list_ports
connect_port 连 COM1，115200，gbk
write_data 发送 hello
read_data 读取日志
```

- 网页发送 → `read_data` 可见 `[用户/网页]`  
- AI 发送 → 网页显示 `[AI Agent 发出]`  
- 下位机数据 → 两边都能看  

---

## MCP 工具

| 工具 | 说明 |
|------|------|
| `list_ports` | 扫描串口（经 Hub） |
| `connect_port` | 连接串口（与网页共用） |
| `close_port` | 断开 |
| `write_data` | AI 发送（网页标 AI） |
| `read_data` | 增量读日志（网页+下位机+系统） |
| `set_encoding` | utf-8 / gbk / gb18030 |
| `hub_status` | Hub 与串口状态 |
| `start_monitor_ui` | 检查 Hub 是否在线 |

---

## 项目脚本

| 文件 | 用途 |
|------|------|
| `scripts/start-hub.bat` | 启动常驻 Hub |
| `scripts/install-hub-autostart.ps1` | 开机自启 Hub |
| `scripts/stm32_debug.py` | 命令行经 Hub 调试（需 Hub 已启动） |

---

## 文档

- [使用教程](docs/使用教程.md)
- [部署到其他电脑](docs/部署到其他电脑.md)

---

## 环境变量

| 变量 | 含义 | 默认 |
|------|------|------|
| `SERIAL_MCP_HUB` | MCP/脚本连接的 Hub URL | `http://127.0.0.1:8080` |
| `SERIAL_MCP_HTTP_PORT` | Hub HTTP 端口 | `8080` |
| `SERIAL_MCP_HOST` | Hub 监听地址 | `127.0.0.1` |
| `SERIAL_MCP_ENCODING` | 默认编码 | Windows: `gbk` |
| `SERIAL_MCP_AUTO_RECONNECT` | 串口异常自动重连 | `1`（开） |
| `SERIAL_MCP_TX_QUEUE` | 发送队列长度 | `256` |
| `SERIAL_MCP_BUFFER_MAX` | 日志环形缓冲条数 | `5000` |

**改代码后必须重启 Hub**；网页顶部黄条或 `hub_status` 会提示版本/能力不匹配。

---

## 入口命令

```text
start-serial-hub   # 常驻 Hub
start-serial-mcp   # MCP 瘦客户端（给 AI 客户端配置）

# 等价
python -m umeko_serial_mcp --hub
python -m umeko_serial_mcp
```

---

## License

MIT
