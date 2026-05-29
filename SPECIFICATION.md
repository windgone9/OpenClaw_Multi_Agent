# OpenClaw 模型调度系统 — 算法与功能函数详细说明文档

## 1. 系统概述

本系统基于 OpenClaw 2026.4.27 构建，实现了一个多模型智能调度框架。系统从上层应用接收 JSON 请求，由主 Agent（MainDispatcherAgent）接管并根据调度策略（优先级、负载均衡等）分配到本地模型或云端模型，通过 Hook 机制接管请求的预处理和后处理，最终将处理结果以 JSON 形式返回给上层应用。

### 1.1 技术栈

| 组件 | 技术 | 版本 |
|------|------|------|
| AI 编排框架 | OpenClaw (npm) | 2026.4.27 |
| Web 框架 | FastAPI | ≥0.100.0 |
| ASGI 服务器 | Uvicorn | ≥0.20.0 |
| HTTP 客户端 | httpx | ≥0.24.0 |
| 数据校验 | Pydantic | ≥2.0.0 |
| Python | CPython | ≥3.9 |

### 1.2 系统架构

```
┌──────────────────────────────────────────────────────────┐
│                     上层应用 (Upper App)                   │
│                 发送 JSON 请求 / 接收 JSON 响应             │
└────────────────────────┬─────────────────────────────────┘
                         │ HTTP POST /dispatch
                         ▼
┌──────────────────────────────────────────────────────────┐
│              FastAPI Gateway (scheduler/main.py)          │
│    /dispatch  /dispatch/batch  /models  /hooks  /health   │
└────────────────────────┬─────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────┐
│             MainDispatcherAgent (主调度 Agent)             │
│  1. 执行 Pre-Hooks 链                                     │
│  2. 调用 StrategyRouter 进行路由决策                       │
│  3. 分发到对应 Sub-Agent                                  │
│  4. 执行 Post-Hooks 链                                    │
│  5. 聚合结果返回                                          │
└───────┬────────────────────────────┬─────────────────────┘
        │                            │
        ▼                            ▼
┌───────────────────┐      ┌───────────────────┐
│  LocalModelAgent   │      │  CloudModelAgent   │
│  (本地模型子Agent)  │      │  (云端模型子Agent)  │
│  - Ollama          │      │  - OpenAI          │
│  - LMStudio        │      │  - Anthropic       │
│  - 自定义本地端点    │      │  - DeepSeek        │
└───────┬───────────┘      └───────┬───────────┘
        │                            │
        ▼                            ▼
┌───────────────────┐      ┌───────────────────┐
│  Pre-Hook Chain    │      │  Pre-Hook Chain    │
│  - RateLimit       │      │  - RateLimit       │
│  - Validation      │      │  - Validation      │
│  - Enrichment      │      │  - Enrichment      │
│  - Logging         │      │  - Logging         │
└───────┬───────────┘      └───────┬───────────┘
        │                            │
        ▼                            ▼
┌───────────────────┐      ┌───────────────────┐
│  Post-Hook Chain   │      │  Post-Hook Chain   │
│  - Sanitization    │      │  - Sanitization    │
│  - CostCalc        │      │  - CostCalc        │
│  - RetryDecision   │      │  - RetryDecision   │
│  - Logging         │      │  - Logging         │
└───────────────────┘      └───────────────────┘
```

---

## 2. 目录结构

