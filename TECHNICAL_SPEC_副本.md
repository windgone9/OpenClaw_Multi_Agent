# OpenClaw Multi-Agent 调度系统 — 技术说明文档

> 版本: 2026.4.27 | 最后更新: 2026-05-26

---

## 目录

1. [系统概述](#1-系统概述)
2. [系统架构](#2-系统架构)
3. [项目结构](#3-项目结构)
4. [核心模块详解](#4-核心模块详解)
   - 4.1 [调度引擎 (Scheduler)](#41-调度引擎-scheduler)
   - 4.2 [OpenClaw Bridge (Node.js)](#42-openclaw-bridge-nodejs)
   - 4.3 [前端监控面板 (Dashboard)](#43-前端监控面板-dashboard)
5. [数据模型](#5-数据模型)
   - 5.1 [请求模型 (DispatchRequest)](#51-请求模型-dispatchrequest)
   - 5.2 [响应模型 (DispatchResponse)](#52-响应模型-dispatchresponse)
   - 5.3 [模型端点 (ModelEndpoint)](#53-模型端点-modelendpoint)
6. [调度策略体系](#6-调度策略体系)
   - 6.1 [自适应策略 (AdaptiveStrategy)](#61-自适应策略-adaptivestrategy)
   - 6.2 [优先级策略 (PriorityStrategy)](#62-优先级策略-prioritystrategy)
   - 6.3 [负载均衡策略 (LoadBalanceStrategy)](#63-负载均衡策略-loadbalancestrategy)
   - 6.4 [策略路由器 (StrategyRouter)](#64-策略路由器-strategyrouter)
7. [Hook 机制](#7-hook-机制)
   - 7.1 [Pre-Hook 管线](#71-pre-hook-管线)
   - 7.2 [Post-Hook 管线](#72-post-hook-管线)
8. [API 接口参考](#8-api-接口参考)
9. [调度流程全链路追踪](#9-调度流程全链路追踪)
10. [配置说明](#10-配置说明)
11. [可扩展性设计](#11-可扩展性设计)
12. [部署与运维](#12-部署与运维)

---

## 1. 系统概述

OpenClaw Multi-Agent 调度系统是一个基于 OpenClaw AI Agent 框架（v2026.4.27）构建的**智能模型调度与多 Agent 编排平台**。系统核心能力包括：

- **自适应调度**: 根据请求类型、优先级、约束条件、历史表现动态选择最优模型端点
- **多模型管理**: 统一管理本地模型（Ollama）和云端模型（Kimi、DeepSeek），透明切换
- **Fallback 容错**: 主端点失败时自动降级到备用端点，保障请求成功率
- **Hook 管线**: 可插拔的前/后处理管线，支持请求校验、限流、脱敏、计费等横切关注点
- **实时监控**: 前端 Dashboard 直观展示资源状态、调度决策、运行日志和追踪链路
- **Bridge 桥接**: Node.js 实现的 OpenClaw Bridge，桥接 Python 调度器与 OpenClaw Gateway

### 技术栈

| 层级 | 技术 |
|------|------|
| 调度服务 | Python 3.9 + FastAPI + Pydantic v2 |
| HTTP 客户端 | httpx (async) |
| Bridge 桥接 | Node.js (原生 http 模块) |
| 前端面板 | 原生 HTML/CSS/JavaScript |
| 本地模型 | Ollama (Qwen2.5 3B) |
| 云端模型 | Moonshot Kimi K2.6, DeepSeek Chat |
| 进程监控 | psutil |

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        用户 / 前端 Dashboard                        │
│                    http://localhost:8000/                            │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ HTTP REST API
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     FastAPI 调度服务 (:8000)                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │  Pre-Hooks   │→│ StrategyRouter│→│ MainDispatcherAgent      │  │
│  │  · 校验      │  │  · Adaptive  │  │  · LocalModelAgent      │  │
│  │  · 限流      │  │  · Priority  │  │  · CloudModelAgent      │  │
│  │  · 约束增强  │  │  · LoadBalance│  │  · OpenClawAgent ───────┼──┼──┐
│  │  · 日志      │  │              │  │                          │  │  │
│  └──────────────┘  └──────────────┘  └──────────────────────────┘  │  │
│           ▲                                          │             │  │
│  ┌────────┴──────────────────────────────────────────┘             │  │
│  │  Post-Hooks                                                      │  │
│  │  · 脱敏 · 计费 · 重试决策 · 日志                                  │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  ┌──────────────────┐  ┌──────────────────────────────────────────┐   │
│  │  ModelRegistry   │  │  统计 / 健康 / 历史                       │   │
│  │  · 端点注册      │  │  · /stats · /health · /history           │   │
│  │  · 可用性检测    │  │  · /models · /hooks                      │   │
│  └──────────────────┘  └──────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────┘
                               │                            │
              ┌────────────────┘                            │
              ▼                                             ▼
┌──────────────────────────┐              ┌──────────────────────────────┐
│   本地模型 (Ollama)       │              │  OpenClaw Bridge (:3001)     │
│   http://localhost:11434  │              │  Node.js HTTP Server         │
│   · qwen2.5:3b           │              │  · 自适应路由 (JS 实现)       │
│   · 零成本 · 低延迟       │              │  · 模型目录 + 端点状态        │
└──────────────────────────┘              │  · Agent 消息转发            │
                                          │  · Fallback 容错             │
┌──────────────────────────┐              └──────────────┬───────────────┘
│   云端模型                │                             │
│   · Kimi K2.6 (Moonshot) │◄────────────────────────────┘
│   · DeepSeek Chat        │        直接调用 / 通过 Bridge
│   · 高能力 · 工具调用     │
└──────────────────────────┘
```

### 双层调度架构

系统采用**双层调度**设计：

1. **Python 调度层** (FastAPI): 负责请求接收、Hook 管线、策略路由、Agent 执行、统计收集
2. **Node.js Bridge 层**: 作为 OpenClaw 框架的桥接器，实现 JS 侧的自适应路由和模型调用

两层通过 HTTP 通信，Python 侧的 `OpenClawAgent` 优先尝试 Bridge 路由，失败后降级为 Python 侧直接调度。

---

## 3. 项目结构

```
OpenClaw_Multi_Agent/
├── scheduler/                    # Python 调度服务
│   ├── main.py                   # FastAPI 入口 + API 端点定义
│   ├── agents/                   # Agent 执行层
│   │   ├── base_agent.py         # Agent 基类 (HTTP 调用、负载追踪)
│   │   ├── local_agent.py        # 本地模型 Agent
│   │   ├── cloud_agent.py        # 云端模型 Agent
│   │   ├── openclaw_agent.py     # OpenClaw Bridge Agent
│   │   └── main_agent.py         # 主调度 Agent (编排所有子 Agent)
│   ├── config/
│   │   └── settings.py           # 全局配置 (环境变量 + 单例)
│   ├── hooks/                    # Hook 管线
│   │   ├── hook_manager.py       # Hook 管理器 + 基类定义
│   │   ├── pre_hook.py           # Pre-Hook 实现 (4 个)
│   │   └── post_hook.py          # Post-Hook 实现 (4 个)
│   ├── models/                   # 数据模型
│   │   ├── request.py            # 请求模型 (DispatchRequest)
│   │   ├── response.py           # 响应模型 (DispatchResponse)
│   │   └── model_config.py       # 模型端点 + 注册表
│   └── strategy/                 # 调度策略
│       ├── adaptive.py           # 自适应策略
│       ├── priority.py           # 优先级策略
│       ├── load_balance.py       # 负载均衡策略
│       └── router.py             # 策略路由器 (策略选择 + 路由决策)
├── bridge/
│   └── orchestrator.mjs          # OpenClaw Bridge (Node.js)
├── static/
│   └── dashboard.html            # 前端监控面板
├── openclaw.json                 # OpenClaw 配置文件
├── run.py                        # 启动入口
├── requirements.txt              # Python 依赖
└── SPECIFICATION.md              # 原始规格文档
```

---

## 4. 核心模块详解

### 4.1 调度引擎 (Scheduler)

#### 4.1.1 入口与生命周期 — `scheduler/main.py`

**职责**: FastAPI 应用定义、模型注册、Hook 初始化、Bridge 启停、API 端点

**生命周期管理**:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动阶段
    _init_default_models()          # 注册默认模型端点
    hook_manager = _init_hooks()    # 初始化 Hook 管线
    bridge_process = _start_bridge() # 启动 Bridge 子进程
    dispatcher = MainDispatcherAgent(registry, hook_manager, ...)
    yield
    # 关闭阶段
    await dispatcher.close()
    _stop_bridge(bridge_process)
```

**默认模型注册**:

| 端点名 | 类型 | Provider | 上下文长度 | 工具调用 | 输入成本/1k | 并发上限 |
|--------|------|----------|-----------|---------|------------|---------|
| ollama-qwen2.5 | local | Ollama | 32,768 | ❌ | $0 | 5 |
| kimi-k2.6 | cloud | Moonshot | 262,144 | ✅ | $0.76 | 15 |
| deepseek-chat | cloud | DeepSeek | 64,000 | ❌ | $0.00014 | 20 |

#### 4.1.2 主调度 Agent — `scheduler/agents/main_agent.py`

**职责**: 编排整个调度流程，是系统的核心调度器

**调度流程**:

```
dispatch(request)
  │
  ├── 1. execute_pre_hooks(request)          # 执行前置管线
  │
  ├── 2. if use_openclaw:
  │      └── _dispatch_openclaw()            # 尝试 Bridge 路由
  │           ├── strategy_router.route()     # 路由决策
  │           ├── openclaw_agent.execute()    # Bridge 调用
  │           └── 成功 → 返回 / 失败 → 降级
  │
  ├── 3. strategy_router.route(request)      # Python 侧路由决策
  │      → RoutingDecision { agent_type, endpoint, fallbacks, strategy }
  │
  ├── 4. agent.execute(request, endpoint)    # 执行模型调用
  │
  ├── 5. _try_fallbacks()                    # 尝试 Fallback 端点
  │
  ├── 6. execute_post_hooks(request, result) # 执行后置管线
  │
  └── 7. 构建 DispatchResponse 返回
```

**关键设计**:
- OpenClaw 优先: 当 `use_openclaw=True` 时，先尝试 Bridge 路由，失败后降级到 Python 直接调度
- Fallback 机制: 主端点失败时，按优先级尝试最多 2 个备用端点
- 全链路追踪: 每个步骤记录 `agent_trace`，包含 agent 名、消息、时间戳

#### 4.1.3 Agent 基类 — `scheduler/agents/base_agent.py`

**职责**: 定义 Agent 接口，实现通用的模型 API 调用逻辑

**核心方法**:

| 方法 | 说明 |
|------|------|
| `execute(request, endpoint)` | 抽象方法，子类实现 |
| `can_handle(request)` | 抽象方法，判断是否能处理 |
| `call_model_api(endpoint, request)` | 通用 OpenAI 兼容 API 调用 |
| `execute_with_fallback(request, endpoints, max_retries)` | 带 Fallback 的执行 |

**API 调用流程** (`call_model_api`):

1. `endpoint.increment_load()` — 增加端点并发计数
2. 构建 OpenAI 兼容 payload (`model`, `messages`, `max_tokens`)
3. 构建 Headers (含 `Authorization: Bearer`)
4. `httpx.AsyncClient.post()` 发起异步请求
5. 解析响应: 提取 `content`, `reasoning_content`, `usage`, `finish_reason`
6. 计算费用: `input_cost + output_cost`
7. `endpoint.update_latency()` / `endpoint.update_success_rate()` — 更新端点统计
8. `endpoint.decrement_load()` — 减少并发计数 (finally)

**响应解析特殊处理**:
- 优先取 `message.content`，若为空则取 `message.reasoning_content`（适配推理模型）

#### 4.1.4 本地模型 Agent — `scheduler/agents/local_agent.py`

**端点选择逻辑**:
1. 若有 `model_hint`，优先匹配名称
2. 若有 `preferred_providers` 约束，优先匹配 Provider
3. 按 `(load_factor, avg_latency_ms)` 排序选最优

#### 4.1.5 云端模型 Agent — `scheduler/agents/cloud_agent.py`

**端点选择逻辑**:
1. 若有 `model_hint`，优先匹配名称
2. 若有 `preferred_providers`，优先匹配
3. 若有 `excluded_providers`，排除指定 Provider
4. 若有 `max_cost_per_request`，过滤超出预算的端点
5. 按 `(priority, load_factor, avg_latency_ms)` 排序选最优

#### 4.1.6 OpenClaw Agent — `scheduler/agents/openclaw_agent.py`

**职责**: 桥接 Python 调度器与 Node.js Bridge

**执行策略**:
1. 若指定了 `endpoint`，直接调用该端点 API
2. 否则尝试通过 Bridge (`/dispatch`) 路由
3. Bridge 失败则降级为 `_fallback_direct()` — 从 Registry 中选最优端点直接调用

**Bridge 通信协议**:

```python
# 请求 payload
{
    "appid": "demo-app",
    "type": "chat",
    "prompt": "...",
    "priority": 3,
    "constraints": { "require_local": true, ... },
    "model_hint": "...",
    "parameters": { "temperature": 0.7, ... },
    "context": [ {"role": "user", "content": "..."} ],
    "request_id": "uuid",
    "timeout_ms": 30000
}
```

---

### 4.2 OpenClaw Bridge (Node.js)

**文件**: `bridge/orchestrator.mjs`  
**端口**: 3001 (默认)  
**职责**: 作为 OpenClaw 框架的桥接层，实现 Node.js 侧的自适应路由

#### 模型目录 (MODEL_CATALOG)

Bridge 内置了与 Python 侧一致的模型目录，包含 `local` 和 `cloud` 两个分组，每个模型包含:
- `id`, `provider`, `modelId`, `baseUrl`, `contextLength`
- `supportsStreaming`, `supportsTools`
- `costPer1kInput`, `costPer1kOutput`, `priority`, `tags`

#### 端点状态追踪

```javascript
endpointState[modelId] = {
    currentLoad: 0,       // 当前并发
    maxConcurrent: 5/20,  // 最大并发
    avgLatencyMs: 0,      // 平均延迟 (EMA)
    successRate: 1.0,     // 成功率 (EMA)
    totalRequests: 0,     // 总请求数
    failedRequests: 0     // 失败请求数
}
```

#### 自适应路由算法 (`adaptiveRoute`)

```
adaptiveRoute(request)
  │
  ├── constraints.require_local? → 本地优先 (adaptive_local_required)
  ├── constraints.preferred_providers? → 匹配偏好 (adaptive_preferred_provider)
  ├── priority ≤ 2 && localFirst? → 本地优先 (adaptive_priority_local_first)
  ├── 复杂类型 (tool_call/image/audio)? → 云端优先 (adaptive_complex_cloud_first)
  └── 默认: localFirst? → 本地优先 (adaptive_local_first)
      └── 无本地可用 → 云端 (adaptive_cloud_only)
```

#### API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/dispatch` | 自适应调度请求 |
| POST | `/agent/message` | 向指定 Agent 发送消息 |
| GET | `/models` | 获取模型目录 + 状态 |
| GET | `/routing/config` | 获取路由配置 |
| GET | `/agents` | 获取 Agent 列表 |
| GET | `/health` | 健康检查 |
| GET | `/stats` | Bridge 统计信息 |

---

### 4.3 前端监控面板 (Dashboard)

**文件**: `static/dashboard.html`  
**访问**: `http://localhost:8000/`

#### 三栏布局

```
┌──────────────┬────────────────────────┬──────────────────┐
│   左栏        │      中栏              │     右栏         │
│  资源监控      │   请求管理 + 结果       │   实时日志       │
│              │                        │                  │
│ · CPU/内存    │ · 请求表单             │ · 时间戳         │
│ · 请求统计    │   (类型/优先级/约束)    │ · 级别标签       │
│ · 模型状态    │ · 发送/批量测试按钮     │ · 调度消息       │
│   (并发/延迟   │ · 调度结果卡片         │                  │
│    成功率)    │   (策略/模型/输入输出)  │                  │
│ · 自适应权重  │ · 链路追踪时间线        │                  │
│ · Bridge状态  │                        │                  │
└──────────────┴────────────────────────┴──────────────────┘
```

#### 功能清单

| 功能 | 说明 | 数据源 |
|------|------|--------|
| 系统资源 | CPU、内存、请求统计 | `/stats` (3s 轮询) |
| 模型状态 | 并发、延迟、成功率、负载条 | `/models` (3s 轮询) |
| 自适应权重 | 本地/云端权重条形图 | `/openclaw/adaptive/state` |
| Bridge 状态 | 连通性、模型数、策略 | `/openclaw/bridge/health` |
| 单次请求 | 类型/优先级/约束/提示词 | `POST /dispatch` |
| 批量测试 | 5 种策略一键测试 | `POST /dispatch` × 5 |
| 调度卡片 | 策略标签、模型、延迟、费用 | DispatchResponse |
| 链路追踪 | Pre-Hook → Router → Agent → Fallback → Post-Hook | `agent_trace` |
| 实时日志 | 每条请求的调度过程 | 前端 JS 生成 |

---

## 5. 数据模型

### 5.1 请求模型 (DispatchRequest)

```python
class DispatchRequest(BaseModel):
    appid: str                              # 应用标识 (必填)
    type: RequestType                       # 请求类型 (必填)
    prompt: str                             # 提示词 (必填)
    priority: Priority = NORMAL             # 优先级 (默认 3)
    model_hint: Optional[str]               # 模型名称提示
    constraints: Optional[ModelConstraint]  # 模型选择约束
    parameters: Optional[Dict[str, Any]]    # 额外参数 (temperature 等)
    context: Optional[List[Dict[str, str]]] # 对话上下文
    metadata: Optional[Dict[str, Any]]      # 附加元数据
    request_id: Optional[str]               # 请求 ID (幂等)
    timeout_ms: Optional[int] = 30000       # 超时 (毫秒)
```

**RequestType 枚举**:

| 值 | 说明 |
|----|------|
| `chat` | 对话补全 |
| `completion` | 文本补全 |
| `embedding` | 向量嵌入 |
| `image` | 图像生成 |
| `audio` | 语音处理 |
| `tool_call` | 工具/函数调用 |

**Priority 枚举**:

| 值 | 名称 | 说明 |
|----|------|------|
| 1 | CRITICAL | 关键任务，延迟敏感 |
| 2 | HIGH | 高优先级 |
| 3 | NORMAL | 普通优先级 (默认) |
| 4 | LOW | 低优先级 |
| 5 | BACKGROUND | 后台任务，成本敏感 |

**ModelConstraint 模型**:

| 字段 | 类型 | 说明 |
|------|------|------|
| `max_latency_ms` | int | 最大可接受延迟 |
| `min_context_length` | int | 最小上下文窗口 |
| `require_streaming` | bool | 是否需要流式响应 |
| `require_local` | bool | 是否强制本地模型 (隐私) |
| `require_gpu` | bool | 是否需要 GPU 加速 |
| `preferred_providers` | List[str] | 偏好 Provider 列表 |
| `excluded_providers` | List[str] | 排除 Provider 列表 |
| `max_cost_per_request` | float | 单次请求最大费用 (USD) |

### 5.2 响应模型 (DispatchResponse)

```python
class DispatchResponse(BaseModel):
    request_id: str                         # 请求唯一标识
    appid: str                              # 应用标识
    status: DispatchStatus                  # 调度状态
    result: Optional[ModelResult]           # 主模型结果
    fallback_results: Optional[List[ModelResult]]  # Fallback 结果
    error: Optional[ErrorDetail]            # 错误详情
    agent_trace: Optional[List[Dict]]       # Agent 执行追踪
    hooks_applied: Optional[List[str]]      # 已执行的 Hook 列表
    timestamp: str                          # 响应时间戳
    total_latency_ms: Optional[int]         # 端到端延迟
```

**DispatchStatus 枚举**:

| 值 | 说明 |
|----|------|
| `success` | 调度成功 |
| `partial` | 部分成功 (Fallback) |
| `failed` | 调度失败 |
| `timeout` | 请求超时 |
| `rejected` | 请求被拒绝 (限流等) |

**ModelResult 模型**:

| 字段 | 类型 | 说明 |
|------|------|------|
| `model_name` | str | 处理请求的模型名称 |
| `model_type` | str | 模型类型 (local/cloud) |
| `provider` | str | 模型提供商 |
| `output` | Any | 模型输出内容 |
| `usage` | Dict | Token 使用统计 |
| `latency_ms` | int | 处理延迟 (毫秒) |
| `cost` | float | 预估费用 (USD) |
| `finish_reason` | str | 完成原因 |

**ErrorDetail 模型**:

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | str | 错误码 |
| `message` | str | 错误消息 |
| `agent` | str | 出错的 Agent |
| `retryable` | bool | 是否可重试 |

### 5.3 模型端点 (ModelEndpoint)

```python
class ModelEndpoint(BaseModel):
    name: str                          # 唯一端点名称
    display_name: Optional[str]        # 显示名称
    model_type: ModelType              # local / cloud / hybrid
    provider: ModelProvider            # ollama / openai / deepseek / moonshot / ...
    base_url: str                      # API 基础 URL
    api_key: Optional[str]             # API 密钥
    model_id: str                      # API 调用使用的模型 ID
    max_context_length: int = 4096     # 最大上下文长度
    supports_streaming: bool = True    # 是否支持流式
    supports_tools: bool = False       # 是否支持工具调用
    cost_per_1k_input_tokens: float    # 输入 Token 单价
    cost_per_1k_output_tokens: float   # 输出 Token 单价
    priority: int = 5                  # 优先级 (1=最高)
    weight: int = 1                    # 负载均衡权重
    max_concurrent: int = 10           # 最大并发数
    current_load: int = 0              # 当前并发数
    avg_latency_ms: int = 0            # 平均延迟 (EMA)
    success_rate: float = 1.0          # 成功率 (EMA)
    enabled: bool = True               # 是否启用
    tags: Optional[List[str]]          # 标签
    metadata: Optional[Dict]           # 附加元数据
```

**运行时属性**:

| 属性 | 计算 | 说明 |
|------|------|------|
| `is_available` | `enabled and current_load < max_concurrent` | 端点是否可用 |
| `load_factor` | `current_load / max_concurrent` | 负载因子 (0~1) |

**运行时方法**:

| 方法 | 说明 |
|------|------|
| `increment_load()` | 并发 +1 |
| `decrement_load()` | 并发 -1 |
| `update_latency(ms)` | EMA 更新延迟: `0.7 * old + 0.3 * new` |
| `update_success_rate(bool)` | EMA 更新成功率: `0.95 * old + 0.05 * (1 or 0)` |

**ModelRegistry**:

| 方法 | 说明 |
|------|------|
| `register(endpoint)` | 注册端点 |
| `unregister(name)` | 注销端点 |
| `get(name)` | 按名称获取 |
| `get_by_type(model_type)` | 按类型筛选 |
| `get_by_provider(provider)` | 按 Provider 筛选 |
| `get_available()` | 获取所有可用端点 |
| `get_local_available()` | 获取可用本地端点 |
| `get_cloud_available()` | 获取可用云端端点 |

---

## 6. 调度策略体系

### 6.1 自适应策略 (AdaptiveStrategy)

**文件**: `scheduler/strategy/adaptive.py`

**核心思想**: 根据请求特征自动选择最优调度策略，并基于历史执行结果动态调整本地/云端权重。

#### 请求分类决策树

```
_classify_request(request)
  │
  ├── constraints.require_local?     → PRIVACY_FIRST
  ├── constraints.require_gpu?       → CAPABILITY_OPTIMIZED
  ├── type ∈ {tool_call, image, audio}? → CAPABILITY_OPTIMIZED
  ├── type == EMBEDDING?             → CAPABILITY_OPTIMIZED
  ├── priority ∈ {CRITICAL, HIGH}?   → LATENCY_OPTIMIZED
  ├── priority ∈ {LOW, BACKGROUND}?  → COST_OPTIMIZED
  ├── max_cost_per_request ≤ 0?      → COST_OPTIMIZED
  ├── max_latency_ms ≤ 1000?         → LATENCY_OPTIMIZED
  ├── context_length > 16000?        → CAPABILITY_OPTIMIZED
  └── 默认                           → LOCAL_FIRST
```

#### 六种策略

| 策略 | 触发条件 | 路由偏好 |
|------|---------|---------|
| `LOCAL_FIRST` | 默认策略 | 优先本地，本地连续失败 ≥3 次切云端 |
| `CLOUD_FIRST` | 本地不可用 / 复杂任务 | 优先云端 |
| `COST_OPTIMIZED` | 低优先级 / 预算约束 | 优先免费端点 |
| `LATENCY_OPTIMIZED` | 高优先级 / 延迟约束 | 选最低延迟端点 |
| `CAPABILITY_OPTIMIZED` | 工具调用 / 长上下文 | 选能力最强端点 |
| `PRIVACY_FIRST` | require_local 约束 | 强制本地 |

#### 自适应权重更新

```python
alpha = 0.1

# 本地成功 + 低延迟 → local += alpha, cloud -= alpha
# 本地失败           → local -= 2*alpha, cloud += 2*alpha
# 云端成功           → cloud += 0.5*alpha, local -= 0.5*alpha
# 云端失败           → cloud -= alpha, local += alpha

# 权重范围: [0.1, 0.9] / [0.0, 1.0]
```

**设计要点**:
- 本地失败的惩罚 (2×alpha) 大于云端失败 (1×alpha)，因为本地失败通常意味着服务不可用
- 云端成功的增益 (0.5×alpha) 小于本地成功 (1×alpha)，防止云端权重过快增长
- 初始权重: `local=0.6, cloud=0.4`

#### Agent 类型确定

```
_determine_agent_type(strategy, request)
  │
  ├── PRIVACY_FIRST → local (无本地则降级 cloud)
  ├── LOCAL_FIRST   → local (无本地则降级 cloud)
  ├── CLOUD_FIRST   → cloud (无云端则降级 local)
  ├── LATENCY_OPTIMIZED → 比较本地/云端最低延迟
  ├── COST_OPTIMIZED    → local (免费)
  └── CAPABILITY_OPTIMIZED → tool_call → cloud; 长上下文 → cloud; 否则 local
```

### 6.2 优先级策略 (PriorityStrategy)

**文件**: `scheduler/strategy/priority.py`

**核心方法**: `select(request, candidates)` — 对候选端点评分排序，返回最优

**评分公式**:

```
score = 100.0
score -= (endpoint.priority - 1) × 15.0          # 优先级惩罚
score -= endpoint.load_factor × 20.0              # 负载惩罚
score += endpoint.success_rate × 10.0             # 成功率奖励

# 条件加分/扣分:
if 超出延迟约束: score -= 50.0
if 满足延迟约束: score += (1 - latency_ratio) × 15.0
if require_local + 是本地: score += 30.0
if require_local + 非本地: score -= 100.0
if CRITICAL + 本地低延迟: score += 20.0
if LOW/BACKGROUND + 零成本: score += 15.0
if model_hint 匹配: score += 25.0 / 20.0
if preferred_provider 匹配: score += 20.0
if excluded_provider 匹配: score -= 100.0
```

### 6.3 负载均衡策略 (LoadBalanceStrategy)

**文件**: `scheduler/strategy/load_balance.py`

**五种算法**:

| 算法 | 说明 | 适用场景 |
|------|------|---------|
| `ROUND_ROBIN` | 轮询 | 端点性能相近 |
| `WEIGHTED_RANDOM` | 加权随机 | 端点性能差异大 |
| `LEAST_CONNECTIONS` | 最少连接数 | 长连接场景 |
| `LEAST_LATENCY` | 最低延迟 | 延迟敏感 |
| `POWER_OF_TWO` | 二选一 | 通用推荐 (随机选2，取负载低的) |

**使用方式**:
- 本地端点默认使用 `POWER_OF_TWO` (避免热点)
- 云端端点默认使用 `LEAST_CONNECTIONS` (均匀分布)

### 6.4 策略路由器 (StrategyRouter)

**文件**: `scheduler/strategy/router.py`

**职责**: 整合三种策略，根据是否启用 OpenClaw 选择路由路径

**路由决策输出** (`RoutingDecision`):

```python
@dataclass
class RoutingDecision:
    agent_type: str                          # "local" / "cloud"
    selected_endpoint: Optional[ModelEndpoint]  # 选中的端点
    fallback_endpoints: List[ModelEndpoint]     # 备用端点列表
    strategy_name: str                      # 策略名称
    reason: str                             # 决策原因
    use_openclaw: bool                      # 是否使用 OpenClaw
```

**路由路径**:

```
route(request)
  │
  ├── use_openclaw=True → _route_adaptive(request)
  │   │
  │   ├── PRIVACY_FIRST → _route_local()
  │   ├── LOCAL_FIRST   → _route_local_first_adaptive()
  │   │   ├── 本地可用 → POWER_OF_TWO 选本地 + 本地+云端 Fallback
  │   │   └── 本地不可用 → LEAST_CONNECTIONS 选云端
  │   ├── CLOUD_FIRST   → _route_cloud_first_adaptive()
  │   │   ├── tool_call → 优先选支持工具的云端模型
  │   │   └── 其他 → PriorityStrategy 选云端
  │   ├── LATENCY_OPTIMIZED → _route_latency_optimized()
  │   │   └── 所有端点按 avg_latency_ms 排序
  │   ├── COST_OPTIMIZED → _route_cost_optimized()
  │   │   └── 优先本地 (零成本) → 低价云端
  │   └── CAPABILITY_OPTIMIZED → _route_capability_optimized()
  │       ├── tool_call → 支持工具的云端模型
  │       ├── 长上下文 → 大上下文窗口的云端模型
  │       └── 特殊类型 → 专用模型
  │
  └── use_openclaw=False → _route_legacy(request)
      └── 传统优先级 + 负载均衡路由
```

---

## 7. Hook 机制

### 7.1 Pre-Hook 管线

Pre-Hook 在请求路由前执行，按 `priority` 升序排列（数值越小越先执行）。

| Hook | Priority | 说明 |
|------|----------|------|
| `RateLimitHook` | 5 | 全局限流 (60 RPM) + App 级限流 (30 RPM/appid) |
| `RequestValidationHook` | 10 | 校验 appid/prompt 非空、timeout ≥ 1s、max_latency ≥ 100ms |
| `ConstraintEnrichmentHook` | 20 | 约束增强：为 NORMAL 及以下优先级自动设置 `max_cost_per_request=0.05`；支持按 appid 配置默认约束 |
| `RequestLoggingHook` | 90 | 记录请求日志 (appid, type, priority, prompt_len) |

**RateLimitHook 实现细节**:
- 滑动窗口算法，窗口大小 60 秒
- 全局限制: `max_requests_per_minute` (默认 60)
- App 级限制: `max_requests_per_appid` (默认 30)
- 超限抛出 `RuntimeError`

**ConstraintEnrichmentHook 扩展点**:
- `set_app_defaults(appid, defaults)` — 为指定 appid 设置默认约束
- 自动为低优先级请求设置费用上限

### 7.2 Post-Hook 管线

Post-Hook 在模型返回结果后执行，按 `priority` 升序排列。

| Hook | Priority | 说明 |
|------|----------|------|
| `ResponseSanitizationHook` | 10 | 脱敏处理：正则匹配 api_key/password/token/secret 并替换为 `[REDACTED]` |
| `CostCalculationHook` | 20 | 费用计算：若 `result.cost` 为空，按 `usage` 和默认费率估算 |
| `RetryDecisionHook` | 30 | 重试决策：当 `finish_reason=error` 或输出为空时，记录重试次数 (max_retries=2) |
| `ResponseLoggingHook` | 90 | 记录响应日志 (model, provider, latency, cost, output_len) |

**HookManager 关键行为**:
- `priority ≤ 50` 的 Hook 失败时会向上抛出异常（关键 Hook）
- `priority > 50` 的 Hook 失败仅记录日志（非关键 Hook）
- `hooks_applied` 列表记录所有成功执行的 Hook 名称

---

## 8. API 接口参考

### 核心调度接口

#### POST /dispatch

发送单个调度请求。

**请求体**: `DispatchRequest` (见 5.1)

**响应体**: `DispatchResponse` (见 5.2)

**示例**:

```bash
curl -X POST http://localhost:8000/dispatch \
  -H "Content-Type: application/json" \
  -d '{
    "appid": "demo-app",
    "type": "chat",
    "prompt": "用一句话介绍量子计算",
    "priority": 3,
    "constraints": {
      "max_latency_ms": 5000,
      "require_local": false
    }
  }'
```

**响应示例**:

```json
{
  "request_id": "a1b2c3d4-...",
  "appid": "demo-app",
  "status": "success",
  "result": {
    "model_name": "ollama-qwen2.5",
    "model_type": "local",
    "provider": "ollama",
    "output": "量子计算利用量子叠加和纠缠原理...",
    "usage": {"prompt_tokens": 12, "completion_tokens": 45, "total_tokens": 57},
    "latency_ms": 23801,
    "cost": 0.0,
    "finish_reason": "stop"
  },
  "fallback_results": null,
  "agent_trace": [
    {"agent": "pre_hooks", "message": "Executed pre-processing hooks", "timestamp": 1748...},
    {"agent": "openclaw_router", "message": "OpenClaw routing: agent=local, endpoint=name='ollama-qwen2.5', strategy=adaptive_local_first", "timestamp": 1748...},
    {"agent": "openclaw_agent", "message": "OpenClaw result from ollama-qwen2.5, latency=23801ms", "timestamp": 1748...},
    {"agent": "post_hooks", "message": "Executed post-processing hooks", "timestamp": 1748...}
  ],
  "hooks_applied": ["pre:rate_limit", "pre:request_validation", "pre:constraint_enrichment", "pre:request_logging", "post:response_sanitization", "post:cost_calculation", "post:retry_decision", "post:response_logging"],
  "timestamp": "2026-05-26T...",
  "total_latency_ms": 23850
}
```

#### POST /dispatch/batch

批量调度多个请求（并发执行）。

**请求体**: `List[DispatchRequest]`

**响应体**: `List[DispatchResponse]`

### 模型管理接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/models` | 列出所有模型端点 (可按 type 过滤) |
| POST | `/models` | 注册新模型端点 |
| DELETE | `/models/{name}` | 注销模型端点 |
| GET | `/models/{name}/status` | 获取端点详细状态 |

**GET /models 响应字段**:

```json
{
  "name": "ollama-qwen2.5",
  "display_name": "Ollama Qwen2.5 3B (Local)",
  "model_type": "local",
  "provider": "ollama",
  "model_id": "qwen2.5:3b",
  "enabled": true,
  "available": true,
  "current_load": 0,
  "max_concurrent": 5,
  "avg_latency_ms": 23801,
  "success_rate": 0.9524,
  "priority": 1,
  "weight": 3,
  "load_factor": 0.0,
  "cost_per_1k_input_tokens": 0.0,
  "cost_per_1k_output_tokens": 0.0,
  "tags": ["chat", "completion"]
}
```

### Hook 管理接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/hooks` | 列出所有 Hook |
| POST | `/hooks/{name}/enable` | 启用 Hook |
| POST | `/hooks/{name}/disable` | 禁用 Hook |

### 监控与运维接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/stats` | 系统统计 (请求/资源/模型) |
| GET | `/history` | 最近调度历史 (默认 20 条) |
| GET | `/health` | 健康检查 |

**GET /stats 响应**:

```json
{
  "requests": {
    "total": 42,
    "success": 40,
    "failed": 2,
    "success_rate": 0.9524,
    "avg_latency_ms": 15230.5
  },
  "system": {
    "cpu_percent": 23.5,
    "memory_total_gb": 16.0,
    "memory_used_gb": 10.2,
    "memory_percent": 63.8
  },
  "models": {
    "ollama-qwen2.5": {
      "model_type": "local",
      "current_load": 0,
      "max_concurrent": 5,
      "load_factor": 0.0,
      "avg_latency_ms": 23801,
      "success_rate": 0.9524
    }
  }
}
```

### OpenClaw 集成接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/openclaw/bridge/health` | Bridge 健康检查 |
| GET | `/openclaw/bridge/models` | Bridge 模型列表 |
| GET | `/openclaw/bridge/stats` | Bridge 统计 |
| POST | `/openclaw/agent/{agent_id}/message` | 向 Agent 发消息 |
| GET | `/openclaw/adaptive/state` | 自适应权重状态 |
| POST | `/openclaw/toggle?enabled=true/false` | 开关 OpenClaw 集成 |

**GET /openclaw/adaptive/state 响应**:

```json
{
  "adaptive_weights": {"local": 0.85, "cloud": 0.15},
  "local_success_streak": 5,
  "cloud_success_streak": 1,
  "local_fail_streak": 0,
  "cloud_fail_streak": 0,
  "latency_threshold_ms": 3000,
  "cost_threshold": 0.01
}
```

---

## 9. 调度流程全链路追踪

以一个完整的 `POST /dispatch` 请求为例，展示从接收到返回的全过程：

```
客户端请求 ──────────────────────────────────────────────────────────────────
    │
    ▼
[1] FastAPI /dispatch
    │  生成 request_id, 初始化 _agent_trace
    │
    ▼
[2] Pre-Hook 管线 (按 priority 升序)
    │  ┌─ RateLimitHook (p=5):      检查全局/appid 限流
    │  ├─ RequestValidationHook (p=10): 校验 appid/prompt/timeout
    │  ├─ ConstraintEnrichmentHook (p=20): 增强约束 (费用上限等)
    │  └─ RequestLoggingHook (p=90):  记录请求日志
    │
    ▼
[3] OpenClaw 路由 (if use_openclaw)
    │  ┌─ StrategyRouter.route(request)
    │  │   └─ AdaptiveStrategy.route(request)
    │  │       ├─ _classify_request() → 决定策略类型
    │  │       ├─ _select_strategy() → 选择具体策略
    │  │       └─ _determine_agent_type() → 确定本地/云端
    │  │
    │  └─ OpenClawAgent.execute()
    │      ├─ 尝试 Bridge /dispatch → 成功 → 返回
    │      └─ Bridge 失败 → 降级到 Python 直接调度
    │
    ▼ (若 OpenClaw 失败/禁用)
[4] Python 直接路由
    │  StrategyRouter.route(request)
    │  → RoutingDecision { agent_type, endpoint, fallbacks, strategy }
    │
    ▼
[5] Agent 执行
    │  ┌─ LocalModelAgent / CloudModelAgent
    │  │   └─ call_model_api(endpoint, request)
    │  │       ├─ increment_load()
    │  │       ├─ POST {base_url}/chat/completions
    │  │       ├─ 解析响应 + 计算费用
    │  │       ├─ update_latency() / update_success_rate()
    │  │       └─ decrement_load()
    │  │
    │  └─ _try_fallbacks() (if 主端点失败)
    │      └─ 依次尝试 fallback_endpoints[:2]
    │
    ▼
[6] Post-Hook 管线 (按 priority 升序)
    │  ┌─ ResponseSanitizationHook (p=10): 脱敏处理
    │  ├─ CostCalculationHook (p=20):      费用计算
    │  ├─ RetryDecisionHook (p=30):        重试决策
    │  └─ ResponseLoggingHook (p=90):      响应日志
    │
    ▼
[7] 构建 DispatchResponse
    │  包含: result, fallback_results, agent_trace, hooks_applied
    │
    ▼
[8] 更新统计
    │  _stats: total_requests++, success/failed++, history[]
    │
    ▼
客户端响应 ◄────────────────────────────────────────────────────────────────
```

**agent_trace 示例** (完整链路):

```json
[
  {"agent": "pre_hooks", "message": "Executed pre-processing hooks", "timestamp": 1748...},
  {"agent": "openclaw_router", "message": "Attempting OpenClaw adaptive routing", "timestamp": 1748...},
  {"agent": "openclaw_router", "message": "OpenClaw routing: agent=local, endpoint=name='ollama-qwen2.5', strategy=adaptive_local_first", "timestamp": 1748...},
  {"agent": "openclaw_agent", "message": "OpenClaw result from ollama-qwen2.5, latency=23801ms", "timestamp": 1748...},
  {"agent": "post_hooks", "message": "Executed post-processing hooks", "timestamp": 1748...}
]
```

---

## 10. 配置说明

### 10.1 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SCHEDULER_APP_NAME` | OpenClaw Model Scheduler | 应用名称 |
| `SCHEDULER_HOST` | 0.0.0.0 | 监听地址 |
| `SCHEDULER_PORT` | 8000 | 监听端口 |
| `SCHEDULER_LOG_LEVEL` | INFO | 日志级别 |
| `OPENCLAW_GATEWAY_URL` | http://localhost:3000 | OpenClaw Gateway URL |
| `OPENCLAW_API_KEY` | - | OpenClaw API 密钥 |
| `RATE_LIMIT_RPM` | 60 | 全局每分钟请求上限 |
| `RATE_LIMIT_PER_APPID` | 30 | 每 appid 每分钟请求上限 |
| `MAX_RETRIES` | 2 | 最大重试次数 |
| `DEFAULT_TIMEOUT_MS` | 30000 | 默认超时 (毫秒) |
| `OPENCLAW_GATEWAY_URL` (Bridge) | http://localhost:3000 | Bridge 连接的 Gateway |
| `BRIDGE_PORT` | 3001 | Bridge 监听端口 |

### 10.2 openclaw.json 配置

```json
{
  "agent": {
    "model": {
      "primary": "ollama/qwen2.5:3b",
      "fallbacks": ["moonshot/kimi-k2.6", "deepseek/deepseek-chat"]
    },
    "thinkingLevel": "medium",
    "workspace": "./workspace"
  },
  "agents": {
    "list": [
      {
        "id": "main",
        "default": true,
        "model": {
          "primary": "ollama/qwen2.5:3b",
          "fallbacks": ["moonshot/kimi-k2.6", "deepseek/deepseek-chat"]
        }
      },
      {
        "id": "local-dispatcher",
        "model": { "primary": "ollama/qwen2.5:3b" }
      },
      {
        "id": "cloud-dispatcher",
        "model": {
          "primary": "moonshot/kimi-k2.6",
          "fallbacks": ["deepseek/deepseek-chat"]
        }
      },
      {
        "id": "code-executor",
        "model": {
          "primary": "deepseek/deepseek-chat",
          "fallbacks": ["moonshot/kimi-k2.6"]
        }
      }
    ]
  },
  "models": {
    "catalog": { "local": [...], "cloud": [...] },
    "routing": {
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
  },
  "gateway": {
    "port": 3000,
    "host": "0.0.0.0",
    "auth": { "enabled": false }
  }
}
```

---

## 11. 可扩展性设计

### 11.1 添加新模型端点

**方式一**: 运行时通过 API 注册

```bash
curl -X POST http://localhost:8000/models \
  -H "Content-Type: application/json" \
  -d '{
    "name": "gpt-4o",
    "display_name": "GPT-4o (Cloud)",
    "model_type": "cloud",
    "provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-...",
    "model_id": "gpt-4o",
    "max_context_length": 128000,
    "supports_streaming": true,
    "supports_tools": true,
    "cost_per_1k_input_tokens": 0.005,
    "cost_per_1k_output_tokens": 0.015,
    "priority": 2,
    "weight": 2,
    "max_concurrent": 10,
    "tags": ["chat", "completion", "tool_call"]
  }'
```

**方式二**: 在 `main.py` 的 `_init_default_models()` 中添加

**方式三**: Bridge 侧在 `MODEL_CATALOG` 中添加 + `openclaw.json` 中同步

### 11.2 添加新调度策略

1. 在 `AdaptiveStrategy` 中定义新策略常量:

```python
class AdaptiveStrategy:
    MY_STRATEGY = "my_strategy"
```

2. 在 `_classify_request()` 中添加触发条件
3. 在 `_select_strategy()` 中添加策略选择逻辑
4. 在 `_determine_agent_type()` 中添加 Agent 类型确定逻辑
5. 在 `StrategyRouter` 中添加对应的 `_route_xxx()` 方法

### 11.3 添加新 Hook

1. 继承 `PreHook` 或 `PostHook`:

```python
from scheduler.hooks.hook_manager import PreHook

class MyCustomHook(PreHook):
    def __init__(self):
        super().__init__(name="my_custom", priority=50)

    async def execute(self, request: DispatchRequest) -> DispatchRequest:
        # 自定义逻辑
        return request
```

2. 在 `main.py` 的 `_init_hooks()` 中注册:

```python
hook_manager.register_pre_hook(MyCustomHook())
```

3. 可通过 API 动态启用/禁用: `POST /hooks/my_custom/enable`

### 11.4 添加新 Agent

1. 继承 `BaseAgent`:

```python
from scheduler.agents.base_agent import BaseAgent

class MyAgent(BaseAgent):
    def __init__(self, registry):
        super().__init__(name="my_agent", registry=registry)

    async def execute(self, request, endpoint):
        # 自定义执行逻辑
        return result

    def can_handle(self, request):
        return True
```

2. 在 `MainDispatcherAgent.__init__()` 中实例化
3. 在 `_get_agent()` 中添加路由映射

### 11.5 添加新 Provider

在 `ModelProvider` 枚举中添加新值:

```python
class ModelProvider(str, Enum):
    # ... 现有值
    ZHIPU = "zhipu"       # 智谱
    BAIDU = "baidu"       # 百度
    ALIBABA = "alibaba"   # 阿里
```

### 11.6 扩展 Bridge

Bridge (`orchestrator.mjs`) 可通过以下方式扩展:

- **新路由策略**: 在 `adaptiveRoute()` 函数中添加新分支
- **新 API 端点**: 在 `createServer` 回调中添加新路由
- **新 Agent**: 在 `openclaw.json` 的 `agents.list` 中添加配置
- **中间件**: 在请求处理链中插入预处理/后处理逻辑

### 11.7 扩展前端 Dashboard

Dashboard (`static/dashboard.html`) 可扩展:

- **图表可视化**: 集成 Chart.js / ECharts 展示延迟趋势、费用分布
- **WebSocket 实时推送**: 替代轮询，实现真正的实时更新
- **历史对比**: 调度策略效果对比面板
- **告警规则**: 自定义阈值告警 (延迟/失败率/费用)

---

## 12. 部署与运维

### 12.1 快速启动

```bash
# 1. 安装 Python 依赖
pip install -r requirements.txt

# 2. 确保 Ollama 运行 (本地模型)
ollama serve
ollama pull qwen2.5:3b

# 3. 启动调度服务 (自动启动 Bridge)
python run.py
# 或
uvicorn scheduler.main:app --host 0.0.0.0 --port 8000

# 4. 访问 Dashboard
open http://localhost:8000/
```

### 12.2 服务端口

| 服务 | 端口 | 说明 |
|------|------|------|
| FastAPI 调度服务 | 8000 | 主服务，提供 REST API + Dashboard |
| OpenClaw Bridge | 3001 | Node.js 桥接层 |
| OpenClaw Gateway | 3000 | OpenClaw 框架网关 (可选) |
| Ollama | 11434 | 本地模型服务 |

### 12.3 健康检查

```bash
# 调度服务
curl http://localhost:8000/health

# Bridge
curl http://localhost:3001/health

# 自适应状态
curl http://localhost:8000/openclaw/adaptive/state
```

### 12.4 常见运维操作

```bash
# 开关 OpenClaw 集成
curl -X POST "http://localhost:8000/openclaw/toggle?enabled=false"

# 禁用/启用 Hook
curl -X POST http://localhost:8000/hooks/rate_limit/disable
curl -X POST http://localhost:8000/hooks/rate_limit/enable

# 查看调度历史
curl http://localhost:8000/history?limit=10

# 注销模型端点
curl -X DELETE http://localhost:8000/models/deepseek-chat
```

---

> **文档维护说明**: 本文档基于 OpenClaw Multi-Agent 调度系统 v2026.4.27 代码自动生成，如代码有变更请同步更新对应章节。
