# OpenClaw Multi-Agent 智能调度系统

基于 OpenClaw 的多 Agent 模型调度系统，支持 Hermes Agent 智能路由、五类调度路径、K8S AIWorkload 集成、Smart Router 智能分流，以及实时监控 Dashboard。

## 架构概览

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Dashboard   │────▶│  Bridge :3001    │────▶│  Gateway :3000  │──▶ Ollama/Moonshot/DeepSeek
│  :8082       │     │  Smart Router    │     │  轻量路由       │
│              │     ├──────────────────┤     └─────────────────┘
│              │     │  Agent Chain     │────▶│  Official GW    │
│              │     │  Soul/Plan/Exec  │     │  :3005          │
└──────────────┘     └──────────────────┘     └────────┬────────┘
                                                       │
                      ┌────────────────────────────────┘
                      │
┌─────────────────────▼──────────────────────────────────────────┐
│  Hermes Agent :8082  (核心调度层)                                │
│                                                                 │
│  ┌─────────────┐   ┌──────────────┐   ┌───────────────────┐   │
│  │ Request     │──▶│ Route via    │──▶│ Dispatch Worker   │   │
│  │ Queue       │   │ Agent/LLM    │   │                   │   │
│  └─────────────┘   └──────────────┘   └───────┬───────────┘   │
│                                                │               │
│  ┌─────────────┐                               │               │
│  │ Result      │◀──────────────────────────────┘               │
│  │ Queue       │     5 类路由路径:                               │
│  └─────────────┘     ├─ direct_local  → Ollama/vLLM 直连       │
│                      ├─ gateway        → OfficialGW (Agent链)   │
│  ┌─────────────┐     ├─ k8s_gateway    → K8S AIWorkload        │
│  │ Feedback    │     ├─ multimodal     → 本地多模态模型          │
│  │ Queue       │     └─ local_inference→ Ollama (隐私约束)      │
│  └─────────────┘                                               │
│       │                                                        │
│       ▼                                                        │
│  MEMORY.md 自学习闭环                                          │
└────────────────────────────────────────────────────────────────┘
```

**五类路由路径**：

| 路径 | 目标 | 适用场景 |
|------|------|----------|
| `direct_local` | Ollama/vLLM 直连 | 单步问答、低延迟 |
| `gateway` | OfficialGW 直连 | 多步批处理/Volcano/Agent 链 |
| `k8s_gateway` | OpenClaw K8S 插件 | AIWorkload 提交/查询/删除 |
| `multimodal` | 本地多模态模型 | 图片/音频/视频处理 |
| `local_inference` | Ollama/vLLM 直连 | 隐私敏感、本地执行 |

**Smart Router**：基于六维复杂度评分（请求类型、工具调用、提示长度、关键词、上下文、优先级），自动选择最优路径。

## 快速开始

### 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | >= 3.9 | Hermes Agent 运行环境 |
| Node.js | >= 18 | Bridge / Gateway 运行环境 |
| Ollama | 最新 | 本地模型服务（可选） |
| OpenClaw | >= 2026.4.27 | Agent 执行链（可选） |

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

> **重要**：Python 调度器通过 `python-dotenv` 自动加载 `.env` 文件，无需手动 `source`。

### 3. 安装依赖

```bash
# Node.js 依赖
npm install

# Python 依赖（推荐使用虚拟环境）
python3 -m venv hermes-official-venv
source hermes-official-venv/bin/activate
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
cd /path/to/openclaw && node openclaw.mjs gateway --port 3005 --force --allow-unconfigured

# 终端 4: Hermes Agent 调度器
source hermes-official-venv/bin/activate
python -m hermes.server
```

### 6. 验证服务

```bash
# 验证 Hermes Agent
curl http://localhost:8082/health

# 验证 Gateway
curl http://localhost:3000/health

# 验证 Bridge
curl http://localhost:3001/health