```
Multi-Agent/
├── scheduler/                        # 核心调度包
│   ├── __init__.py
│   ├── main.py                       # FastAPI 应用入口，API 路由定义
│   ├── models/                       # 数据模型层
│   │   ├── __init__.py
│   │   ├── request.py                # 请求模型 (DispatchRequest, ModelConstraint)
│   │   ├── response.py               # 响应模型 (DispatchResponse, ModelResult)
│   │   └── model_config.py           # 模型配置 (ModelEndpoint, ModelRegistry)
│   ├── agents/                       # Agent 层
│   │   ├── __init__.py
│   │   ├── base_agent.py             # 基础 Agent 抽象类
│   │   ├── local_agent.py            # 本地模型子 Agent
│   │   ├── cloud_agent.py            # 云端模型子 Agent
│   │   └── main_agent.py             # 主调度 Agent
│   ├── strategy/                     # 调度策略层
│   │   ├── __init__.py
│   │   ├── priority.py               # 优先级策略
│   │   ├── load_balance.py           # 负载均衡策略
│   │   └── router.py                 # 策略路由器
│   ├── hooks/                        # Hook 机制层
│   │   ├── __init__.py
│   │   ├── hook_manager.py           # Hook 管理器
│   │   ├── pre_hook.py               # 前置 Hook 集合
│   │   └── post_hook.py              # 后置 Hook 集合
│   └── config/                       # 配置层
│       ├── __init__.py
│       └── settings.py               # 全局配置
├── run.py                            # 启动脚本
├── requirements.txt                  # Python 依赖
├── package.json                      # Node.js 依赖 (openclaw)
└── SPECIFICATION.md                  # 本文档
```

---

## 3. 数据模型详细说明

### 3.1 DispatchRequest — 调度请求模型

**文件**: `scheduler/models/request.py`

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `appid` | `str` | ✅ | — | 应用标识符，用于路由和追踪 |
| `type` | `RequestType` | ✅ | — | 请求类型枚举：chat/completion/embedding/image/audio/tool_call |
| `prompt` | `str` | ✅ | — | 输入提示文本 |
| `priority` | `Priority` | ❌ | NORMAL | 优先级：CRITICAL(1)/HIGH(2)/NORMAL(3)/LOW(4)/BACKGROUND(5) |
| `model_hint` | `str` | ❌ | None | 模型名称提示，用于精确路由 |
| `constraints` | `ModelConstraint` | ❌ | None | 模型选择约束条件 |
| `parameters` | `Dict[str, Any]` | ❌ | None | 额外模型参数（temperature, top_p 等） |
| `context` | `List[Dict[str, str]]` | ❌ | None | 对话上下文消息列表 |
| `metadata` | `Dict[str, Any]` | ❌ | None | 附加元数据 |
| `request_id` | `str` | ❌ | None | 请求 ID（幂等性） |
| `timeout_ms` | `int` | ❌ | 30000 | 超时时间（毫秒） |

### 3.2 ModelConstraint — 模型约束

| 字段 | 类型 | 说明 |
|------|------|------|
| `max_latency_ms` | `int` | 最大可接受延迟 |
| `min_context_length` | `int` | 最小上下文窗口长度 |
| `require_streaming` | `bool` | 是否需要流式响应 |
| `require_local` | `bool` | 是否必须使用本地模型（数据隐私） |
| `require_gpu` | `bool` | 是否需要 GPU 加速 |
| `preferred_providers` | `List[str]` | 首选模型提供商 |
| `excluded_providers` | `List[str]` | 排除的模型提供商 |
| `max_cost_per_request` | `float` | 单次请求最大成本（USD） |

### 3.3 DispatchResponse — 调度响应模型

**文件**: `scheduler/models/response.py`

| 字段 | 类型 | 说明 |
|------|------|------|
| `request_id` | `str` | 唯一请求标识 |
| `appid` | `str` | 应用标识 |
| `status` | `DispatchStatus` | 状态：SUCCESS/PARTIAL/FAILED/TIMEOUT/REJECTED |
| `result` | `ModelResult` | 主模型结果 |
| `fallback_results` | `List[ModelResult]` | 备选模型结果 |
| `error` | `ErrorDetail` | 错误详情 |
| `agent_trace` | `List[Dict]` | Agent 执行追踪 |
| `hooks_applied` | `List[str]` | 已应用的 Hook 列表 |
| `timestamp` | `str` | 响应时间戳 |
| `total_latency_ms` | `int` | 端到端总延迟 |

### 3.4 ModelEndpoint — 模型端点配置

