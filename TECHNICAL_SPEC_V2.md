# OpenClaw 多智能体模型调度系统 — 技术说明书 V2

> 版本: 2026.4.27  
> 生成日期: 2026-05-27  
> 基于代码实际实现编写，覆盖整体架构、模块详细设计、调用关系、上下游接口及技术细节

---

## 目录

1. [系统概述](#1-系统概述)
2. [整体架构](#2-整体架构)
3. [模块详细设计](#3-模块详细设计)
4. [调用关系与数据流](#4-调用关系与数据流)
5. [上下游接口定义](#5-上下游接口定义)
6. [核心算法与实现细节](#6-核心算法与实现细节)
7. [部署与运维](#7-部署与运维)

---

## 1. 系统概述

OpenClaw 多智能体模型调度系统是一个基于 FastAPI + Node.js 的双路径 AI 模型调度框架，核心能力包括：

- **双路径调度架构**：Python 直连路径（快速响应）与 Bridge→Gateway 路径（Agent 工具链支持）
- **6 种自适应调度策略**：LOCAL_FIRST、CLOUD_FIRST、COST_OPTIMIZED、LATENCY_OPTIMIZED、CAPABILITY_OPTIMIZED、PRIVACY_FIRST
- **智能 Fallback 机制**：仅在主请求失败/超时时触发降级，避免不必要的冗余调用
- **Hook 管道**：Pre-Hook（限流、校验、约束增强、日志）与 Post-Hook（脱敏、计费、重试决策、日志）
- **OpenClaw Gateway 集成**：Bridge 通过 Gateway 的 `/v1/chat/completions` 端点路由到 OpenClaw Agent
- **实时监控 Dashboard**：支持双路径对比测试、Agent 选择、调度历史查看

---

## 2. 整体架构

### 2.1 架构总览图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        客户端 (Browser / API Client)                 │
│                              │                                       │
│                    ┌─────────▼──────────┐                            │
│                    │  Dashboard (HTML)   │                            │
│                    │  :8000/             │                            │
│                    └─────────┬──────────┘                            │
└──────────────────────────────┼──────────────────────────────────────┘
                               │ HTTP
┌──────────────────────────────▼──────────────────────────────────────┐
│                    FastAPI 调度服务 (:8000)                           │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                   MainDispatcherAgent                         │   │
│  │  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌───────────┐  │   │
│  │  │ Pre-Hook │  │ Strategy │  │   Agent   │  │ Post-Hook │  │   │
│  │  │ Pipeline │→│  Router  │→│  Executor  │→│ Pipeline  │  │   │
│  │  └──────────┘  └──────────┘  └───────────┘  └───────────┘  │   │
│  │       │              │              │              │          │   │
│  │       ▼              ▼              ▼              ▼          │   │
│  │  ┌──────────────────────────────────────────────────────┐    │   │
│  │  │              ModelRegistry (端点注册表)               │    │   │
│  │  │  ┌─────────────┐ ┌─────────────┐ ┌───────────────┐  │    │   │
│  │  │  │ ollama-qwen │ │  kimi-k2.6  │ │ deepseek-chat │  │    │   │
│  │  │  │   (local)   │ │   (cloud)   │ │    (cloud)    │  │    │   │
│  │  │  └─────────────┘ └─────────────┘ └───────────────┘  │    │   │
│  │  └──────────────────────────────────────────────────────┘    │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                               │                                      │
│              ┌────────────────┼────────────────┐                     │
│              ▼ (路径1)        ▼ (路径2)         │                     │
│     ┌────────────────┐ ┌──────────────────┐    │                     │
│     │  Python 直连    │ │ OpenClawAgent    │    │                     │
│     │  LocalAgent    │ │ → Bridge :3001   │    │                     │
│     │  CloudAgent    │ │   → Gateway :3000│    │                     │
│     └───────┬────────┘ └────────┬─────────┘    │                     │
└─────────────┼───────────────────┼──────────────┘                     │
              │                   │                                      │
              ▼                   ▼                                      │
┌──────────────────┐  ┌───────────────────────────────────────┐        │
│  模型厂商 API     │  │       OpenClaw Gateway (:3000)        │        │
│  ┌─────────────┐ │  │  ┌─────────────────────────────────┐ │        │
│  │ Ollama      │ │  │  │ /v1/chat/completions            │ │        │
│  │ :11434/v1   │ │  │  │  → openclaw/local-dispatcher    │ │        │
│  └─────────────┘ │  │  │  → openclaw/cloud-dispatcher    │ │        │
│  ┌─────────────┐ │  │  │  → openclaw/code-executor       │ │        │
│  │ Moonshot    │ │  │  └─────────────────────────────────┘ │        │
│  │ api.moonshot│ │  └───────────────────────────────────────┘        │
│  └─────────────┘ │                                                    │
│  ┌─────────────┐ │                                                    │
│  │ DeepSeek    │ │                                                    │
│  │ api.deepseek│ │                                                    │
│  └─────────────┘ │                                                    │
└──────────────────┘                                                     │
```

### 2.2 双路径调度架构

| 特性 | 路径1: Python 直连 | 路径2: Bridge→Gateway |
|------|-------------------|----------------------|
| 入口 | `LocalAgent` / `CloudAgent` | `OpenClawAgent` → Bridge :3001 |
| 调用链 | FastAPI → Agent → 厂商API | FastAPI → OpenClawAgent → Bridge → Gateway → Agent |
| 延迟 | 低（1跳） | 中（3跳） |
| 工具链支持 | 无 | 支持 OpenClaw Agent 工具链 |
| 适用场景 | 简单对话、快速响应 | 需要工具调用、代码执行、多步推理 |
| Fallback | Agent 内部 fallback | Bridge 内部 fallback + Python 层 fallback |

### 2.3 目录结构

```
OpenClaw_Multi_Agent/
├── run.py                          # 启动入口
├── openclaw.json                   # OpenClaw 项目配置（Agent/模型/路由）
├── scheduler/
│   ├── __init__.py
│   ├── main.py                     # FastAPI 应用入口，API 端点定义
│   ├── config/
│   │   ├── __init__.py
│   │   └── settings.py             # 环境变量配置（单例模式）
│   ├── models/
│   │   ├── __init__.py
│   │   ├── request.py              # 请求模型（DispatchRequest, Priority, RequestType）
│   │   ├── response.py             # 响应模型（DispatchResponse, ModelResult, ErrorDetail）
│   │   └── model_config.py         # 模型端点注册（ModelEndpoint, ModelRegistry）
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── base_agent.py           # Agent 基类（HTTP调用、负载追踪）
│   │   ├── local_agent.py          # 本地模型代理
│   │   ├── cloud_agent.py          # 云端模型代理
│   │   ├── openclaw_agent.py       # OpenClaw Bridge 通信代理
│   │   └── main_agent.py           # 主调度代理（编排所有子代理）
│   ├── strategy/
│   │   ├── __init__.py
│   │   ├── router.py               # 策略路由器（策略分发）
│   │   ├── adaptive.py             # 自适应调度策略（6种策略选择）
│   │   ├── priority.py             # 优先级评分策略
│   │   └── load_balance.py         # 负载均衡策略（5种算法）
│   └── hooks/
│       ├── __init__.py
│       ├── hook_manager.py         # Hook 管理器（管道编排）
│       ├── pre_hook.py             # Pre-Hook 实现（限流/校验/增强/日志）
│       └── post_hook.py            # Post-Hook 实现（脱敏/计费/重试/日志）
├── bridge/
│   └── orchestrator.mjs            # Node.js Bridge 服务（Gateway路由）
├── static/
│   └── dashboard.html              # 前端监控面板
└── TECHNICAL_SPEC_V2.md            # 本文档
```

---

## 3. 模块详细设计

### 3.1 入口与启动模块

#### 3.1.1 run.py

启动入口，通过 uvicorn 运行 FastAPI 应用。

```python
uvicorn.run("scheduler.main:app", host=settings.host, port=settings.port)
```

#### 3.1.2 scheduler/main.py

FastAPI 应用核心，负责：

1. **生命周期管理**（`lifespan` 上下文管理器）：
   - 初始化默认模型端点（`_init_default_models`）
   - 初始化 Hook 管道（`_init_hooks`）
   - 启动 Bridge 子进程（`_start_bridge`）
   - 创建 `MainDispatcherAgent` 实例
   - 关闭时清理资源

2. **API 端点定义**：

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/dispatch` | 单请求调度 |
| POST | `/dispatch/batch` | 批量调度 |
| GET | `/models` | 列出所有模型端点 |
| POST | `/models` | 注册新模型端点 |
| DELETE | `/models/{name}` | 注销模型端点 |
| GET | `/models/{name}/status` | 获取端点详细状态 |
| GET | `/hooks` | 列出所有 Hook |
| POST | `/hooks/{name}/enable` | 启用 Hook |
| POST | `/hooks/{name}/disable` | 禁用 Hook |
| GET | `/stats` | 调度统计 + 系统资源 |
| GET | `/history` | 最近调度历史 |
| GET | `/health` | 健康检查 |
| GET | `/openclaw/bridge/health` | Bridge 健康检查 |
| GET | `/openclaw/bridge/models` | Bridge 模型列表 |
| GET | `/openclaw/bridge/stats` | Bridge 统计 |
| POST | `/openclaw/agent/{agent_id}/message` | Agent 消息发送 |
| GET | `/openclaw/adaptive/state` | 自适应状态 |
| POST | `/openclaw/toggle` | 开关 OpenClaw 集成 |
| GET | `/` | Dashboard 页面 |

3. **默认模型端点初始化**（`_init_default_models`）：

| 端点名 | 类型 | Provider | 模型ID | 上下文长度 | 工具支持 | 输入成本/1k | 优先级 |
|--------|------|----------|--------|-----------|---------|------------|--------|
| ollama-qwen2.5 | local | ollama | qwen2.5:3b | 32768 | 否 | $0.00 | 1 |
| kimi-k2.6 | cloud | moonshot | kimi-k2.6 | 262144 | 是 | $0.76 | 2 |
| deepseek-chat | cloud | deepseek | deepseek-chat | 64000 | 否 | $0.00014 | 3 |

4. **Hook 初始化**（`_init_hooks`）：

Pre-Hook（按优先级排序）：
- `rate_limit` (priority=5)
- `request_validation` (priority=10)
- `constraint_enrichment` (priority=20)
- `request_logging` (priority=90)

Post-Hook（按优先级排序）：
- `response_sanitization` (priority=10)
- `cost_calculation` (priority=20)
- `retry_decision` (priority=30)
- `response_logging` (priority=90)

5. **Bridge 子进程管理**（`_start_bridge` / `_stop_bridge`）：
   - 通过 `subprocess.Popen` 启动 `node bridge/orchestrator.mjs`
   - 关闭时先 `terminate()`，5秒超时后 `kill()`

---

### 3.2 请求模型层

**文件**: `scheduler/models/request.py`

#### DispatchRequest

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| appid | str | 是 | - | 应用标识，用于路由和追踪 |
| type | RequestType | 是 | - | 请求类型 |
| prompt | str | 是 | - | 输入提示文本 |
| priority | Priority | 否 | NORMAL(3) | 优先级 |
| model_hint | Optional[str] | 否 | None | 模型名称提示 |
| constraints | Optional[ModelConstraint] | 否 | None | 模型选择约束 |
| parameters | Optional[Dict] | 否 | None | 额外模型参数 |
| context | Optional[List[Dict]] | 否 | None | 对话上下文消息 |
| metadata | Optional[Dict] | 否 | None | 附加元数据 |
| request_id | Optional[str] | 否 | None | 请求ID（幂等性） |
| timeout_ms | Optional[int] | 否 | 30000 | 超时毫秒数 |

#### RequestType 枚举

| 值 | 说明 |
|----|------|
| CHAT | 对话 |
| COMPLETION | 补全 |
| EMBEDDING | 向量嵌入 |
| IMAGE | 图像生成 |
| AUDIO | 音频处理 |
| TOOL_CALL | 工具调用 |

#### Priority 枚举

| 值 | 数值 | 说明 |
|----|------|------|
| CRITICAL | 1 | 紧急 |
| HIGH | 2 | 高优先级 |
| NORMAL | 3 | 普通 |
| LOW | 4 | 低优先级 |
| BACKGROUND | 5 | 后台 |

#### ModelConstraint

| 字段 | 类型 | 说明 |
|------|------|------|
| max_latency_ms | Optional[int] | 最大可接受延迟(ms) |
| min_context_length | Optional[int] | 最小上下文窗口长度 |
| require_streaming | Optional[bool] | 是否需要流式响应 |
| require_local | Optional[bool] | 是否必须本地模型（隐私） |
| require_gpu | Optional[bool] | 是否需要GPU加速 |
| preferred_providers | Optional[List[str]] | 首选模型供应商 |
| excluded_providers | Optional[List[str]] | 排除的模型供应商 |
| max_cost_per_request | Optional[float] | 单请求最大成本(USD) |

---

### 3.3 响应模型层

**文件**: `scheduler/models/response.py`

#### DispatchResponse

| 字段 | 类型 | 说明 |
|------|------|------|
| request_id | str | 唯一请求标识 |
| appid | str | 应用标识 |
| status | DispatchStatus | 调度状态 |
| result | Optional[ModelResult] | 主模型结果 |
| fallback_results | Optional[List[ModelResult]] | 降级模型结果 |
| error | Optional[ErrorDetail] | 错误详情 |
| agent_trace | Optional[List[Dict]] | Agent 执行追踪 |
| hooks_applied | Optional[List[str]] | 已应用的 Hook 列表 |
| timestamp | str | 响应时间戳 |
| total_latency_ms | Optional[int] | 端到端总延迟(ms) |

#### DispatchStatus 枚举

| 值 | 说明 |
|----|------|
| SUCCESS | 成功 |
| PARTIAL | 部分成功（主请求失败，降级成功） |
| FAILED | 失败 |
| TIMEOUT | 超时 |
| REJECTED | 被拒绝（限流等） |

#### ModelResult

| 字段 | 类型 | 说明 |
|------|------|------|
| model_name | str | 处理请求的模型名称 |
| model_type | str | 模型类型(local/cloud) |
| provider | str | 模型供应商 |
| output | Any | 模型输出内容 |
| usage | Optional[Dict] | Token 使用统计 |
| latency_ms | int | 处理延迟(ms) |
| cost | Optional[float] | 估算成本(USD) |
| finish_reason | Optional[str] | 完成原因 |
| routed_via_gateway | bool | 是否经过 Gateway 路由 |
| actual_model | Optional[str] | Gateway 实际使用的模型ID |

#### ErrorDetail

| 字段 | 类型 | 说明 |
|------|------|------|
| code | str | 错误码 |
| message | str | 错误消息 |
| agent | Optional[str] | 出错的 Agent |
| retryable | bool | 是否可重试 |

---

### 3.4 模型注册与端点管理

**文件**: `scheduler/models/model_config.py`

#### ModelType 枚举

| 值 | 说明 |
|----|------|
| LOCAL | 本地模型 |
| CLOUD | 云端模型 |
| HYBRID | 混合模型 |

#### ModelProvider 枚举

| 值 | 说明 |
|----|------|
| OLLAMA | Ollama 本地推理 |
| LMSTUDIO | LM Studio |
| OPENAI | OpenAI |
| ANTHROPIC | Anthropic |
| GOOGLE | Google |
| AZURE | Azure OpenAI |
| DEEPSEEK | DeepSeek |
| MOONSHOT | Moonshot (Kimi) |
| CUSTOM | 自定义 |

#### ModelEndpoint

核心属性：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| name | str | - | 唯一端点名称 |
| display_name | Optional[str] | None | 显示名称 |
| model_type | ModelType | - | 本地/云端 |
| provider | ModelProvider | - | 供应商 |
| base_url | str | - | API 基础URL |
| api_key | Optional[str] | None | API密钥 |
| model_id | str | - | 模型标识符 |
| max_context_length | int | 4096 | 最大上下文长度 |
| supports_streaming | bool | True | 是否支持流式 |
| supports_tools | bool | False | 是否支持工具调用 |
| cost_per_1k_input_tokens | float | 0.0 | 输入Token成本 |
| cost_per_1k_output_tokens | float | 0.0 | 输出Token成本 |
| priority | int | 5 | 优先级(1最高) |
| weight | int | 1 | 负载均衡权重 |
| max_concurrent | int | 10 | 最大并发数 |
| current_load | int | 0 | 当前活跃请求数 |
| avg_latency_ms | int | 0 | 平均延迟(ms) |
| success_rate | float | 1.0 | 成功率(0.0-1.0) |
| enabled | bool | True | 是否启用 |
| tags | Optional[List[str]] | None | 分类标签 |

计算属性与方法：

- `is_available` → `enabled and current_load < max_concurrent`
- `load_factor` → `current_load / max_concurrent`
- `increment_load()` / `decrement_load()` → 负载增减
- `update_latency(latency_ms)` → EMA 延迟更新（α=0.7旧值, 0.3新值）
- `update_success_rate(success)` → EMA 成功率更新（α=0.95）

#### ModelRegistry

端点注册表，提供按类型/供应商/可用性查询的能力：

| 方法 | 返回 | 说明 |
|------|------|------|
| `register(endpoint)` | None | 注册端点 |
| `unregister(name)` | None | 注销端点 |
| `get(name)` | Optional[ModelEndpoint] | 按名称获取 |
| `get_by_type(model_type)` | List[ModelEndpoint] | 按类型筛选（已启用） |
| `get_by_provider(provider)` | List[ModelEndpoint] | 按供应商筛选（已启用） |
| `get_available()` | List[ModelEndpoint] | 所有可用端点 |
| `get_local_available()` | List[ModelEndpoint] | 可用的本地端点 |
| `get_cloud_available()` | List[ModelEndpoint] | 可用的云端端点 |
| `list_all()` | List[ModelEndpoint] | 所有端点（含不可用） |

---

### 3.5 调度代理层

#### 3.5.1 BaseAgent（基类）

**文件**: `scheduler/agents/base_agent.py`

所有 Agent 的抽象基类，提供：

- **HTTP 客户端管理**：`httpx.AsyncClient` 懒初始化，120秒超时
- **`call_model_api(endpoint, request)`**：核心方法，直连厂商 API
  - 构建请求 payload（`_build_payload`）
  - 构建请求头（`_build_headers`，含 Bearer Token）
  - 发送 POST 到 `{endpoint.base_url}/chat/completions`
  - 解析响应（`_parse_response`）
  - 更新端点延迟和成功率
  - 负载追踪（increment/decrement）
  - 异常处理：`TimeoutError` / `HTTPStatusError` / 通用异常
- **`execute_with_fallback(request, endpoints, max_retries)`**：带降级的执行

Payload 构建规则：
```json
{
  "model": "<endpoint.model_id>",
  "messages": [<context...>, {"role": "user", "content": "<prompt>"}],
  "max_tokens": 1024,
  "...<request.parameters>",
  "stream": true
}
```

响应解析规则：
- 提取 `choices[0].message.content` 作为输出
- 若 content 为空但存在 `reasoning_content`，则使用 reasoning_content
- 计算 cost = (input_tokens/1000 × input_price) + (output_tokens/1000 × output_price)

#### 3.5.2 LocalModelAgent

**文件**: `scheduler/agents/local_agent.py`

本地模型代理，`can_handle` 始终返回 True（当有本地端点可用时）。

端点选择策略（`_select_endpoint`）：
1. 若有 `model_hint`，优先匹配名称或模型ID
2. 若有 `preferred_providers`，优先匹配供应商
3. 按 `(load_factor, avg_latency_ms)` 排序选择最优

#### 3.5.3 CloudModelAgent

**文件**: `scheduler/agents/cloud_agent.py`

云端模型代理，端点选择策略：

1. 若有 `model_hint`，优先匹配
2. 若有 `preferred_providers`，优先匹配
3. 若有 `excluded_providers`，排除对应供应商
4. 若有 `max_cost_per_request`，过滤超出预算的端点
5. 按 `(priority, load_factor, avg_latency_ms)` 排序选择

#### 3.5.4 OpenClawAgent

**文件**: `scheduler/agents/openclaw_agent.py`

OpenClaw Bridge 通信代理，实现双路径调度：

**`execute(request, endpoint)`** 流程：
1. 若指定了 endpoint → 调用 `call_model_api` 直连
2. 否则 → 尝试 `_dispatch_via_bridge` (Bridge :3001)
3. Bridge 失败 → `_fallback_direct` (直连厂商 API)

**`_dispatch_via_bridge(request)`**：
- POST `{bridge_url}/dispatch`
- 超时: `request.timeout_ms / 1000` 秒
- 解析 Bridge 返回的 JSON，构造 `ModelResult`
- Bridge 返回 HTTP 4xx/5xx → 返回 None（触发降级）

**`_fallback_direct(request)`**：
- 从 Registry 获取可用端点
- 优先本地端点，其次云端端点
- 按 `(load_factor, avg_latency_ms)` 选择最优

**`send_agent_message(agent_id, prompt, ...)`**：
- POST `{bridge_url}/agent/message`
- 用于直接与特定 Agent 通信

**辅助方法**：
- `get_bridge_health()` → GET `{bridge_url}/health`
- `get_bridge_models(model_type)` → GET `{bridge_url}/models`
- `get_bridge_stats()` → GET `{bridge_url}/stats`

#### 3.5.5 MainDispatcherAgent（主调度代理）

**文件**: `scheduler/agents/main_agent.py`

核心编排器，协调所有子代理和 Hook 管道。

**初始化参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| registry | ModelRegistry | - | 模型注册表 |
| hook_manager | Optional[HookManager] | None | Hook 管理器 |
| use_openclaw | bool | True | 是否启用 OpenClaw |
| bridge_url | str | "http://localhost:3001" | Bridge 地址 |

**`dispatch(request)` 主流程**：

```
1. 执行 Pre-Hook 管道
2. 若 use_openclaw=True:
   a. 尝试 _dispatch_openclaw (OpenClaw 路径)
   b. 成功 → 返回结果
   c. 失败 → 降级到直连路径
3. StrategyRouter.route(request) → RoutingDecision
4. 根据 agent_type 选择 Agent (local/cloud)
5. agent.execute(request, endpoint) → ModelResult
6. 记录自适应结果
7. 执行 Post-Hook 管道
8. 构造 DispatchResponse 返回
```

**异常处理与 Fallback**：

- `TimeoutError`：记录失败 → 尝试 fallback_endpoints（最多2个） → 返回 PARTIAL 或 TIMEOUT
- `Exception`：记录失败 → 尝试 fallback_endpoints → 返回 PARTIAL 或 FAILED
- **Fallback 仅在主请求失败时触发**，成功时 `fallback_results = None`

**`_dispatch_openclaw(request, request_id, start_time)`**：
- 调用 `strategy_router.route(request)` 获取路由决策
- 通过 `openclaw_agent.execute(request, endpoint)` 执行
- 成功 → 返回 DispatchResponse
- 失败 → 返回 None（触发降级到直连路径）

**`_try_fallbacks(request, primary_agent, fallback_endpoints)`**：
- 遍历 fallback_endpoints（最多2个）
- 跳过不可用端点
- 根据 endpoint.model_type 选择对应 Agent
- 收集成功结果

**Agent 追踪**（`_trace`）：
- 记录每步执行信息到 `_agent_trace`
- 包含 agent 名称、消息、时间戳
- 随 DispatchResponse 返回给客户端

---

### 3.6 策略路由层

**文件**: `scheduler/strategy/router.py`

#### RoutingDecision 数据类

| 字段 | 类型 | 说明 |
|------|------|------|
| agent_type | str | 代理类型(local/cloud) |
| selected_endpoint | Optional[ModelEndpoint] | 选中的端点 |
| fallback_endpoints | List[ModelEndpoint] | 降级端点列表 |
| strategy_name | str | 策略名称 |
| reason | str | 路由原因 |
| use_openclaw | bool | 是否使用 OpenClaw |

#### StrategyRouter

路由分发器，根据 `use_openclaw` 标志选择路由模式：

- `use_openclaw=True` → `_route_adaptive`（自适应路由）
- `use_openclaw=False` → `_route_legacy`（传统路由）

**自适应路由决策树**：

```
AdaptiveStrategy.route(request) → (agent_type, strategy)
│
├── PRIVACY_FIRST → _route_local (强制本地)
├── LOCAL_FIRST → _route_local_first_adaptive
│   ├── 有本地端点 → Power-of-Two 选择 + 云端降级
│   └── 无本地端点 → Least-Connections 选择云端
├── CLOUD_FIRST → _route_cloud_first_adaptive
│   ├── TOOL_CALL → 优先工具支持端点
│   ├── 普通请求 → Priority 选择
│   └── 无云端 → 降级本地
├── LATENCY_OPTIMIZED → _route_latency_optimized
│   ├── 有历史延迟 → 选最低延迟
│   ├── 无历史 → 本地优先
│   └── 仅云端可用 → 选云端
├── COST_OPTIMIZED → _route_cost_optimized
│   ├── 有本地 → 本地(零成本)
│   ├── 有低成本云端 → 选最便宜
│   └── 仅高成本云端 → 选最便宜
├── CAPABILITY_OPTIMIZED → _route_capability_optimized
│   ├── TOOL_CALL → 工具支持端点
│   ├── 长上下文 → 大上下文端点
│   ├── 特殊类型 → 专用端点
│   └── 本地可处理 → 本地
└── 默认 → _route_default (本地优先)
```

**降级端点选择规则**：
- 最多选择 2-3 个 fallback 端点
- 优先同类型端点，然后跨类型
- 按 load_factor 和 avg_latency_ms 排序

---

### 3.7 自适应调度策略

**文件**: `scheduler/strategy/adaptive.py`

#### 6 种策略常量

| 策略 | 常量 | 触发条件 |
|------|------|---------|
| 本地优先 | LOCAL_FIRST | 默认策略，无特殊约束 |
| 云端优先 | CLOUD_FIRST | preferred_providers 匹配云端供应商 |
| 成本优化 | COST_OPTIMIZED | 低优先级 / max_cost_per_request ≤ 0 |
| 延迟优化 | LATENCY_OPTIMIZED | CRITICAL/HIGH 优先级 / max_latency_ms ≤ 1000 |
| 能力优化 | CAPABILITY_OPTIMIZED | TOOL_CALL/IMAGE/AUDIO / 长上下文 / EMBEDDING |
| 隐私优先 | PRIVACY_FIRST | require_local=True |

#### 请求分类算法（`_classify_request`）

```
1. require_local → PRIVACY_FIRST
2. require_gpu → CAPABILITY_OPTIMIZED
3. preferred_providers 匹配云端 → CLOUD_FIRST
4. type ∈ {tool_call, image, audio} → CAPABILITY_OPTIMIZED
5. type == embedding → CAPABILITY_OPTIMIZED
6. priority ∈ {CRITICAL, HIGH} → LATENCY_OPTIMIZED
7. priority ∈ {LOW, BACKGROUND} → COST_OPTIMIZED
8. max_cost_per_request ≤ 0 → COST_OPTIMIZED
9. max_latency_ms ≤ 1000 → LATENCY_OPTIMIZED
10. 估计上下文 > 16000 → CAPABILITY_OPTIMIZED
11. 默认 → LOCAL_FIRST
```

#### 策略选择算法（`_select_strategy`）

根据分类结果和当前端点可用性细化策略：

- **PRIVACY_FIRST**: 直接返回，强制本地
- **CAPABILITY_OPTIMIZED**: 
  - TOOL_CALL 且有工具支持云端 → CAPABILITY_OPTIMIZED
  - 本地可处理 → LOCAL_FIRST
  - 否则 → CLOUD_FIRST
- **LATENCY_OPTIMIZED**:
  - 本地延迟 ≤ 阈值(3000ms) → LATENCY_OPTIMIZED
  - 云端延迟 ≤ 阈值 → LATENCY_OPTIMIZED
  - 有本地 → LOCAL_FIRST
  - 否则 → CLOUD_FIRST
- **COST_OPTIMIZED**:
  - 有本地 → COST_OPTIMIZED
  - 有低成本云端(≤$0.01/1k) → COST_OPTIMIZED
  - 否则 → CLOUD_FIRST
- **LOCAL_FIRST**:
  - 本地可用且连续失败 < 3 → LOCAL_FIRST
  - 本地连续失败 ≥ 3 → 自动切换 CLOUD_FIRST

#### Agent 类型确定（`_determine_agent_type`）

根据策略和端点可用性确定使用 local 还是 cloud Agent：

| 策略 | 优先 Agent | 降级 Agent |
|------|-----------|-----------|
| PRIVACY_FIRST | local | cloud (降级警告) |
| LOCAL_FIRST | local | cloud |
| CLOUD_FIRST | cloud | local |
| LATENCY_OPTIMIZED | 最低延迟方 | 另一方 |
| COST_OPTIMIZED | local | cloud |
| CAPABILITY_OPTIMIZED | 视情况 | 视情况 |

#### 自适应权重更新（`_update_adaptive_weights`）

采用指数移动平均（EMA）方式动态调整 local/cloud 权重：

- **本地成功且延迟 ≤ 阈值**: local += 0.1, cloud -= 0.1
- **本地失败**: local -= 0.2, cloud += 0.2
- **云端成功**: cloud += 0.05, local -= 0.05
- **云端失败**: cloud -= 0.1, local += 0.1
- 权重范围: [0.1, 0.9]（local）/ [0.1, 0.9]（cloud）
- 初始权重: local=0.6, cloud=0.4

#### 上下文长度估算（`_estimate_context_length`）

```
estimated = len(prompt) × 2
for msg in context:
    estimated += len(msg.content) × 2
if min_context_length:
    estimated = max(estimated, min_context_length)
```

乘以2是粗略的字符→Token估算系数。

---

### 3.8 负载均衡策略

**文件**: `scheduler/strategy/load_balance.py`

#### 5 种负载均衡算法

| 算法 | 常量 | 说明 |
|------|------|------|
| 轮询 | ROUND_ROBIN | 按顺序循环选择 |
| 加权随机 | WEIGHTED_RANDOM | 按 weight 概率选择 |
| 最少连接 | LEAST_CONNECTIONS | 选 current_load 最小的 |
| 最低延迟 | LEAST_LATENCY | 选 avg_latency_ms 最小的 |
| Power-of-Two | POWER_OF_TWO | 随机选2个，取负载低的 |

**Power-of-Two 算法**（推荐用于本地端点）：
1. 从候选列表中随机抽取 2 个端点
2. 比较 `current_load`，选择负载较低的那个
3. 优点：在保持负载均衡的同时减少选择开销

**`distribute(request, candidates, algorithm, count)`**：
- 按 `(load_factor, avg_latency_ms)` 排序
- 返回前 count 个端点（用于 fallback 列表）

---

### 3.9 优先级评分策略

**文件**: `scheduler/strategy/priority.py`

为每个候选端点计算综合评分，评分公式：

```
基础分 = 100.0
- (endpoint.priority - 1) × 15.0          # 优先级惩罚
- endpoint.load_factor × 20.0             # 负载惩罚
+ endpoint.success_rate × 10.0            # 成功率奖励
± 延迟相关调整                              # 见下表
± 约束相关调整                              # 见下表
```

**延迟调整规则**：

| 条件 | 分值调整 |
|------|---------|
| 延迟 > max_latency_ms | -50.0 |
| 延迟 ≤ max_latency_ms | +(1 - latency/max_latency) × 15.0 |

**约束调整规则**：

| 条件 | 分值调整 |
|------|---------|
| require_local + 端点是本地 | +30.0 |
| require_local + 端点是云端 | -100.0 |
| CRITICAL + 本地 + 延迟 < 500ms | +20.0 |
| LOW/BACKGROUND + 零成本 | +15.0 |
| model_hint 匹配端点名称 | +25.0 |
| model_hint 匹配模型ID | +20.0 |
| preferred_providers 匹配 | +20.0 |
| excluded_providers 匹配 | -100.0 |

---

### 3.10 Hook 管道机制

**文件**: `scheduler/hooks/hook_manager.py`, `scheduler/hooks/pre_hook.py`, `scheduler/hooks/post_hook.py`

#### Hook 基类架构

```
BaseHook (ABC)
├── PreHook → execute(request) → DispatchRequest
└── PostHook → execute(request, result) → ModelResult
```

- 每个 Hook 有 `name`、`hook_type`、`priority`、`enabled` 属性
- 按 priority 升序执行（数值越小越先执行）
- `priority ≤ 50` 的 Hook 失败会中断管道（抛出异常）

#### HookManager 管道执行

**Pre-Hook 管道**：
```
execute_pre_hooks(request):
    for hook in sorted(pre_hooks, key=priority):
        if hook.enabled:
            request = hook.execute(request)
            applied_hooks.append("pre:{hook.name}")
    return request
```

**Post-Hook 管道**：
```
execute_post_hooks(request, result):
    for hook in sorted(post_hooks, key=priority):
        if hook.enabled:
            result = hook.execute(request, result)
            applied_hooks.append("post:{hook.name}")
    return result
```

#### Pre-Hook 详细设计

| Hook | 优先级 | 功能 | 关键逻辑 |
|------|--------|------|---------|
| RateLimitHook | 5 | 限流 | 全局60 RPM / 单appid 30 RPM，滑动窗口 |
| RequestValidationHook | 10 | 请求校验 | appid/prompt 非空，timeout ≥ 1000ms，max_latency ≥ 100ms |
| ConstraintEnrichmentHook | 20 | 约束增强 | 按 appid 填充默认约束，NORMAL及以下默认 max_cost=0.05 |
| RequestLoggingHook | 90 | 请求日志 | 记录 appid/type/priority/prompt_len |

**RateLimitHook 限流算法**：
- 滑动窗口：保留最近60秒的请求时间戳
- 全局限制：`max_requests_per_minute`（默认60）
- 单应用限制：`max_requests_per_appid`（默认30）
- 超限 → 抛出 RuntimeError

**ConstraintEnrichmentHook 增强逻辑**：
- 支持按 appid 设置默认约束（`set_app_defaults`）
- 为 NORMAL 及以下优先级自动设置 `max_cost_per_request = 0.05`

#### Post-Hook 详细设计

| Hook | 优先级 | 功能 | 关键逻辑 |
|------|--------|------|---------|
| ResponseSanitizationHook | 10 | 响应脱敏 | 正则替换 api_key/password/token/secret |
| CostCalculationHook | 20 | 成本计算 | 若 result.cost 为空，按默认费率估算 |
| RetryDecisionHook | 30 | 重试决策 | finish_reason=error 或 output 为空时标记重试 |
| ResponseLoggingHook | 90 | 响应日志 | 记录 model/provider/latency/cost/output_len |

**ResponseSanitizationHook 脱敏规则**：
```python
patterns = [
    r'(api[_-]?key\s*[:=]\s*)["\']?[\w\-]{20,}["\']?',
    r'(password\s*[:=]\s*)["\']?[\w\-]{8,}["\']?',
    r'(token\s*[:=]\s*)["\']?[\w\-]{20,}["\']?',
    r'(secret\s*[:=]\s*)["\']?[\w\-]{20,}["\']?',
]
# 替换为: \1[REDACTED]
```

**CostCalculationHook 默认费率**：
- input: $0.001/1k tokens
- output: $0.002/1k tokens
- 仅在 `result.cost is None` 且 `result.usage` 存在时计算

**RetryDecisionHook 重试逻辑**：
- 最大重试次数: `max_retries`（默认2）
- 触发条件: `finish_reason == "error"` 或 `output is None/empty`
- 按 `request_id` 跟踪重试计数
- 超过最大重试次数后清除计数

---

### 3.11 OpenClaw Bridge (Node.js)

**文件**: `bridge/orchestrator.mjs`

Node.js 实现的 Bridge 服务，作为 Python 调度层与 OpenClaw Gateway 之间的中间层。

#### 环境变量配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| OPENCLAW_GATEWAY_URL | http://localhost:3000 | Gateway 地址 |
| BRIDGE_PORT | 3001 | Bridge 监听端口 |
| USE_OPENCLAW_GATEWAY | true | 是否通过 Gateway 路由 |

#### 模型目录（MODEL_CATALOG）

与 Python 端 `ModelRegistry` 镜像的模型目录：

| ID | Provider | 模型ID | 类型 | 工具支持 | 上下文长度 |
|----|----------|--------|------|---------|-----------|
| ollama/qwen2.5:3b | ollama | qwen2.5:3b | local | 否 | 32768 |
| moonshot/kimi-k2.6 | moonshot | kimi-k2.6 | cloud | 是 | 262144 |
| deepseek/deepseek-chat | deepseek | deepseek-chat | cloud | 否 | 64000 |

#### 端点状态追踪（endpointState）

每个端点维护运行时状态：

```javascript
{
  currentLoad: 0,        // 当前负载
  maxConcurrent: 5/20,   // 最大并发
  avgLatencyMs: 0,       // 平均延迟
  successRate: 1.0,      // 成功率
  totalRequests: 0,      // 总请求数
  failedRequests: 0      // 失败请求数
}
```

延迟和成功率更新采用 EMA：
- 延迟: `avgLatencyMs = 0.7 × old + 0.3 × new`
- 成功率: `successRate = 0.95 × old + 0.05 × (1/0)`

#### Gateway 路由映射（ENDPOINT_TO_AGENT）

| 端点 ID | Gateway Agent ID |
|---------|-----------------|
| ollama/qwen2.5:3b | openclaw/local-dispatcher |
| moonshot/kimi-k2.6 | openclaw/cloud-dispatcher |
| deepseek/deepseek-chat | openclaw/code-executor |

`resolveGatewayModel(endpoint)`: 将端点 ID 映射为 OpenClaw Agent ID，未匹配时返回 `"openclaw"`。

#### 自适应路由算法（adaptiveRoute）

与 Python 端 `AdaptiveStrategy` 对应的路由逻辑：

```
1. require_local → adaptive_local_required
2. preferred_providers 匹配 → adaptive_preferred_provider
3. priority ≤ 2 且 localFirst → adaptive_priority_local_first
4. 复杂类型 + preferCloudForComplex → adaptive_complex_cloud_first
5. localFirst + 本地可用 → adaptive_local_first
6. 仅云端可用 → adaptive_cloud_only
7. 无可用端点 → adaptive_no_endpoint
```

#### 模型选择算法（selectBestModel）

评分公式：
```javascript
score = 100.0
score -= (priority - 1) × 15.0    // 优先级惩罚
score -= loadFactor × 20.0        // 负载惩罚
score += successRate × 10.0       // 成功率奖励
if (avgLatencyMs > max_latency_ms):
    score -= 50.0                  // 延迟超标惩罚
```

#### API 调用（callModelApi）

**Gateway 模式**（`USE_OPENCLAW_GATEWAY=true`）：
- URL: `{OPENCLAW_GATEWAY_URL}/v1/chat/completions`
- model: `resolveGatewayModel(endpoint)` → 如 `"openclaw/local-dispatcher"`
- 超时: 120秒

**直连模式**（`USE_OPENCLAW_GATEWAY=false`）：
- URL: `{endpoint.baseUrl}/chat/completions`
- model: `endpoint.modelId` → 如 `"qwen2.5:3b"`
- 超时: 30秒

请求格式：
```json
{
  "model": "<agent_id 或 model_id>",
  "messages": [<context...>, {"role": "user", "content": "<prompt>"}],
  "max_tokens": 1024,
  "...<request.parameters>"
}
```

#### Bridge API 端点

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/dispatch` | 调度请求（自适应路由 + 模型调用） |
| POST | `/agent/message` | Agent 消息（按 openclaw.json 配置路由） |
| GET | `/models` | 模型列表（含状态） |
| GET | `/routing/config` | 路由配置 |
| GET | `/agents` | Agent 列表 |
| GET | `/health` | 健康检查 |
| GET | `/stats` | 统计信息 |

#### Agent 消息处理（handleOpenClawAgentMessage）

1. 根据 `agent_id` 查找 `openclaw.json` 中的 Agent 配置
2. 获取 Agent 的 `model.primary`
3. 在 MODEL_CATALOG 中查找对应端点
4. 调用 `callModelApi` 执行
5. 若失败且有 `model.fallbacks`，依次尝试降级模型

#### Fallback 机制

```
dispatchViaOpenClaw:
  1. adaptiveRoute → selected_endpoint + fallback_endpoints
  2. callModelApi(selected_endpoint)
  3. 成功 → 返回结果
  4. 失败 → 遍历 fallback_endpoints（最多2个）
  5. 任一降级成功 → 返回降级结果
  6. 全部失败 → ALL_ENDPOINTS_FAILED
```

---

### 3.12 前端 Dashboard

**文件**: `static/dashboard.html`

单页面 HTML 应用，提供：

- **路由模式选择器**：Python 直连 / Bridge→Gateway
- **Agent 选择器**：local-dispatcher / cloud-dispatcher / code-executor
- **双路径对比测试**：同时发送两种路径请求，对比延迟和结果
- **调度历史展示**：实时显示最近调度记录
- **模型状态监控**：各端点负载、延迟、成功率
- **系统资源监控**：CPU、内存使用率

---

### 3.13 配置管理

#### 3.13.1 Settings（Python 端）

**文件**: `scheduler/config/settings.py`

单例模式，从环境变量读取配置：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| SCHEDULER_APP_NAME | "OpenClaw Model Scheduler" | 应用名称 |
| SCHEDULER_HOST | "0.0.0.0" | 监听地址 |
| SCHEDULER_PORT | 8000 | 监听端口 |
| SCHEDULER_LOG_LEVEL | INFO | 日志级别 |
| OPENCLAW_GATEWAY_URL | http://localhost:3000 | Gateway URL |
| OPENCLAW_API_KEY | None | Gateway API Key |
| RATE_LIMIT_RPM | 60 | 全局限流(RPM) |
| RATE_LIMIT_PER_APPID | 30 | 单应用限流(RPM) |
| MAX_RETRIES | 2 | 最大重试次数 |
| DEFAULT_TIMEOUT_MS | 30000 | 默认超时(ms) |

#### 3.13.2 openclaw.json（项目配置）

**文件**: `openclaw.json`

顶层结构：

```json
{
  "agent": { ... },
  "agents": { ... },
  "models": { ... },
  "gateway": { ... },
  "channels": { ... },
  "heartbeat": { ... }
}
```

**Agent 配置**：

| Agent ID | 主模型 | 降级模型 | 工具 |
|----------|--------|---------|------|
| main | ollama/qwen2.5:3b | kimi-k2.6, deepseek-chat | bash, read, write, edit, glob, grep |
| local-dispatcher | ollama/qwen2.5:3b | - | bash, read, write, edit |
| cloud-dispatcher | moonshot/kimi-k2.6 | deepseek-chat | bash, read, write, edit, browser |
| code-executor | deepseek/deepseek-chat | moonshot/kimi-k2.6 | bash, read, write, edit |

**路由策略配置**：

```json
{
  "defaultStrategy": "adaptive",
  "strategies": {
    "adaptive": {
      "localFirst": true,
      "latencyThresholdMs": 3000,
      "costThreshold": 0.01,
      "fallbackOnTimeout": true,
      "fallbackOnError": true,
      "preferLocalForPrivacy": true,
      "preferCloudForComplex": true,
      "complexityIndicators": ["tool_call", "code_execution", "multi_step"]
    }
  }
}
```

---

## 4. 调用关系与数据流

### 4.1 完整调度流程时序图

```
Client          FastAPI         MainDispatcher     StrategyRouter    Agent          Bridge        Gateway       ModelAPI
  │                │                │                  │               │              │              │             │
  │ POST /dispatch │                │                  │               │              │              │             │
  │───────────────>│                │                  │               │              │              │             │
  │                │ dispatch(req)  │                  │               │              │              │             │
  │                │───────────────>│                  │               │              │              │             │
  │                │                │ Pre-Hook Pipeline│               │              │              │             │
  │                │                │ (限流→校验→增强→日志)             │              │              │             │
  │                │                │                  │               │              │              │             │
  │                │                │ [use_openclaw?]  │               │              │              │             │
  │                │                │─── Yes ──────────│               │              │              │             │
  │                │                │                  │               │              │              │             │
  │                │                │ route(request)   │               │              │              │             │
  │                │                │─────────────────>│               │              │              │             │
  │                │                │                  │               │              │              │             │
  │                │                │  RoutingDecision │               │              │              │             │
  │                │                │<─────────────────│               │              │              │             │
  │                │                │                  │               │              │              │             │
  │                │                │ execute(req, ep) │               │              │              │             │
  │                │                │─────────────────────────────────>│              │              │             │
  │                │                │                  │               │              │              │             │
  │                │                │                  │  [路径选择]    │              │              │             │
  │                │                │                  │               │              │              │             │
  │                │                │                  │  ┌── Python直连 ──────────────────────────────────────>│
  │                │                │                  │  │            │              │              │   POST      │
  │                │                │                  │  │            │              │              │  /chat/     │
  │                │                │                  │  │            │              │              │ completions │
  │                │                │                  │  │            │              │              │             │
  │                │                │                  │  └── Bridge→Gateway ──────────────────────>│             │
  │                │                │                  │               │ POST /dispatch│             │             │
  │                │                │                  │               │─────────────>│             │             │
  │                │                │                  │               │              │ POST /v1/    │             │
  │                │                │                  │               │              │ chat/comp.   │             │
  │                │                │                  │               │              │────────────>│             │
  │                │                │                  │               │              │             │────────────>│
  │                │                │                  │               │              │             │             │
  │                │                │  ModelResult     │               │              │             │             │
  │                │                │<──────────────────────────────────│              │             │             │
  │                │                │                  │               │              │             │             │
  │                │                │ Post-Hook Pipeline│              │              │             │             │
  │                │                │ (脱敏→计费→重试→日志)            │              │             │             │
  │                │                │                  │               │              │             │             │
  │                │ DispatchResponse│                 │               │              │             │             │
  │                │<───────────────│                  │               │              │             │             │
  │ DispatchResponse│               │                  │               │              │             │             │
  │<───────────────│                │                  │               │              │             │             │
```

### 4.2 Fallback 触发流程

```
主请求执行
│
├── 成功 → fallback_results = None → 返回 SUCCESS
│
├── TimeoutError
│   ├── 记录失败到 adaptive
│   ├── 有 fallback_endpoints?
│   │   ├── Yes → _try_fallbacks (最多2个)
│   │   │   ├── 任一成功 → 返回 PARTIAL
│   │   │   └── 全部失败 → 返回 TIMEOUT
│   │   └── No → 返回 TIMEOUT
│
└── Exception
    ├── 记录失败到 adaptive
    ├── 有 fallback_endpoints?
    │   ├── Yes → _try_fallbacks (最多2个)
    │   │   ├── 任一成功 → 返回 PARTIAL
    │   │   └── 全部失败 → 返回 FAILED
    │   └── No → 返回 FAILED
```

### 4.3 OpenClaw 降级链路

```
MainDispatcher.dispatch()
│
├── use_openclaw = True
│   └── _dispatch_openclaw()
│       ├── OpenClawAgent.execute(request, endpoint)
│       │   ├── call_model_api(endpoint, request) → 直连厂商
│       │   └── 成功 → 返回 DispatchResponse(SUCCESS)
│       └── 异常 → 返回 None
│           └── 降级到直连路径
│               ├── StrategyRouter.route() → RoutingDecision
│               ├── Agent.execute(request, endpoint)
│               └── 返回结果
│
└── use_openclaw = False
    └── 直接走直连路径
```

### 4.4 Bridge 内部调用链

```
Bridge /dispatch
│
├── adaptiveRoute(request) → routing decision
│   ├── selected_endpoint
│   └── fallback_endpoints
│
├── callModelApi(selected_endpoint, request)
│   ├── USE_OPENCLAW_GATEWAY = true
│   │   ├── URL: {GATEWAY_URL}/v1/chat/completions
│   │   ├── model: resolveGatewayModel(endpoint)
│   │   └── timeout: 120s
│   └── USE_OPENCLAW_GATEWAY = false
│       ├── URL: {endpoint.baseUrl}/chat/completions
│       ├── model: endpoint.modelId
│       └── timeout: 30s
│
├── 成功 → 返回 result
│
└── 失败 → Fallback
    ├── callModelApi(fallback_endpoints[0])
    ├── callModelApi(fallback_endpoints[1])
    └── 全部失败 → ALL_ENDPOINTS_FAILED
```

---

## 5. 上下游接口定义

### 5.1 上游接口（客户端 → 调度服务）

#### POST /dispatch

**请求体**：
```json
{
  "appid": "my-app",
  "type": "chat",
  "prompt": "你好，请介绍一下量子计算",
  "priority": 3,
  "model_hint": "qwen",
  "constraints": {
    "max_latency_ms": 5000,
    "require_local": false,
    "preferred_providers": ["ollama"],
    "max_cost_per_request": 0.01
  },
  "parameters": {
    "temperature": 0.7,
    "top_p": 0.9
  },
  "context": [
    {"role": "system", "content": "你是一个AI助手"},
    {"role": "user", "content": "之前的问题"}
  ],
  "request_id": "req-001",
  "timeout_ms": 30000
}
```

**响应体（成功）**：
```json
{
  "request_id": "req-001",
  "appid": "my-app",
  "status": "success",
  "result": {
    "model_name": "ollama-qwen2.5",
    "model_type": "local",
    "provider": "ollama",
    "output": "量子计算是利用量子力学原理...",
    "usage": {
      "prompt_tokens": 25,
      "completion_tokens": 150,
      "total_tokens": 175
    },
    "latency_ms": 453,
    "cost": 0.0,
    "finish_reason": "stop",
    "routed_via_gateway": false,
    "actual_model": null
  },
  "fallback_results": null,
  "error": null,
  "agent_trace": [
    {"agent": "pre_hooks", "message": "Executed pre-processing hooks", "timestamp": 1748352000.0},
    {"agent": "openclaw_router", "message": "OpenClaw routing: agent=local, endpoint=name='ollama-qwen2.5', strategy=adaptive_local_first", "timestamp": 1748352000.1},
    {"agent": "openclaw_agent", "message": "OpenClaw result from ollama-qwen2.5, latency=453ms", "timestamp": 1748352000.5},
    {"agent": "post_hooks", "message": "Executed post-processing hooks", "timestamp": 1748352000.5}
  ],
  "hooks_applied": [
    "pre:rate_limit", "pre:request_validation", "pre:constraint_enrichment", "pre:request_logging",
    "post:response_sanitization", "post:cost_calculation", "post:retry_decision", "post:response_logging"
  ],
  "timestamp": "2026-05-27T08:00:00.000000",
  "total_latency_ms": 520
}
```

**响应体（降级成功）**：
```json
{
  "request_id": "req-002",
  "appid": "my-app",
  "status": "partial",
  "result": {
    "model_name": "deepseek-chat",
    "model_type": "cloud",
    "provider": "deepseek",
    "output": "...",
    "latency_ms": 1200,
    "cost": 0.0001,
    "routed_via_gateway": false,
    "actual_model": null
  },
  "fallback_results": null,
  "agent_trace": ["..."],
  "total_latency_ms": 35000
}
```

**响应体（失败）**：
```json
{
  "request_id": "req-003",
  "appid": "my-app",
  "status": "failed",
  "result": null,
  "error": {
    "code": "INTERNAL_ERROR",
    "message": "Model kimi-k2.6 timed out after 30003ms",
    "agent": null,
    "retryable": true
  },
  "agent_trace": ["..."],
  "total_latency_ms": 30050
}
```

#### POST /dispatch/batch

**请求体**：`List[DispatchRequest]`

**响应体**：`List[DispatchResponse]`

#### POST /openclaw/agent/{agent_id}/message

**查询参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| prompt | str | 是 | 提示文本 |
| context | List[Dict] | 否 | 上下文 |
| parameters | Dict | 否 | 参数 |
| timeout_ms | int | 否 | 超时(默认30000) |

**响应体**：
```json
{
  "status": "success",
  "agent_id": "local-dispatcher",
  "result": {
    "model_name": "ollama/qwen2.5:3b",
    "output": "...",
    "latency_ms": 500,
    "routed_via_gateway": true,
    "actual_model": "qwen2.5:3b"
  },
  "used_model": "ollama/qwen2.5:3b",
  "fallback": false
}
```

### 5.2 内部接口（调度服务 → Bridge）

#### POST {bridge_url}/dispatch

**请求体**：
```json
{
  "appid": "my-app",
  "type": "chat",
  "prompt": "你好",
  "priority": 3,
  "model_hint": "qwen",
  "constraints": {
    "max_latency_ms": 5000,
    "require_local": true
  },
  "request_id": "req-001",
  "timeout_ms": 30000
}
```

**响应体**：
```json
{
  "request_id": "req-001",
  "appid": "my-app",
  "status": "success",
  "result": {
    "model_name": "ollama/qwen2.5:3b",
    "model_type": "local",
    "provider": "ollama",
    "output": "...",
    "usage": {"prompt_tokens": 10, "completion_tokens": 50},
    "latency_ms": 300,
    "cost": 0,
    "finish_reason": "stop",
    "routed_via_gateway": true,
    "actual_model": "qwen2.5:3b"
  },
  "fallback_results": null,
  "agent_trace": ["..."],
  "strategy_name": "adaptive_local_first",
  "routing_decision": {
    "agent_type": "local",
    "selected_endpoint": {"..."},
    "fallback_endpoints": ["..."],
    "strategy_name": "adaptive_local_first",
    "reason": "Default: local model selected (low latency, zero cost)"
  }
}
```

#### POST {bridge_url}/agent/message

**请求体**：
```json
{
  "agent_id": "local-dispatcher",
  "prompt": "执行bash命令: ls -la",
  "context": ["..."],
  "parameters": {"..."},
  "timeout_ms": 30000
}
```

#### GET {bridge_url}/health

**响应体**：
```json
{
  "status": "healthy",
  "openclaw_version": "2026.4.27",
  "bridge_port": 3001,
  "gateway_url": "http://localhost:3000",
  "gateway_routing": true,
  "local_models": 1,
  "cloud_models": 2,
  "total_endpoints": 3,
  "strategy": "adaptive"
}
```

### 5.3 下游接口（Bridge → Gateway / 厂商API）

#### Gateway 模式: POST {GATEWAY_URL}/v1/chat/completions

**请求体**：
```json
{
  "model": "openclaw/local-dispatcher",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "你好"}
  ],
  "max_tokens": 1024
}
```

**响应体**（OpenAI 兼容格式）：
```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "model": "qwen2.5:3b",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "你好！有什么可以帮助你的？"
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 15,
    "total_tokens": 25
  }
}
```

#### 直连模式: POST {endpoint.baseUrl}/chat/completions

请求/响应格式同上，但 `model` 字段使用原始模型ID（如 `qwen2.5:3b`）。

### 5.4 接口汇总图

```
┌─────────────┐     POST /dispatch          ┌──────────────────┐
│   Client     │────────────────────────────>│  FastAPI :8000   │
│  (Browser/   │     POST /dispatch/batch    │                  │
│   API)       │────────────────────────────>│  /models         │
│              │     GET /models             │  /hooks          │
│              │────────────────────────────>│  /stats          │
│              │     GET /stats              │  /health         │
│              │────────────────────────────>│  /history        │
│              │     GET /history            │  /openclaw/*     │
│              │────────────────────────────>│                  │
└─────────────┘                              └────────┬─────────┘
                                                      │
                                    POST /dispatch    │  GET /health
                                                      ▼
                                             ┌──────────────────┐
                                             │  Bridge :3001    │
                                             │                  │
                                             │  /dispatch       │
                                             │  /agent/message  │
                                             │  /models         │
                                             │  /health         │
                                             │  /stats          │
                                             └────────┬─────────┘
                                                      │
                              [Gateway模式]            │  [直连模式]
                              POST /v1/chat/completions│  POST /chat/completions
                                                      ▼
                                             ┌──────────────────┐
                              ┌──────────────│ Gateway :3000    │──────────────┐
                              │              └──────────────────┘              │
                              │                                                │
                              ▼                                                ▼
                     ┌──────────────────┐                          ┌──────────────────┐
                     │  Ollama :11434   │                          │  Moonshot API    │
                     │  /v1/chat/comp.  │                          │  /v1/chat/comp.  │
                     └──────────────────┘                          └──────────────────┘
                                                                      │
                                                              ┌──────────────────┐
                                                              │  DeepSeek API    │
                                                              │  /v1/chat/comp.  │
                                                              └──────────────────┘
```

---

## 6. 核心算法与实现细节

### 6.1 自适应权重动态调整

系统维护 local/cloud 两组权重，根据每次请求结果动态调整：

```
初始状态: local_weight=0.6, cloud_weight=0.4

本地成功(延迟≤阈值):  local += 0.1, cloud -= 0.1
本地失败:             local -= 0.2, cloud += 0.2
云端成功:             cloud += 0.05, local -= 0.05
云端失败:             cloud -= 0.1, local += 0.1

权重边界: [0.1, 0.9]
```

连续失败自动切换：当本地连续失败 ≥ 3 次时，自动从 LOCAL_FIRST 切换到 CLOUD_FIRST。

### 6.2 EMA 延迟与成功率更新

所有端点的延迟和成功率采用指数移动平均（EMA）更新：

**延迟更新**：
```
avg_latency = 0.7 × old_latency + 0.3 × new_latency
```

**成功率更新**：
```
success_rate = 0.95 × old_rate + 0.05 × (1 if success else 0)
```

EMA 的优势：
- 无需存储历史数据，内存开销 O(1)
- 对近期数据更敏感，能快速反映端点状态变化
- α 值（延迟0.7/成功率0.95）可根据业务需求调整

### 6.3 Power-of-Two 负载均衡

```
1. 从 N 个候选端点中随机选取 2 个
2. 比较两者的 current_load
3. 选择负载较低的那个

时间复杂度: O(1)
空间复杂度: O(1)
负载均衡效果: 接近最优（理论偏差 ≤ 1/3）
```

### 6.4 优先级评分算法

综合评分 = 基础分(100) + 优先级调整 + 负载调整 + 成功率调整 + 延迟调整 + 约束调整

评分示例（ollama-qwen2.5，priority=1，无负载，success_rate=1.0