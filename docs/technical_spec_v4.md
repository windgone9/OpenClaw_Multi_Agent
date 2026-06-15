# OpenClaw Multi-Agent 智能调度系统 — 技术说明文档 v4

> 版本: 4.0 | 更新日期: 2026-06-12 | 基于 Hermes Agent 直连架构（无 Bridge 中间层）

---

## 目录

1. [整体架构](#1-整体架构)
2. [模块详细设计](#2-模块详细设计)
3. [模块调用关系](#3-模块调用关系)
4. [上下游输入与输出](#4-上下游输入与输出)
5. [数据流图](#5-数据流图)
6. [自我进化闭环](#6-自我进化闭环)
7. [并发控制与稳定性](#7-并发控制与稳定性)
8. [可扩展性](#8-可扩展性)
9. [API 参考](#9-api-参考)
10. [配置参数](#10-配置参数)

---

## 1. 整体架构

### 1.1 架构演进

| 版本 | 架构 | 特点 |
|------|------|------|
| v1 | Python FastAPI + Node.js Bridge + Gateway | 三层分离，Bridge 做智能路由 |
| v2 | Smart Router 双路径（Gateway 路径 + Agent 执行链） | 复杂度评分驱动的双路径分发 |
| v3 | Bridge 层 Smart Router 评分 + EMA 权重更新 | 自适应路由 + Agent Soul |
| **v4** | **Hermes Agent 直连架构（无 Bridge 中间层）** | **Memory 驱动路由 + 熔断器 + 自我进化闭环** |

v4 的核心变化：**移除 Bridge/Gateway 中间层**，Hermes Agent 直接连接下游服务（Ollama、OfficialGW），通过 MEMORY.md 实现自我进化闭环。

### 1.2 架构总览

| 层级 | 组件 | 端口 | 技术栈 | 职责 |
|------|------|------|--------|------|
| 客户端层 | Dashboard UI | :8082/static | HTML/JS | 请求提交、全链路追踪、监控指标、Memory 闭环 |
| 调度层 | Hermes Server (FastAPI) | :8082 | Python/FastAPI | 请求接收、队列管理、Watchdog、统计 |
| 路由层 | OfficialHermesAdapter | - | Python | Ollama 路由决策、关键词修正、Memory 注入、熔断器 |
| 执行层 | DispatchWorker | - | Python/Threading | 4 类路由分发、信号量控制、结果回写 |
| 下游服务 | Ollama / OfficialGW | :11434 / :3005 | 各厂商 | 实际推理执行 |
| 进化层 | MEMORY.md + Feedback | - | 文件系统 | 路由规则学习、延迟统计、反馈历史 |

### 1.3 架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Dashboard UI (:8082)                        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ │
│  │全链路追踪│ │服务管理  │ │监控指标  │ │Memory闭环│ │实时日志  │ │
│  └─────┬────┘ └─────┬────┘ └─────┬────┘ └─────┬────┘ └─────┬────┘ │
└────────┼────────────┼────────────┼────────────┼────────────┼───────┘
         │            │            │            │            │
         ▼            ▼            ▼            ▼            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Hermes Server (FastAPI :8082)                    │
│                                                                     │
│  ┌─────────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │ /queue/submit   │  │  Watchdog    │  │  /proxy/health        │  │
│  │ /queue/submit-  │  │  (30s 间隔)  │  │  /stats               │  │
│  │   sync          │  │  健康检查    │  │  /memory/content      │  │
│  └────────┬────────┘  │  进程存活    │  │  /route/analyze       │  │
│           │           │  自动恢复    │  └───────────────────────┘  │
│           ▼           └──────┬───────┘                             │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │              Message Queue (InProcess/Redis)                │   │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐        │   │
│  │  │ Requests     │ │ Results      │ │ Feedback     │        │   │
│  │  │ Queue        │ │ Queue        │ │ Queue        │        │   │
│  │  └──────┬───────┘ └──────────────┘ └──────┬───────┘        │   │
│  └─────────┼──────────────────────────────────┼────────────────┘   │
│            │                                     │                   │
│            ▼                                     │                   │
│  ┌─────────────────────────────────────────┐    │                   │
│  │        DispatchWorker (4 threads)        │    │                   │
│  │                                          │    │                   │
│  │  ┌──────────────────────────────────┐   │    │                   │
│  │  │   OfficialHermesAdapter          │   │    │                   │
│  │  │   ┌────────────────────────┐     │   │    │                   │
│  │  │   │ Ollama 路由决策        │     │   │    │                   │
│  │  │   │ (qwen2.5:3b, 15s超时)  │     │   │    │                   │
│  │  │   │ + 熔断器(2次失败跳过)   │     │   │    │                   │
│  │  │   │ + 关键词修正(post_val)  │     │   │    │                   │
│  │  │   │ + MEMORY.md 规则注入    │     │   │    │                   │
│  │  │   └────────────────────────┘     │   │    │                   │
│  │  │   ┌────────────────────────┐     │   │    │                   │
│  │  │   │ _post_validate_route   │     │   │    │                   │
│  │  │   │ 多模态关键词 → multimodal│    │   │    │                   │
│  │  │   │ 隐私关键词 → local_inf  │    │   │    │                   │
│  │  │   │ 代码关键词 → gateway    │     │   │    │                   │
│  │  │   └────────────────────────┘     │   │    │                   │
│  │  └──────────────────────────────────┘   │    │                   │
│  │                                          │    │                   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ │    │                   │
│  │  │Ollama    │ │OfficialGW│ │Ollama    │ │    │                   │
│  │  │Semaphore │ │Semaphore │ │Semaphore │ │    │                   │
│  │  │(limit=2) │ │(limit=3) │ │(route=1) │ │    │                   │
│  │  └────┬─────┘ └────┬─────┘ └──────────┘ │    │                   │
│  └───────┼─────────────┼────────────────────┘    │                   │
└──────────┼─────────────┼─────────────────────────┼───────────────────┘
           │             │                         │
           ▼             ▼                         ▼
┌──────────────┐ ┌──────────────┐    ┌──────────────────────────┐
│   Ollama     │ │  OfficialGW  │    │      MEMORY.md           │
│  :11434      │ │  :3005       │    │  ┌──────────────────┐    │
│              │ │              │    │  │ Routing Patterns  │    │
│ qwen2.5:3b  │ │ OpenClaw     │    │  │ Key Rules         │    │
│ llava:7b    │ │ Agent→Volcano│    │  │ Latency Stats     │    │
│              │ │              │    │  │ Feedback History   │    │
└──────────────┘ └──────────────┘    │  └──────────────────┘    │
                                     └──────────────────────────┘
```

### 1.4 四类路由路径

| 路由 | 目标 | 延迟 | 适用场景 | 信号量 |
|------|------|------|----------|--------|
| **direct_local** | Ollama 直连 | 2~15s | 简单问答、翻译、计算 | Ollama(limit=2) |
| **gateway** | OfficialGW 直连 | 50~300s | 代码执行、Volcano、多步编排 | GW(limit=3) |
| **multimodal** | 本地多模态模型 | 60~200s | 图片/音频/视频处理 | Ollama(limit=2) |
| **local_inference** | Ollama 直连(隐私) | 20~120s | 隐私敏感数据、医疗、脱敏 | Ollama(limit=2) |

---

## 2. 模块详细设计

### 2.1 server.py — 核心服务入口

**职责**：FastAPI 应用主入口，提供 REST API、队列管理、Watchdog 监控。

**核心组件**：

| 组件 | 说明 |
|------|------|
| `FastAPI app` | HTTP 服务，CORS 中间件，静态文件托管 |
| `MessageQueue` | 请求/结果/反馈队列（InProcess 或 Redis） |
| `DispatchWorker` | 4 线程调度 worker，消费请求队列 |
| `OfficialHermesAdapter` | 路由决策适配器 |
| `Watchdog` | 30s 间隔健康检查 + 自动恢复 |
| `_pending_sync` | submit-sync 的 Event 通知机制 |
| `_stats` | 请求统计（total/success/failed/latency） |

**关键 API 端点**：

```
POST /queue/submit          → 异步提交请求到队列
POST /queue/submit-sync     → 同步提交，等待结果（Event 通知 + peek fallback）
GET  /queue/status          → 队列状态 + worker 状态 + 统计
GET  /queue/results         → 获取结果队列
POST /queue/flush           → 清空所有队列

GET  /proxy/health          → 所有服务健康状态（Hermes/Agent/OfficialGW/Ollama）
GET  /proxy/agent-health    → Agent 详细健康 + 插件 + 进化状态
GET  /proxy/ollama-ps       → Ollama 运行模型状态

GET  /stats                 → 系统统计（CPU/内存/请求/路由分布）
GET  /memory/content        → MEMORY.md 内容
POST /route/analyze         → 分析请求复杂度（不执行）

POST /services/start        → 启动托管服务
POST /services/stop         → 停止托管服务
POST /watchdog/start        → 启动 Watchdog
POST /watchdog/stop         → 停止 Watchdog
```

**submit-sync 工作机制**：

```
Client → POST /queue/submit-sync
         │
         ├─ 创建 threading.Event + holder
         ├─ 推入 Requests Queue
         │
         └─ 等待循环 (timeout=400s):
              ├─ event.is_set()? → 返回结果
              ├─ 每 10s peek Results Queue → 匹配 request_id → 返回结果
              └─ 超时 → 返回 {status: "timeout"} + 记录 _stats.failed
```

### 2.2 dispatch_worker.py — 请求调度与分发

**职责**：消费请求队列，执行路由决策，分发到下游服务，回写结果。

**核心流程**：

```
_worker_loop:
  while running:
    msg = queue.pop(QUEUE_REQUESTS)     # 阻塞等待
    _process_request(msg)               # 处理请求

_process_request:
  1. 路由决策: adapter.route_via_agent(request)
     - 获取 _routing_semaphore (limit=1)
     - Ollama 推理路由 (15s 超时)
     - 熔断器检查 (连续 2 次失败 → 跳过 Ollama)
     - _post_validate_route 关键词修正

  2. 分发执行: _dispatch(request, routing)
     - direct_local    → _dispatch_to_local()
     - gateway         → _dispatch_to_official_gw()
     - multimodal      → _dispatch_to_multimodal()
     - local_inference → _dispatch_to_local(privacy=True)

  3. 结果回写: queue.push(QUEUE_RESULTS, result)
  4. 通知等待者: event.set() (submit-sync)
  5. 反馈记录: _record_feedback() → Feedback Queue
```

**信号量体系**：

| 信号量 | 限制 | 用途 |
|--------|------|------|
| `_routing_semaphore` | 1 | 路由决策并发（Ollama GPU 资源） |
| `_ollama_semaphore` | 2 | Ollama 执行并发（GPU 显存限制） |
| `_gw_semaphore` | 3 | OfficialGW 并发（Volcano 容量限制） |

**持久化 httpx 客户端**：

Ollama 使用持久化 `httpx.Client`（连接池复用），避免每次请求新建 TCP 连接。超时后自动重置连接池清除 stale 连接。

### 2.3 official_agent_adapter.py — 路由决策与自我进化

**职责**：智能路由决策、MEMORY.md 规则注入、熔断器、反馈写入。

**路由决策流程**：

```
route_via_agent(request):
  │
  ├─ 熔断器检查
  │   └─ 连续 2 次 Ollama 超时 → 跳过 Ollama，直接 post_validate
  │      冷却 120s 后自动重试
  │
  ├─ _route_via_ollama_direct(request)
  │   ├─ 构建 system_message (路由规则 + 延迟统计)
  │   ├─ Ollama 推理 (max_tokens=128, temperature=0)
  │   ├─ 解析 JSON 路由结果
  │   └─ 超时 → _post_validate_route fallback
  │
  ├─ _post_validate_route(result, request)
  │   ├─ 多模态关键词检测 → multimodal
  │   ├─ 隐私关键词检测 → local_inference
  │   ├─ 代码关键词检测 → gateway
  │   ├─ require_local 约束 → local_inference
  │   └─ type=code/tool_call → gateway
  │
  └─ 返回路由决策
      {route_path, complexity_score, selected_model, reason,
       agent_decision, memory_context_used, post_validated}
```

**system_message 构建**：

```
你是Hermes智能路由Agent...

路由规则(从MEMORY.md学习):
- 单步问答(你好,hello,hi) → direct_local
- 代码执行(代码,code,脚本) → gateway
- 多模态(图片,音频,视频) → multimodal
- 隐私(个人信息,医疗,脱敏) → local_inference
... (最多10条规则，每条最多4个关键词)

延迟统计:
- direct_local: avg=41314ms
- gateway: avg=91855ms
- multimodal: avg=52481ms
- local_inference: avg=44853ms

输出JSON: {route_path, complexity_score, selected_model, reason}
```

**熔断器机制**：

```
状态机:
  CLOSED (正常)
    │ Ollama 路由成功 → 重置 fail_count
    │ Ollama 路由失败 → fail_count++
    │ fail_count >= 2
    ▼
  OPEN (跳过 Ollama)
    │ 直接走 _post_validate_route (节省 15s/请求)
    │ 等待 120s 冷却
    ▼
  HALF-OPEN (重试)
    │ 下一个请求尝试 Ollama
    │ 成功 → CLOSED
    │ 失败 → OPEN (重新计时)
```

**MEMORY.md 路径策略**：

```
优先: <project_root>/.hermes/memories/MEMORY.md  (避免 macOS TCC 限制)
回退: ~/.hermes/memories/MEMORY.md
环境: HERMES_MEMORY_FILE 环境变量覆盖
```

**Ollama 预热**：

启动时后台线程发送 `{"messages":[{"role":"user","content":"hi"}], "max_tokens":1}` 到 Ollama，强制模型加载到 GPU，避免首次请求 25-30s 冷启动。

### 2.4 message_queue.py — 消息队列层

**职责**：请求/结果/反馈队列管理，支持 InProcess 和 Redis 两种后端。

**队列命名**：

| 队列 | 名称 | 用途 |
|------|------|------|
| 请求队列 | `openclaw:requests` | 入队请求，worker 消费 |
| 结果队列 | `openclaw:results` | 完成结果，前端消费 |
| 反馈队列 | `openclaw:feedback` | 反馈事件，Memory 进化 |

**InProcess 后端**：

- 基于 `threading.Queue` + JSONL 文件持久化
- 增量追加模式（不再全量重写）
- 结果队列自动裁剪到 50 条，防止无限增长
- 路径: `<project_root>/.hermes/queues/*.jsonl`

**Redis 后端**（生产环境）：

- 自动检测 Redis 连接
- 支持分布式 worker
- 降级到 InProcess 如果 Redis 不可用

**消息格式**：

```json
// Request
{
  "request_id": "uuid",
  "appid": "default",
  "type": "chat|code|tool_call",
  "prompt": "...",
  "priority": 1-5,
  "model_hint": "optional",
  "parameters": {},
  "context": [],
  "tools": [],
  "constraints": { "require_local": false },
  "timestamp": "ISO8601"
}

// Result
{
  "request_id": "uuid",
  "appid": "default",
  "status": "success|failed|timeout",
  "routing": {
    "route_path": "gateway|direct_local|multimodal|local_inference",
    "complexity_score": 0-100,
    "selected_model": "...",
    "reason": "...",
    "post_validated": true
  },
  "result": {
    "model_name": "...",
    "model_type": "local|openclaw-agent",
    "output": "...",
    "latency_ms": 0,
    "usage": {},
    "finish_reason": "stop",
    "routed_via": "hermes_direct_local"
  },
  "error": null,
  "total_latency_ms": 0,
  "timestamp": "ISO8601"
}

// Feedback
{
  "request_id": "uuid",
  "route_path": "gateway",
  "success": true,
  "latency_ms": 5000,
  "prompt_preview": "...",
  "timestamp": "ISO8601"
}
```

### 2.5 router.py — 路由评分引擎

**职责**：复杂度评分、路由策略选择、EMA 权重更新、模型性能追踪。

**核心类**：

| 类 | 说明 |
|-----|------|
| `RoutePath` | 路由枚举（gateway/direct_local/multimodal/local_inference/agent_chain） |
| `ComplexityLevel` | 复杂度枚举（simple/moderate/complex/highly_complex） |
| `RoutingScore` | 路由评分（total/breakdown/level/recommended_path） |
| `ModelPerformance` | 模型性能追踪（请求数/成功率/平均延迟/EMA） |
| `HermesRouter` | 主路由器（评分/策略/EMA/SQLite 持久化） |

**复杂度评分维度**：

| 维度 | 权重 | 说明 |
|------|------|------|
| request_type | 0.25 | chat/code/tool_call/multi_step |
| keyword_complexity | 0.20 | 关键词匹配复杂度 |
| has_tools | 0.20 | 是否需要工具调用 |
| require_local | 0.15 | 是否需要本地执行 |
| prompt_length | 0.10 | prompt 长度 |
| context_depth | 0.10 | 上下文深度 |

**EMA 权重更新**：

```python
# 指数移动平均
alpha = 0.1  # 学习率
new_weight = alpha * observed_value + (1 - alpha) * old_weight
```

### 2.6 agent.py — Agent 技能系统

**职责**：路由技能注册、匹配、组合、进化。

**核心类**：

| 类 | 说明 |
|-----|------|
| `SkillCondition` | 技能触发条件（pattern_type/pattern_value/weight） |
| `SkillAction` | 技能动作（route_path/model_hint/fallback_path） |
| `RoutingSkill` | 路由技能（conditions/action/confidence/source/tags） |
| `HermesAgent` | Agent 核心（技能注册/匹配/决策/进化） |

**技能匹配流程**：

```
request → 遍历所有 ACTIVE 技能
         → 每个 SkillCondition 匹配 → 加权得分
         → 得分最高的技能 → AgentDecision
            ├─ USE_SKILL: 使用该技能的 route_path
            ├─ COMBINE_SKILLS: 组合多个技能
            ├─ OVERRIDE: 覆盖默认路由
            ├─ FALLBACK: 回退到默认
            └─ EXPLORE: 探索新路由（ε-greedy）
```

### 2.7 llm_enhancer.py — LLM 增强分类器

**职责**：使用 Ollama 对请求进行精细分类，补充路由决策信息。

**输出格式**：

```json
{
  "intent": "chat|code|analysis|tool_call|multi_step|creative|reasoning",
  "complexity": 1-10,
  "requires_local": true/false,
  "requires_tools": true/false,
  "key_concepts": ["概念1", "概念2"],
  "suggested_path": "direct_local|gateway|agent_chain",
  "confidence": 0.0-1.0
}
```

### 2.8 skill_bootstrap.py — 技能引导

**职责**：从路由规则生成初始技能集，注册到 HermesAgent。

**预定义技能**：

| 技能名 | 条件 | 路由 | 置信度 |
|--------|------|------|--------|
| privacy_local_route | require_local=true | direct_local | 0.95 |
| simple_chat_local | type=chat, complexity=0-14 | direct_local | 0.85 |
| moderate_chat_gateway | type=chat, complexity=15-39 | gateway | 0.75 |
| complex_agent_chain | type=chat, complexity=40-69 | agent_chain | 0.80 |
| highly_complex_agent_chain | type=chat, complexity=70-100 | agent_chain | 0.90 |
| tool_call_gateway | has_tools=true | gateway | 0.85 |
| code_execution_agent | type=code | agent_chain | 0.80 |

### 2.9 Dashboard (dashboard.html) — 前端监控面板

**职责**：全链路追踪、服务管理、监控指标、Memory 闭环、实时日志。

**五大 Tab**：

| Tab | 功能 | 数据源 |
|-----|------|--------|
| **全链路追踪** | 5 阶段状态（消息队列→智能路由→Agent进化→执行引擎→结果返回） | /queue/status, /proxy/agent-health |
| **服务管理** | 4 服务卡片 + 启停 + Watchdog + 刷新 | /proxy/health, /services/status |
| **监控指标** | CPU/内存/GPU/路由分布/延迟趋势/成功率 | /stats, /proxy/ollama-ps |
| **Memory 闭环** | 反馈记录/路由规则/延迟统计/自学习指标 | /memory/content, /queue/feedback |
| **实时日志** | 服务/级别过滤/搜索/导出 | 前端 JS 生成 |

**请求发送流程**：

```
sendQueueRequest(body, label):
  1. POST /queue/submit-sync?timeout=400
  2. 等待响应 (最长 400s)
  3. 解析结果 → 渲染调度卡片
  4. 更新 AR[] (所有请求记录)
  5. 更新 RC{} (路由分布)
  6. 更新 SH[] (成功率历史)
  7. 更新 LH[] (延迟历史)
  8. refreshPipeline() + updateCharts()
```

---

## 3. 模块调用关系

### 3.1 模块依赖图

```
server.py
  ├── router.py (HermesRouter, RoutePath, RoutingScore)
  │     └── llm_enhancer.py (LLMEnhancer)
  │     └── agent.py (HermesAgent, RoutingSkill)
  │           └── skill_bootstrap.py (bootstrap_routing_skills)
  ├── official_agent_adapter.py (OfficialHermesAdapter)
  │     └── MEMORY.md (读写)
  ├── message_queue.py (MessageQueue, create_queue)
  │     └── InProcess / Redis 后端
  └── dispatch_worker.py (DispatchWorker)
        └── official_agent_adapter.py (路由决策)
        └── message_queue.py (队列操作)
        └── httpx (Ollama/OfficialGW 请求)
```

### 3.2 运行时调用链

```
[Client Request]
    │
    ▼
server.py: POST /queue/submit-sync
    │
    ├─ 创建 Event + holder
    ├─ msg_queue.push(QUEUE_REQUESTS, message)
    │
    ▼
dispatch_worker.py: _worker_loop
    │
    ├─ msg_queue.pop(QUEUE_REQUESTS)
    │
    ▼
dispatch_worker.py: _process_request
    │
    ├─ _routing_semaphore.acquire()
    │   │
    │   ▼
    │   official_agent_adapter.py: route_via_agent
    │     │
    │     ├─ 熔断器检查 (OPEN → 直接 post_validate)
    │     │
    │     ├─ _route_via_ollama_direct
    │     │   ├─ _build_routing_system_message (MEMORY.md 规则注入)
    │     │   ├─ Ollama 推理 (15s 超时)
    │     │   └─ 解析 JSON → 路由决策
    │     │
    │     └─ _post_validate_route (关键词修正)
    │         ├─ 多模态关键词 → multimodal
    │         ├─ 隐私关键词 → local_inference
    │         └─ 代码关键词 → gateway
    │
    ├─ _dispatch(request, routing)
    │   │
    │   ├─ direct_local → _dispatch_to_local
    │   │   └─ _ollama_semaphore.acquire()
    │   │   └─ _ollama_http_client.post("/v1/chat/completions")
    │   │
    │   ├─ gateway → _dispatch_to_official_gw
    │   │   └─ _gw_semaphore.acquire()
    │   │   └─ httpx.Client.post("OfficialGW/v1/chat/completions")
    │   │
    │   ├─ multimodal → _dispatch_to_multimodal
    │   │   └─ _ollama_semaphore.acquire()
    │   │   └─ _ollama_http_client.post("/v1/chat/completions", model=llava:7b)
    │   │
    │   └─ local_inference → _dispatch_to_local(privacy=True)
    │
    ├─ queue.push(QUEUE_RESULTS, result)
    │
    ├─ event.set() (通知 submit-sync)
    │
    └─ _record_feedback → queue.push(QUEUE_FEEDBACK)
                          → _update_memory_file (异步写入 MEMORY.md)
    │
    ▼
server.py: submit-sync 返回结果
```

---

## 4. 上下游输入与输出

### 4.1 上下游关系总览

```
┌─────────┐    ┌─────────────┐    ┌──────────────┐    ┌──────────────┐
│ Dashboard│───▶│ Hermes      │───▶│ Ollama       │───▶│ GPU 推理     │
│ / SDK    │    │ Server      │    │ :11434       │    │ qwen2.5:3b   │
│ / curl   │    │ :8082       │    │              │    │ llava:7b     │
└─────────┘    └──────┬──────┘    └──────────────┘    └──────────────┘
                      │
                      │           ┌──────────────┐    ┌──────────────┐
                      ├──────────▶│ OfficialGW   │───▶│ OpenClaw     │
                      │           │ :3005        │    │ Agent→Volcano│
                      │           └──────────────┘    └──────────────┘
                      │
                      │           ┌──────────────┐
                      └──────────▶│ MEMORY.md    │
                                  │ (自我进化)    │
                                  └──────────────┘
```

### 4.2 各接口输入输出

| 接口 | 输入 | 输出 | 超时 |
|------|------|------|------|
| POST /queue/submit-sync | `{appid, type, prompt, priority, model, context, tools, constraints}` | `{request_id, status, routing, result, total_latency_ms}` | 400s |
| POST /queue/submit | 同上 | `{request_id, status: "queued"}` | 无 |
| GET /queue/status | - | `{queue_backend, worker: {is_running, max_workers, processed, queue_sizes}}` | 5s |
| GET /proxy/health | - | `{hermes, hermesAgent, officialGateway, ollama}` | 20s |
| GET /stats | - | `{system: {cpu, memory}, requests: {total, success, failed, success_rate, by_route}}` | 5s |
| GET /memory/content | - | `{content, rules, latency_stats, feedback_count, file_path}` | 5s |
| POST /route/analyze | `{prompt, type, priority}` | `{complexity_score, recommended_path, breakdown, skill_matches}` | 30s |

### 4.3 下游服务接口

| 服务 | 接口 | 输入 | 输出 | 超时 |
|------|------|------|------|------|
| Ollama | POST /v1/chat/completions | `{model, messages, stream, options}` | `{choices, usage, model}` | 120s |
| Ollama | GET /api/tags | - | `{models: [{name, size}]}` | 5s |
| Ollama | GET /api/ps | - | `{models: [{name, size, expires_at}]}` | 5s |
| OfficialGW | POST /v1/chat/completions | `{model, messages, max_tokens, tools}` | `{choices, usage, model}` | 300s |
| Agent 8642 | GET /health | - | `{status: "ok"}` | 5s |

---

## 5. 数据流图

### 5.1 请求完整生命周期

```
                    ┌─────────────┐
                    │   Client    │
                    │ (Dashboard) │
                    └──────┬──────┘
                           │ POST /queue/submit-sync
                           ▼
                    ┌─────────────┐
                 ┌─▶│ Req Queue   │
                 │  └──────┬──────┘
                 │         │ pop()
                 │         ▼
                 │  ┌─────────────┐     ┌──────────────────┐
                 │  │   Worker    │────▶│ OfficialHermes   │
                 │  │  Thread     │     │ Adapter          │
                 │  │             │     │ ┌──────────────┐ │
                 │  │             │     │ │ 熔断器检查   │ │
                 │  │             │     │ │ Ollama 路由  │ │
                 │  │             │     │ │ 关键词修正   │ │
                 │  │             │     │ │ MEMORY 注入  │ │
                 │  │             │     │ └──────────────┘ │
                 │  │             │     └────────┬─────────┘
                 │  │             │              │ routing decision
                 │  │             │              ▼
                 │  │             │     ┌──────────────────┐
                 │  │             │     │    Dispatch      │
                 │  │             │     │ ┌──────┐┌──────┐│
                 │  │             │     │ │Ollama││ OGW  ││
                 │  │             │     │ │Sem=2 ││Sem=3 ││
                 │  │             │     │ └──┬───┘└──┬───┘│
                 │  │             │     └────┼───────┼────┘
                 │  │             │          │       │
                 │  │             │          ▼       ▼
                 │  │             │   ┌───────┐ ┌───────┐
                 │  │             │   │Ollama │ │ OGW   │
                 │  │             │   │:11434 │ │ :3005 │
                 │  │             │   └───┬───┘ └───┬───┘
                 │  │             │       │         │
                 │  │             │       ▼         ▼
                 │  │             │   ┌───────────────┐
                 │  │             │   │  Result       │
                 │  │             │   └───────┬───────┘
                 │  │             │           │
                 │  │      ┌──────┴──┐       │
                 │  │      │  push   │       │
                 │  │      └──────┬──┘       │
                 │  │             │          │
                 │  ▼             ▼          │
                 │  ┌─────────────┐          │
                 │  │ Res Queue   │◀─────────┘
                 │  └──────┬──────┘
                 │         │ event.set() / peek
                 │         ▼
                 │  ┌─────────────┐
                 └──│   Client    │
                    │  Response   │
                    └─────────────┘
```

### 5.2 自我进化数据流

```
┌──────────┐     ┌──────────────┐     ┌──────────────┐
│  请求    │────▶│  路由决策    │────▶│  执行结果    │
│  输入    │     │  (Ollama+规则)│     │  (成功/失败) │
└──────────┘     └──────┬───────┘     └──────┬───────┘
                        │                     │
                        │    ┌────────────┐   │
                        │    │ MEMORY.md  │   │
                        │    │ 规则注入   │◀──┤
                        │    └──────┬─────┘   │
                        │           │         │
                        │           ▼         │
                        │    ┌────────────┐   │
                        │    │ 反馈写入   │◀──┘
                        │    │ _record_   │
                        │    │ feedback   │
                        │    └──────┬─────┘
                        │           │
                        │           ▼
                        │    ┌────────────┐
                        └───▶│ 延迟统计   │
                             │ 规则更新   │
                             │ 反馈历史   │
                             └────────────┘
                                   │
                                   ▼
                             下次路由决策
                             (规则更准确)
```

---

## 6. 自我进化闭环

### 6.1 进化机制

```
请求 → 路由 → 执行 → 反馈 → 学习 → 优化
  ↑                                │
  └────────────────────────────────┘
```

### 6.2 MEMORY.md 结构

```markdown
# Routing Decision Memory

## Routing Patterns Learned
- 单步问答 [你好,hello,hi] → direct_local (avg~6s)
- 代码执行 [代码,code,脚本] → gateway (avg~60s)
- 多模态 [图片,音频,视频] → multimodal
- 隐私 [个人信息,医疗,脱敏] → local_inference

## Key Rules
- require_local=true → local_inference
- type=code → gateway
- 多模态关键词 → multimodal
- 简单问答 → direct_local

## Latency Stats (auto-updated)
- direct_local: avg=41314ms, p95=390320ms, samples=204
- gateway: avg=91855ms, p95=237519ms, samples=198
- multimodal: avg=52481ms, p95=298884ms, samples=86
- local_inference: avg=44853ms, p95=290799ms, samples=58

## Feedback History
- multimodal ✓ 26779ms '识别图片中的物体并分类'
- local_inference ✓ 21320ms '分析这份内部财务数据'
```

### 6.3 进化触发条件

| 触发 | 动作 | 频率 |
|------|------|------|
| 每次请求完成 | 写入 Feedback Queue | 实时 |
| Feedback 积累 | 更新 MEMORY.md 延迟统计 | 每次请求 |
| 新路由模式发现 | 添加 Routing Pattern | 反馈触发 |
| 规则冲突 | 更新 Key Rules | 反馈触发 |

---

## 7. 并发控制与稳定性

### 7.1 信号量体系

```
┌─────────────────────────────────────────────────┐
│                 GPU 资源 (共享)                   │
│                                                  │
│  ┌─────────────────┐    ┌─────────────────┐     │
│  │ Routing Semaphore│    │ Ollama Semaphore │     │
│  │ limit = 1        │    │ limit = 2        │     │
│  │                  │    │                  │     │
│  │ 路由决策专用     │    │ 执行请求专用     │     │
│  │ (1-3s/请求)      │    │ (2-300s/请求)    │     │
│  └─────────────────┘    └─────────────────┘     │
│                                                  │
│  ┌─────────────────┐                             │
│  │ GW Semaphore    │                             │
│  │ limit = 3       │                             │
│  │                 │                             │
│  │ OfficialGW 请求 │                             │
│  │ (50-300s/请求)  │                             │
│  └─────────────────┘                             │
└─────────────────────────────────────────────────┘
```

### 7.2 熔断器

| 参数 | 值 | 说明 |
|------|-----|------|
| 阈值 | 2 次连续失败 | 触发熔断 |
| 冷却时间 | 120s | 熔断后等待时间 |
| 熔断时行为 | 跳过 Ollama，直接关键词路由 | 节省 15s/请求 |
| 恢复条件 | 冷却后首次 Ollama 成功 | 重置计数 |

### 7.3 Watchdog 监控

| 参数 | 值 | 说明 |
|------|-----|------|
| 检查间隔 | 30s | 健康检查频率 |
| 健康检查超时 | 5s | HTTP 请求超时 |
| 启动宽限期 | 45s | 启动后不检查 |
| 连续失败阈值 | 2 | 触发自动恢复 |
| 最大重启次数 | 3 | 防止无限重启 |
| 重启冷却 | 60s | 两次重启间隔 |

**健康检查策略**：

```
HTTP 检查成功 → healthy
HTTP 检查失败 + 进程存活 → busy (不触发重启)
HTTP 检查失败 + 进程不存在 → unhealthy (触发重启)
```

### 7.4 Dashboard 稳定性

| 机制 | 说明 |
|------|------|
| 健康检查缓存 | fetch 失败时保留上次缓存，不立即标记不可用 |
| 连续失败检测 | 3 次连续失败才显示"不可用"警告 |
| 繁忙状态识别 | `status=busy` 时重置失败计数 |

---

## 8. 可扩展性

### 8.1 水平扩展

| 维度 | 当前 | 扩展方案 |
|------|------|----------|
| Worker 线程 | 4 | 环境变量 `DISPATCH_WORKERS` 调整 |
| GPU 并发 | Ollama Semaphore=2 | 增加 GPU 或使用 vLLM 多卡 |
| Gateway 并发 | GW Semaphore=3 | 增加 OfficialGW 实例 |
| 队列后端 | InProcess | 切换 Redis 支持分布式 worker |

### 8.2 路由扩展

| 维度 | 当前 | 扩展方案 |
|------|------|----------|
| 路由类别 | 4 类 | 添加新 RoutePath 枚举 + dispatch 方法 |
| 路由规则 | MEMORY.md | 支持外部规则源（数据库/API） |
| 路由模型 | qwen2.5:3b | 切换更大模型或云端 LLM |
| 关键词库 | 硬编码 | 支持动态加载/学习 |

### 8.3 下游服务扩展

```
新增下游服务步骤:
1. 在 RoutePath 枚举中添加新路由
2. 在 DispatchWorker._dispatch() 中添加分发方法
3. 在 _post_validate_route() 中添加关键词匹配
4. 在 MEMORY.md 中添加路由规则
5. 添加对应信号量控制并发
```

### 8.4 进化闭环扩展

| 维度 | 当前 | 扩展方案 |
|------|------|----------|
| 记忆存储 | MEMORY.md 文件 | SQLite / 向量数据库 |
| 反馈维度 | 成功/失败/延迟 | 用户评分/质量评估/成本 |
| 学习算法 | 规则更新 + 延迟统计 | 强化学习 / 在线学习 |
| 规则发现 | 关键词模式 | 自动聚类 / 异常检测 |

### 8.5 配置化扩展

所有关键参数均支持环境变量覆盖：

```bash
# 服务端口
HERMES_PORT=8082

# 下游服务
OLLAMA_URL=http://localhost:11434
OPENCLAW_OFFICIAL_GATEWAY_URL=http://127.0.0.1:3005

# 并发控制
DISPATCH_WORKERS=4
OLLAMA_MAX_CONCURRENT=2
ROUTING_MAX_CONCURRENT=1
GW_MAX_CONCURRENT=3

# 超时
OLLAMA_TIMEOUT=120
OFFICIAL_GW_TIMEOUT=300

# 模型
LOCAL_MODEL=qwen2.5:3b
MULTIMODAL_MODEL=llava:7b

# Memory
HERMES_MEMORY_FILE=/path/to/MEMORY.md
```

---

## 9. API 参考

### 9.1 队列接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /queue/submit | 异步提交请求 |
| POST | /queue/submit-sync | 同步提交（等待结果） |
| GET | /queue/status | 队列 + Worker 状态 |
| GET | /queue/results | 获取结果列表 |
| GET | /queue/results/{id} | 获取指定结果 |
| GET | /queue/feedback | 获取反馈列表 |
| POST | /queue/flush | 清空所有队列 |
| POST | /queue/test-loop | 端到端测试 |

### 9.2 路由接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /route | 路由决策（含执行） |
| POST | /route/analyze | 仅分析复杂度（不执行） |

### 9.3 监控接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /health | Hermes 健康检查 |
| GET | /stats | 系统统计 |
| GET | /state | 学习状态 |
| GET | /proxy/health | 所有服务健康 |
| GET | /proxy/agent-health | Agent 详细状态 |
| GET | /proxy/ollama-ps | Ollama 运行模型 |

### 9.4 Memory 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /memory/content | MEMORY.md 内容 |
| GET | /memory/recent | 最近路由历史 |
| GET | /memory/search | 搜索路由记忆 |

### 9.5 Agent 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /agent | Agent 状态 |
| POST | /agent/config | 更新配置 |
| POST | /agent/bootstrap | 重新引导技能 |
| GET | /agent/skills | 列出技能 |
| POST | /agent/skills | 注册技能 |
| DELETE | /agent/skills/{name} | 删除技能 |

### 9.6 Official Agent 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /official-agent | 状态 |
| POST | /official-agent/route | 路由决策 |
| POST | /official-agent/feedback | 记录反馈 |
| POST | /official-agent/toggle | 启用/禁用 |
| POST | /official-agent/mode | 切换模式 |
| POST | /official-agent/refresh-memory | 刷新缓存 |

### 9.7 服务管理接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /services/start | 启动服务 |
| POST | /services/stop | 停止服务 |
| GET | /services/status | 所有服务状态 |
| POST | /watchdog/start | 启动 Watchdog |
| POST | /watchdog/stop | 停止 Watchdog |
| GET | /watchdog/status | Watchdog 状态 |
| POST | /watchdog/config | 更新配置 |

---

## 10. 配置参数

### 10.1 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HERMES_PORT` | 8082 | 服务端口 |
| `OLLAMA_URL` | http://localhost:11434 | Ollama 地址 |
| `OPENCLAW_OFFICIAL_GATEWAY_URL` | http://127.0.0.1:3005 | OfficialGW 地址 |
| `OPENCLAW_TOKEN` | "" | OfficialGW 认证 Token |
| `DISPATCH_WORKERS` | 4 | Worker 线程数 |
| `OLLAMA_MAX_CONCURRENT` | 2 | Ollama 执行并发 |
| `ROUTING_MAX_CONCURRENT` | 1 | 路由决策并发 |
| `GW_MAX_CONCURRENT` | 3 | OfficialGW 并发 |
| `OLLAMA_TIMEOUT` | 120 | Ollama 请求超时(s) |
| `OFFICIAL_GW_TIMEOUT` | 300 | OfficialGW 请求超时(s) |
| `LOCAL_MODEL` | qwen2.5:3b | 本地模型 |
| `MULTIMODAL_MODEL` | llava:7b | 多模态模型 |
| `HERMES_MEMORY_FILE` | (自动) | MEMORY.md 路径 |

### 10.2 Watchdog 配置 (watchdog_config.json)

```json
{
  "enabled": true,
  "interval_seconds": 30,
  "max_restart_attempts": 3,
  "restart_cooldown_seconds": 60,
  "startup_grace_seconds": 45,
  "shutdown_timeout_seconds": 5,
  "port_cleanup_wait_seconds": 3,
  "health_check_timeout_seconds": 5,
  "consecutive_failures_before_restart": 2
}
```

### 10.3 日志配置

| 输出 | 路径 | 策略 |
|------|------|------|
| 控制台 | stdout | 实时输出 |
| 文件 | hermes/logs/hermes_server.log | 10MB 轮转，保留 3 份 |
| 队列持久化 | .hermes/queues/*.jsonl | 增量追加 |

---

## 附录 A: 与 v3 架构的差异

| 维度 | v3 (Bridge 架构) | v4 (Hermes 直连架构) |
|------|-------------------|----------------------|
| 中间层 | Bridge (Node.js :3001) + Gateway (:3000) | 无中间层，直接连接 |
| 路由决策 | Bridge Smart Router (EMA 评分) | Ollama + MEMORY.md 规则注入 |
| 自我进化 | EMA 权重更新 | MEMORY.md 闭环学习 |
| 并发控制 | 无 | 三级信号量 (routing/ollama/gw) |
| 熔断器 | 无 | Ollama 路由熔断器 |
| 队列 | 无 | 三队列 (requests/results/feedback) |
| Watchdog | 无 | 30s 健康检查 + 自动恢复 |
| Dashboard | 简单 | 五 Tab 全功能面板 |
| 超时处理 | 无保护 | 多级超时 + 缓存 + fallback |

## 附录 B: 文件结构

```
OpenClaw_Multi_Agent/
├── hermes/                          # 核心 Python 包
│   ├── __init__.py
│   ├── server.py                    # FastAPI 服务入口
│   ├── dispatch_worker.py           # 请求调度与分发
│   ├── official_agent_adapter.py    # 路由决策 + Memory 进化
│   ├── router.py                    # 路由评分引擎
│   ├── agent.py                     # Agent 技能系统
│   ├── llm_enhancer.py              # LLM 增强分类器
│   ├── message_queue.py             # 消息队列层
│   ├── skill_bootstrap.py           # 技能引导
│   ├── watchdog_config.json         # Watchdog 配置
│   ├── logs/                        # 日志目录
│   └── docs/                        # 路由策略文档
├── static/
│   └── dashboard.html               # Dashboard 前端
├── bridge/
│   └── orchestrator.mjs             # Bridge (v3 遗留)
├── gateway/
│   └── gateway.mjs                  # Gateway (v3 遗留)
├── .hermes/
│   ├── memories/
│   │   └── MEMORY.md                # 自我进化记忆
│   ├── queues/
│   │   ├── openclaw_requests.jsonl  # 请求队列持久化
│   │   ├── openclaw_results.jsonl   # 结果队列持久化
│   │   └── openclaw_feedback.jsonl  # 反馈队列持久化
│   └── routing_feedback/
│       └── feedback.jsonl           # 路由反馈记录
├── docs/                            # 技术文档
├── batch_test.py                    # 批量测试脚本
└── .env.example                     # 环境变量示例
```