**文件**: `scheduler/models/model_config.py`

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 唯一端点名称 |
| `model_type` | `ModelType` | local/cloud/hybrid |
| `provider` | `ModelProvider` | ollama/openai/anthropic/google/deepseek 等 |
| `base_url` | `str` | API 基础 URL |
| `api_key` | `str` | API 密钥（可选） |
| `model_id` | `str` | API 调用使用的模型标识 |
| `max_context_length` | `int` | 最大上下文长度 |
| `priority` | `int` | 默认优先级（1=最高, 10=最低） |
| `weight` | `int` | 负载均衡权重 |
| `max_concurrent` | `int` | 最大并发请求数 |
| `current_load` | `int` | 当前活跃请求数 |
| `avg_latency_ms` | `int` | 平均延迟（毫秒） |
| `success_rate` | `float` | 成功率（0.0-1.0） |
| `cost_per_1k_input_tokens` | `float` | 每 1K 输入 token 成本 |
| `cost_per_1k_output_tokens` | `float` | 每 1K 输出 token 成本 |

### 3.5 ModelRegistry — 模型注册表

管理所有模型端点的注册、查询和状态追踪。

| 方法 | 签名 | 说明 |
|------|------|------|
| `register` | `(endpoint: ModelEndpoint) -> None` | 注册新端点 |
| `unregister` | `(name: str) -> None` | 注销端点 |
| `get` | `(name: str) -> Optional[ModelEndpoint]` | 按名称获取端点 |
| `get_by_type` | `(model_type: ModelType) -> List[ModelEndpoint]` | 按类型筛选 |
| `get_by_provider` | `(provider: ModelProvider) -> List[ModelEndpoint]` | 按提供商筛选 |
| `get_available` | `() -> List[ModelEndpoint]` | 获取所有可用端点 |
| `get_local_available` | `() -> List[ModelEndpoint]` | 获取本地可用端点 |
| `get_cloud_available` | `() -> List[ModelEndpoint]` | 获取云端可用端点 |

---

## 4. Agent 层详细说明

### 4.1 BaseAgent — 基础 Agent 抽象类

**文件**: `scheduler/agents/base_agent.py`

所有 Agent 的基类，提供模型 API 调用、负载追踪和容错机制。

#### 核心方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `execute` | `async (request, endpoint) -> ModelResult` | 抽象方法，执行模型调用 |
| `can_handle` | `(request) -> bool` | 抽象方法，判断是否能处理该请求 |
| `call_model_api` | `async (endpoint, request) -> ModelResult` | 调用模型 API 并处理响应 |
| `execute_with_fallback` | `async (request, endpoints, max_retries) -> ModelResult` | 带降级的执行，依次尝试多个端点 |

#### call_model_api 算法流程

```
1. 记录开始时间 start_time
2. endpoint.increment_load() — 增加端点负载计数
3. 构建 HTTP 请求：
   a. _build_payload() — 构建 OpenAI 兼容格式的请求体
   b. _build_headers() — 构建请求头（含 Authorization）
4. 发送 POST 请求到 {base_url}/chat/completions
5. 成功时：
   a. 计算延迟 latency_ms
   b. endpoint.update_latency(latency_ms) — EMA 更新平均延迟
   c. endpoint.update_success_rate(True) — EMA 更新成功率
   d. _parse_response() — 解析响应为 ModelResult
6. 超时时：抛出 TimeoutError
7. HTTP 错误时：抛出 RuntimeError
8. finally: endpoint.decrement_load() — 减少负载计数
```

#### EMA（指数移动平均）更新算法

平均延迟和成功率使用 EMA 算法进行平滑更新：

- **延迟更新**: `new_avg = 0.7 × old_avg + 0.3 × new_value`
  - α=0.7 使历史值权重较高，避免单次异常值剧烈影响
- **成功率更新**: `new_rate = 0.95 × old_rate + 0.05 × (1 或 0)`
  - α=0.95 使成功率变化更平滑，需要多次失败才能显著降低

#### execute_with_fallback 降级算法

```
1. 遍历 endpoints 列表
2. 跳过不可用的端点 (is_available == False)
3. 尝试执行 execute()
4. 成功则立即返回
5. 失败则记录错误，尝试下一个端点
6. 最多尝试 max_retries + 1 次
7. 全部失败则抛出 RuntimeError
```

### 4.2 LocalModelAgent — 本地模型子 Agent

**文件**: `scheduler/agents/local_agent.py`

