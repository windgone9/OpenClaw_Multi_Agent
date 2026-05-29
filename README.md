# 🦞 OpenClaw Multi-Agent 智能调度系统

基于 OpenClaw 的多 Agent 模型调度系统，支持自适应路由策略、Smart Router 智能分流、Agent Soul 执行链，以及实时监控 Dashboard。

## 架构概览

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Dashboard   │────▶│  Bridge :3001    │────▶│  Gateway :3000  │──▶ Ollama
│  :3001       │     │  Smart Router    │     │  轻量路由       │──▶ Moonshot
│              │     ├──────────────────┤     └─────────────────┘──▶ DeepSeek
│              │     │  Agent Chain     │────▶│  Official GW    │
│              │     │  Soul/Plan/Exec  │     │  :3005          │
└─────────────┘     └──────────────────┘     └─────────────────┘
                            │
                     ┌──────▼──────┐
                     │  Scheduler  │
                     │  :8000      │
                     │  FastAPI    │
                     └─────────────┘
```

**双路径路由**：
- **Gateway 路径**（低延迟）：简单请求 → Custom Gateway → 直接调用模型 API
- **Agent 路径**（高能力）：复杂请求 → OpenClaw Official Gateway → Agent Soul 系统 → 多步编排执行

**Smart Router**：基于六维复杂度评分（请求类型、工具调用、提示长度、关键词、上下文、优先级），自动选择最优路径。

## 快速开始

### 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Node.js | ≥ 18 | Bridge / Gateway 运行环境 |
| Python | ≥ 3.9 | 调度器运行环境 |
| Ollama | 最新 | 本地模型服务（可选） |
| OpenClaw | ≥ 2026.4.27 | Agent 执行链（可选） |

### 1. 克隆项目

```bash
git clone https://github.com/windgone9/OpenClaw_Multi_Agent.git
cd OpenClaw_Multi_Agent
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入 API Key：

```bash
# 必填：至少配置一个云端模型 API Key
MOONSHOT_API_KEY=sk-your-moonshot-api-key
DEEPSEEK_API_KEY=sk-your-deepseek-api-key
```

API Key 申请地址：
- Moonshot (Kimi): https://platform.moonshot.cn/console/api-keys
- DeepSeek: https://platform.deepseek.cn/api_keys

> **重要**：Python 调度器通过 `python-dotenv` 自动加载 `.env` 文件，无需手动 `source`。Node.js 服务通过 `start-all.sh` 自动加载。

### 3. 安装依赖

```bash
# Node.js 依赖
npm install

# Python 依赖（推荐使用虚拟环境）
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. 启动 Ollama（本地模型，可选）

```bash
# 安装 Ollama（如未安装）
# macOS: brew install ollama
# Linux: curl -fsSL https://ollama.com/install.sh | sh

# 启动 Ollama 服务
ollama serve

# 拉取模型（另开终端）
ollama pull qwen2.5:3b
```

### 5. 启动所有服务

**方式 A：一键启动（推荐）**

```bash
chmod +x start-all.sh
./start-all.sh
```

**方式 B：手动逐个启动**

```bash
# 终端 1: Custom Gateway
node gateway/gateway.mjs

# 终端 2: Bridge 智能路由层
node bridge/orchestrator.mjs

# 终端 3: OpenClaw 官方 Gateway（可选）
node /path/to/openclaw/openclaw.mjs gateway run --port 3005 --auth none --force

# 终端 4: Python 调度器
source venv/bin/activate
python run.py
```

### 6. 验证服务

启动完成后，逐一验证各服务状态：

```bash
# 查看服务状态
./start-all.sh status

# 验证 Gateway
curl http://localhost:3000/health

# 验证 Bridge
curl http://localhost:3001/health

# 验证 Python 调度器
curl http://localhost:8000/models

