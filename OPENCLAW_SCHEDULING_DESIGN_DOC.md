# OpenClaw 多智能体模型调度系统 — 技术设计说明书

> **版本**: 2026.4.27  
> **日期**: 2026-05-27  
> **说明**: 本文档基于代码实际实现编写，覆盖整体架构、各分模块详细设计、调用关系、上下游输入输出接口及实现技术细节。原有 `TECHNICAL_SPEC.md` 和 `SPECIFICATION.md` 保留不变。

---

## 目录

1. [系统概述](#1-系统概述)
2. [整体架构设计](#2-整体架构设计)
3. [模块详细设计](#3-模块详细设计)
4. [调用关系与数据流](#4-调用关系与数据流)
5. [上下游接口定义](#5-上下游接口定义)
6. [核心算法与实现细节](#6-核心算法与实现细节)
7. [配置体系](#7-配置体系)
8. [部署与运维](#8-部署与运维)
9. [可扩展性设计](#9-可扩展性设计)
10. [变更记录](#10-变更记录)

---

## 1. 系统概述

OpenClaw 多智能体模型调度系统是一个基于 **FastAPI + Node.js** 的双路径 AI 模型调度框架，构建于 OpenClaw AI Agent 框架（v2026.4.27）之上。核心能力包括：

- **双路径调度架构**：Python 直连路径（快速响应）与 Bridge→Gateway 路径（Agent 工具链支持）
- **6 种自适应调度策略**：LOCAL_FIRST、CLOUD_FIRST、COST_OPTIMIZED、LATENCY_OPTIMIZED、CAPABILITY_OPTIMIZED、PRIVACY_FIRST
- **智能 Fallback 机制**：仅在主请求失败/超时时触发降级，避免不必要的冗余调用
- **Hook 管道**：Pre-Hook（限流、校验、约束增强、日志）与 Post-Hook（脱敏、计费、重试决策、日志）
- **OpenClaw Gateway 集成**：Bridge 通过 Gateway 的 `/v1/chat/completions` 端点路由到 OpenClaw Agent
- **实时监控 Dashboard**：支持双路径对比测试、Agent 选择、调度历史查看
- **自适应权重动态调整**：基于请求结果 EMA 更新 local/cloud 权重，自动故障切换

### 技术栈

| 层级 | 技术 | 版本要求 |
|------|------|---------|
| AI 编排框架 | OpenClaw (npm) | 2026.4.27 |
| Web 框架 | FastAPI | ≥0.100.0 |
| ASGI 服务器 | Uvicorn | ≥0.20.0 |
| HTTP 客户端 | httpx | ≥0.24.0 |
| 数据校验 | Pydantic | ≥2.0.0 |
| Bridge 服务 | Node.js (ESM) | ≥18.0.0 |
| 本地推理 | Ollama | ≥0.1.0 |
| Python | CPython | ≥3.9 |

---

## 2. 整体架构设计

### 2.1 架构总览图

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        客户端 (Browser / API Client)                      │
│                              │                                            │
│                    ┌─────────▼──────────┐                                 │
│                    │  Dashboard (HTML)   │                                 │
│                    │  :8000/             │                                 │
│                    └─────────┬──────────┘                                 │
└──────────────────────────────┼───────────────────────────────────────────┘
                               │ HTTP
┌──────────────────────────────▼───────────────────────────────────────────┐
│                    FastAPI 调度服务 (:8000)                                │
│  ┌────────────────────────────────────────────────────────────────────┐   │
│  │                   MainDispatcherAgent                              │   │
│  │  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌───────────┐       │   │
│  │  │ Pre-Hook │  │ Strategy │  │   Agent   │  │ Post-Hook │       │   │
│  │  │ Pipeline │→│  Router  │→│  Executor  │→│ Pipeline  │       │   │
│  │  └──────────┘  └──────────┘  └───────────┘  └───────────┘       │   │
│  │       │              │              │              │               │   │
│  │       ▼              ▼              ▼              ▼               │   │
│  │  ┌──────────────────────────────────────────────────────────┐     │   │
│  │  │              ModelRegistry (端点注册表)                   │     │   │
│  │  │  ┌─────────────┐ ┌─────────────┐ ┌───────────────┐      │     │   │
│  │  │  │ ollama-qwen │ │  kimi-k2.6  │ │ deepseek-chat │      │     │   │
│  │  │  │   (local)   │ │   (cloud)   │ │    (cloud)    │      │     │   │
│  │  │  └─────────────┘ └─────────────┘ └───────────────┘      │     │   │
│  │  └──────────────────────────────────────────────────────────┘     │   │
│  └────────────────────────────────────────────────────────────────────┘   │
│                               │                                            │
│              ┌────────────────┼────────────────┐                           │
│              ▼ (路径1)        ▼ (路径2)         │                           │
│     ┌────────────────┐ ┌──────────────────┐    │                           │
│     │  Python 直连    │ │ OpenClawAgent    │    │                           │
│     │  LocalAgent    │ │ → Bridge :3001   │    │                           │
│     │  CloudAgent    │ │   → Gateway :3000│    │                           │
│     └───────┬────────┘ └────────┬─────────┘    │                           │
└─────────────┼───────────────────┼──────────────┘                           │
              │                   │                                          │
              ▼                   ▼                                          │
┌──────────────────┐  ┌───────────────────────────────────────┐              │
│  模型厂商 API     │  │       OpenClaw Gateway (:3000)        │              │
│  ┌─────────────┐ │  │  ┌─────────────────────────────────┐ │              │
│  │ Ollama      │ │  │  │ /v1/chat/completions            │ │              │
│  │ :11434/v1   │ │  │  │  → openclaw/local-dispatcher    │ │              │
│  └─────────────┘ │  │  │  → openclaw/cloud-dispatcher    │ │              │
│  ┌─────────────┐ │  │  │  → openclaw/code-executor       │ │              │
│  │ Moonshot    │ │  │  └─────────────────────────────────┘ │              │
│  │ api.moonshot│ │  └───────────────────────────────────────┘              │
│  └─────────────┘ │                                                          │
│  ┌─────────────┐ │                                                          │
│  │ DeepSeek    │ │                                                          │
│  │ api.deepseek│ │                                                          │
│  └─────────────┘ │                                                          │
└──────────────────┘                                                          │
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
| 路由标识 | `routed_via_gateway=False` | `routed_via_gateway=True` |

### 2.3 目录结构

```
OpenClaw_Multi_Agent/
├── run.py                          # 启动入口
├── openclaw.json                   # OpenClaw 项目配置（Agent/模型/路由）
├── package.json                    # Node.js 项目配置
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
├── TECHNICAL_SPEC.md               # 原始技术规格书（保留）
├── SPECIFICATION.md                # 原始功能规格书（保留）
└── OPENCLAW_SCHEDULING_DESIGN_DOC.md  # 本文档
```

---

## 3. 模块详细设计

### 3.1 启动与入口模块

#### 3.1.1 run.py

启动入口，通过 uvicorn 运行 FastAPI 应用：

```python
uvicorn.run("scheduler.main:app", host=settings.host, port=settings.port)
```

配置来源于 `Settings` 单例，支持环境变量覆盖。

#### 3.1.2 scheduler/main.py

FastAPI 应用核心，负责生命周期管理、API 端点定义、统计追踪。

**1) 生命周期管理**（`lifespan` 上下文管理器）：

```
启动阶段:
  _init_default_models() → 注册3个默认模型端点
  _init_hooks()          → 注册8个Hook（4 Pre + 4 Post）
  _start_bridge()        → subprocess.Popen 启动 Bridge 子进程
  MainDispatcherAgent()  → 创建主调度代理

关闭阶段:
  dispatcher.close()     → 关闭所有 Agent 的 HTTP 客户端
  _stop_bridge()         → terminate() + 5秒超时后 kill()
```

**2) API 端点定义**：

| 方法 | 路径 | 功能 | 输入 | 输出 |
|------|------|------|------|------|
| POST | `/dispatch` | 单请求调度 | `DispatchRequest` | `DispatchResponse` |
| POST | `/dispatch/batch` | 批量调度 | `List[DispatchRequest]` | `List[DispatchResponse]` |
| GET | `/models` | 列出所有模型端点 | `?model_type=local\|cloud` | 端点信息列表 |
| POST | `/models` | 注册新模型端点 | `ModelEndpoint` | `{"status":"registered"}` |
| DELETE | `/models/{name}` | 注销模型端点 | path: name | `{"status":"unregistered"}` |
| GET | `/models/{name}/status` | 获取端点详细状态 | path: name | 端点状态详情 |
| GET | `/hooks` | 列出所有 Hook | - | Hook 列表 |
| POST | `/hooks/{name}/enable` | 启用 Hook | path: name | `{"status":"enabled"}` |
| POST | `/hooks/{name}/disable` | 禁用 Hook | path: name | `{"status":"disabled"}` |
| GET | `/stats` | 调度统计 + 系统资源 | - | 统计信息 |
| GET | `/history` | 最近调度历史 | `?limit=N` | 历史记录列表 |
| GET | `/health` | 健康检查 | - | 健康状态 |
| GET | `/openclaw/bridge/health` | Bridge 健康检查 | - | Bridge 状态 |
| GET | `/openclaw/bridge/models` | Bridge 模型列表 | `?model_type=...` | 模型列表 |
| GET | `/openclaw/bridge/stats` | Bridge 统计 | - | Bridge 统计信息 |
| POST | `/openclaw/agent/{agent_id}/message` | Agent 消息发送 | prompt, context, parameters | Agent 响应 |
| GET | `/openclaw/adaptive/state` | 自适应状态 | - | 权重/连击信息 |
| POST | `/openclaw/toggle` | 开关 OpenClaw | `?enabled=true\|false` | 开关状态 |
| GET | `/` | Dashboard 页面 | - | HTML 页面 |

**3) 默认模型端点初始化**（`_init_default_models`）：

| 端点名 | 类型 | Provider | 模型ID | 上下文长度 | 工具支持 | 输入成本/1k tokens | 优先级 | 权重 | 最大并发 |
|--------|------|----------|--------|-----------|---------|-------------------|--------|------|---------|
| ollama-qwen2.5 | local | ollama | qwen2.5:3b | 32768 | 否 | $0.00 | 1 | 3 | 5 |
| kimi-k2.6 | cloud | moonshot | kimi-k2.6 | 262144 | 是 | $0.76 | 2 | 2 | 15 |
| deepseek-chat | cloud | deepseek | deepseek-chat | 64000 | 否 | $0.00014 | 3 | 3 | 20 |

**4) Hook 初始化**（`_init_hooks`）：

Pre-Hook（按优先级排序）：
- `rate_limit` (priority=5) — 全局/单应用限流
- `request_validation` (priority=10) — 请求参数校验
- `constraint_enrichment` (priority=20) — 约束自动增强
- `request_logging` (priority=90) — 请求日志记录

Post-Hook（按优先级排序）：
- `response_sanitization` (priority=10) — 响应脱敏
- `cost_calculation` (priority=20) — 成本计算
- `retry_decision` (priority=30) — 重试决策
- `response_logging` (priority=90) — 响应日志记录

**5) Bridge 子进程管理**（`_start_bridge` / `_stop_bridge`）：
- 通过 `subprocess.Popen` 启动 `node bridge/orchestrator.mjs`
- 关闭时先 `terminate()`，5秒超时后 `kill()`
- Bridge 启动失败仅 warning，不阻塞主服务

**6) 统计追踪**（`_stats` 字典）：
- `total_requests` / `success_requests` / `failed_requests` — 请求计数
- `total_latency_ms` — 累计延迟
- `dispatch_history` — 最近50条调度记录（含时间戳）

---

### 3.2 配置管理模块

**文件**: `scheduler/config/settings.py`

采用**单例模式**，通过环境变量注入配置：

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|---------|--------|------|
| app_name | SCHEDULER_APP_NAME | "OpenClaw Model Scheduler" | 应用名称 |
| host | SCHEDULER_HOST | "0.0.0.0" | 监听地址 |
| port | SCHEDULER_PORT | 8000 | 监听端口 |
| log_level | SCHEDULER_LOG_LEVEL | "INFO" | 日志级别 |
| openclaw_gateway_url | OPENCLAW_GATEWAY_URL | "http://localhost:3000" | Gateway URL |
| openclaw_api_key | OPENCLAW_API_KEY | None | API 密钥 |
| rate_limit_rpm | RATE_LIMIT_RPM | 60 | 全局每分钟限流 |
| rate_limit_per_appid | RATE_LIMIT_PER_APPID | 30 | 单应用每分钟限流 |
| max_retries | MAX_RETRIES | 2 | 最大重试次数 |
| default_timeout_ms | DEFAULT_TIMEOUT_MS | 30000 | 默认超时(ms) |

单例获取方式：
```python
settings = get_settings()  # 返回 Settings 唯一实例
```

---

### 3.3 请求模型层

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

### 3.4 响应模型层

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
| timestamp | str | 响应时间戳（ISO格式） |
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
| routed_via_gateway | bool | 是否经过 Gateway 路由（默认 False） |
| actual_model | Optional[str] | Gateway 实际使用的模型ID |

#### ErrorDetail

| 字段 | 类型 | 说明 |
|------|------|------|
| code | str | 错误码 |
| message | str | 错误消息 |
| agent | Optional[str] | 出错的 Agent |
| retryable | bool | 是否可重试（默认 True） |

---

### 3.5 模型注册与端点管理

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
| metadata | Optional[Dict] | None | 附加元数据 |

计算属性与方法：

| 方法/属性 | 返回类型 | 说明 |
|-----------|---------|------|
| `is_available` | bool | `enabled and current_load < max_concurrent` |
| `load_factor` | float | `current_load / max_concurrent` |
| `increment_load()` | void | `current_load += 1` |
| `decrement_load()` | void | `current_load -= 1`（不低于0） |
| `update_latency(ms)` | void | EMA更新：`0.7 * old + 0.3 * new` |
| `update_success_rate(ok)` | void | EMA更新：`0.95 * old + 0.05 * (1 or 0)` |

#### ModelRegistry

端点注册表，管理所有模型端点的生命周期：

| 方法 | 签名 | 说明 |
|------|------|------|
| `register` | `(endpoint: ModelEndpoint) -> None` | 注册端点（按name去重） |
| `unregister` | `(name: str) -> None` | 注销端点 |
| `get` | `(name: str) -> Optional[ModelEndpoint]` | 按名称查询 |
| `get_by_type` | `(model_type: ModelType) -> List[ModelEndpoint]` | 按类型过滤（仅enabled） |
| `get_by_provider` | `(provider: ModelProvider) -> List[ModelEndpoint]` | 按供应商过滤（仅enabled） |
| `get_available` | `() -> List[ModelEndpoint]` | 所有可用端点（enabled + 未满载） |
| `get_local_available` | `() -> List[ModelEndpoint]` | 可用的本地端点 |
| `get_cloud_available` | `() -> List[ModelEndpoint]` | 可用的云端端点 |
| `list_all` | `() -> List[ModelEndpoint]` | 所有端点（含disabled） |

---

### 3.6 Agent 执行层

#### 3.6.1 BaseAgent（基类）

**文件**: `scheduler/agents/base_agent.py`

所有 Agent 的抽象基类，提供统一的 HTTP 调用、负载追踪、响应解析能力。

```
BaseAgent (ABC)
├── name: str                    # Agent 名称
├── registry: ModelRegistry      # 端点注册表引用
├── _client: httpx.AsyncClient   # 懒初始化的 HTTP 客户端
│
├── execute(request, endpoint)   # 抽象方法：执行请求
├── can_handle(request)          # 抽象方法：判断能否处理
├── call_model_api(endpoint, request)  # 通用模型 API 调用
├── execute_with_fallback(request, endpoints, max_retries)  # 带降级的执行
├── _build_payload(endpoint, request)  # 构建请求体
├── _build_headers(endpoint)     # 构建请求头
├── _parse_response(data, endpoint, latency_ms)  # 解析响应
├── get_client()                 # 获取/创建 HTTP 客户端
└── close()                      # 关闭 HTTP 客户端
```

**call_model_api 核心流程**：

```
1. endpoint.increment_load()        → 递增负载计数
2. _build_payload(endpoint, request) → 构建 OpenAI 兼容请求体
3. POST {base_url}/chat/completions  → 直接调用厂商 API
4. _parse_response(data)             → 解析响应、计算成本
5. endpoint.update_latency()         → EMA 更新延迟
6. endpoint.update_success_rate()    → EMA 更新成功率
7. endpoint.decrement_load()         → 递减负载计数 (finally)
```

请求体格式（OpenAI 兼容）：
```json
{
  "model": "<model_id>",
  "messages": [
    {"role": "context_msg_1", "content": "..."},
    {"role": "user", "content": "<prompt>"}
  ],
  "max_tokens": 1024,
  "stream": false
}
```

响应解析特殊处理：
- 支持 `reasoning_content` 字段（DeepSeek R1 等），当 `content` 为空时回退到 `reasoning_content`
- 成本计算：`input_cost + output_cost`，基于端点配置的 token 单价

#### 3.6.2 LocalModelAgent

**文件**: `scheduler/agents/local_agent.py`

本地模型代理，处理 LOCAL 类型端点的请求。

```
LocalModelAgent(BaseAgent)
├── name = "local_model_agent"
├── can_handle(request) → 检查是否有可用本地端点
├── execute(request, endpoint) → 调用 call_model_api
├── _select_endpoint(request) → 选择最佳本地端点
└── get_available_endpoints() → 返回可用本地端点列表
```

**端点选择策略**（`_select_endpoint`）：
1. 若有 `model_hint`，优先匹配名称/模型ID
2. 若有 `preferred_providers`，优先匹配供应商
3. 按 `(load_factor, avg_latency_ms)` 排序选择最优

#### 3.6.3 CloudModelAgent

**文件**: `scheduler/agents/cloud_agent.py`

云端模型代理，处理 CLOUD 类型端点的请求。

```
CloudModelAgent(BaseAgent)
├── name = "cloud_model_agent"
├── can_handle(request) → 检查是否有可用云端端点
├── execute(request, endpoint) → 调用 call_model_api
├── _select_endpoint(request) → 选择最佳云端端点
└── get_available_endpoints() → 返回可用云端端点列表
```

**端点选择策略**（`_select_endpoint`）：
1. 若有 `model_hint`，优先匹配名称/模型ID
2. 若有 `preferred_providers`，优先匹配供应商
3. 若有 `excluded_providers`，排除指定供应商
4. 若有 `max_cost_per_request`，过滤超出预算的端点
5. 按 `(priority, load_factor, avg_latency_ms)` 排序选择最优

#### 3.6.4 OpenClawAgent

**文件**: `scheduler/agents/openclaw_agent.py`

OpenClaw Bridge 通信代理，负责与 Bridge 服务交互，实现 Gateway 路径调度。

```
OpenClawAgent(BaseAgent)
├── name = "openclaw_agent"
├── bridge_url: str               # Bridge 服务地址 (默认 http://localhost:3001)
│
├── execute(request, endpoint)    # 双路径执行
│   ├── endpoint != None → call_model_api (直连)
│   └── endpoint == None → _dispatch_via_bridge → _fallback_direct
│
├── _dispatch_via_bridge(request) # Bridge 路径调度
├── _fallback_direct(request)     # Bridge 失败后直连降级
├── _select_best(candidates, request) # 选择最优端点
├── _build_bridge_payload(request) # 构建 Bridge 请求体
│
├── send_agent_message(agent_id, prompt, ...) # Agent 消息发送
├── get_bridge_health()           # Bridge 健康检查
├── get_bridge_models(model_type) # Bridge 模型列表
└── get_bridge_stats()            # Bridge 统计信息
```

**execute 执行逻辑**：
```
if endpoint is not None:
    → call_model_api(endpoint, request)   # 直连模式
else:
    → _dispatch_via_bridge(request)        # Bridge 模式
    → 若 Bridge 失败 → _fallback_direct   # 降级到直连
```

**_dispatch_via_bridge 流程**：
```
1. POST {bridge_url}/dispatch
2. 解析 Bridge 响应
3. 成功 → 返回 ModelResult
4. 失败 → 返回 None（触发 _fallback_direct）
```

**_fallback_direct 流程**：
```
1. 获取所有可用端点
2. 若 require_local → 优先本地
3. 否则 → 本地优先，云端兜底
4. 调用 call_model_api 直连
```

#### 3.6.5 MainDispatcherAgent

**文件**: `scheduler/agents/main_agent.py`

主调度代理，编排所有子代理，是整个调度系统的核心入口。

```
MainDispatcherAgent
├── registry: ModelRegistry
├── local_agent: LocalModelAgent
├── cloud_agent: CloudModelAgent
├── openclaw_agent: OpenClawAgent
├── strategy_router: StrategyRouter
├── hook_manager: HookManager
├── use_openclaw: bool
├── _agent_trace: List[Dict]      # 执行追踪链
│
├── dispatch(request) → DispatchResponse       # 主调度入口
├── _dispatch_openclaw(request, ...) → Optional[DispatchResponse]  # OpenClaw 路径
├── dispatch_agent_message(agent_id, ...) → Dict  # Agent 消息分发
├── _try_fallbacks(request, agent, endpoints) → List[ModelResult]  # 降级尝试
├── _get_agent(agent_type) → Optional[BaseAgent]  # 获取子代理
├── _trace(agent, message) → void                # 追踪记录
├── _build_error_response(...) → DispatchResponse # 构建错误响应
└── close() → void                               # 关闭所有子代理
```

**dispatch 主流程**：

```
1. 生成 request_id
2. 执行 Pre-Hook 管道
3. 若 use_openclaw:
   a. 调用 _dispatch_openclaw
   b. 若成功 → 返回响应
   c. 若失败 → 继续步骤4
4. 调用 strategy_router.route(request) → RoutingDecision
5. 获取对应 Agent (local/cloud)
6. 执行 agent.execute(request, endpoint)
7. 记录自适应结果
8. fallback_results = None（成功时不触发降级）
9. 执行 Post-Hook 管道
10. 返回 DispatchResponse(SUCCESS)

异常处理:
- TimeoutError → 尝试 fallback_endpoints → PARTIAL/TIMEOUT
- Exception   → 尝试 fallback_endpoints → PARTIAL/FAILED
```

**Fallback 机制**（仅在主请求失败时触发）：
```
主请求成功 → fallback_results = None（不执行降级）
主请求超时 → _try_fallbacks(fallback_endpoints[:2])
主请求异常 → _try_fallbacks(fallback_endpoints[:2])
降级成功 → status=PARTIAL
降级失败 → status=TIMEOUT/FAILED
```

**_dispatch_openclaw 流程**：
```
1. 调用 strategy_router.route(request) → RoutingDecision
2. 调用 openclaw_agent.execute(request, endpoint)
3. 成功 → 记录自适应结果 → 返回 DispatchResponse(SUCCESS)
4. 失败 → 记录失败 → 返回 None（回退到直连路径）
```

---

### 3.7 策略调度层

#### 3.7.1 StrategyRouter（策略路由器）

**文件**: `scheduler/strategy/router.py`

策略路由器，根据请求特征选择最优调度策略和端点。

```
StrategyRouter
├── registry: ModelRegistry
├── priority_strategy: PriorityStrategy
├── load_balance_strategy: LoadBalanceStrategy
├── adaptive_strategy: AdaptiveStrategy
├── use_openclaw: bool
│
├── route(request) → RoutingDecision        # 主路由入口
├── _route_adaptive(request) → RoutingDecision  # 自适应路由
├── _route_local_first_adaptive(request) → RoutingDecision
├── _route_cloud_first_adaptive(request) → RoutingDecision
├── _route_latency_optimized(request) → RoutingDecision
├── _route_cost_optimized(request) → RoutingDecision
├── _route_capability_optimized(request) → RoutingDecision
├── _route_local(request, ...) → RoutingDecision
├── _route_specialized(request) → RoutingDecision
├── _route_default(request) → RoutingDecision
└── record_adaptive_result(agent_type, success, latency_ms)  # 记录结果
```

**RoutingDecision 数据结构**：

| 字段 | 类型 | 说明 |
|------|------|------|
| agent_type | str | "local" / "cloud" |
| selected_endpoint | Optional[ModelEndpoint] | 选中的端点 |
| fallback_endpoints | List[ModelEndpoint] | 降级端点列表 |
| strategy_name | str | 策略名称 |
| reason | str | 选择原因 |
| use_openclaw | bool | 是否使用 OpenClaw |

**路由决策流程**：

```
route(request)
├── use_openclaw=True → _route_adaptive(request)
│   ├── AdaptiveStrategy.route(request) → (agent_type, strategy)
│   ├── PRIVACY_FIRST → _route_local()
│   ├── LOCAL_FIRST → _route_local_first_adaptive()
│   ├── CLOUD_FIRST → _route_cloud_first_adaptive()
│   ├── LATENCY_OPTIMIZED → _route_latency_optimized()
│   ├── COST_OPTIMIZED → _route_cost_optimized()
│   └── CAPABILITY_OPTIMIZED → _route_capability_optimized()
└── use_openclaw=False → _route_legacy(request)
```

**各策略路由详细逻辑**：

**LOCAL_FIRST**：
1. 优先选择本地端点（Power-of-Two 负载均衡）
2. Fallback: 本地其余端点 + 云端前2个
3. 无本地可用 → 降级到云端（Least-Connections）

**CLOUD_FIRST**：
1. 若请求类型为 TOOL_CALL → 优先选择支持工具的云端端点
2. 否则按优先级选择云端端点
3. Fallback: 云端其余端点 + 本地前1个
4. 无云端可用 → 降级到本地

**LATENCY_OPTIMIZED**：
1. 合并所有端点，选择 `avg_latency_ms` 最低的
2. Fallback: 按延迟排序的前3个
3. 无延迟数据 → 默认本地优先

**COST_OPTIMIZED**：
1. 优先本地端点（零成本）
2. 无本地 → 选择成本低于阈值的云端端点
3. 无低成本云端 → 选择最便宜的云端端点

**CAPABILITY_OPTIMIZED**：
1. TOOL_CALL → 选择支持工具的云端端点
2. 长上下文（>16000 tokens）→ 选择大上下文窗口端点
3. 特殊类型（EMBEDDING/IMAGE/AUDIO）→ 专用路由
4. 本地能力足够 → 本地优先
5. 否则 → 云端优先

**PRIVACY_FIRST**：
1. 强制本地端点
2. 无本地可用 → 降级到云端（带警告）

#### 3.7.2 AdaptiveStrategy（自适应策略）

**文件**: `scheduler/strategy/adaptive.py`

根据请求特征和历史表现动态选择调度策略。

```
AdaptiveStrategy
├── LOCAL_FIRST = "local_first"
├── CLOUD_FIRST = "cloud_first"
├── COST_OPTIMIZED = "cost_optimized"
├── LATENCY_OPTIMIZED = "latency_optimized"
├── CAPABILITY_OPTIMIZED = "capability_optimized"
├── PRIVACY_FIRST = "privacy_first"
│
├── _latency_threshold_ms: int = 3000
├── _cost_threshold: float = 0.01
├── _local_success_streak: int = 0
├── _cloud_success_streak: int = 0
├── _local_fail_streak: int = 0
├── _cloud_fail_streak: int = 0
├── _adaptive_weights: Dict = {"local": 0.6, "cloud": 0.4}
│
├── route(request) → (agent_type, strategy)
├── _classify_request(request) → strategy_name
├── _select_strategy(decision, request) → strategy_name
├── _determine_agent_type(strategy, request) → agent_type
├── record_result(model_type, success, latency_ms)
├── _update_adaptive_weights(model_type, success, latency_ms)
├── _can_local_handle(request, local_eps) → bool
├── _estimate_context_length(request) → int
└── get_adaptive_state() → Dict
```

**请求分类决策树**（`_classify_request`）：

```
request.constraints.require_local?     → PRIVACY_FIRST
request.constraints.require_gpu?       → CAPABILITY_OPTIMIZED
request.constraints.preferred_providers 且匹配云端? → CLOUD_FIRST
request.type in {tool_call, image, audio}? → CAPABILITY_OPTIMIZED
request.type == EMBEDDING?             → CAPABILITY_OPTIMIZED
request.priority in {CRITICAL, HIGH}?  → LATENCY_OPTIMIZED
request.priority in {LOW, BACKGROUND}? → COST_OPTIMIZED
request.constraints.max_cost <= 0?     → COST_OPTIMIZED
request.constraints.max_latency <= 1000? → LATENCY_OPTIMIZED
context_length > 16000?               → CAPABILITY_OPTIMIZED
默认                                   → LOCAL_FIRST
```

**自适应权重更新算法**（EMA，α=0.1）：

```
本地成功且延迟 ≤ 阈值:
  local_weight = min(1.0, local_weight + 0.1)
  cloud_weight = max(0.0, cloud_weight - 0.1)

本地失败:
  local_weight = max(0.1, local_weight - 0.2)
  cloud_weight = min(0.9, cloud_weight + 0.2)

云端成功:
  cloud_weight = min(0.9, cloud_weight + 0.05)
  local_weight = max(0.1, local_weight - 0.05)

云端失败:
  cloud_weight = max(0.1, cloud_weight - 0.1)
  local_weight = min(0.9, local_weight + 0.1)
```

**故障切换机制**：
- 本地连续失败 ≥ 3 次 → 自动切换到 CLOUD_FIRST
- 权重范围约束：`[0.1, 0.9]`，避免极端偏向

#### 3.7.3 PriorityStrategy（优先级评分策略）

**文件**: `scheduler/strategy/priority.py`

基于多维评分的端点选择策略。

**评分公式**：

```
初始分 = 100.0

扣分项:
- 端点优先级: -(priority - 1) * 15.0
- 负载因子: -load_factor * 20.0

加分项:
- 成功率: +success_rate * 10.0
- 延迟在约束内: +(1 - latency_ratio) * 15.0

约束匹配:
- require_local + 本地端点: +30.0
- require_local + 云端端点: -100.0
- CRITICAL + 本地低延迟: +20.0
- LOW/BACKGROUND + 零成本: +15.0
- model_hint 匹配名称: +25.0
- model_hint 匹配模型ID: +20.0
- preferred_providers 匹配: +20.0
- excluded_providers 匹配: -100.0
- 延迟超出约束: -50.0
```

#### 3.7.4 LoadBalanceStrategy（负载均衡策略）

**文件**: `scheduler/strategy/load_balance.py`

提供5种负载均衡算法：

| 算法 | 常量 | 说明 |
|------|------|------|
| ROUND_ROBIN | `round_robin` | 轮询，按顺序分配 |
| WEIGHTED_RANDOM | `weighted_random` | 加权随机，按 weight 概率分配 |
| LEAST_CONNECTIONS | `least_connections` | 最少连接，选择 current_load 最低的 |
| LEAST_LATENCY | `least_latency` | 最低延迟，选择 avg_latency_ms 最低的 |
| POWER_OF_TWO | `power_of_two` | 二选一，随机选2个取负载低的 |

**Power-of-Two 算法**：
```
1. 从候选列表随机选2个端点
2. 比较 current_load
3. 返回负载较低的那个
```

**distribute 方法**：按 `(load_factor, avg_latency_ms)` 排序，返回前 N 个端点用于 fallback。

---

### 3.8 Hook 管道层

#### 3.8.1 HookManager（Hook 管理器）

**文件**: `scheduler/hooks/hook_manager.py`

管理 Pre-Hook 和 Post-Hook 的注册、排序、执行。

```
HookManager
├── _pre_hooks: List[PreHook]     # 按 priority 升序排列
├── _post_hooks: List[PostHook]   # 按 priority 升序排列
├── _applied_hooks: List[str]     # 本次请求已应用的 Hook
│
├── register_pre_hook(hook)       # 注册并排序
├── register_post_hook(hook)      # 注册并排序
├── unregister_hook(name)         # 注销
├── execute_pre_hooks(request) → request  # 执行 Pre-Hook 管道
├── execute_post_hooks(request, result) → result  # 执行 Post-Hook 管道
├── get_applied_hooks() → List[str]
├── list_hooks() → Dict
├── enable_hook(name)
└── disable_hook(name)
```

**执行规则**：
- Hook 按 `priority` 升序执行（数字小优先级高）
- `enabled=False` 的 Hook 跳过
- Hook 执行失败时，`priority ≤ 50` 的关键 Hook 抛出异常中断管道
- `priority > 50` 的非关键 Hook 仅记录错误，不中断

#### 3.8.2 Pre-Hook 实现

**文件**: `scheduler/hooks/pre_hook.py`

| Hook | 优先级 | 功能 | 关键逻辑 |
|------|--------|------|---------|
| RateLimitHook | 5 | 限流 | 全局60rpm + 单应用30rpm，滑动窗口 |
| RequestValidationHook | 10 | 校验 | appid/prompt 非空，timeout≥1000ms，max_latency≥100ms |
| ConstraintEnrichmentHook | 20 | 约束增强 | 按 appid 填充默认约束，NORMAL 及以下自动设 max_cost=0.05 |
| RequestLoggingHook | 90 | 日志 | 记录 appid/type/priority/prompt_len |

**RateLimitHook 滑动窗口算法**：
```
1. 清理超过60秒的请求时间戳
2. 检查全局请求数是否超过 max_rpm
3. 检查单 appid 请求数是否超过 max_per_appid
4. 通过 → 记录当前时间戳
5. 超限 → 抛出 RuntimeError
```

**ConstraintEnrichmentHook 增强逻辑**：
```
1. 查找 appid 对应的默认配置
2. 填充 request.constraints 中为 None 的字段
3. 若 priority ≥ NORMAL(3) 且 max_cost_per_request 为 None → 设为 0.05
```

#### 3.8.3 Post-Hook 实现

**文件**: `scheduler/hooks/post_hook.py`

| Hook | 优先级 | 功能 | 关键逻辑 |
|------|--------|------|---------|
| ResponseSanitizationHook | 10 | 脱敏 | 正则替换 api_key/password/token/secret |
| CostCalculationHook | 20 | 计费 | 若 cost 为 None，按 usage 估算 |
| RetryDecisionHook | 30 | 重试决策 | finish_reason=error 或 output 为空时标记重试 |
| ResponseLoggingHook | 90 | 日志 | 记录 model/provider/latency/cost/output_len |

**ResponseSanitizationHook 脱敏规则**：
```
匹配模式:
- api_key=xxx / api-key:xxx  → 替换为 [REDACTED]
- password=xxx               → 替换为 [REDACTED]
- token=xxx                  → 替换为 [REDACTED]
- secret=xxx                 → 替换为 [REDACTED]
```

**CostCalculationHook 估算公式**：
```
estimated_cost = (prompt_tokens / 1000) * 0.001 + (completion_tokens / 1000) * 0.002
```

**RetryDecisionHook 重试逻辑**：
```
if finish_reason == "error" or output is None/empty:
    retry_count = _retry_count.get(request_id, 0)
    if retry_count < max_retries (默认2):
        标记重试，计数+1
    else:
        超过最大重试次数，清除计数
else:
    清除重试计数
```

---

### 3.9 Bridge 桥接服务

**文件**: `bridge/orchestrator.mjs`

Node.js 实现的 Bridge 服务，桥接 Python 调度器与 OpenClaw Gateway。

#### 3.9.1 核心配置

```javascript
OPENCLAW_GATEWAY_URL = process.env.OPENCLAW_GATEWAY_URL || "http://localhost:3000"
BRIDGE_PORT = parseInt(process.env.BRIDGE_PORT || "3001", 10)
USE_OPENCLAW_GATEWAY = process.env.USE_OPENCLAW_GATEWAY !== "false"
```

#### 3.9.2 模型目录（MODEL_CATALOG）

| ID | Provider | 模型ID | 类型 | 上下文长度 | 工具支持 | 成本/1k input |
|----|----------|--------|------|-----------|---------|--------------|
| ollama/qwen2.5:3b | ollama | qwen2.5:3b | local | 32768 | 否 | $0.00 |
| moonshot/kimi-k2.6 | moonshot | kimi-k2.6 | cloud | 262144 | 是 | $0.76 |
| deepseek/deepseek-chat | deepseek | deepseek-chat | cloud | 64000 | 否 | $0.00014 |

#### 3.9.3 Gateway 路由映射（ENDPOINT_TO_AGENT）

```javascript
const ENDPOINT_TO_AGENT = {
  "ollama/qwen2.5:3b": "openclaw/local-dispatcher",
  "moonshot/kimi-k2.6": "openclaw/cloud-dispatcher",
  "deepseek/deepseek-chat": "openclaw/code-executor",
};
```

**resolveGatewayModel(endpoint)**：
- 查找 ENDPOINT_TO_AGENT 映射
- 未找到 → 默认返回 `"openclaw"`

#### 3.9.4 自适应路由（adaptiveRoute）

Bridge 端的独立自适应路由逻辑，与 Python 端策略对齐：

```
1. require_local → 本地优先
2. preferred_providers → 匹配供应商
3. 高优先级 (≤2) → 本地优先（低延迟）
4. 复杂类型 (tool_call/image/audio) → 云端优先
5. 默认 → 本地优先（localFirst=true）
6. 无本地 → 云端兜底
```

**selectBestModel 评分算法**：
```
初始分 = 100.0
- (priority - 1) * 15.0     → 优先级扣分
- loadFactor * 20.0          → 负载扣分
+ successRate * 10.0         → 成功率加分
- 50.0 (if 超出延迟约束)     → 延迟惩罚
```

#### 3.9.5 callModelApi 核心流程

```
1. endpoint.currentLoad++
2. 构建 messages 数组
3. 构建 payload:
   - model = USE_OPENCLAW_GATEWAY ? resolveGatewayModel(endpoint) : endpoint.modelId
   - messages, max_tokens: 1024
4. 确定 targetUrl:
   - USE_OPENCLAW_GATEWAY → ${OPENCLAW_GATEWAY_URL}/v1/chat/completions
   - 否则 → ${endpoint.baseUrl}/chat/completions
5. fetch(targetUrl, {POST, headers, body, signal})
6. 解析响应，计算成本
7. updateEndpointState(modelId, latencyMs, success)
8. endpoint.currentLoad-- (finally)
```

**Gateway 模式 vs 直连模式**：

| 维度 | Gateway 模式 | 直连模式 |
|------|-------------|---------|
| targetUrl | `localhost:3000/v1/chat/completions` | `{baseUrl}/chat/completions` |
| model 参数 | `openclaw/local-dispatcher` 等 | `qwen2.5:3b` 等 |
| 超时 | 120秒 | 30秒 |
| routed_via_gateway | true | false |

#### 3.9.6 dispatchViaOpenClaw 流程

```
1. adaptiveRoute(request) → routing decision
2. 若无可用端点 → 返回 failed
3. callModelApi(selected, request)
4. 成功 → 返回 success（fallback_results=null）
5. 失败 → 遍历 fallback_endpoints[:2]
   a. callModelApi(fb, request)
   b. 成功 → 返回 success（标记 fallback）
   c. 失败 → 继续下一个
6. 全部失败 → 返回 failed
```

#### 3.9.7 handleOpenClawAgentMessage 流程

```
1. 查找 openclaw.json 中的 agent 配置
2. 获取 agent 的 primary model
3. 在 MODEL_CATALOG 中查找对应端点
4. callModelApi(endpoint, request)
5. 若失败且有 fallbacks → 依次尝试
6. 返回结果
```

#### 3.9.8 Bridge API 端点

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/dispatch` | 请求调度 |
| POST | `/agent/message` | Agent 消息发送 |
| GET | `/models` | 模型列表 |
| GET | `/routing/config` | 路由配置 |
| GET | `/agents` | Agent 列表 |
| GET | `/health` | 健康检查 |
| GET | `/stats` | 统计信息 |

#### 3.9.9 端点状态管理

```javascript
endpointState[modelId] = {
  currentLoad: 0,
  maxConcurrent: 5 | 20,
  avgLatencyMs: 0,
  successRate: 1.0,
  totalRequests: 0,
  failedRequests: 0,
}
```

**更新算法**（EMA）：
```
avgLatencyMs = 0.7 * old + 0.3 * new
successRate = 0.95 * old + 0.05 * (1 or 0)
```

---

### 3.10 前端监控面板

**文件**: `static/dashboard.html`

单文件 HTML 应用，提供实时监控和交互测试能力。

#### 功能模块

| 模块 | 位置 | 功能 |
|------|------|------|
| 模型状态面板 | 左侧 | 显示所有端点的负载、延迟、成功率 |
| 自适应权重 | 左侧 | 显示 local/cloud 权重条 |
| Bridge 状态 | 左侧 | Bridge 连接状态和配置 |
| 请求表单 | 中部 | 构建并发送调度请求 |
| 调度结果 | 中部 | 显示调度卡片（策略、模型、延迟、追踪） |
| 日志面板 | 右侧 | 实时日志流 |

#### 请求表单字段

| 字段 | 控件 | 说明 |
|------|------|------|
| 路由模式 | select | Python直连 / Bridge→Gateway |
| Agent | select | local-dispatcher / cloud-dispatcher / code-executor |
| 请求类型 | select | chat / completion / embedding / image / audio / tool_call |
| 优先级 | select | CRITICAL / HIGH / NORMAL / LOW / BACKGROUND |
| 应用ID | input | 应用标识 |
| Prompt | textarea | 输入文本 |

#### 特殊按钮

| 按钮 | 功能 |
|------|------|
| 🚀 发送请求 | 发送单次调度请求 |
| 🔀 双路径对比测试 | 同时发送 Python直连 和 Bridge→Gateway 请求，对比结果 |
| 🔄 刷新状态 | 刷新模型状态和自适应权重 |

---

## 4. 调用关系与数据流

### 4.1 完整调度链路（Python 直连路径）

```
Client
  │ POST /dispatch
  ▼
FastAPI (main.py)
  │ dispatcher.dispatch(request)
  ▼
MainDispatcherAgent.dispatch()
  │
  ├─1. hook_manager.execute_pre_hooks(request)
  │     ├─ RateLimitHook.execute(request)
  │     ├─ RequestValidationHook.execute(request)
  │     ├─ ConstraintEnrichmentHook.execute(request)
  │     └─ RequestLoggingHook.execute(request)
  │
  ├─2. strategy_router.route(request)
  │     ├─ AdaptiveStrategy.route(request)
  │     │   ├─ _classify_request(request) → strategy
  │     │   ├─ _select_strategy(strategy, request) → strategy
  │     │   └─ _determine_agent_type(strategy, request) → agent_type
  │     └─ StrategyRouter._route_xxx(request) → RoutingDecision
  │
  ├─3. agent.execute(request, endpoint)
  │     └─ BaseAgent.call_model_api(endpoint, request)
  │           ├─ endpoint.increment_load()
  │           ├─ POST {base_url}/chat/completions
  │           ├─ _parse_response(data)
  │           ├─ endpoint.update_latency(ms)
  │           ├─ endpoint.update_success_rate(ok)
  │           └─ endpoint.decrement_load()
  │
  ├─4. strategy_router.record_adaptive_result(agent_type, success, latency)
  │
  ├─5. hook_manager.execute_post_hooks(request, result)
  │     ├─ ResponseSanitizationHook.execute(request, result)
  │     ├─ CostCalculationHook.execute(request, result)
  │     ├─ RetryDecisionHook.execute(request, result)
  │     └─ ResponseLoggingHook.execute(request, result)
  │
  └─6. 返回 DispatchResponse
```

### 4.2 完整调度链路（Bridge→Gateway 路径）

```
Client
  │ POST /dispatch
  ▼
FastAPI (main.py)
  │ dispatcher.dispatch(request)
  ▼
MainDispatcherAgent.dispatch()
  │
  ├─1. hook_manager.execute_pre_hooks(request)
  │
  ├─2. _dispatch_openclaw(request, request_id, start_time)
  │     ├─ strategy_router.route(request) → RoutingDecision
  │     └─ openclaw_agent.execute(request, endpoint)
  │           │
  │           ▼
  │     OpenClawAgent._dispatch_via_bridge(request)
  │           │ POST {bridge_url}/dispatch
  │           ▼
  │     Bridge (orchestrator.mjs)
  │           ├─ adaptiveRoute(request) → routing
  │           ├─ callModelApi(selected, request)
  │           │     ├─ resolveGatewayModel(endpoint) → "openclaw/local-dispatcher"
  │           │     └─ POST {GATEWAY_URL}/v1/chat/completions
  │           │           │
  │           │           ▼
  │           │     OpenClaw Gateway (:3000)
  │           │           └─ 路由到对应 Agent 执行
  │           └─ 返回结果
  │
  ├─3. hook_manager.execute_post_hooks(request, result)
  │
  └─4. 返回 DispatchResponse (routed_via_gateway=True)
```

### 4.3 Fallback 链路

```
主请求失败/超时
  │
  ▼
MainDispatcherAgent._try_fallbacks(request, agent, fallback_endpoints)
  │
  ├─ 遍历 fallback_endpoints[:2]
  │   ├─ 检查 ep.is_available
  │   ├─ _get_agent(ep.model_type.value) → local/cloud agent
  │   ├─ agent.execute(request, ep)
  │   │   ├─ 成功 → 加入 results
  │   │   └─ 失败 → 记录 warning，继续下一个
  │   └─ 返回 results
  │
  ├─ 有降级结果 → DispatchResponse(PARTIAL)
  └─ 无降级结果 → DispatchResponse(TIMEOUT/FAILED)
```

### 4.4 模块间调用关系图

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  FastAPI     │────▶│ MainDispatcher│────▶│ StrategyRouter│
│  (main.py)  │     │    Agent      │     │              │
└─────────────┘     └──────┬───────┘     └──────┬───────┘
                           │                     │
              ┌────────────┼────────────┐        │
              ▼            ▼            ▼        ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐
        │LocalAgent│ │CloudAgent│ │OpenClaw  │ │AdaptiveStrategy│
        │          │ │          │ │  Agent   │ └──────┬───────┘
        └────┬─────┘ └────┬─────┘ └────┬─────┘        │
             │            │            │               ▼
             │            │            │        ┌──────────────┐
             │            │            │        │PriorityStrategy│
             │            │            │        └──────────────┘
             │            │            │               │
             ▼            ▼            ▼               ▼
        ┌─────────────────────────────────────┐ ┌──────────────┐
        │         BaseAgent.call_model_api     │ │LoadBalance   │
        │    (HTTP POST to vendor API)         │ │  Strategy    │
        └─────────────────────────────────────┘ └──────────────┘
                           │
                           ▼
              ┌──────────────────────┐
              │    ModelRegistry     │
              │  (端点注册与状态管理)  │
              └──────────────────────┘

        ┌─────────────┐     ┌──────────────┐
        │ HookManager │────▶│  Pre-Hooks   │
        │             │────▶│  Post-Hooks  │
        └─────────────┘     └──────────────┘
```

---

## 5. 上下游接口定义

### 5.1 上游接口（客户端 → 调度系统）

#### 5.1.1 单请求调度

```
POST /dispatch
Content-Type: application/json

请求体:
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
  "parameters": {"temperature": 0.7},
  "context": [{"role": "system", "content": "你是一个助手"}],
  "request_id": "req-123",
  "timeout_ms": 30000
}

响应体:
{
  "request_id": "req-123",
  "appid": "my-app",
  "status": "success",
  "result": {
    "model_name": "ollama-qwen2.5",
    "model_type": "local",
    "provider": "ollama",
    "output": "量子计算是...",
    "usage": {"prompt_tokens": 15, "completion_tokens": 200, "total_tokens": 215},
    "latency_ms": 450,
    "cost": 0.0,
    "finish_reason": "stop",
    "routed_via_gateway": false,
    "actual_model": null
  },
  "fallback_results": null,
  "error": null,
  "agent_trace": [
    {"agent": "pre_hooks", "message": "Executed pre-processing hooks", "timestamp": ...},
    {"agent": "openclaw_router", "message": "OpenClaw routing: agent=local, ...", "timestamp": ...},
    {"agent": "openclaw_agent", "message": "OpenClaw result from ollama-qwen2.5, latency=450ms", "timestamp": ...},
    {"agent": "post_hooks", "message": "Executed post-processing hooks", "timestamp": ...}
  ],
  "hooks_applied": ["pre:rate_limit", "pre:request_validation", "pre:constraint_enrichment", "pre:request_logging", "post:response_sanitization", "post:cost_calculation", "post:retry_decision", "post:response_logging"],
  "timestamp": "2026-05-27T10:00:00.000000",
  "total_latency_ms": 520
}
```

#### 5.1.2 批量调度

```
POST /dispatch/batch
Content-Type: application/json

请求体: [DispatchRequest, DispatchRequest, ...]
响应体: [DispatchResponse, DispatchResponse, ...]
```

#### 5.1.3 Agent 消息发送

```
POST /openclaw/agent/{agent_id}/message?prompt=...&timeout_ms=30000
Content-Type: application/json

请求体 (可选):
{
  "context": [{"role": "user", "content": "..."}],
  "parameters": {"temperature": 0.7}
}

响应体:
{
  "status": "success",
  "agent_id": "local-dispatcher",
  "result": { ... ModelResult ... },
  "used_model": "ollama/qwen2.5:3b",
  "fallback": false
}
```

### 5.2 下游接口（调度系统 → 模型厂商）

#### 5.2.1 OpenAI 兼容 API（直连模式）

```
POST {base_url}/chat/completions
Content-Type: application/json
Authorization: Bearer {api_key}

请求体:
{
  "model": "qwen2.5:3b",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "prompt text"}
  ],
  "max_tokens": 1024,
  "stream": false
}

响应体:
{
  "id": "chatcmpl-xxx",
  "choices": [{
    "message": {"role": "assistant", "content": "response text"},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 15, "completion_tokens": 200, "total_tokens": 215},
  "model": "qwen2.5:3b"
}
```

#### 5.2.2 OpenClaw Gateway API（Bridge 模式）

```
POST {GATEWAY_URL}/v1/chat/completions
Content-Type: application/json

请求体:
{
  "model": "openclaw/local-dispatcher",
  "messages": [
    {"role": "user", "content": "prompt text"}
  ],
  "max_tokens": 1024
}

响应体: (OpenAI 兼容格式)
{
  "choices": [{
    "message": {"role": "assistant", "content": "response text"},
    "finish_reason": "stop"
  }],
  "usage": {...},
  "model": "qwen2.5:3b"
}
```

### 5.3 内部接口（Python ↔ Bridge）

#### 5.3.1 Bridge 调度接口

```
POST http://localhost:3001/dispatch
Content-Type: application/json

请求体:
{
  "appid": "my-app",
  "type": "chat",
  "prompt": "...",
  "priority": 3,
  "constraints": {...},
  "parameters": {...},
  "context": [...],
  "request_id": "req-123",
  "timeout_ms": 30000
}

响应体:
{
  "request_id": "req-123",
  "appid": "my-app",
  "status": "success",
  "result": {
    "model_name": "ollama/qwen2.5:3b",
    "model_type": "local",
    "provider": "ollama",
    "output": "...",
    "usage": {...},
    "latency_ms": 500,
    "cost": 0.0,
    "finish_reason": "stop",
    "routed_via_gateway": true,
    "actual_model": "qwen2.5:3b"
  },
  "fallback_results": null,
  "agent_trace": [...],
  "strategy_name": "adaptive_local_first",
  "routing_decision": {...}
}
```

#### 5.3.2 Bridge Agent 消息接口

```
POST http://localhost:3001/agent/message
Content-Type: application/json

请求体:
{
  "agent_id": "local-dispatcher",
  "prompt": "...",
  "context": [...],
  "parameters": {...},
  "timeout_ms": 30000
}

响应体:
{
  "status": "success",
  "agent_id": "local-dispatcher",
  "result": { ... ModelResult ... },
  "used_model": "ollama/qwen2.5:3b",
  "fallback": false
}
```

#### 5.3.3 Bridge 健康检查接口

```
GET http://localhost:3001/health

响应体:
{
  "status": "healthy",
  "openclaw_version": "2026.4.27",
  "bridge_port": 3001,
  "gateway_url": "http://localhost:3000",
  "gateway_routing": true,
  "local_models": 1,
  "cloud_models": 2,
  "total_models": 3
}
```

---

## 6. 核心算法与实现细节

### 6.1 EMA（指数移动平均）更新算法

系统在多处使用 EMA 进行状态平滑更新，避免单次请求结果造成剧烈波动：

| 应用场景 | α 值 | 公式 | 说明 |
|---------|------|------|------|
| 延迟更新 | 0.3 | `new = 0.7 * old + 0.3 * current` | 延迟变化较快，α较大以快速响应 |
| 成功率更新 | 0.05 | `new = 0.95 * old + 0.05 * (1 or 0)` | 成功率需长期观察，α较小以平滑 |
| 自适应权重 | 0.1 | 见3.7.2节 | 权重调整需适度，避免震荡 |

### 6.2 自适应调度决策树

完整的请求分类与策略选择流程：

```
输入: DispatchRequest
  │
  ├─ 约束检查
  │   ├─ require_local? → PRIVACY_FIRST
  │   ├─ require_gpu? → CAPABILITY_OPTIMIZED
  │   └─ preferred_providers 匹配云端? → CLOUD_FIRST
  │
  ├─ 请求类型检查
  │   ├─ TOOL_CALL / IMAGE / AUDIO → CAPABILITY_OPTIMIZED
  │   └─ EMBEDDING → CAPABILITY_OPTIMIZED
  │
  ├─ 优先级检查
  │   ├─ CRITICAL / HIGH → LATENCY_OPTIMIZED
  │   └─ LOW / BACKGROUND → COST_OPTIMIZED
  │
  ├─ 成本约束
  │   └─ max_cost_per_request ≤ 0 → COST_OPTIMIZED
  │
  ├─ 延迟约束
  │   └─ max_latency_ms ≤ 1000 → LATENCY_OPTIMIZED
  │
  ├─ 上下文长度
  │   └─ estimated > 16000 → CAPABILITY_OPTIMIZED
  │
  └─ 默认 → LOCAL_FIRST
```

### 6.3 故障切换与熔断机制

**本地熔断**：
- `_local_fail_streak` 记录本地连续失败次数
- 连续失败 ≥ 3 次 → 自动切换到 CLOUD_FIRST
- 本地成功一次 → 重置计数器

**权重保护**：
- local/cloud 权重范围：`[0.1, 0.9]`
- 避免任一路径被完全排除
- 本地失败时权重下降速度（α*2）是成功时上升速度（α）的2倍

**Fallback 限制**：
- 最多尝试 2 个降级端点
- 降级端点必须 `is_available`（enabled + 未满载）
- 降级端点按负载排序优先选择

### 6.4 上下文长度估算

```python
def _estimate_context_length(request):
    estimated = len(request.prompt) * 2  # 中文字符约2 token
    if request.context:
        for msg in request.context:
            content = msg.get("content", "")
            estimated += len(content) * 2
    if request.constraints.min_context_length:
        estimated = max(estimated, request.constraints.min_context_length)
    return estimated
```

- 按1字符≈2 token 估算（中文场景）
- 长上下文阈值：16000 tokens
- 超过阈值 → 选择大上下文窗口的云端模型

### 6.5 Power-of-Two 负载均衡

```
1. 从候选列表中随机选取2个端点
2. 比较 current_load
3. 返回负载较低者

优势:
- O(1) 时间复杂度
- 无需全局状态同步
- 自然分散热点
- 比纯随机更好的负载分布
```

### 6.6 Bridge Gateway 路由映射

```
端点ID → OpenClaw Agent ID 映射:

ollama/qwen2.5:3b   → openclaw/local-dispatcher
moonshot/kimi-k2.6  → openclaw/cloud-dispatcher
deepseek/deepseek-chat → openclaw/code-executor
未知端点             → openclaw (默认)

Gateway 接收到 model="openclaw/local-dispatcher" 后:
1. 查找 openclaw.json 中 id="local-dispatcher" 的 Agent
2. 获取 Agent 的 primary model: "ollama/qwen2.5:3b"
3. 路由到对应模型执行
4. 若失败 → 尝试 Agent 的 fallbacks 列表
```

### 6.7 Hook 管道执行保证

```
关键 Hook (priority ≤ 50):
- 执行失败 → 抛出异常，中断请求
- 包括: rate_limit(5), request_validation(10), constraint_enrichment(20)

非关键 Hook (priority > 50):
- 执行失败 → 仅记录错误，不中断
- 包括: request_logging(90), response_logging(90)

设计原则:
- 安全/校验类 Hook 优先级高，必须成功
- 日志类 Hook 优先级低，允许降级
```

---

## 7. 配置体系

### 7.1 环境变量配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| SCHEDULER_HOST | 0.0.0.0 | FastAPI 监听地址 |
| SCHEDULER_PORT | 8000 | FastAPI 监听端口 |
| SCHEDULER_LOG_LEVEL | INFO | 日志级别 |
| OPENCLAW_GATEWAY_URL | http://localhost:3000 | OpenClaw Gateway URL |
| OPENCLAW_API_KEY | None | OpenClaw API 密钥 |
| RATE_LIMIT_RPM | 60 | 全局每分钟限流 |
| RATE_LIMIT_PER_APPID | 30 | 单应用每分钟限流 |
| MAX_RETRIES | 2 | 最大重试次数 |
| DEFAULT_TIMEOUT_MS | 30000 | 默认超时(ms) |
| BRIDGE_PORT | 3001 | Bridge 服务端口 |
| USE_OPENCLAW_GATEWAY | true | Bridge 是否使用 Gateway 路由 |

### 7.2 openclaw.json 配置

项目根目录的 `openclaw.json` 定义了 Agent、模型目录和路由策略：

```json
{
  "agent": {
    "model": {
      "primary": "ollama/qwen2.5:3b",
      "fallbacks": ["moonshot/kimi-k2.6", "deepseek/deepseek-chat"]
    }
  },
  "agents": {
    "list": [
      {
        "id": "main",
        "model": {"primary": "ollama/qwen2.5:3b", "fallbacks": [...]}
      },
      {
        "id": "local-dispatcher",
        "model": {"primary": "ollama/qwen2.5:3b"},
        "skills": ["bash", "read", "write"]
      },
      {
        "id": "cloud-dispatcher",
        "model": {"primary": "moonshot/kimi-k2.6", "fallbacks": ["deepseek/deepseek-chat"]},
        "skills": ["bash", "browser"]
      },
      {
        "id": "code-executor",
        "model": {"primary": "deepseek/deepseek-chat", "fallbacks": ["moonshot/kimi-k2.6"]},
        "skills": ["bash", "python"]
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
          "preferCloudForComplex": true
        }
      }
    }
  },
  "gateway": {
    "port": 3000,
    "auth": {"enabled": false}
  }
}
```

### 7.3 OpenClaw Gateway 配置

Gateway 配置位于 `~/.openclaw/openclaw.json`，关键配置项：

```json
{
  "gateway": {
    "http": {
      "endpoints": {
        "chatCompletions": {
          "enabled": true
        }
      }
    },
    "auth": {
      "mode": "none"
    }
  }
}
```

---

## 8. 部署与运维

### 8.1 系统要求

| 组件 | 最低要求 |
|------|---------|
| Python | ≥3.9 |
| Node.js | ≥18.0.0 |
| Ollama | 已安装并运行 |
| 内存 | ≥4GB |
| 磁盘 | ≥2GB（模型存储另计） |

### 8.2 启动流程

```bash
# 1. 安装 Python 依赖
pip install fastapi uvicorn httpx pydantic psutil

# 2. 启动 Ollama（本地模型）
ollama serve

# 3. 拉取模型
ollama pull qwen2.5:3b

# 4. 启动 OpenClaw Gateway
openclaw start

# 5. 启动调度服务（自动启动 Bridge 子进程）
python run.py
```

服务启动顺序：
1. Ollama 服务（:11434）
2. OpenClaw Gateway（:3000）
3. Bridge 服务（:3001，由 FastAPI 自动启动）
4. FastAPI 调度服务（:8000）

### 8.3 健康检查

```bash
# 调度服务健康检查
curl http://localhost:8000/health

# Bridge 健康检查
curl http://localhost:8000/openclaw/bridge/health

# 模型状态
curl http://localhost:8000/models

# 自适应状态
curl http://localhost:8000/openclaw/adaptive/state
```

### 8.4 监控指标

| 指标 | 获取方式 | 说明 |
|------|---------|------|
| 请求总数/成功/失败 | `GET /stats` | 全局请求统计 |
| 平均延迟 | `GET /stats` | 端到端平均延迟 |
| 端点负载 | `GET /models` | 各端点 current_load/max_concurrent |
| 端点成功率 | `GET /models` | 各端点 success_rate |
| 自适应权重 | `GET /openclaw/adaptive/state` | local/cloud 权重 |
| 系统资源 | `GET /stats` | CPU/内存使用率 |
| 调度历史 | `GET /history` | 最近50条调度记录 |

### 8.5 常见问题排查

| 问题 | 可能原因 | 排查方法 |
|------|---------|---------|
| Bridge 不可用 | Node.js 未安装/端口冲突 | `GET /openclaw/bridge/health` |
| 本地模型超时 | Ollama 未启动/模型未拉取 | `curl localhost:11434/v1/models` |
| Gateway 404 | chatCompletions 未启用 | 检查 `~/.openclaw/openclaw.json` |
| 认证失败 | Gateway auth mode | 设置 `gateway.auth.mode=none` |
| 全部降级 | 主端点满载/故障 | 检查 `GET /models` 端点状态 |

---

## 9. 可扩展性设计

### 9.1 新增模型端点

通过 API 动态注册，无需重启服务：

```bash
curl -X POST http://localhost:8000/models \
  -H "Content-Type: application/json" \
  -d '{
    "name": "gpt-4o",
    "display_name": "GPT-4o (Cloud)",
    "model_type": "cloud",
    "provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-xxx",
    "model_id": "gpt-4o",
    "max_context_length": 128000,
    "supports_tools": true,
    "cost_per_1k_input_tokens": 0.005,
    "cost_per_1k_output_tokens": 0.015,
    "priority": 2,
    "weight": 3,
    "max_concurrent": 10
  }'
```

### 9.2 新增 Hook

继承 `PreHook` 或 `PostHook`，注册到 HookManager：

```python
class CustomPreHook(PreHook):
    def __init__(self):
        super().__init__(name="custom_check", priority=15)

    async def execute(self, request):
        # 自定义逻辑
        return request

hook_manager.register_pre_hook(CustomPreHook())
```

### 9.3 新增调度策略

在 `AdaptiveStrategy` 中添加新策略常量和分类逻辑：

```python
class AdaptiveStrategy:
    NEW_STRATEGY = "new_strategy"

    def _classify_request(self, request):
        # 添加新的分类条件
        if ...:
            return self.NEW_STRATEGY
        # ...

    def _select_strategy(self, decision, request):
        if decision == self.NEW_STRATEGY:
            return self.NEW_STRATEGY
        # ...
```

在 `StrategyRouter` 中添加对应的路由方法：

```python
def _route_new_strategy(self, request):
    # 新策略的路由逻辑
    return RoutingDecision(...)
```

### 9.4 新增负载均衡算法

在 `LoadBalanceStrategy` 中添加新算法：

```python
class LoadBalanceStrategy:
    NEW_ALGORITHM = "new_algorithm"

    def select(self, request, candidates, algorithm=...):
        if algorithm == self.NEW_ALGORITHM:
            return self._new_algorithm(candidates)
        # ...
```

### 9.5 新增 OpenClaw Agent

在 `openclaw.json` 中添加 Agent 配置：

```json
{
  "id": "new-agent",
  "model": {
    "primary": "moonshot/kimi-k2.6",
    "fallbacks": ["deepseek/deepseek-chat"]
  },
  "skills": ["bash", "python", "browser"],
  "tools": {
    "allow": ["bash", "read", "write", "edit"]
  }
}
```

在 Bridge 的 `ENDPOINT_TO_AGENT` 中添加映射：

```javascript
"moonshot/kimi-k2.6": "openclaw/new-agent"
```

---

## 10. 变更记录

| 日期 | 版本 | 变更内容 |
|------|------|---------|
| 2026-05-27 | 2026.4.27 | 初始版本：完整技术设计说明书 |
| 2026-05-27 | 2026.4.27 | Bridge 修改：Gateway 路由替代直连厂商 API |
| 2026-05-27 | 2026.4.27 | Fallback 优化：仅在主请求失败时触发降级 |
| 2026-05-27 | 2026.4.27 | AdaptiveStrategy 修复：preferred_providers 正确路由到云端 |
| 2026-05-27 | 2026.4.27 | Dashboard 增强：双路径对比测试、Agent 选择器 |
| 2026-05-27 | 2026.4.27 | Gateway 配置：启用 chatCompletions 端点、关闭认证 |