负责调度本地部署的模型（如 Ollama、LMStudio）。

#### 端点选择算法 (_select_endpoint)

```
1. 获取所有本地可用端点
2. 如果请求有 model_hint：
   a. 优先匹配 name 或 model_id 包含 hint 的端点
3. 如果约束有 preferred_providers：
   a. 按顺序匹配 provider 符合偏好的端点
4. 默认：按 (load_factor, avg_latency_ms) 升序排序
   a. 优先选择负载最低的
   b. 负载相同时选择延迟最低的
5. 返回排序后的第一个端点
```

### 4.3 CloudModelAgent — 云端模型子 Agent

**文件**: `scheduler/agents/cloud_agent.py`

负责调度云端 API 模型（如 OpenAI、Anthropic、DeepSeek）。

#### 端点选择算法 (_select_endpoint)

```
1. 获取所有云端可用端点
2. 如果请求有 model_hint：精确匹配
3. 如果约束有 preferred_providers：按偏好匹配
4. 如果约束有 excluded_providers：排除指定提供商
5. 如果约束有 max_cost_per_request：过滤超出预算的端点
6. 按 (priority, load_factor, avg_latency_ms) 升序排序
   a. 优先级高的优先
   b. 同优先级选负载低的
   c. 负载相同选延迟低的
7. 返回排序后的第一个端点
```

### 4.4 MainDispatcherAgent — 主调度 Agent

**文件**: `scheduler/agents/main_agent.py`

系统的核心调度器，协调所有子 Agent、Hook 和策略路由。

#### dispatch 算法完整流程

```
1. 生成或使用请求 ID
2. 执行 Pre-Hooks 链：
   a. 限流检查 (RateLimitHook)
   b. 请求验证 (RequestValidationHook)
   c. 约束增强 (ConstraintEnrichmentHook)
   d. 请求日志 (RequestLoggingHook)
3. 调用 StrategyRouter.route() 获取路由决策：
   a. 决策包含：agent_type, selected_endpoint, fallback_endpoints, strategy_name
4. 根据 agent_type 获取对应 Agent (LocalModelAgent / CloudModelAgent)
5. 执行主端点调用：
   a. agent.execute(request, endpoint)
6. 如果有 fallback_endpoints，尝试降级调用（最多 2 个备选）
7. 执行 Post-Hooks 链：
   a. 响应脱敏 (ResponseSanitizationHook)
   b. 成本计算 (CostCalculationHook)
   c. 重试决策 (RetryDecisionHook)
   d. 响应日志 (ResponseLoggingHook)
8. 构建并返回 DispatchResponse
9. 异常处理：
   a. TimeoutError → DispatchStatus.TIMEOUT
   b. 其他异常 → DispatchStatus.FAILED
```

---

## 5. 调度策略详细说明

### 5.1 PriorityStrategy — 优先级策略

**文件**: `scheduler/strategy/priority.py`

基于多维评分的端点选择策略，为每个候选端点计算综合得分。

#### 评分算法 (_score)

基础分 100.0，通过以下维度加减分：

| 维度 | 计算方式 | 分值影响 |
|------|----------|----------|
| 端点优先级 | `-(priority - 1) × 15.0` | 优先级 1 加 0 分，优先级 10 减 135 分 |
| 负载因子 | `-load_factor × 20.0` | 满载减 20 分，空载减 0 分 |
| 成功率 | `+success_rate × 10.0` | 100% 成功率加 10 分 |
| 延迟约束 | 超出约束减 50 分，未超出按比例加 0-15 分 | |
| 本地要求 | 满足加 30 分，不满足减 100 分 | |
| 关键优先级 | 本地低延迟加 20 分 | |
| 低优先级 | 免费模型加 15 分 | |
| 模型提示 | 名称匹配加 25 分，ID 匹配加 20 分 | |
| 偏好提供商 | 匹配加 20 分 | |
| 排除提供商 | 匹配减 100 分 | |

#### rank 方法

对候选端点按评分降序排序，返回完整排序列表，用于确定主选和备选端点。

### 5.2 LoadBalanceStrategy — 负载均衡策略

**文件**: `scheduler/strategy/load_balance.py`