# 验证 Ollama
curl http://localhost:11434/api/tags
```

### 7. 访问 Dashboard

打开浏览器访问：http://localhost:8082/static/dashboard.html

## 服务端口一览

| 服务 | 端口 | 说明 |
|------|------|------|
| **Hermes Agent** | **:8082** | **核心调度层（队列 + 路由 + 分发 + K8S）** |
| Dashboard | :8082 | 通过 Hermes 提供静态文件 |
| Bridge 智能路由 | :3001 | Smart Router + 双路径路由 |
| Custom Gateway | :3000 | 轻量模型路由代理 |
| OpenClaw Official GW | :3005 | Agent 执行链入口（可选） |
| Ollama | :11434 | 本地模型推理服务 |

## API 接口

### Hermes Agent API（:8082）— 核心调度

#### 队列接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/queue/submit` | 异步提交请求到队列 |
| POST | `/queue/submit-sync` | 同步提交请求（阻塞等待结果） |
| GET | `/queue/results` | 轮询结果队列 |
| POST | `/queue/feedback` | 提交反馈（自学习闭环） |

#### K8S AIWorkload 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/k8s/workloads` | 提交 AIWorkload 到 K8S 集群（202 Accepted） |
| GET | `/k8s/workloads/{id}` | 查询 AIWorkload 状态 |
| DELETE | `/k8s/workloads/{id}` | 删除 AIWorkload |
| POST | `/k8s/callback` | OpenClaw 状态回调接收端点 |

#### 系统接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/models` | 注册模型列表 |
| GET | `/stats` | 系统资源 + 请求统计 |
| GET | `/openclaw/adaptive/state` | 自适应权重状态 |

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

### 请求示例

```bash
# ── Hermes 队列调度（推荐）──

# 普通对话（自动路由到 Ollama 本地模型）
curl -X POST 'http://localhost:8082/queue/submit-sync?timeout=60' \
  -H 'Content-Type: application/json' \
  -d '{"appid":"test","type":"chat","priority":3,"prompt":"1+1等于几"}'

# K8S AIWorkload 提交（自动路由到 k8s_gateway）
curl -X POST 'http://localhost:8082/queue/submit-sync?timeout=30' \
  -H 'Content-Type: application/json' \
  -d '{
    "appid":"test","type":"k8s_workload","priority":3,
    "prompt":"提交批量推理任务",
    "k8s_workload":{
      "taskType":"batch-inference",
      "intent":{"batchInference":{
        "model":{"name":"resnet50","framework":"pytorch"},
        "input":{"uri":"s3://datasets/imgs/"},
        "output":{"uri":"s3://results/demo/"},
        "scale":{"items":1000,"batchSize":32}
      }}
    }
  }'

# ── K8S 独立 API ──

# 提交 AIWorkload
curl -X POST 'http://localhost:8082/k8s/workloads?appid=my-team' \
  -H 'Content-Type: application/json' \
  -d '{
    "taskType":"batch-inference",
    "intent":{"batchInference":{
      "model":{"name":"resnet50","framework":"pytorch"},
      "input":{"uri":"s3://datasets/imgs/"},
      "output":{"uri":"s3://results/demo/"},
      "scale":{"items":100000,"batchSize":32}
    }}
  }'

# 查询状态
curl 'http://localhost:8082/k8s/workloads/wl-xxx'

# 删除
curl -X DELETE 'http://localhost:8082/k8s/workloads/wl-xxx'

# ── Bridge 调度 ──

# Smart 智能路由
curl -X POST http://localhost:3001/dispatch \
  -H "Content-Type: application/json" \
  -d '{"appid":"test","type":"chat","prompt":"1+1=?","priority":3,"route_mode":"smart"}'

# Tool Call（自动路由到支持函数调用的模型）
curl -X POST http://localhost:3001/dispatch \
  -H "Content-Type: application/json" \
  -d '{
    "appid":"test","type":"tool_call","prompt":"请搜索最新AI新闻","priority":2,
    "tools":[{"type":"function","function":{"name":"search","description":"Search web","parameters":{"type":"object","properties":{"query":{"type":"string"}}}}}],
    "tool_choice":"auto","route_mode":"smart"
  }'
```