# 验证 Ollama
curl http://localhost:11434/api/tags
```

### 7. 访问 Dashboard

打开浏览器访问：http://localhost:3001/static/dashboard.html

## 服务端口一览

| 服务 | 端口 | 说明 |
|------|------|------|
| Dashboard | :3001 | 通过 Bridge 提供静态文件 |
| Bridge 智能路由 | :3001 | Smart Router + 双路径路由 |
| Custom Gateway | :3000 | 轻量模型路由代理 |
| OpenClaw Official GW | :3005 | Agent 执行链入口（可选） |
| Python 调度器 | :8000 | FastAPI 自适应调度 |
| Ollama | :11434 | 本地模型推理服务 |

## API 接口

### Bridge API（:3001）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/dispatch` | 智能路由调度（支持 smart/gateway/agent 模式） |
| GET | `/health` | Bridge 健康检查 + Gateway 状态 |
| GET | `/models` | 可用模型列表 |
| GET | `/agents/souls` | Agent Soul 列表 |
| POST | `/route/reload` | 热重载 Smart Router 配置 |
| GET | `/services/status` | 服务运行状态 |
| POST | `/services/start` | 启动指定服务 |
| POST | `/services/stop` | 停止指定服务 |

### Python 调度器 API（:8000）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/dispatch` | 自适应模型调度 |
| GET | `/models` | 注册模型列表 |
| GET | `/stats` | 系统资源 + 请求统计 |
| GET | `/openclaw/adaptive/state` | 自适应权重状态 |

### 请求示例

```bash
# Smart 智能路由（自动选择 Gateway 或 Agent）
curl -X POST http://localhost:3001/dispatch \
  -H "Content-Type: application/json" \
  -d '{
    "appid": "test",
    "type": "chat",
    "prompt": "1+1=?",
    "priority": 3,
    "route_mode": "smart"
  }'

# Python 直连调度
curl -X POST http://localhost:8000/dispatch \
  -H "Content-Type: application/json" \
  -d '{
    "appid": "test",
    "type": "chat",
    "prompt": "介绍一下人工智能",
    "priority": 3
  }'

# Tool Call（自动路由到支持函数调用的模型）
curl -X POST http://localhost:3001/dispatch \
  -H "Content-Type: application/json" \
  -d '{
    "appid": "test",
    "type": "tool_call",
    "prompt": "请搜索最新AI新闻",
    "priority": 2,
    "tools": [{"type":"function","function":{"name":"search","description":"Search web","parameters":{"type":"object","properties":{"query":{"type":"string"}}}}}],
    "tool_choice": "auto",
    "route_mode": "smart"
  }'
```

## Smart Router 路由策略

Smart Router 基于六维复杂度评分模型自动选择路由路径：

| 维度 | 权重 | 说明 |
|------|------|------|
| 请求类型 | 5-40 | chat=5, tool_call=35, code_execution=40 |
| 工具调用 | 30+10n | 基础分30 + 每个额外工具+10 |
| 提示长度 | 0-18 | ≤50字=0, >1000字=18 |
| 复杂关键词 | 0-20 | "分析/比较"=moderate, "多步骤/自主"=high |
| 上下文 | 0-15 | 有历史上下文加分 |
| 优先级 | -5~+10 | priority=1加10分, priority=5减5分 |

**决策规则**：总分 < 阈值(40) → Gateway 路径；总分 ≥ 阈值 → Agent 路径

配置文件 `openclaw.json` 中的 `models.smartRouter` 部分支持自定义所有评分参数，修改后通过 `/route/reload` 热重载。

## 项目结构