提供 5 种负载均衡算法：

#### 5.2.1 Round Robin（轮询）

```
算法：维护全局索引 _rr_index
选择：candidates[_rr_index % len(candidates)]
每次选择后索引 +1
特点：均匀分配，不考虑端点差异
```

#### 5.2.2 Weighted Random（加权随机）

```
算法：
1. 收集所有端点的 weight 值
2. 计算总权重 total = sum(weights)
3. 生成 [0, total) 范围的随机数 r
4. 累加权重，当 cumulative >= r 时选择该端点
特点：权重高的端点被选中概率更大
```

#### 5.2.3 Least Connections（最少连接）

```
算法：min(candidates, key=lambda ep: ep.current_load)
选择当前活跃连接数最少的端点
特点：实时感知负载，适合长连接场景
```

#### 5.2.4 Least Latency（最低延迟）

```
算法：min(candidates, key=lambda ep: ep.avg_latency_ms)
选择平均延迟最低的端点
特点：优先响应速度，适合延迟敏感场景
```

#### 5.2.5 Power of Two（二次随机选择）

```
算法：
1. 从候选列表中随机选取 2 个端点
2. 从这 2 个中选择 current_load 较低的一个
特点：兼顾随机性和负载感知，避免惊群效应
```

### 5.3 StrategyRouter — 策略路由器

**文件**: `scheduler/strategy/router.py`

根据请求特征选择最优路由策略，返回 RoutingDecision。

#### 路由决策算法 (route)

```
1. 如果约束 require_local=True → _route_local()
2. 如果约束有 preferred_providers → 检查偏好属于本地还是云端
3. 如果优先级 CRITICAL/HIGH → _route_priority_high()
4. 如果类型是 embedding/image/audio → _route_specialized()
5. 默认 → _route_default()
```

#### 各路由策略详解

| 策略方法 | 触发条件 | 选择逻辑 | 备选策略 |
|----------|----------|----------|----------|
| `_route_priority_high` | 优先级 1-2 | 本地优先，PriorityStrategy 排序 | 本地备选 + 云端备选 |
| `_route_local` | require_local=True | Least Connections 选择本地 | 无本地时降级到云端 |
| `_route_cloud` | 偏好云端提供商 | PriorityStrategy 排序云端 | 云端备选 |
| `_route_specialized` | embedding/image/audio | 按 tags 匹配专用端点 | 无匹配时用全部端点 |
| `_route_default` | 默认 | Power of Two 选择本地 | 本地备选 + 云端备选 |

#### RoutingDecision 数据结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `agent_type` | `str` | 目标 Agent 类型 (local/cloud) |
| `selected_endpoint` | `ModelEndpoint` | 选中的主端点 |
| `fallback_endpoints` | `List[ModelEndpoint]` | 备选端点列表 |
| `strategy_name` | `str` | 使用的策略名称 |
| `reason` | `str` | 路由决策原因 |

---

## 6. Hook 机制详细说明

### 6.1 HookManager — Hook 管理器

**文件**: `scheduler/hooks/hook_manager.py`

管理 Pre-Hook 和 Post-Hook 的注册、执行和生命周期。

#### 核心方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `register_pre_hook` | `(hook: PreHook) -> None` | 注册前置 Hook |
| `register_post_hook` | `(hook: PostHook) -> None` | 注册后置 Hook |
| `unregister_hook` | `(name: str) -> None` | 注销 Hook |
| `execute_pre_hooks` | `async (request) -> DispatchRequest` | 按优先级执行前置 Hook 链 |
| `execute_post_hooks` | `async (request, result) -> ModelResult` | 按优先级执行后置 Hook 链 |
| `enable_hook` | `(name: str) -> None` | 启用 Hook |
| `disable_hook` | `(name: str) -> None` | 禁用 Hook |

#### Hook 执行规则

1. **按优先级排序**：priority 值越小越先执行
2. **短路机制**：priority ≤ 50 的 Hook 失败时抛出异常，终止后续执行
3. **容错机制**：priority > 50 的 Hook 失败时仅记录日志，继续执行
4. **可动态开关**：通过 enable/disable 控制是否执行