## K8S AIWorkload 集成

Hermes Agent 支持通过 OpenClaw 网关向 K8S 集群提交 AIWorkload，提供两种调用方式：

### 方式一：Hermes 自动路由（队列接口）

通过 `POST /queue/submit-sync` 提交，设置 `type=k8s_workload`，Hermes 自动识别并路由到 `k8s_gateway`（跳过 LLM 路由，零 GPU 开销）。

### 方式二：独立 API

直接调用 `/k8s/workloads` 系列 API，不经过 Hermes 路由决策。

### 支持的 taskType

| taskType | intent slice | 说明 |
|----------|-------------|------|
| `batch-inference` | `batchInference` | 批量推理任务 |
| `distributed-training` | `distributedTraining` | 分布式训练任务 |
| `hyperparameter-tuning` | `hyperparameterTuning` | 超参调优任务 |

详细接口规范参见 [openclaw-k8s-integration-plugin-spec.md](docs/openclaw-k8s-integration-plugin-spec.md)。

## Smart Router 路由策略

Smart Router 基于六维复杂度评分模型自动选择路由路径：

| 维度 | 权重 | 说明 |
|------|------|------|
| 请求类型 | 5-40 | chat=5, tool_call=35, code_execution=40 |
| 工具调用 | 30+10n | 基础分30 + 每个额外工具+10 |
| 提示长度 | 0-18 | <=50字=0, >1000字=18 |
| 复杂关键词 | 0-20 | "分析/比较"=moderate, "多步骤/自主"=high |
| 上下文 | 0-15 | 有历史上下文加分 |
| 优先级 | -5~+10 | priority=1加10分, priority=5减5分 |

**决策规则**：总分 < 阈值(40) -> Gateway 路径；总分 >= 阈值 -> Agent 路径

配置文件 `openclaw.json` 中的 `models.smartRouter` 部分支持自定义所有评分参数，修改后通过 `/route/reload` 热重载。

## 项目结构

```
OpenClaw_Multi_Agent/
├── hermes/                        # Hermes Agent 核心调度层
│   ├── server.py                  # FastAPI 入口（队列API + K8S API + 系统API）
│   ├── dispatch_worker.py         # 分发工作器（5类路由 + K8S Gateway 转发）
│   ├── official_agent_adapter.py  # 路由决策适配器（LLM路由 + K8S快速路径）
│   ├── agent.py                   # Hermes Agent 实现
│   ├── router.py                  # 路由器（5类路由路径定义）
│   ├── llm_enhancer.py           # LLM 增强器
│   ├── message_queue.py           # 消息队列（请求/结果/反馈）
│   └── docs/
│       └── routing-strategy.md    # 路由策略文档
├── bridge/
│   └── orchestrator.mjs           # Bridge 智能路由层（Smart Router + 双路径路由）
├── gateway/
│   └── gateway.mjs                # Custom Gateway 轻量路由代理
├── scheduler/
│   ├── main.py                    # FastAPI 调度器入口
│   ├── agents/                    # Agent 实现（Local/Cloud/OpenClaw）
│   ├── config/                    # 配置管理
│   ├── hooks/                     # 请求预处理/后处理 Hook
│   ├── models/                    # 数据模型（Request/Response/ModelConfig）
│   └── strategy/                  # 调度策略（Adaptive/LoadBalance/Priority/Router）
├── static/
│   └── dashboard.html             # 实时监控 Dashboard（含 K8S 工作负载面板）
├── docs/
│   ├── openclaw-k8s-integration-plugin-spec.md  # K8S 集成接口规范
│   ├── technical_spec_v3.md       # 技术说明文档
│   ├── architecture.svg           # 架构图
│   ├── dataflow.svg               # 数据流图
│   └── sequence.svg               # 时序图
├── .hermes/
│   ├── memories/MEMORY.md         # 自学习记忆文件
│   └── queues/                    # 持久化队列存储
├── openclaw.json                  # 核心配置文件（Smart Router / Agents / Models）
├── .env.example                   # 环境变量模板
├── run.py                         # Python 调度器启动入口（自动加载 .env）
├── start-all.sh                   # 一键启动脚本
├── stop-all.sh                    # 一键停止脚本
├── requirements.txt               # Python 依赖
└── package.json                   # Node.js 依赖
```

