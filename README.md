# Umeko Serial MCP（Hub 架构）

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT">
  <img src="https://img.shields.io/badge/Version-0.3.0-orange" alt="Version 0.3.0">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Platform">
</p>

<p align="center">
  <b>常驻 Serial Hub 独占串口 + Web 监控面板；Codex / Cursor 等通过 MCP 瘦客户端共用同一 COM 与日志。</b><br>
  适合 STM32 / 嵌入式串口联调：网页旁路收发，AI 用自然语言读写串口。
</p>

![Dashboard](assets/image-1.png)

---

## 功能特性

| 类别 | 能力 |
|------|------|
| **架构** | Hub 常驻独占 COM；MCP 不 open 串口，只调 HTTP API |
| **Web 监控** | 实时日志、WebSocket 长连/自动重连、字号与接收显示（文本/HEX） |
| **发送** | 文本/HEX、结尾符（无/CR/LF/CRLF）、校验（SUM8/XOR/CRC16-Modbus） |
| **单条 / 多条** | 发送后内容保留；多条发送弹窗（勾选、循环、分页、导入导出，localStorage 持久化） |
| **周期发送** | 单条周期 / 多条轮询，周期单位 ms |
| **编码** | utf-8 / gbk / gb18030（Windows 默认 gbk，兼容 XCOM 中文） |
| **日志** | 环形缓冲（默认 5000 条）；导出 TXT；MCP 增量 `read_data` |
| **可靠性** | 发送队列串行化；串口异常自动重连；版本/能力握手（旧 Hub 会提示重启） |

---

## 架构（必读）

```text
scripts/start-hub.bat
        │
        ▼
┌───────────────────────────────────────┐
│  Serial Hub（只起一个，常驻）           │
│  · 独占 COM                           │
│  · HTTP  http://127.0.0.1:8080        │
│  · WS    ws://127.0.0.1:8081          │
│  · 环形日志：网页 / AI / 下位机        │
└───────────────▲───────────────────────┘
                │
     ┌──────────┴──────────┐
     │                     │
 浏览器网页              Codex MCP（瘦客户端）
 监控 + 干预发送         write_data / read_data
                        不 open COM
```

| 正确 | 错误 |
|------|------|
| 先起 Hub，再开网页 / Codex | 手动再起一个占 COM 的旧 MCP，和 Hub 抢口 |
| 只保留一个 Hub 进程 | 关掉 Hub 还指望网页/MCP 能用 |
| 改代码后**重启 Hub** + 强刷网页 | 改完源码不重启，出现 `unknown path` 等旧行为 |

---

## 快速开始（Windows）

### 1. 安装

```powershell
cd <项目目录>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .
```

确认入口：

```powershell
dir .\.venv\Scripts\start-serial-*.exe
```

应有 `start-serial-hub.exe`、`start-serial-mcp.exe`。

### 2. 启动 Hub（保持窗口不关）

```powershell
.\scripts\start-hub.bat
```

启动日志中应看到类似：

```text
版本:  0.3.0
网页:  http://127.0.0.1:8080
```

浏览器打开：**http://127.0.0.1:8080**

1. 选串口 / 刷新  
2. 编码选 **GBK**（中文固件 / XCOM 常用）  
3. 连接  

可选：登录后自动起 Hub：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-hub-autostart.ps1
```

### 3. 配置 Codex

编辑 `%USERPROFILE%\.codex\config.toml`（路径改成你的实际位置）：

```toml
[mcp_servers.serial-mcp]
command = 'F:\AI\串口MCP\umeko_serial_mcp\.venv\Scripts\start-serial-mcp.exe'