### 6.2 Pre-Hook 详细说明

**文件**: `scheduler/hooks/pre_hook.py`

#### RateLimitHook（优先级 5）

```
算法：滑动窗口限流
1. 维护全局请求时间列表 _request_times["global"]
2. 维护每个 appid 的请求时间列表 _appid_times[appid]
3. 每次请求时清理 60 秒窗口外的记录
4. 检查全局 RPM 是否超限（默认 60/分钟）
5. 检查 appid RPM 是否超限（默认 30/分钟）
6. 超限则抛出 RuntimeError
参数：
  - max_requests_per_minute: 全局每分钟最大请求数
  - max_requests_per_appid: 每个 appid 每分钟最大请求数
```

#### RequestValidationHook（优先级 10）

```
验证规则：
1. appid 不能为空
2. prompt 不能为空
3. timeout_ms 不能小于 1000ms
4. max_latency_ms 不能小于 100ms
验证失败抛出 ValueError
```

#### ConstraintEnrichmentHook（优先级 20）

```
约束增强算法：
1. 查找 appid 对应的默认配置 _appid_defaults[appid]
2. 填充约束中未指定的字段：
   - max_latency_ms: 从默认配置继承
   - require_local: 从默认配置继承
   - preferred_providers: 从默认配置继承
   - excluded_providers: 从默认配置继承
3. 对于 NORMAL 及以下优先级，自动设置 max_cost_per_request=0.05
```

#### RequestLoggingHook（优先级 90）

```
记录请求日志，包含：
- appid, type, priority, model_hint, prompt_len
```

### 6.3 Post-Hook 详细说明

**文件**: `scheduler/hooks/post_hook.py`

#### ResponseSanitizationHook（优先级 10）

```
脱敏算法：
1. 使用正则表达式匹配敏感信息模式：
   - api_key = "xxx"
   - password = "xxx"
   - token = "xxx"
   - secret = "xxx"
2. 将匹配内容替换为 [REDACTED]
3. 仅处理字符串类型的 output
```

#### CostCalculationHook（优先级 20）

```
成本计算算法：
1. 如果 result.cost 已有值，直接返回
2. 从 result.usage 获取 token 用量
3. 估算公式：
   estimated_cost = (input_tokens / 1000) × 0.001 + (output_tokens / 1000) × 0.002
   （使用通用估算费率，实际费率在 ModelEndpoint 中配置）
```

#### RetryDecisionHook（优先级 30）

```
重试决策算法：
1. 检查 result.finish_reason 是否为 "error" 或 output 为空
2. 维护每个 request_id 的重试计数 _retry_count
3. 如果重试次数 < max_retries（默认 2）：
   - 记录警告日志，标记可重试
4. 如果重试次数 >= max_retries：
   - 记录错误日志，清除计数
```

#### ResponseLoggingHook（优先级 90）

```
记录响应日志，包含：
- appid, model_name, provider, latency_ms, cost, output_len
```

---

## 7. API 接口详细说明

### 7.1 POST /dispatch — 单次调度

**请求体**:
```json
{
  "appid": "my-app-001",
  "type": "chat",
  "prompt": "解释量子计算的基本原理",
  "priority": 2,
  "model_hint": "gpt-4o",
  "constraints": {
    "max_latency_ms": 5000,
    "preferred_providers": ["openai", "anthropic"],
    "max_cost_per_request": 0.1
  },
  "parameters": {
    "temperature": 0.7,
    "max_tokens": 2048
  },
  "context": [
    {"role": "system", "content": "你是一个专业的科学顾问"},
    {"role": "user", "content": "你好"}
  ],
  "timeout_ms": 30000
}
```

**响应体**:
```json
{
  "request_id": "uuid-xxx",
  "appid": "my-app-001",
  "status": "success",
  "result": {
    "model_name": "openai-gpt4o",
    "model_type": "cloud",
    "provider": "openai",
    "output": "量子计算是利用量子力学原理...",
    "usage": {"prompt_tokens": 50, "completion_tokens": 200, "total_tokens": 250},
    "latency_ms": 1200,
    "cost": 0.00213,
    "finish_reason": "stop"
  },
  "fallback_results": null,
  "error": null,
  "agent_trace": [...],
  "hooks_applied": ["pre:rate_limit", "pre:request_validation", ...],
  "timestamp": "2026-05-25T09:00:00",
  "total_latency_ms": 1250
}
```