```
OpenClaw_Multi_Agent/
├── bridge/
│   └── orchestrator.mjs       # Bridge 智能路由层（Smart Router + 双路径路由）
├── gateway/
│   └── gateway.mjs            # Custom Gateway 轻量路由代理
├── scheduler/
│   ├── main.py                # FastAPI 调度器入口
│   ├── agents/                # Agent 实现（Local/Cloud/OpenClaw）
│   ├── config/                # 配置管理
│   ├── hooks/                 # 请求预处理/后处理 Hook
│   ├── models/                # 数据模型（Request/Response/ModelConfig）
│   └── strategy/              # 调度策略（Adaptive/LoadBalance/Priority/Router）
├── static/
│   └── dashboard.html         # 实时监控 Dashboard
├── docs/
│   ├── technical_spec_v3.md   # 技术说明文档
│   ├── architecture.svg       # 架构图
│   ├── dataflow.svg           # 数据流图
│   └── sequence.svg           # 时序图
├── openclaw.json              # 核心配置文件（Smart Router / Agents / Models）
├── .env.example               # 环境变量模板
├── run.py                     # Python 调度器启动入口（自动加载 .env）
├── start-all.sh               # 一键启动脚本
├── requirements.txt           # Python 依赖
└── package.json               # Node.js 依赖
```

## 环境变量说明

所有配置项均可通过 `.env` 文件管理，复制 `.env.example` 为 `.env` 后按需修改：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MOONSHOT_API_KEY` | - | Moonshot API Key（必填） |
| `DEEPSEEK_API_KEY` | - | DeepSeek API Key（必填） |
| `OPENCLAW_TOKEN` | - | OpenClaw Gateway 认证 Token |
| `OPENCLAW_API_KEY` | - | OpenClaw API Key |
| `GATEWAY_PORT` | 3000 | Custom Gateway 端口 |
| `BRIDGE_PORT` | 3001 | Bridge 端口 |
| `SCHEDULER_PORT` | 8000 | Python 调度器端口 |
| `OPENCLAW_GATEWAY_URL` | http://localhost:3000 | Custom Gateway 地址 |
| `OPENCLAW_OFFICIAL_GATEWAY_URL` | http://127.0.0.1:3005 | 官方 Gateway 地址 |
| `USE_OPENCLAW_GATEWAY` | true | 是否启用 Gateway 路由 |
| `DEFAULT_ROUTE_MODE` | smart | 默认路由模式（smart/gateway/agent） |
| `SMART_ROUTER_THRESHOLD` | 40 | Smart Router 复杂度阈值 |
| `RATE_LIMIT_RPM` | 60 | 全局限流 RPM |
| `MAX_RETRIES` | 2 | 最大重试次数 |

## 常见问题

### Q: 启动后 Dashboard 显示 Gateway 不可达？

在 Dashboard 界面点击 **▶ 启动** 按钮，或手动启动：
```bash
node gateway/gateway.mjs
```

### Q: Ollama 模型返回 HTTP 502？

检查 Ollama 服务是否运行，以及模型是否已拉取：
```bash
ollama serve          # 启动服务
ollama pull qwen2.5:3b  # 拉取模型
```

### Q: 云端模型调用返回 HTTP 401（API Key 错误）？

1. 检查 `.env` 中的 API Key 是否正确
2. 确保 `.env` 文件存在于项目根目录
3. Python 调度器通过 `python-dotenv` 自动加载 `.env`，无需手动 `source`
4. Node.js 服务通过 `start-all.sh` 启动时自动加载 `.env`

### Q: Official Gateway 启动失败？

确保已安装 OpenClaw：
```bash
npm install -g openclaw
# 或
npm install openclaw
```

### Q: 如何修改 Smart Router 阈值？

编辑 `openclaw.json` 中 `models.smartRouter.threshold`，然后热重载：
```bash
curl -X POST http://localhost:3001/route/reload
```

### Q: 端口被占用怎么办？

```bash
# 查看占用进程
lsof -i :3000
lsof -i :3001
lsof -i :8000

# 终止占用进程
kill $(lsof -ti:3000)
```

或使用一键停止：
```bash
./start-all.sh stop
```

## 停止服务

```bash
./start-all.sh stop
```

或手动停止：
```bash
kill $(lsof -ti:3001)  # Bridge
kill $(lsof -ti:3000)  # Gateway
kill $(lsof -ti:3005)  # Official Gateway
kill $(lsof -ti:8000)  # Scheduler
```

## License

MIT