[mcp_servers.serial-mcp.env]
SERIAL_MCP_HUB = "http://127.0.0.1:8080"
```

保存后**重启 Codex**。

### 4. 在 Codex 中使用

```text
用 serial-mcp：
1. hub_status
2. list_ports
3. connect_port 连接 COM1，115200，gbk
4. write_data 发送 hello
5. read_data 读取日志
```

| 来源 | 网页标签 | `read_data` 标签 |
|------|----------|------------------|
| AI `write_data` | `[AI Agent 发出]` | `[AI]` |
| 网页发送 | `[人类/网页 发送]` | `[用户/网页]` |
| 下位机 | `[下位机 返回]` | `[下位机]` |

---

## Web 面板要点

- **单条发送**：内容发送后**不清除**；可勾选周期发送（ms）  
- **多条发送...**：弹窗编辑多条；勾选发送 / 循环；序号从 1 起；默认一页 10 条；「添加一页」；条目自动保存  
- **HEX 勾选**：文本 ↔ HEX 互转（按连接栏编码；需 Hub ≥ 0.3.0 的 convert）  
- **导出 TXT**：可选保存路径（Chrome/Edge 另存为）  
- **版本黄条**：若出现「请重启 Hub」，说明进程过旧，请重启后再 Ctrl+F5  

---

## MCP 工具

| 工具 | 说明 |
|------|------|
| `list_ports` | 扫描本机串口 |
| `connect_port` | 连接串口（与网页共用） |
| `close_port` | 断开（并取消自动重连） |
| `write_data` | AI 向串口发送 |
| `read_data` | 增量读取缓冲（不清空，多客户端可并存） |
| `set_encoding` | utf-8 / gbk / gb18030 |
| `hub_status` | 版本、能力、连接状态 |
| `start_monitor_ui` | Hub 健康检查与监控地址 |

---

## HTTP API（调试）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 版本、features、端口 |
| GET | `/api/status` | 串口状态 |
| GET | `/api/ports` | 串口列表 |
| GET | `/api/read?cursor=&limit=` | 增量日志 |
| GET | `/api/recent?limit=` | 最近 N 条 |
| POST | `/api/connect` | `{"port","baudrate","encoding"}` |
| POST | `/api/close` | 断开 |
| POST | `/api/write` | `{"data","source","mode","eol","checksum"}` |
| POST | `/api/encoding` | `{"encoding"}` |
| POST | `/api/convert` | `{"direction":"to_hex\|to_text","data","encoding"}` |

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/health
```

---

## 项目结构

```text
umeko_serial_mcp/
├── src/umeko_serial_mcp/
│   ├── hub.py           # 常驻 Hub（串口、HTTP、WS、队列、重连）
│   ├── mcp_bridge.py    # MCP 瘦客户端
│   ├── server.py        # 入口：--hub 或 MCP
│   ├── dashboard.html   # Web 面板
│   └── version.py       # 版本与能力声明
├── scripts/
│   ├── start-hub.bat              # 启动 Hub
│   ├── install-hub-autostart.ps1  # 开机自启
│   └── stm32_debug.py             # 命令行经 Hub 调试
├── docs/
│   ├── 使用教程.md
│   └── 部署到其他电脑.md
└── README.md
```

---

## 环境变量

| 变量 | 含义 | 默认 |
|------|------|------|
| `SERIAL_MCP_HUB` | MCP/脚本连接的 Hub URL | `http://127.0.0.1:8080` |
| `SERIAL_MCP_HTTP_PORT` | Hub HTTP 端口（WS = 端口+1） | `8080` |
| `SERIAL_MCP_HOST` | Hub 监听地址 | `127.0.0.1` |
| `SERIAL_MCP_ENCODING` | 默认文本编码 | Windows: `gbk` |
| `SERIAL_MCP_AUTO_RECONNECT` | 串口异常自动重连 | `1`（开） |
| `SERIAL_MCP_TX_QUEUE` | 发送队列容量 | `256` |
| `SERIAL_MCP_BUFFER_MAX` | 日志环形缓冲条数 | `5000` |

局域网访问示例（注意无鉴权，仅可信网络）：

```powershell
$env:SERIAL_MCP_HOST="0.0.0.0"
.\scripts\start-hub.bat
```

---

## 入口命令

```text
start-serial-hub          # 常驻 Hub
start-serial-mcp          # MCP 瘦客户端（给 AI 配置）

# 等价
python -m umeko_serial_mcp --hub
python -m umeko_serial_mcp --hub --host 127.0.0.1 --http-port 8080
python -m umeko_serial_mcp
```

---

## 常见问题

| 现象 | 处理 |
|------|------|
| 网页打不开 / 拒绝连接 | 先运行 `start-hub.bat`，保持窗口不关 |
| `PermissionError 13` / 8081 占用 | 只留一个 Hub；关掉多余 python / 旧 MCP |
| `unknown path: /api/convert` 或网页黄条 | **重启 Hub** 后再 Ctrl+F5 |
| Codex 读不到网页/下位机 | 确认 MCP 连的是同一 Hub；先 `hub_status` |
| 中文乱码 | 编码改为 **GBK** |
| 改代码不生效 | 重启 Hub + 重启 Codex + 强刷网页 |

与 XCOM 对测时：使用虚拟串口对，Hub 连一端、XCOM 连另一端，**不要**同时打开同一物理 COM。

---

## 文档

- [使用教程](docs/使用教程.md) — 日常操作、Codex 提示词、故障排查  
- [部署到其他电脑](docs/部署到其他电脑.md) — 拷贝、安装、跨机配置  

---

## License

MIT