### 7.2 POST /dispatch/batch — 批量调度

接受请求数组，并行执行所有调度，返回响应数组。

### 7.3 GET /models — 列出模型

查询参数：`model_type` (可选，local/cloud)

### 7.4 POST /models — 注册模型

请求体为 ModelEndpoint JSON。

### 7.5 DELETE /models/{name} — 注销模型

### 7.6 GET /models/{name}/status — 模型状态

### 7.7 GET /hooks — 列出 Hook

### 7.8 POST /hooks/{name}/enable — 启用 Hook

### 7.9 POST /hooks/{name}/disable — 禁用 Hook

### 7.10 GET /health — 健康检查

---

## 8. 配置说明

### 8.1 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `SCHEDULER_APP_NAME` | OpenClaw Model Scheduler | 应用名称 |
| `SCHEDULER_HOST` | 0.0.0.0 | 监听地址 |
| `SCHEDULER_PORT` | 8000 | 监听端口 |
| `SCHEDULER_LOG_LEVEL` | INFO | 日志级别 |
| `OPENCLAW_GATEWAY_URL` | http://localhost:3000 | OpenClaw 网关地址 |
| `OPENCLAW_API_KEY` | None | OpenClaw API 密钥 |
| `RATE_LIMIT_RPM` | 60 | 全局每分钟请求限制 |
| `RATE_LIMIT_PER_APPID` | 30 | 每 appid 每分钟请求限制 |
| `MAX_RETRIES` | 2 | 最大重试次数 |
| `DEFAULT_TIMEOUT_MS` | 30000 | 默认超时时间 |

---

## 9. 使用示例

### 9.1 启动服务

```bash
cd /Users/yangxu/MyWork/Multi-Agent
source venv/bin/activate
python run.py
```

### 9.2 发送调度请求（本地优先）

```bash
curl -X POST http://localhost:8000/dispatch \
  -H "Content-Type: application/json" \
  -d '{
    "appid": "app-001",
    "type": "chat",
    "prompt": "Hello",
    "priority": 1,
    "constraints": {"require_local": true}
  }'
```

### 9.3 发送调度请求（云端模型）

```bash
curl -X POST http://localhost:8000/dispatch \
  -H "Content-Type: application/json" \
  -d '{
    "appid": "app-002",
    "type": "chat",
    "prompt": "分析这段代码",
    "model_hint": "gpt-4o",
    "constraints": {
      "preferred_providers": ["openai"],
      "max_cost_per_request": 0.05
    }
  }'
```

### 9.4 注册新模型端点

```bash
curl -X POST http://localhost:8000/models \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-custom-model",
    "model_type": "local",
    "provider": "ollama",
    "base_url": "http://localhost:11434/v1",
    "model_id": "my-model",
    "max_context_length": 4096,
    "priority": 3,
    "weight": 2,
    "max_concurrent": 5
  }'
```

### 9.5 动态管理 Hook

```bash
# 列出所有 Hook
curl http://localhost:8000/hooks

# 禁用限流 Hook
curl -X POST http://localhost:8000/hooks/rate_limit/disable

# 启用限流 Hook
curl -X POST http://localhost:8000/hooks/rate_limit/enable
```

---

## 10. 调度策略选择指南

| 场景 | 推荐策略 | 原因 |
|------|----------|------|
| 数据隐私要求高 | require_local=True | 确保数据不出本地 |
| 低延迟要求 | priority=CRITICAL + 本地模型 | 本地模型无网络开销 |
| 成本敏感 | priority=LOW + 免费本地模型 | 自动选择零成本端点 |
| 高质量要求 | model_hint 指定高端模型 | 精确路由到目标模型 |
| 高并发场景 | 默认策略 (Power of Two) | 兼顾负载均衡和随机性 |
| 专用任务 | type=embedding/image/audio | 按 tags 匹配专用端点 |
