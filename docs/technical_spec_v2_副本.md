# OpenClaw Multi-Agent 智能调度系统 — 技术说明文档

> 版本: 2026.5.28 | 基于 OpenClaw 2026.4.27

---

## 目录

1. [整体架构](#1-整体架构)
2. [模块调用关系](#2-模块调用关系)
3. [详细设计](#3-详细设计)
4. [上下游输入与输出](#4-上下游输入与输出)
5. [整体数据流图](#5-整体数据流图)
6. [可扩展性](#6-可扩展性)

---

## 1. 整体架构

### 1.1 系统总览

OpenClaw Multi-Agent 智能调度系统采用**四层架构**设计，通过 Smart Router 实现请求复杂度感知的自动路由，在低延迟（Gateway 直连）与高能力（Agent 执行链）之间取得最优平衡。

```
┌─────────────────────────────────────────────────────────────────┐
│                        客户端层 (Client)                         │
│   Dashboard UI  │  curl/API  │  Python SDK  │  第三方集成       │
└───────────────┬───────────────────────────────┬─────────────────┘
                │                               │
                ▼                               ▼
┌───────────────────────────┐   ┌───────────────────────────────────┐
│   Python 调度器 (FastAPI)  │   │     Bridge 智能路由层 (Node.js)    │
│   Port: 8000              │   │   Port: 3001                      │
│   ┌─────────────────────┐ │   │   ┌─────────────────────────────┐ │
│   │ MainDispatcherAgent │ │   │   │     Smart Router Engine     │ │
│   │ StrategyRouter      │ │   │   │  ┌───────┐  ┌───────────┐  │ │
│   │ HookManager         │ │   │   │  │评分器  │  │ 决策器     │  │ │
│   │ OpenClawAgent       │───►   │  └───────┘  └───────────┘  │ │
│   └─────────────────────┘ │   │   └─────────────────────────────┘ │
└───────────────────────────┘   └──────┬──────────────┬────────────┘
                                       │              │
                          ┌────────────┘              └────────────┐
                          ▼                                        ▼
              ┌──────────────────────┐              ┌──────────────────────────┐
              │  Gateway 轻量路由     │              │  OpenClaw Agent 执行链   │
              │  Port: 3000          │              │  Port: 3005              │
              │  (低延迟直连)         │              │  (Agent Soul + 工具调用)  │
              └──────────┬───────────┘              └──────────┬───────────────┘
                         │                                     │
              ┌──────────▼───────────┐              ┌──────────▼───────────────┐
              │  模型提供商           │              │  模型提供商               │
              │  Ollama / Moonshot / │              │  Ollama / Moonshot /     │
              │  DeepSeek            │              │  DeepSeek                │
              └──────────────────────┘              └──────────────────────────┘
```

### 1.2 架构图 (Mermaid)

```mermaid
graph TB
    subgraph Client["客户端层"]
        UI["Dashboard UI<br/>:3001/static"]
        API["REST API 客户端"]
        SDK["Python SDK"]
    end

    subgraph Scheduler["Python 调度器 (FastAPI :8000)"]
        MA["MainDispatcherAgent"]
        SR["StrategyRouter"]
        HM["HookManager<br/>Pre/Post Hooks"]
        OA["OpenClawAgent"]
        LA["LocalModelAgent"]
        CA["CloudModelAgent"]
    end

    subgraph Bridge["Bridge 智能路由层 (Node.js :3001)"]
        SMR["Smart Router Engine"]
        SC["复杂度评分器<br/>smartRouterScore()"]
        SD["路由决策器<br/>smartRouteDecision()"]
        GW["Gateway 路由<br/>dispatchViaOpenClaw()"]
        AC["Agent 执行链<br/>dispatchViaAgentChain()"]
        EC["AgentExecutionContext"]
        SOUL["Agent Soul 系统"]
    end

    subgraph Gateway["Gateway 轻量路由 (:3000)"]
        GM["模型解析器<br/>resolveModel()"]
        GP["代理映射<br/>AGENT_MAP"]
        GL["请求日志"]
    end

    subgraph OfficialGW["OpenClaw 官方 Gateway (:3005)"]
        OG["/v1/chat/completions"]
        OGA["Agent 调度"]
    end

    subgraph Providers["模型提供商"]
        OLL["Ollama<br/>qwen2.5:3b<br/>:11434"]
        MOON["Moonshot<br/>kimi-k2.6"]
        DEEP["DeepSeek<br/>deepseek-chat"]
    end

    subgraph Config["配置中心"]
        CFG["openclaw.json<br/>smartRouter / agents / models"]
    end

    UI -->|HTTP| Bridge
    API -->|HTTP| Scheduler
    SDK -->|HTTP| Scheduler
    Scheduler -->|Bridge /dispatch| Bridge
    MA --> SR
    MA --> HM
    MA --> OA
    MA --> LA
    MA --> CA
    OA -->|HTTP| Bridge

    Bridge --> SMR
    SMR --> SC
    SMR --> SD
    SD -->|score < threshold| GW
    SD -->|score >= threshold| AC
    AC --> EC
    AC --> SOUL
    GW --> Gateway
    AC --> OfficialGW

    Gateway --> GM
    GM --> GP
    GM --> Providers
    OfficialGW --> OGA
    OGA --> Providers

    OLL -->|Local| Providers
    MOON -->|Cloud| Providers
    DEEP -->|Cloud| Providers

    Config -->|load| Bridge
    Config -->|load| Gateway
```

### 1.3 核心设计原则

| 原则 | 说明 |
|------|------|
| **复杂度感知路由** | Smart Router 通过 6 维度评分自动选择 Gateway（快）或 Agent（强）路径 |
| **配置驱动** | 所有路由规则、阈值、Agent Soul 从 `openclaw.json` 加载，支持热重载 |
| **优雅降级** | Agent 执行链失败时自动回退到 Gateway 直连；Gateway 失败时回退到直接调厂商 API |
| **可观测性** | 每次请求附带完整 trace 链、评分明细、执行步骤和耗时统计 |

---

## 2. 模块调用关系

### 2.1 模块依赖图 (Mermaid)

```mermaid
graph LR
    subgraph Python["Python 层"]
        main["scheduler/main.py<br/>FastAPI 入口"]
        main_agent["agents/main_agent.py<br/>主调度器"]
        strategy["strategy/router.py<br/>策略路由"]
        openclaw_ag["agents/openclaw_agent.py<br/>OpenClaw 代理"]
        local_ag["agents/local_agent.py<br/>本地模型代理"]
        cloud_ag["agents/cloud_agent.py<br/>云端模型代理"]
        hooks["hooks/<br/>Pre/Post Hooks"]
        models["models/<br/>Request/Response"]
    end

    subgraph NodeJS["Node.js 层"]
        orchestrator["bridge/orchestrator.mjs<br/>Bridge 核心"]
        gateway["gateway/gateway.mjs<br/>Gateway 核心"]
    end

    subgraph Config["配置"]
        openclaw_json["openclaw.json"]
    end

    main --> main_agent
    main_agent --> strategy
    main_agent --> openclaw_ag
    main_agent --> local_ag
    main_agent --> cloud_ag
    main_agent --> hooks
    main_agent --> models

    openclaw_ag -->|HTTP :3001| orchestrator
    orchestrator -->|HTTP :3000| gateway
    orchestrator -->|HTTP :3005| OfficialGW["OpenClaw 官方 Gateway"]

    openclaw_json --> orchestrator
    openclaw_json --> gateway
```

### 2.2 模块职责表

| 模块 | 文件 | 端口 | 职责 |
|------|------|------|------|
| **Python 调度器** | `scheduler/main.py` | 8000 | FastAPI 入口，Hook 管理，模型注册，服务生命周期 |
| **主调度 Agent** | `scheduler/agents/main_agent.py` | — | 统一调度入口，策略路由，Fallback 管理 |
| **OpenClaw Agent** | `scheduler/agents/openclaw_agent.py` | — | 通过 Bridge 调度，失败回退直连 |
| **策略路由器** | `scheduler/strategy/router.py` | — | 优先级/约束/自适应路由决策 |
| **Bridge 核心** | `bridge/orchestrator.mjs` | 3001 | Smart Router + 双路径调度 + Agent Soul |
| **Gateway 核心** | `gateway/gateway.mjs` | 3000 | 模型解析、代理映射、请求转发 |
| **Dashboard** | `static/dashboard.html` | 3001/8000 | 前端交互界面 |

---

## 3. 详细设计

### 3.1 Smart Router 引擎

Smart Router 是 Bridge 的核心决策模块，负责根据请求复杂度自动选择 Gateway 路径（低延迟）或 Agent 执行链路径（高能力）。

#### 3.1.1 六维度评分模型

```
总分 = Σ(类型权重, 工具评分, Prompt长度评分, 关键词评分, 上下文评分, 优先级调整)
总分 ∈ [0, 100]
```

| 维度 | 评分规则 | 分值范围 | 配置键 |
|------|---------|---------|--------|
| **请求类型权重** | `chat=5, completion=5, tool_call=35, code_execution=40, image=25, audio=25, analysis=20, translation=10` | 5~40 | `typeWeights` |
| **工具调用** | 有工具 +30，多工具额外 +10/个 | 0~40 | `toolCallBase` + `multiToolBonus` |
| **Prompt 长度** | ≤50=0, ≤200=3, ≤500=8, ≤1000=12, >1000=18 | 0~18 | `promptLengthThresholds` |
| **复杂度关键词** | 中等词(分析/比较/设计...) = +5/个，高词(多步骤/自主/编排...) = +10/个 | 0~∞ | `complexityKeywords` |
| **上下文长度** | 每条消息 +5，上限 15 | 0~15 | `contextWeight` + `contextMaxScore` |
| **优先级调整** | P1=+10, P2=+5, P3=0, P4=-3, P5=-5 | -5~+10 | `priorityBoost` |

#### 3.1.2 路由决策逻辑

```
if 总分 >= 阈值(默认40):
    → Agent 执行链 (dispatchViaAgentChain)
else:
    → Gateway 轻量路由 (dispatchViaOpenClaw)
```

**决策原因生成规则：**

| 条件 | Gateway 原因 | Agent 原因 |
|------|-------------|-----------|
| score < 10 | "simple request, gateway sufficient" | — |
| 10 ≤ score < threshold | "below agent threshold, gateway for low latency" | — |
| score ≥ threshold | — | 列出触发因素（工具/类型/关键词/长度/上下文） |

#### 3.1.3 配置驱动架构

Smart Router 的所有规则从 `openclaw.json` 的 `models.smartRouter` 节加载：

```json
{
  "models": {
    "smartRouter": {
      "enabled": true,
      "threshold": 40,
      "defaultMode": "smart",
      "rules": {
        "typeWeights": { ... },
        "toolCallBase": 30,
        "multiToolBonus": 10,
        "promptLengthThresholds": [ ... ],
        "complexityKeywords": { "moderate": [...], "high": [...] },
        "contextWeight": 5,
        "contextMaxScore": 15,
        "priorityBoost": { "1": 10, "2": 5, "3": 0, "4": -3, "5": -5 }
      }
    }
  }
}
```

**加载流程：**

```
openclaw.json → loadOpenClawConfig() → buildSmartRouterRules()
                                         ↓
                                    合并默认值 + 配置值
                                         ↓
                                    SMART_ROUTER_RULES (运行时)
```

**热重载机制：** 调用 `POST /route/reload` 可在不重启服务的情况下重新加载配置。

#### 3.1.4 评分示例

| 请求 | 类型 | Prompt | 工具 | 评分明细 | 总分 | 路由 |
|------|------|--------|------|---------|------|------|
| "1+1=?" | chat | len=5 | 无 | type=+5, tools=+0, len=+0, kw=+0, ctx=+0, prio=+0 | **5** | 🌉 Gateway |
| "请分析市场趋势" | chat | len=7 | 无 | type=+5, tools=+0, len=+0, 分析=+5, ctx=+0, prio=+0 | **10** | 🌉 Gateway |
| "查询北京天气" | tool_call | len=6 | 1个 | type=+35, tools=+30, len=+0, kw=+0, ctx=+0, prio=+0 | **65** | 🤖 Agent |
| "Write sort script" | code_execution | len=36 | 无 | type=+40, tools=+0, len=+0, kw=+0, ctx=+0, prio=+0 | **40** | 🤖 Agent |
| "多步骤自主执行计划" | chat | len=10 | 无 | type=+5, tools=+0, len=+0, 设计+优化+多步骤+自主+执行计划=+50, ctx=+0, prio=+5 | **60** | 🤖 Agent |

### 3.2 Gateway 轻量路由

Gateway（端口 3000）是低延迟路径的核心，负责将 `openclaw/<agentId>` 格式的模型名解析为实际模型端点并转发请求。

#### 3.2.1 模型解析流程

```
请求 model="openclaw/cloud-dispatcher"
    → AGENT_MAP["cloud-dispatcher"].model.primary = "moonshot/kimi-k2.6"
    → MODEL_CATALOG["moonshot/kimi-k2.6"] = { baseUrl, apiKey, modelId }
    → 转发到 https://api.moonshot.cn/v1/chat/completions
```

#### 3.2.2 代理映射表

| 逻辑模型名 | Agent ID | 实际模型 | 提供商 |
|-----------|----------|---------|--------|
| `openclaw/local-dispatcher` | local-dispatcher | ollama/qwen2.5:3b | Ollama (本地) |
| `openclaw/cloud-dispatcher` | cloud-dispatcher | moonshot/kimi-k2.6 | Moonshot (云端) |
| `openclaw/code-executor` | code-executor | deepseek/deepseek-chat | DeepSeek (云端) |
| `openclaw/main` | main | ollama/qwen2.5:3b | Ollama (本地) |

### 3.3 Agent 执行链

Agent 执行链是高能力路径，通过 OpenClaw 官方 Gateway（端口 3005）执行，支持 Agent Soul、工具调用和多步骤推理。

#### 3.3.1 Agent Soul 系统

每个 Agent 拥有独立的 Soul，定义其人格、能力和执行计划：

```json
{
  "id": "cloud-dispatcher",
  "soul": {
    "name": "Cloud Dispatcher",
    "personality": "全面、强大、能力优先",
    "capabilities": ["云端模型调度", "工具调用", "多步推理", "长上下文处理"],
    "executionSteps": ["analyze_request", "select_model", "execute_inference", "validate_output", "report_result"]
  }
}
```

#### 3.3.2 执行上下文 (AgentExecutionContext)

每次 Agent 执行创建独立的执行上下文，记录完整的执行轨迹：

```
AgentExecutionContext
├── agentId: "cloud-dispatcher"
├── soul: Agent Soul 引用
├── trace: [{ agent, message, timestamp, elapsed }]
├── steps: [{ step, status, startTime, endTime, duration, result }]
└── status: "initialized" → "completed" | "failed"
```

#### 3.3.3 执行步骤

| 步骤 | 说明 | 输出 |
|------|------|------|
| `analyze_request` | 分析请求类型和复杂度 | `{ type, complexity, requires_tools, recommendedModel }` |
| `select_model` | 基于 Soul 和分析选择模型 | `{ endpoint, fallbacks, reason }` |
| `execute_inference` | 通过 OpenClaw 官方 Gateway 执行推理 | `{ output, usage, latency_ms, tool_calls }` |
| `validate_output` | 验证输出质量 | `{ quality, isComplete, outputLength }` |
| `report_result` | 生成执行报告 | `{ steps_completed, total_duration_ms }` |

#### 3.3.4 降级策略

```
OpenClaw 官方 Gateway (:3005)
    ↓ 失败
直接调用模型 API (callModelApi)
    ↓ 失败
Fallback 模型链 (最多 2 个)
    ↓ 全部失败
返回 ALL_MODELS_FAILED 错误
```

### 3.4 Python 调度器

Python 调度器（FastAPI :8000）是系统的入口服务，负责请求预处理、Hook 管理和统一调度。

#### 3.4.1 Hook 系统

| 阶段 | Hook | 功能 |
|------|------|------|
| **Pre** | `RequestValidationHook` | 请求参数校验 |
| **Pre** | `ConstraintEnrichmentHook` | 约束条件补充 |
| **Pre** | `RateLimitHook` | 速率限制 |
| **Pre** | `RequestLoggingHook` | 请求日志 |
| **Post** | `ResponseSanitizationHook` | 响应清洗 |
| **Post** | `CostCalculationHook` | 成本计算 |
| **Post** | `RetryDecisionHook` | 重试决策 |
| **Post** | `ResponseLoggingHook` | 响应日志 |

#### 3.4.2 调度流程

```
请求 → Pre Hooks → StrategyRouter.route()
                    ├── OpenClaw 可用 → OpenClawAgent → Bridge /dispatch
                    └── OpenClaw 不可用 → LocalAgent / CloudAgent → 直连模型
        → Post Hooks → 响应
```

---

## 4. 上下游输入与输出

### 4.1 Bridge API 接口规范

#### 4.1.1 POST /dispatch — 智能调度

**请求：**

```json
{
  "appid": "string",
  "type": "chat | completion | tool_call | code_execution | image | audio | analysis | translation",
  "prompt": "string",
  "priority": 1-5,
  "route_mode": "smart | gateway | agent",
  "tools": [{ "type": "function", "function": { "name": "...", "parameters": {} } }],
  "tool_choice": "auto | none | { type: 'function', function: { name: '...' } }",
  "context": [{ "role": "user|assistant|system", "content": "..." }],
  "constraints": { "require_local": true, "max_latency_ms": 3000, "preferred_providers": ["ollama"] },
  "parameters": { "temperature": 0.7, "top_p": 0.9 },
  "agent_id": "string",
  "api_key": "string",
  "timeout_ms": 30000,
  "model_hint": "string"
}
```

**响应（Smart 模式成功）：**

```json
{
  "request_id": "uuid",
  "appid": "string",
  "status": "success",
  "result": {
    "model_name": "ollama/qwen2.5:3b",
    "model_type": "local",
    "provider": "ollama",
    "output": "string",
    "usage": { "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30 },
    "latency_ms": 326,
    "cost": 0,
    "finish_reason": "stop",
    "routed_via_gateway": true,
    "actual_model": "qwen2.5:3b"
  },
  "smart_routing": {
    "mode": "smart",
    "decision": "gateway | agent",
    "score": 5,
    "threshold": 40,
    "breakdown": "type=chat → +5 | no tools → +0 | len=5 → +0 | ...",
    "reason": "simple request, gateway sufficient",
    "estimated_latency": "300-800ms"
  },
  "agent_trace": [{ "agent": "openclaw_router", "message": "..." }],
  "strategy_name": "adaptive_local_first",
  "routing_decision": { ... }
}
```

**响应（Agent 链路模式）：**

```json
{
  "status": "success",
  "result": { ... },
  "agent_trace": [
    { "agent": "agent_router", "message": "Agent chain mode: routing to agent=cloud-dispatcher" },
    { "agent": "agent_soul", "message": "Loaded soul: Cloud Dispatcher (全面、强大、能力优先)" },
    { "agent": "agent_analyzer", "message": "Request analysis: type=tool_call, complexity=complex" },
    { "agent": "agent_selector", "message": "Model selected: moonshot/kimi-k2.6" },
    { "agent": "agent_executor", "message": "OpenClaw Gateway inference completed" },
    { "agent": "agent_validator", "message": "Output validation: quality=good" }
  ],
  "execution_context": {
    "agent_id": "cloud-dispatcher",
    "soul_name": "Cloud Dispatcher",
    "steps_completed": 5,
    "steps_total": 5,
    "total_duration_ms": 47290
  }
}
```

#### 4.1.2 POST /route/score — 评分预览

**请求：** 同 /dispatch 的 body

**响应：**

```json
{
  "score": 65,
  "threshold": 40,
  "decision": "agent",
  "scores": {
    "type": { "value": 35, "detail": "type=tool_call → +35" },
    "tools": { "value": 30, "detail": "1 tool(s) → +30" },
    "promptLength": { "value": 0, "detail": "len=6 → +0" },
    "keywords": { "value": 0, "detail": "no keywords → +0" },
    "context": { "value": 0, "detail": "0 messages → +0" },
    "priority": { "value": 0, "detail": "priority=3 → +0" }
  },
  "breakdown": "type=tool_call → +35 | 1 tool(s) → +30 | len=6 → +0 | ...",
  "reason": "requires tool execution, complex type (tool_call)",
  "estimatedLatency": "3-60s"
}
```

#### 4.1.3 GET /route/rules — 查看路由规则

**响应：**

```json
{
  "threshold": 40,
  "rules": { ... },
  "default_mode": "smart",
  "config_source": "openclaw.json",
  "config_enabled": true
}
```

#### 4.1.4 POST /route/mode — 切换路由模式

**请求：** `{ "mode": "smart | gateway | agent" }`

#### 4.1.5 POST /route/reload — 热重载配置

**响应：**

```json
{
  "reloaded": true,
  "threshold": 40,
  "default_mode": "smart",
  "config_source": "openclaw.json",
  "config_enabled": true
}
```

#### 4.1.6 其他端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查（含 Gateway 可达性） |
| GET | `/models` | 模型列表（含状态） |
| GET | `/agents` | Agent 列表 |
| GET | `/agents/souls` | Agent Soul 详情 |
| GET | `/routing/config` | 路由配置 |
| GET | `/stats` | 统计信息 |
| POST | `/agent/message` | 直接向 Agent 发消息 |

### 4.2 Python 调度器 API 接口规范

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/dispatch` | 调度请求 |
| POST | `/dispatch/batch` | 批量调度 |
| GET | `/models` | 模型列表 |
| POST | `/models` | 注册模型 |
| DELETE | `/models/{name}` | 注销模型 |
| GET | `/models/{name}/status` | 模型状态 |
| GET | `/hooks` | Hook 列表 |
| POST | `/hooks/{name}/enable` | 启用 Hook |
| POST | `/hooks/{name}/disable` | 禁用 Hook |
| GET | `/stats` | 统计信息 |
| GET | `/history` | 调度历史 |
| GET | `/health` | 健康检查 |
| GET | `/openclaw/bridge/health` | Bridge 健康检查 |
| GET | `/openclaw/bridge/models` | Bridge 模型列表 |
| GET | `/openclaw/bridge/stats` | Bridge 统计 |
| POST | `/openclaw/agent/{agent_id}/message` | Agent 消息 |
| GET | `/openclaw/adaptive/state` | 自适应状态 |
| POST | `/openclaw/toggle` | 开关 OpenClaw |

---

## 5. 整体数据流图

### 5.1 Smart Router 决策流程图 (Mermaid)

```mermaid
flowchart TD
    A["客户端请求<br/>POST /dispatch"] --> B{"route_mode?"}

    B -->|"smart"| C["Smart Router 评分"]
    B -->|"gateway"| D["dispatchViaOpenClaw()"]
    B -->|"agent"| E["dispatchViaAgentChain()"]

    C --> F["6维度评分计算"]
    F --> F1["类型权重<br/>chat=5, tool_call=35, ..."]
    F --> F2["工具评分<br/>base=30, multi=+10"]
    F --> F3["Prompt长度<br/>0~18分"]
    F --> F4["关键词匹配<br/>moderate=+5, high=+10"]
    F --> F5["上下文长度<br/>5分/条, 上限15"]
    F --> F6["优先级调整<br/>P1=+10 ~ P5=-5"]

    F1 & F2 & F3 & F4 & F5 & F6 --> G["总分 = Σ各维度"]
    G --> H{"score >= threshold<br/>(默认40)?"}

    H -->|"否"| I["🌉 Gateway 路径"]
    H -->|"是"| J["🤖 Agent 执行链"]

    I --> D
    J --> E

    D --> D1["adaptiveRoute()<br/>自适应路由"]
    D1 --> D2["selectBestModel()<br/>选择最优模型"]
    D2 --> D3["callModelApi()<br/>调用模型API"]
    D3 --> D4{"调用成功?"}
    D4 -->|"是"| D5["返回结果"]
    D4 -->|"否"| D6["Fallback 模型"]
    D6 --> D7{"Fallback 成功?"}
    D7 -->|"是"| D5
    D7 -->|"否"| D8["返回失败"]

    E --> E1["resolveAgentId()<br/>解析Agent ID"]
    E1 --> E2["加载 Agent Soul"]
    E2 --> E3["analyze_request<br/>分析请求"]
    E3 --> E4["select_model<br/>选择模型"]
    E4 --> E5["callOpenClawOfficialGateway()<br/>调用官方Gateway :3005"]
    E5 --> E6{"调用成功?"}
    E6 -->|"是"| E7["validate_output<br/>验证输出"]
    E6 -->|"否"| E8["callModelApi()<br/>直接调用模型"]
    E8 --> E9{"直接调用成功?"}
    E9 -->|"是"| E7
    E9 -->|"否"| E10["Fallback 模型链"]
    E10 --> E11{"Fallback 成功?"}
    E11 -->|"是"| E7
    E11 -->|"否"| D8
    E7 --> E12["report_result<br/>生成报告"]
    E12 --> D5

    style C fill:#4CAF50,color:white
    style I fill:#2196F3,color:white
    style J fill:#FF9800,color:white
    style D5 fill:#4CAF50,color:white
    style D8 fill:#F44336,color:white
```

### 5.2 端到端数据流图 (Mermaid)

```mermaid
sequenceDiagram
    participant C as 客户端
    participant P as Python调度器 :8000
    participant B as Bridge :3001
    participant SR as Smart Router
    participant GW as Gateway :3000
    participant OG as OpenClaw官方GW :3005
    participant M as 模型提供商

    C->>P: POST /dispatch
    P->>P: Pre Hooks (验证/限流/日志)
    P->>B: POST /dispatch (route_mode=smart)
    B->>SR: smartRouteDecision(request)

    alt score < 40 (Gateway路径)
        SR-->>B: decision=gateway
        B->>GW: POST /v1/chat/completions
        GW->>GW: resolveModel(openclaw/agentId)
        GW->>M: 转发到实际模型API
        M-->>GW: 模型响应
        GW-->>B: OpenAI格式响应
    else score >= 40 (Agent路径)
        SR-->>B: decision=agent
        B->>B: resolveAgentId() + 加载Soul
        B->>OG: POST /v1/chat/completions (model=openclaw/agentId)
        OG->>M: Agent调度推理
        M-->>OG: 模型响应
        OG-->>B: OpenAI格式响应
        Note over B: 失败时降级到callModelApi()
    end

    B-->>P: 调度结果 + smart_routing + trace
    P->>P: Post Hooks (清洗/计费/日志)
    P-->>C: DispatchResponse
```

### 5.3 配置加载与热重载流程 (Mermaid)

```mermaid
flowchart LR
    subgraph 启动时
        A["openclaw.json"] -->|readFileSync| B["loadOpenClawConfig()"]
        B --> C["openclawConfig (内存)"]
        C --> D["buildSmartRouterRules()"]
        D --> E["SMART_ROUTER_RULES<br/>SMART_ROUTER_THRESHOLD<br/>DEFAULT_ROUTE_MODE"]
        C --> F["loadAgentSouls()"]
        F --> G["AGENT_SOULS"]
    end

    subgraph 运行时热重载
        H["POST /route/reload"] --> I["loadOpenClawConfig()"]
        I --> J["Object.assign(openclawConfig, newConfig)"]
        J --> K["buildSmartRouterRules()"]
        K --> L["SMART_ROUTER_RULES 更新"]
    end

    subgraph API查询
        M["GET /route/rules"] --> N["返回当前规则<br/>+ config_source<br/>+ config_enabled"]
        O["POST /route/score"] --> P["使用当前规则评分"]
    end

    E --> M
    E --> O
    L --> M
    L --> O
```

---

## 6. 可扩展性

### 6.1 水平扩展

| 组件 | 扩展方式 | 说明 |
|------|---------|------|
| **Bridge** | 多实例 + 负载均衡 | 无状态设计，可水平扩展 |
| **Gateway** | 多实例 + Nginx | 请求日志可外置到 Redis |
| **OpenClaw 官方 GW** | 集群模式 | OpenClaw 原生支持 |
| **模型提供商** | 动态注册 | 通过 `POST /models` 热添加 |

### 6.2 垂直扩展

| 扩展点 | 方式 | 配置/代码位置 |
|--------|------|-------------|
| **新增模型** | 在 `openclaw.json` 的 `models.catalog` 中添加 | `openclaw.json` |
| **新增 Agent** | 在 `openclaw.json` 的 `agents.list` 中添加，自动生成 Soul | `openclaw.json` + `buildAgentSoul()` |
| **调整路由阈值** | 修改 `smartRouter.threshold`，调用 `/route/reload` | `openclaw.json` |
| **新增评分维度** | 在 `smartRouterScore()` 中添加新维度计算 | `orchestrator.mjs` |
| **新增关键词** | 在 `complexityKeywords.moderate/high` 中添加 | `openclaw.json` |
| **新增 Hook** | 实现 `BasePreHook` / `BasePostHook` 并注册 | `scheduler/hooks/` |
| **新增路由策略** | 在 `StrategyRouter` 中添加策略 | `scheduler/strategy/router.py` |

### 6.3 未来扩展方向

#### 6.3.1 自适应阈值

当前阈值固定为 40，可扩展为根据系统负载动态调整：

```
threshold = base_threshold + load_adjustment
load_adjustment = f(cpu_usage, queue_depth, avg_latency)
```

当系统负载高时提高阈值（更多请求走快速 Gateway），负载低时降低阈值（更多请求走 Agent 获得更高质量）。

#### 6.3.2 ML 驱动的评分权重

当前评分权重为人工设定，可扩展为基于历史数据的机器学习模型：

```
score = ML_model(request_features)
features = [type_encoded, tool_count, prompt_length, keyword_count, context_size, priority]
```

训练数据来源：历史请求的评分 vs 实际执行结果（质量、延迟、成本）。

#### 6.3.3 A/B 测试框架

对同一请求同时走两条路径，比较结果质量和延迟，用于优化评分权重：

```
request → [Gateway路径] → result_A
       → [Agent路径]  → result_B
compare(result_A, result_B) → 更新权重
```

#### 6.3.4 多租户隔离

在 `openclaw.json` 中按 `appid` 配置独立的 Smart Router 规则：

```json
{
  "models": {
    "smartRouter": {
      "tenants": {
        "app-A": { "threshold": 30, "rules": { ... } },
        "app-B": { "threshold": 50, "rules": { ... } }
      }
    }
  }
}
```

#### 6.3.5 流量灰度

按百分比分配流量到不同路径：

```json
{
  "smartRouter": {
    "trafficSplit": {
      "gateway": 70,
      "agent": 30
    }
  }
}
```

---

## 附录 A: 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BRIDGE_PORT` | 3001 | Bridge 监听端口 |
| `OPENCLAW_GATEWAY_URL` | http://localhost:3000 | 自定义 Gateway 地址 |
| `OPENCLAW_OFFICIAL_GATEWAY_URL` | http://127.0.0.1:3005 | OpenClaw 官方 Gateway 地址 |
| `USE_OPENCLAW_GATEWAY` | true | 是否通过 Gateway 路由 |
| `DEFAULT_ROUTE_MODE` | smart | 默认路由模式 (smart/gateway/agent) |
| `SMART_ROUTER_THRESHOLD` | 40 | Smart Router 阈值（可被配置文件覆盖） |
| `AGENT_EXECUTION_TIMEOUT` | 120000 | Agent 执行超时 (ms) |
| `MOONSHOT_API_KEY` | — | Moonshot API 密钥 |
| `DEEPSEEK_API_KEY` | — | DeepSeek API 密钥 |
| `OPENCLAW_TOKEN` | — | OpenClaw 官方 Gateway 认证令牌 |

## 附录 B: 端口分配

| 端口 | 服务 | 协议 |
|------|------|------|
| 3000 | OpenClaw Gateway (自定义) | HTTP |
| 3001 | Bridge 智能路由 | HTTP |
| 3005 | OpenClaw 官方 Gateway | HTTP |
| 8000 | Python 调度器 (FastAPI) | HTTP |
| 11434 | Ollama 本地模型服务 | HTTP |

## 附录 C: 性能基准

| 场景 | 路径 | 典型延迟 | 说明 |
|------|------|---------|------|
| 简单对话 (1+1=?) | Smart → Gateway | ~300ms | 本地 Ollama 推理 |
| 中等对话 (分析趋势) | Smart → Gateway | ~5s | 本地模型长输出 |
| 工具调用 (查天气) | Smart → Agent | ~47s | OpenClaw GW + 云端模型 |
| 代码执行 | Smart → Agent | ~47s | OpenClaw GW + Agent Soul |
| 复杂任务 (多步骤) | Smart → Agent | ~55s | OpenClaw GW + 长上下文 |