## 环境变量说明

所有配置项均可通过 `.env` 文件管理，复制 `.env.example` 为 `.env` 后按需修改：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MOONSHOT_API_KEY` | - | Moonshot API Key |
| `DEEPSEEK_API_KEY` | - | DeepSeek API Key |
| `OPENCLAW_TOKEN` | - | OpenClaw Gateway 认证 Token |
| `OPENCLAW_API_KEY` | - | OpenClaw API Key |
| `OPENCLAW_K8S_GATEWAY_URL` | 同 `OPENCLAW_OFFICIAL_GATEWAY_URL` | K8S 插件网关地址 |
| `OPENCLAW_OFFICIAL_GATEWAY_URL` | http://127.0.0.1:3005 | 官方 Gateway 地址 |
| `GATEWAY_PORT` | 3000 | Custom Gateway 端口 |
| `BRIDGE_PORT` | 3001 | Bridge 端口 |
| `HERMES_PORT` | 8082 | Hermes Agent 端口 |
| `USE_OPENCLAW_GATEWAY` | true | 是否启用 Gateway 路由 |
| `DEFAULT_ROUTE_MODE` | smart | 默认路由模式（smart/gateway/agent） |
| `SMART_ROUTER_THRESHOLD` | 40 | Smart Router 复杂度阈值 |
| `RATE_LIMIT_RPM` | 60 | 全局限流 RPM |
| `MAX_RETRIES` | 2 | 最大重试次数 |

## 常见问题

### Q: 启动后 Dashboard 显示 Gateway 不可达？

在 Dashboard 界面点击启动按钮，或手动启动：
```bash
node gateway/gateway.mjs
```

### Q: Ollama 模型返回 HTTP 502？

检查 Ollama 服务是否运行，以及模型是否已拉取：
```bash
ollama serve          # 启动服务
ollama pull qwen2.5:3b  # 拉取模型
```

### Q: K8S AIWorkload 提交返回 502 K8S_GATEWAY_ERROR？

1. 确认 OpenClaw Gateway 已启动：`curl http://localhost:3005/health`
2. 确认 K8S 插件已注册到 OpenClaw Gateway
3. 检查 `OPENCLAW_K8S_GATEWAY_URL` 环境变量是否正确
4. 检查 `OPENCLAW_TOKEN` 是否与 OpenClaw 配置一致

### Q: 云端模型调用返回 HTTP 401（API Key 错误）？

1. 检查 `.env` 中的 API Key 是否正确
2. 确保 `.env` 文件存在于项目根目录
3. Python 调度器通过 `python-dotenv` 自动加载 `.env`，无需手动 `source`

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
lsof -i :8082  # Hermes
lsof -i :3000  # Gateway
lsof -i :3001  # Bridge

# 终止占用进程
kill $(lsof -ti:8082)
```

或使用一键停止：
```bash
./stop-all.sh
```

## 停止服务

```bash
./stop-all.sh
```

或手动停止：
```bash
kill $(lsof -ti:8082)  # Hermes Agent
kill $(lsof -ti:3001)  # Bridge
kill $(lsof -ti:3000)  # Gateway
kill $(lsof -ti:3005)  # Official Gateway
```

## License

MIT
