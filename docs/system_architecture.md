# OpenClaw Multi-Agent 系统架构详细文档

## 1. 系统总览架构图

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          External Clients (AI Apps / Dashboard)                │
│                    http://localhost:8080 (Docker) / 8090 (K8S Kind)            │
└──────────────────────────────────────────┬──────────────────────────────────────┘
                                           │
                                           ▼
                              ┌──────────────────────┐
                              │   Nginx LB (:80)     │
                              │   least_conn + ip_hash│
                              │   Port: 8080/8090     │
                              └──────────┬───────────┘
                                         │
          ┌──────────────────────────────┼──────────────────────────────┐
          │                              │                              │
          │   /v1/chat/completions        │   /v1/chat (exact)          │   /v1/stream/ws
          │   /v1/embeddings              │   /k8s/                     │   /v1/stream/sse
          │   /v1/audio/*                 │   /v1/system/metrics        │   /v1/stream/asr
          │   /v1/multimodal/*            │   /dashboard.html           │   /v1/stream/asr/file
          │   /litellm/* /ui/*            │                              │
          │                              │                              │
          ▼                              ▼                              ▼
┌─────────────────────┐    ┌──────────────────────────┐    ┌──────────────────────┐
│  LiteLLM Proxy      │    │    Hermes Agent (:8082)   │    │  Stream Service      │
│  (:4000)            │    │    ★ 核心调度层 ★         │    │  (:8084)             │
│                     │    │                          │    │                      │
│  OpenAI兼容API:     │    │  ┌──────────────────┐    │    │  WebSocket/SSE 推流   │
│  /v1/chat/complete  │    │  │  Queue APIs       │    │    │  ASR 实时语音识别     │
│  /v1/embeddings     │    │  │  /queue/submit     │    │    │  FunASR 集成         │
│  /v1/audio/trans    │    │  │  /queue/submit-sync│    │    │                      │
│  /v1/multimodal     │    │  │  /queue/results    │    │    │  ASR级联:            │
│                     │    │  │  /queue/feedback    │    │    │  FunASR→豆包→Ollama  │
│  模型路由:          │    │  └──────────────────┘    │    │                      │
│  latency-based      │    │  ┌──────────────────┐    │    └──────────┬───────────┘
│  水平扩展(HPA)      │    │  │  K8S APIs         │    │               │
│                     │    │  │  /k8s/workloads    │    │               │
│  模型列表:          │    │  │  /k8s/callback     │    │               │
│  qwen2.5 (local)    │    │  └──────────────────┘    │               │
│  deepseek (cloud)   │    │  ┌──────────────────┐    │               │
│  moonshot (cloud)   │    │  │  System APIs      │    │               │
│  llava (vision)     │    │  │  /health /stats    │    │               │
│  funasr (asr)       │    │  │  /models /state    │    │               │
│  bge (embedding)    │    │  └──────────────────┘    │               │
└──────────┬──────────┘    │  ┌──────────────────┐    │               │
           │               │  │  Unified /v1/chat │    │               │
           │               │  │  type→自动路由     │    │               │
           │               │  └──────────────────┘    │               │
           │               │                          │               │
           │               │  ★ 内部模块详见第3节 ★   │               │
           │               └──────────────────────────┘               │
           │                            │                             │
           │                            │                             │
           ▼                            ▼                             ▼
┌─────────────────────┐    ┌──────────────────────────┐    ┌──────────────────────┐
│   PostgreSQL        │    │      Ollama (:11434)      │    │    FunASR (:8199)    │
│   LiteLLM DB        │    │      本地模型推理         │    │    SenseVoice ASR    │
│   (:5432)           │    │                          │    │                      │
│                     │    │  qwen2.5:7b (对话)       │    └──────────────────────┘
│                     │    │  llava:7b (多模态)       │               │
│                     │    │  nomic-embed-text (向量) │               │
│                     │    │                          │               │
│                     │    └──────────────────────────┘               │
│                     │                                                 │
│                     │    ┌──────────────────────────┐               │
│                     │    │  Official GW (:3005)      │               │
│                     │    │  Agent 执行链 (可选)      │               │
│                     │    │  K8S Plugin 端点          │               │
│                     │    └──────────────────────────┘               │
│                     │                                                 │
│                     │    ┌──────────────────────────┐               │
│                     │    │    MinIO (:9000/:9001)   │               │
│                     │    │    对象存储               │               │
│                     │    └──────────────────────────┘               │
└─────────────────────┘                                                 │
                                                                         │
                                                              ┌──────────┘
                                                              │
                                                              ▼
                                                    ┌──────────────────┐
                                                    │   MinIO (:9000)  │
                                                    │   S3 对象存储     │
                                                    └──────────────────┘
```

## 2. 请求路由路径详细图

### 2.1 Hermes 内部路由决策流程

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Request 进入 Hermes Agent                             │
│                                                                             │
│  ┌──────────────┐                                                           │
│  │ 入口方式      │                                                           │
│  │              │                                                           │
│  │ 方式A:       │  POST /queue/submit                                       │
│  │ 队列异步     │  POST /queue/submit-sync  (阻塞等待结果)                   │
│  │              │                                                           │
│  │ 方式B:       │  POST /route  (直接路由+执行)                               │
│  │ 直接路由     │                                                           │
│  │              │                                                           │
│  │ 方式C:       │  POST /v1/chat  (统一入口, type自动路由)                    │
│  │ 统一入口     │  chat/vision → LiteLLM Proxy                              │
│  │              │  asr → Stream Service                                      │
│  │              │  stream_asr → WebSocket                                    │
│  └──────────────┘                                                           │
│         │                                                                   │
│         ▼                                                                   │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │                 Step 1: 消息队列 (可选路径A)                      │       │
│  │                                                                 │       │
│  │  MessageQueue (InProcessQueue / RedisQueue)                     │       │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │       │
│  │  │ Requests    │  │ Results     │  │ Feedback    │             │       │
│  │  │ Queue       │→ │ Queue       │  │ Queue       │             │       │
│  │  │ openclaw:   │  │ openclaw:   │  │ openclaw:   │             │       │
│  │  │ requests    │  │ results     │  │ feedback    │             │       │
│  │  └─────────────┘  └─────────────┘  └─────────────┘             │       │
│  │                                                                 │       │
│  │  后端选择:                                                       │       │
│  │  QUEUE_BACKEND=auto → 尝试Redis → 降级到InProcess(JSONL)        │       │
│  │  InProcessQueue: JSONL持久化 .hermes/queues/                     │       │
│  └──────────────────────────────────────────────────────────────────┘       │
│         │                                                                   │
│         ▼                                                                   │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │                 Step 2: DispatchWorker 消费                      │       │
│  │                                                                 │       │
│  │  DispatchWorker (4线程)                                         │       │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐            │       │
│  │  │ Worker #1    │ │ Worker #2    │ │ Worker #3    │            │       │
│  │  │ pop(request) │ │ pop(request) │ │ pop(request) │            │       │
│  │  │ route→dispatch│ │ route→dispatch│ │ route→dispatch│            │       │
│  │  │ feedback      │ │ feedback      │ │ feedback      │            │       │
│  │  └──────────────┘ └──────────────┘ └──────────────┘            │       │
│  │                                                                 │       │
│  │  并发控制 (三层Semaphore):                                       │       │
│  │  ┌─ routing_semaphore (limit=1): 路由决策互斥 (共享Ollama GPU)  │       │
│  │  ├─ ollama_semaphore  (limit=2): Ollama推理并发限制             │       │
│  │  └─ gw_semaphore      (limit=3): OfficialGW并发限制             │       │
│  └──────────────────────────────────────────────────────────────────┘       │
│         │                                                                   │
│         ▼                                                                   │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │                 Step 3: 路由决策引擎                              │       │
│  │                                                                 │       │
│  │  OfficialHermesAdapter.route_via_agent(request)                 │       │
│  │                                                                 │       │
│  │  ┌─── 优先级 1: K8S快速路径 ────────────────────────────┐      │       │
│  │  │  type=k8s_workload → 直接返回 k8s_gateway            │      │       │
│  │  │  (跳过LLM路由, 零GPU开销)                             │      │       │
│  │  └──────────────────────────────────────────────────────┘      │       │
│  │                                                                 │       │
│  │  ┌─── 优先级 2: 熔断器检查 ────────────────────────────┐      │       │
│  │  │  2次连续Ollama路由失败 → 开启120s熔断               │      │       │
│  │  │  熔断期间 → _post_validate_route() 关键词路由        │      │       │
│  │  └──────────────────────────────────────────────────────┘      │       │
│  │                                                                 │       │
│  │  ┌─── 优先级 3: 双模路由 ──────────────────────────────┐      │       │
│  │  │                                                       │      │       │
│  │  │  Mode A: ollama_direct (默认, ~1-2s)                  │      │       │
│  │  │  ┌───────────────────────────────────────┐           │      │       │
│  │  │  │ 1. _build_routing_system_message()    │           │      │       │
│  │  │  │    读取 MEMORY.md → 提取路由规则      │           │      │       │
│  │  │  │    + 延迟统计 → 生成system prompt     │           │      │       │
│  │  │  │                                       │           │      │       │
│  │  │  │ 2. 调用 Ollama qwen2.5:3b             │           │      │       │
│  │  │  │    num_ctx=2048, temp=0.0, format=json│           │      │       │
│  │  │  │    → 输出JSON路由决策                  │           │      │       │
│  │  │  │                                       │           │      │       │
│  │  │  │ 3. 解析JSON + _post_validate_route() │           │      │       │
│  │  │  └───────────────────────────────────────┘           │      │       │
│  │  │                                                       │      │       │
│  │  │  Mode B: hermes_agent (~5s)                           │      │       │
│  │  │  ┌───────────────────────────────────────┐           │      │       │
│  │  │  │ 1. 构建Agent专用routing prompt        │           │      │       │
│  │  │  │    (强调no tool calls)                 │           │      │       │
│  │  │  │                                       │           │      │       │
│  │  │  │ 2. 调用 Hermes Agent API              │           │      │       │
│  │  │  │    model="hermes-agent"                │           │      │       │
│  │  │  │    每次新建session (避免prompt膨胀)    │           │      │       │
│  │  │  │                                       │           │      │       │
│  │  │  │ 3. 解析 + _post_validate_route()     │           │      │       │
│  │  │  └───────────────────────────────────────┘           │      │
│  │  └──────────────────────────────────────────────────────┘      │       │
│  └──────────────────────────────────────────────────────────────────┘       │
│         │                                                                   │
│         ▼                                                                   │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │          Step 3b: 后验校验 (_post_validate_route)                │       │
│  │                                                                 │       │
│  │  4类策略, 优先级从高到低:                                        │       │
│  │  ┌─ P1: 隐私覆盖 ─── require_local/隐私关键词 → local_inference│       │
│  │  ├─ P2: 多模态 ─── 图片/音频/视频关键词 → multimodal           │       │
│  │  ├─ P3: 多步骤 ─── 工具调用/code_execution → gateway           │       │
│  │  └─ P4: 简单单步 ──────────────────────── → direct_local       │       │
│  │                                                                 │       │
│  │  ★ LLM complexity_score 不作为硬阈值 (3B模型评分不可靠)         │       │
│  └──────────────────────────────────────────────────────────────────┘       │
│         │                                                                   │
│         ▼                                                                   │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │          Step 4: 下游分发 (DispatchWorker._dispatch)            │       │
│  │                                                                 │       │
│  │  ┌─────────────────┐                                            │       │
│  │  │ direct_local    │→ Ollama :11434 /v1/chat/completions        │       │
│  │  │                 │  model=LOCAL_MODEL (qwen2.5:3b)            │       │
│  │  └─────────────────┘                                            │       │
│  │  ┌─────────────────┐                                            │       │
│  │  │ gateway         │→ OfficialGW :3005 /v1/chat/completions     │       │
│  │  │                 │  model="openclaw/default"                  │       │
│  │  └─────────────────┘                                            │       │
│  │  ┌─────────────────┐                                            │       │
│  │  │ k8s_gateway     │→ OpenClaw K8S Plugin                       │       │
│  │  │                 │  /plugins/k8s/v1/workloads                 │       │
│  │  └─────────────────┘                                            │       │
│  │  ┌─────────────────┐                                            │       │
│  │  │ multimodal      │→ Ollama :11434 /v1/chat/completions       │       │
│  │  │                 │  model=MULTIMODAL_MODEL (llava:7b)         │       │
│  │  └─────────────────┘                                            │       │
│  │  ┌─────────────────┐                                            │       │
│  │  │ local_inference │→ Ollama :11434 (隐私标记, 同direct_local) │       │
│  │  └─────────────────┘                                            │       │
│  │  ┌─────────────────┐                                            │       │
│  │  │ 未知路由         │→ fallback direct_local                    │       │
│  │  └─────────────────┘                                            │       │
│  └──────────────────────────────────────────────────────────────────┘       │
│         │                                                                   │
│         ▼                                                                   │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │          Step 5: 反馈闭环 (详见第4节)                            │       │
│  └──────────────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Nginx 路由映射规则

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Nginx Reverse Proxy (:80)                       │
│                     Host: 8080 (Docker) / 8090 (K8S)               │
│                                                                     │
│  ┌─── LiteLLM 路由 ────────────────────────────────────────────┐   │
│  │  /v1/chat/completions  ─→ litellm_backend (least_conn)      │   │
│  │  /v1/completions       ─→ litellm_backend                   │   │
│  │  /v1/embeddings        ─→ litellm_backend                   │   │
│  │  /v1/audio/*           ─→ litellm_backend (50M body, 120s) │   │
│  │  /v1/models            ─→ litellm_backend                   │   │
│  │  /v1/multimodal/*      ─→ litellm_backend (50M body, 120s) │   │
│  │  /litellm/* /ui/*      ─→ litellm_backend (admin UI)       │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─── Hermes 路由 ──────────────────────────────────────────── ─┐  │
│  │  /v1/chat (exact)      ─→ hermes_backend (300s, 50M body)  │   │
│  │  /k8s/*                 ─→ hermes_backend (120s)            │   │
│  │  /hermes/health         ─→ hermes_backend                   │   │
│  │  /hermes/stats          ─→ hermes_backend                   │   │
│  │  /v1/system/metrics     ─→ hermes_backend                   │   │
│  │  /dashboard.html        ─→ hermes_backend (静态文件)        │   │
│  │  /new_dashboard.html    ─→ hermes_backend                   │   │
│  │  /static/*              ─→ hermes_backend                   │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─── Stream Service 路由 ─────────────────────────────────────┐  │
│  │  /v1/stream/ws         ─→ stream_ws_backend (WS, 3600s)    │   │
│  │  /v1/stream/sse        ─→ stream_backend (ip_hash, 3600s)  │   │
│  │  /v1/stream/asr        ─→ stream_ws_backend (WS, 3600s)    │   │
│  │  /v1/stream/asr/file   ─→ stream_backend (50M body, 120s) │   │
│  │  /v1/stream/health     ─→ stream_backend                   │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─── MinIO 路由 ──────────────────────────────────────────────┐  │
│  │  /v1/minio/*           ─→ minio:9000 (S3 API, 300s)        │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─── Dashboard 健康代理 ──────────────────────────────────────┐  │
│  │  /svc/litellm/*        ─→ LiteLLM health                    │   │
│  │  /svc/stream/*         ─→ Stream health                     │   │
│  │  /svc/funasr/*         ─→ FunASR health                     │   │
│  │  /svc/ollama/*         ─→ Ollama health                     │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ★ 关键设计:                                                       │
│  /v1/chat (精确匹配) → Hermes智能路由                              │
│  /v1/chat/completions → LiteLLM标准OpenAI兼容API                  │
│  两者的区别在于请求是否需要智能路由决策                              │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.3 统一入口 `/v1/chat` 自动路由

```
POST /v1/chat → Hermes server.py

                    ┌─── type=chat ────────────→ LiteLLM Proxy (:4000) /v1/chat/completions
                    │                            本地模型对话
                    │
                    ├─── type=vision ──────────→ LiteLLM Proxy (:4000) /v1/chat/completions
                    │                            切换到vision模型 (llava)
                    │
request.type ───────├─── type=asr ─────────────→ Stream Service (:8084) /v1/stream/asr/file
                    │                            文件上传语音识别
                    │
                    ├─── type=multimodal ──────→ LiteLLM Proxy (:4000) /v1/multimodal/chat
                    │                            多模态统一处理
                    │
                    ├─── type=stream_asr ──────→ WebSocket → Stream Service (:8084) /v1/stream/asr
                    │                            实时流式语音识别
                    │
                    └─── type=k8s_workload ───→ K8S APIs → OpenClaw K8S Plugin
                                               AIWorkload提交/查询/删除
```

## 3. Hermes 内部模块架构图

### 3.1 核心模块关系图

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          Hermes Agent 核心架构                               │
│                                                                              │
│  ┌────────────────────── server.py (FastAPI :8082) ────────────────────────┐│
│  │                                                                        ││
│  │  模块初始化链:                                                          ││
│  │  LLMEnhancer() ──→ HermesAgent().bootstrap_from_rules() ──→            ││
│  │  OfficialHermesAdapter(llm_url, official_gw_url) ──→                   ││
│  │  HermesRouter(llm_enhancer, hermes_agent, official_agent) ──→          ││
│  │  create_queue(backend="auto") ──→                                      ││
│  │  DispatchWorker(queue, adapter, max_workers=4)                         ││
│  │                                                                        ││
│  │  ┌─────────────────────────────────────────────────────────────┐       ││
│  │  │                    API Endpoints                             │       ││
│  │  │                                                             │       ││
│  │  │  队列API: /queue/submit, /queue/submit-sync,               │       ││
│  │  │           /queue/results, /queue/feedback                   │       ││
│  │  │                                                             │       ││
│  │  │  路由API: /route, /route/analyze                            │       ││
│  │  │                                                             │       ││
│  │  │  K8S API: /k8s/workloads (POST/GET/DELETE),                │       ││
│  │  │           /k8s/callback                                     │       ││
│  │  │                                                             │       ││
│  │  │  统一入口: /v1/chat                                         │       ││
│  │  │                                                             │       ││
│  │  │  系统API: /health, /stats, /models, /state,                │       ││
│  │  │           /v1/system/metrics                                │       ││
│  │  │                                                             │       ││
│  │  │  Agent管理: /agent, /agent/config, /agent/skills,          │       ││
│  │  │             /agent/bootstrap                                │       ││
│  │  │                                                             │       ││
│  │  │  Official Agent: /official-agent/route,                     │       ││
│  │  │                  /official-agent/feedback,                   │       ││
│  │  │                  /official-agent/toggle,                     │       ││
│  │  │                  /official-agent/mode,                       │       ││
│  │  │                  /official-agent/skills                      │       ││
│  │  │                                                             │       ││
│  │  │  Memory: /memory/recent, /memory/search,                   │       ││
│  │  │          /memory/content, /evolution                        │       ││
│  │  └─────────────────────────────────────────────────────────────┘       ││
│  └────────────────────────────────────────────────────────────────────────┘│
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────────┐
│  │                        路由决策引擎                                       │
│  │                                                                          │
│  │  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  │              HermesRouter (router.py)                            │    │
│  │  │                                                                 │    │
│  │  │  score_complexity() ─── 六维规则评分                             │    │
│  │  │    ├─ request_type权重 (chat=5, tool_call=35, code=40)         │    │
│  │  │    ├─ tool_call评分  (base=30 + 10×n)                          │    │
│  │  │    ├─ prompt_length (0-18分, 按字数阈值)                       │    │
│  │  │    ├─ 复杂关键词   (moderate=5, high=10)                       │    │
│  │  │    ├─ 上下文评分   (0-15)                                      │    │
│  │  │    └─ 优先级调整   (p1=+10, p5=-5)                             │    │
│  │  │                                                                 │    │
│  │  │  route() ─── 综合决策流程                                       │    │
│  │  │    ├─ [有official_agent] → _route_via_official_agent()          │    │
│  │  │    ├─ [有hermes_agent]   → _route_via_agent()                  │    │
│  │  │    ├─ [默认]             → llm_enhancer.classify() + merge()   │    │
│  │  │    ├─ _match_skill()     → RoutingMemory SQLite (FTS5)         │    │
│  │  │    ├─ _learn_from_memory() → FTS5相似历史检索                   │    │
│  │  │    ├─ exploration       → ε-greedy探索 (rate=0.1)              │    │
│  │  │    └─ _select_model()   → 性能加权选择                          │    │
│  │  │       (success_rate×0.4 + latency×0.3 + cost×0.2 + fresh×0.1) │    │
│  │  │                                                                 │    │
│  │  │  RoutePath 枚举:                                                │    │
│  │  │    DIRECT_LOCAL, GATEWAY, AGENT_CHAIN,                         │    │
│  │  │    DIRECT_CLOUD, LOCAL_INFERENCE, MULTIMODAL                   │    │
│  │  │                                                                 │    │
│  │  │  自学习:                                                         │    │
│  │  │    record_result() → ModelPerformance/PathPerformance EMA更新  │    │
│  │  │                    → RoutingMemory SQLite写入                   │    │
│  │  │                    → _try_generate_skill() (成功模式自动创建)   │    │
│  │  │                    → _evolve() (阈值/探索率/衰减调整)           │    │
│  │  └─────────────────────────────────────────────────────────────────┘    │
│  │          │                                                               │
│  │          │ 调用                                                           │
│  │          ▼                                                               │
│  │  ┌───────────────────────┐  ┌──────────────────────┐  ┌──────────────┐ │
│  │  │  LLMEnhancer          │  │  HermesAgent         │  │  RoutingMemory│ │
│  │  │  (llm_enhancer.py)    │  │  (agent.py)          │  │  (SQLite DB) │ │
│  │  │                       │  │                      │  │              │ │
│  │  │  Ollama qwen2.5:3b    │  │  RoutingSkill系统    │  │              │ │
│  │  │  意图分类             │  │                      │  │  routing_    │ │
│  │  │  merge_scores()       │  │  decide()流程:       │  │  history     │ │
│  │  │  规则60%+LLM40%加权   │  │  ├─cache查找        │  │  (FTS5)      │ │
│  │  │                       │  │  ├─skill候选匹配    │  │  routing_    │ │
│  │  │  激活条件:             │  │  ├─LLM决策(低置信) │  │  skills       │ │
│  │  │  rule_score>=10       │  │  ├─ε-greedy探索     │  │  learning_   │ │
│  │  │                       │  │  └─fallback          │  │  state       │ │
│  │  │  缓存: max 200        │  │                      │  │  evolution_  │ │
│  │  │                       │  │  缓存: max 100       │  │  log         │ │
│  │  └───────────────────────┘  │                      │  │              │ │
│  │                              │  自学习:              │  └──────────────┘ │
│  │                              │  record_feedback()   │                   │
│  │                              │  ├─skill置信度更新   │                   │
│  │                              │  ├─挂起/恢复(<0.2/>0.5)│                 │
│  │                              │  └─探索率自适应       │                   │
│  │                              │                      │                   │
│  │                              │  bootstrap: 18个种子│                   │
│  │                              │  skill从规则加载     │                   │
│  │                              └──────────────────────┘                   │
│  └──────────────────────────────────────────────────────────────────────────┘
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────────┐
│  │              OfficialHermesAdapter (official_agent_adapter.py)           │
│  │                                                                          │
│  │  双模路由:                                                                │
│  │  ┌─ Mode A: ollama_direct (默认, ~1-2s) ─────────────────────────────┐ │
│  │  │  _route_via_ollama_direct()                                        │ │
│  │  │  ┌────────────────────────────────────────────────────────────┐   │ │
│  │  │  │ 1. _build_routing_system_message()                         │   │ │
│  │  │  │    ├─ _load_memory_context() [10s TTL缓存]                │   │ │
│  │  │  │    ├─ _parse_routing_rules_from_memory()                  │   │ │
│  │  │  │    │   解析 "Category [kw1,kw2] → route" 格式             │   │ │
│  │  │  │    ├─ _load_latency_stats_from_memory()                  │   │ │
│  │  │  │    └─ 组合 → system prompt (top 10规则)                  │   │ │
│  │  │  │                                                            │   │ │
│  │  │  │ 2. Ollama /v1/chat/completions                           │   │ │
│  │  │  │    num_ctx=2048, temperature=0.0, format=json             │   │ │
│  │  │  │    → JSON路由决策                                         │   │ │
│  │  │  │                                                            │   │ │
│  │  │  │ 3. _post_validate_route() 后验校验                        │   │ │
│  │  │  └────────────────────────────────────────────────────────────┘   │ │
│  │  └──────────────────────────────────────────────────────────────────┘ │ │
│  │  ┌─ Mode B: hermes_agent (~5s) ─────────────────────────────────────┐ │
│  │  │  _route_via_hermes_agent()                                       │ │
│  │  │  → Hermes Agent API /v1/chat/completions                        │ │
│  │  │  → 每次新建session (避免prompt膨胀)                             │ │
│  │  │  → 同样解析 + 后验校验                                         │ │
│  │  └──────────────────────────────────────────────────────────────────┘ │ │
│  │                                                                          │
│  │  熔断器: 2次连续失败 → 120s熔断 → 关键词路由降级                       │
│  │  预热: _warmup_thread 启动时预加载Ollama模型到GPU                      │
│  │  持久连接: _ollama_client httpx连接池                                  │
│  └──────────────────────────────────────────────────────────────────────────┘
```

### 3.2 DispatchWorker 分发架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│                   DispatchWorker (dispatch_worker.py)                     │
│                                                                          │
│  ┌─ Worker Loop (_worker_loop, 4线程并发) ────────────────────────────┐ │
│  │                                                                    │ │
│  │  while running:                                                    │ │
│  │    msg = queue.pop(QUEUE_REQUESTS, timeout=0.5)                    │ │
│  │    if msg:                                                          │ │
│  │      _process_request(msg):                                        │ │
│  │                                                                    │ │
│  │  ┌─ _process_request ──────────────────────────────────────────┐  │ │
│  │  │                                                              │  │ │
│  │  │  Step 1: 路由决策                                            │  │ │
│  │  │    acquire(_routing_semaphore)  ←── limit=1                  │  │ │
│  │  │    routing = adapter.route_via_agent(request)                │  │ │
│  │  │    release(_routing_semaphore)                                │  │ │
│  │  │                                                              │  │ │
│  │  │  Step 2: 下游分发                                            │  │ │
│  │  │    result = _dispatch(request, routing)                      │  │ │
│  │  │                                                              │  │ │
│  │  │  Step 3: 反馈记录                                            │  │ │
│  │  │    _record_feedback(request, routing, result)                │  │ │
│  │  │    → push(QUEUE_FEEDBACK)                                    │  │ │
│  │  │    → adapter.record_feedback_via_memory()                    │  │ │
│  │  │                                                              │  │ │
│  │  │  Step 4: 结果推送                                            │  │ │
│  │  │    push(QUEUE_RESULTS, result)                               │  │ │
│  │  │                                                              │  │ │
│  │  │  Step 5: 通知同步等待者                                      │  │ │
│  │  │    _pending_sync[request_id].event.set()                     │  │ │
│  │  └──────────────────────────────────────────────────────────────┘  │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─ 分发路径详细 ──────────────────────────────────────────────────────┐ │
│  │                                                                    │ │
│  │  ┌─── direct_local ──────────────────────────────────────────────┐ │ │
│  │  │  acquire(_ollama_semaphore)  ←── limit=2                      │ │ │
│  │  │  Ollama :11434 /v1/chat/completions                           │ │ │
│  │  │  model = qwen2.5:3b                                          │ │ │
│  │  │  持久httpx客户端(连接池, 避免stale连接)                       │ │ │
│  │  │  release(_ollama_semaphore)                                   │ │ │
│  │  └───────────────────────────────────────────────────────────────┘ │ │
│  │                                                                    │ │
│  │  ┌─── gateway ────────────────────────────────────────────────────┐ │ │
│  │  │  acquire(_gw_semaphore)  ←── limit=3                          │ │ │
│  │  │  OfficialGW :3005 /v1/chat/completions                        │ │ │
│  │  │  model = "openclaw/default"                                   │ │ │
│  │  │  release(_gw_semaphore)                                       │ │ │
│  │  └───────────────────────────────────────────────────────────────┘ │ │
│  │                                                                    │ │
│  │  ┌─── k8s_gateway ───────────────────────────────────────────────┐ │ │
│  │  │  OpenClaw K8S Plugin /plugins/k8s/v1/workloads                │ │ │
│  │  │  支持: submit (POST), get (GET), delete (DELETE)             │ │ │
│  │  │  taskType: batch-inference, distributed-training,            │ │ │
│  │  │           hyperparameter-tuning                               │ │ │
│  │  └───────────────────────────────────────────────────────────────┘ │ │
│  │                                                                    │ │
│  │  ┌─── multimodal ────────────────────────────────────────────────┐ │ │
│  │  │  预检查: _get_ollama_models() (60s缓存)                       │ │ │
│  │  │  确认llava可用 → Ollama :11434                                │ │ │
│  │  │  model = llava:7b                                             │ │ │
│  │  └───────────────────────────────────────────────────────────────┘ │ │
│  │                                                                    │ │
│  │  ┌─── local_inference ───────────────────────────────────────────┐ │ │
│  │  │  同direct_local, 但标记privacy=True                           │ │ │
│  │  │  隐私敏感场景, 严格本地执行                                    │ │ │
│  │  └───────────────────────────────────────────────────────────────┘ │ │
│  │                                                                    │ │
│  │  ┌─── 未知/失败 ────────────────────────────────────────────────┐ │ │
│  │  │  fallback → direct_local                                      │ │ │
│  │  └───────────────────────────────────────────────────────────────┘ │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

## 4. 自学习闭环架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    ★ 自学习闭环 — 三条并行反馈路径 ★                         │
│                                                                             │
│                         ┌───────────────┐                                   │
│                         │  Dispatch     │                                   │
│                         │  Result       │                                   │
│                         │  (成功/失败,  │                                   │
│                         │   延迟ms)     │                                   │
│                         └───────┬───────┘                                   │
│                                 │                                           │
│          ┌──────────────────────┼──────────────────────┐                    │
│          │                      │                      │                    │
│          ▼                      ▼                      ▼                    │
│  ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────────────┐    │
│  │  Path 1:         │ │  Path 2:         │ │  Path 3:                │    │
│  │  HermesRouter    │ │  HermesAgent     │ │  MEMORY.md 闭环         │    │
│  │  内部学习        │ │  Skill进化       │ │  (★ 主学习机制 ★)      │    │
│  │  (router.py)     │ │  (agent.py)      │ │  (official_agent_       │    │
│  │                  │ │                  │ │   adapter.py)            │    │
│  │  record_result() │ │  record_         │ │                          │    │
│  │  ├─ ModelPerf    │ │  feedback()      │ │  record_feedback_via_   │    │
│  │  │  EMA更新      │ │  ├─ Skill        │ │  memory()               │    │
│  │  │  (latency/    │ │  │  confidence   │ │                          │    │
│  │  │   success)    │ │  │  = hit/succ   │ │  即时:                   │    │
│  │  ├─ PathPerf     │ │  ├─ 挂起<0.2    │ │  _write_feedback_local() │    │
│  │  │  EMA更新      │ │  │  (5次后)      │ │  → .hermes/routing_     │    │
│  │  ├─ SQLite写入   │ │  ├─ 恢复≥0.5    │ │    feedback/feedback.    │    │
│  │  │  routing_     │ │  └─ 探索率      │ │    jsonl                  │    │
│  │  │  history      │ │     自适应      │ │                          │    │
│  │  │  (FTS5)       │ │     (>0.9→降    │ │  异步(2s间隔):           │    │
│  │  ├─ _try_        │ │     <0.7→升)    │ │  _memory_writer_loop()   │    │
│  │  │  generate_    │ │                  │ │  ├─ Feedback History     │    │
│  │  │  skill()      │ │                  │ │  │  (滚动窗口10条)        │    │
│  │  │  3+成功→自动  │ │                  │ │  ├─ Latency Stats        │    │
│  │  │  创建skill   │ │                  │ │  │  (avg/p95 per route)   │    │
│  │  └─ _evolve()    │ │                  │ │  │                       │    │
│  │     每50次→阈值  │ │                  │ │  ├─ 失败时:              │    │
│  │     每100→探索率 │ │                  │ │  │  _evolve_rule_from_    │    │
│  │     每200→衰减   │ │                  │ │  │  feedback()            │    │
│  │                  │ │                  │ │  │  → 修正MEMORY.md规则  │    │
│  └──────────────────┘ │                  │ │  ├─ 成功时:              │    │
│          │            │                  │ │  │  _discover_and_add_   │    │
│          │            │                  │ │  │  pattern()            │    │
│          │            │                  │ │  │  → 补充关键词到规则   │    │
│          │            │                  │ │  ├─ 高延迟成功:         │    │
│          │            │                  │ │  │  _reinforce_latency_  │    │
│          │            │                  │ │  │  awareness()          │    │
│          │            │                  │ │  │  → Key Rules加注释   │    │
│          │            │                  │ │  ├─ _prune_latency_gap() │    │
│          │            │                  │ │  │  GW 10x慢→强化local  │    │
│          │            │                  │ │  └─ Cache失效:           │    │
│          │            │                  │ │     _memory_context_     │    │
│          │            │                  │ │     cached_at = 0        │    │
│          │            │                  │ │                          │    │
│          │            │                  │ └──────────────────────────┘    │
│          │            │                  │         │                        │
│          │            │                  │         │ 写入                   │
│          │            │                  │         ▼                        │
│          │            │                  │  ┌──────────────────────┐        │
│          │            │                  │  │   .hermes/memories/  │        │
│          │            │                  │  │   MEMORY.md          │        │
│          │            │                  │  │                      │        │
│          │            │                  │  │ ┌─Routing Patterns──┐│        │
│          │            │                  │  │ │ Category [kw]→    ││        │
│          │            │                  │  │ │ route             ││        │
│          │            │                  │  │ └──────────────────┘│        │
│          │            │                  │  │ ┌─Key Rules─────── ─┐│        │
│          │            │                  │  │ │ 延迟差距/覆盖等   ││        │
│          │            │                  │  │ └──────────────────┘│        │
│          │            │                  │  │ ┌─Feedback History─┐│        │
│          │            │                  │  │ │ 滚动10条          ││        │
│          │            │                  │  │ └──────────────────┘│        │
│          │            │                  │  │ ┌─Latency Stats────┐│        │
│          │            │                  │  │ │ avg/p95/samples   ││        │
│          │            │                  │  │ └──────────────────┘│        │
│          │            │                  │  └──────────────────────┘        │
│          │            │                  │         │                        │
│          │            │                  │         │ 下次路由读取           │
│          │            │                  │         ▼                        │
│          │            │                  │  ┌──────────────────────┐        │
│          │            │                  │  │ _build_routing_      │        │
│          │            │                  │  │ system_message()     │        │
│          │            │                  │  │                      │        │
│          │            │                  │  │ MEMORY.md → 提取规则 │        │
│          │            │                  │  │ + 延迟统计 → system  │        │
│          │            │                  │  │ prompt → 注入到      │        │
│          │            │                  │  │ Ollama/Agent路由     │        │
│          │            │                  │  │                      │        │
│          │            │                  │  │ + _check_memory_     │        │
│          │            │                  │  │   override()         │        │
│          │            │                  │  │   MEMORY关键词→后验  │        │
│          │            │                  │  │   覆盖               │        │
│          │            │                  │  └──────────────────────┘        │
│          │            │                  │                                  │
│          ▼            ▼                  ▼                                  │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │                    下次请求路由决策                               │       │
│  │                                                                  │       │
│  │  三条路径汇聚影响:                                               │       │
│  │  ├─ Path1 → HermesRouter路由 (EMA性能+SQLite技能匹配)           │       │
│  │  ├─ Path2 → HermesAgent决策 (Skill置信度+探索率)                │       │
│  │  └─ Path3 → OfficialHermesAdapter (MEMORY.md规则+延迟统计)      │       │
│  │                                                                  │       │
│  │  ★ 形成持续改进闭环 ★                                           │       │
│  └──────────────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 5. Bridge + Gateway + Scheduler 架构图 (传统三层架构)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                  传统三层架构 (Bridge/Gateway/Scheduler)                      │
│                  ★ 当前已降级为可选/遗留路径 ★                                │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐    │
│  │                Scheduler (FastAPI :8000, Python)                      │    │
│  │                                                                      │    │
│  │  入口: POST /dispatch                                                │    │
│  │                                                                      │    │
│  │  ┌─ Pre-Hook Pipeline ────────────────────────────────────────────┐ │    │
│  │  │  P5:  RateLimitHook (全局60RPM, 每appid 30RPM, 滑动60s窗口) │ │    │
│  │  │  P10: RequestValidationHook (appid/prompt非空, timeout>=1s) │ │    │
│  │  │  P20: ConstraintEnrichmentHook (默认约束填充, 限费0.05)   │ │    │
│  │  │  P90: RequestLoggingHook (日志记录)                        │ │    │
│  │  └──────────────────────────────────────────────────────────────────┘ │    │
│  │          │                                                            │    │
│  │          ▼                                                            │    │
│  │  MainDispatcherAgent.dispatch()                                       │    │
│  │  ├─ [use_openclaw=True] → StrategyRouter.route()                     │    │
│  │  │                        → OpenClawAgent.execute()                   │    │
│  │  │                        → Bridge :3001 /dispatch                   │    │
│  │  └─ [use_openclaw=False] → Local/CloudAgent → 直连Provider          │    │
│  │          │                                                            │    │
│  │          ▼                                                            │    │
│  │  ┌─ Post-Hook Pipeline ────────────────────────────────────────────┐ │    │
│  │  │  P10: ResponseSanitizationHook (正则脱敏api_key/password) │ │    │
│  │  │  P20: CostCalculationHook ($0.001/1k输入+$0.002/1k输出) │ │    │
│  │  │  P30: RetryDecisionHook (max_retries=2)                   │ │    │
│  │  │  P90: ResponseLoggingHook (日志)                           │ │    │
│  │  └──────────────────────────────────────────────────────────────────┘ │    │
│  │                                                                      │    │
│  │  子进程管理: _start_bridge() + _start_gateway()                      │    │
│  └──────────────────────────────────────────────────────────────────────┘    │
│          │ (HTTP)                                                           │
│          ▼                                                                  │
│  ┌──────────────────────────────────────────────────────────────────────┐    │
│  │               Bridge (Node.js :3001, orchestrator.mjs)               │    │
│  │                                                                      │    │
│  │  入口: POST /dispatch                                                │    │
│  │                                                                      │    │
│  │  ┌─ Smart Router 六维评分 ────────────────────────────────────────┐ │    │
│  │  │  smartRouterScore() → 0-100分                                    │ │    │
│  │  │  ├─ typeWeights (chat=5, tool_call=35, code=40)              │ │    │
│  │  │  ├─ toolCallScore (base=30 + 10×n)                            │ │    │
│  │  │  ├─ promptLength (≤50→0, ≤200→3, ≤500→8, ≤1k→12, >1k→18) │ │    │
│  │  │  ├─ complexityKeywords (moderate=5, high=10)                 │ │    │
│  │  │  ├─ contextScore (×5, max=15)                                 │ │    │
│  │  │  └─ priorityBoost (p1→+10, p5→-5)                             │ │    │
│  │  │                                                                │ │    │
│  │  │  score < 40 → "gateway" (低延迟本地)                          │ │    │
│  │  │  score ≥ 40 → "agent" (复杂多步, Agent链)                    │ │    │
│  │  └──────────────────────────────────────────────────────────────────┘ │    │
│  │          │                                                            │    │
│  │          ▼                                                            │    │
│  │  route_mode 选择:                                                     │    │
│  │  ├─ "smart" → Smart Router评分 → agent/gateway                     │    │
│  │  ├─ "agent" → 始终 dispatchViaAgentChain                            │    │
│  │  └─ "gateway" → 始终 dispatchViaOpenClaw                            │    │
│  │                                                                      │    │
│  │  ┌─ dispatchViaAgentChain (快速路径) ──────────────────────────────┐ │    │
│  │  │  callOpenClawOfficialGateway(:3005) → 成功:返回              │ │    │
│  │  │                                  → 失败:降级Gateway            │ │    │
│  │  │  ★ 跳过Agent Soul 5步流水线 (OfficialGW内部有Agent循环)       │ │    │
│  │  └──────────────────────────────────────────────────────────────────┘ │    │
│  │  ┌─ dispatchViaOpenClaw (自适应路由) ──────────────────────────── ─┐ │    │
│  │  │  adaptiveRoute() → 6分支决策:                                  │ │    │
│  │  │  ├─ require_local → local + cloud fallback                    │ │    │
│  │  │  ├─ preferred_providers → 匹配provider                        │ │    │
│  │  │  ├─ 高优先级+localFirst → local优先                           │ │    │
│  │  │  ├─ 复杂类型+preferCloud → cloud优先(工具优先)               │ │    │
│  │  │  ├─ 默认localFirst → local+cloud fallback                    │ │    │
│  │  │  └─ 无local → cloud only                                      │ │    │
│  │  │                                                                │ │    │
│  │  │  → callModelApi() → Gateway(:3000) 或直连Provider            │ │    │
│  │  └──────────────────────────────────────────────────────────────────┘ │    │
│  │                                                                      │    │
│  │  Agent Soul 系统:                                                    │    │
│  │  ├─ local-dispatcher  personality="efficient, precise, low-latency" │    │
│  │  ├─ cloud-dispatcher  personality="comprehensive, powerful"         │    │
│  │  ├─ code-executor     personality="rigorous, precise, code-first"  │    │
│  │  └─ main              personality="general, flexible, comprehensive"│    │
│  │  每个: 5步执行流水线 analyze→select→execute→validate→report        │    │
│  │                                                                      │    │
│  │  服务管理: /services/start, /services/stop (子进程启停)             │    │
│  │  Hermes代理: /hermes/* → 转发到 Hermes :8082                       │    │
│  └──────────────────────────────────────────────────────────────────────┘    │
│          │ (HTTP)                                                           │
│          ▼                                                                  │
│  ┌──────────────────────────────────────────────────────────────────────┐    │
│  │              Gateway (Node.js :3000, gateway.mjs)                    │    │
│  │                                                                      │    │
│  │  入口: /v1/chat/completions                                          │    │
│  │                                                                      │    │
│  │  resolveModel() → 三级模型解析:                                      │    │
│  │  ├─ "openclaw/*" → strip→Agent→primary model                       │    │
│  │  ├─ 精确匹配 → MODEL_CATALOG[model_id]                             │    │
│  │  └─ 模糊匹配 → modelId field search                                │    │
│  │                                                                      │    │
│  │  callProviderApi() → {endpoint.baseUrl}/chat/completions            │    │
│  │                                                                      │    │
│  │  Fallback链: agent.fallbacks → 逐个尝试(最多2个)                   │    │
│  │  失败 → 502 ALL_MODELS_FAILED                                       │    │
│  │                                                                      │    │
│  │  Web Console: /console (嵌入式单页HTML, 深色主题)                    │    │
│  │  请求日志: requestLog (200条循环缓冲)                                │    │
│  └──────────────────────────────────────────────────────────────────────┘    │
│          │                                                                   │
│          ▼                                                                   │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                    下游模型 Provider                                │    │
│  │                                                                     │    │
│  │  Ollama (:11434)    Moonshot API    DeepSeek API                   │    │
│  │  qwen2.5:3b         kimi-k2.6       deepseek-chat                  │    │
│  │  llava:7b                                                          │    │
│  │  nomic-embed-text                                                  │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
│  ┌─ StrategyRouter (Python, scheduler/strategy/) ──────────────────────┐    │
│  │                                                                     │    │
│  │  AdaptiveStrategy ── 6策略:                                        │    │
│  │  LOCAL_FIRST, CLOUD_FIRST, COST_OPTIMIZED,                         │    │
│  │  LATENCY_OPTIMIZED, CAPABILITY_OPTIMIZED, PRIVACY_FIRST           │    │
│  │                                                                     │    │
│  │  _classify_request():                                               │    │
│  │  require_local → PRIVACY_FIRST                                     │    │
│  │  require_gpu → CAPABILITY_OPTIMIZED                                │    │
│  │  preferred_providers=cloud → CLOUD_FIRST                           │    │
│  │  type in COMPLEXITY_INDICATORS → CAPABILITY_OPTIMIZED              │    │
│  │  CRITICAL/HIGH priority → LATENCY_OPTIMIZED                       │    │
│  │  LOW/BACKGROUND → COST_OPTIMIZED                                   │    │
│  │  max_cost≤0 → COST_OPTIMIZED                                       │    │
│  │  max_latency≤1s → LATENCY_OPTIMIZED                                │    │
│  │  estimated_context>16k → CAPABILITY_OPTIMIZED                      │    │
│  │  default → LOCAL_FIRST                                              │    │
│  │                                                                     │    │
│  │  自适应权重学习 (alpha=0.1):                                        │    │
│  │  local成功快 → local+=α, cloud-=α                                  │    │
│  │  local失败 → local-=2α, cloud+=2α                                  │    │
│  │  权重区间: [0.1, 0.9]                                              │    │
│  │  初始: local=0.6, cloud=0.4                                        │    │
│  │                                                                     │    │
│  │  LoadBalanceStrategy ── 5算法:                                     │    │
│  │  ROUND_ROBIN, WEIGHTED_RANDOM, LEAST_CONNECTIONS,                 │    │
│  │  LEAST_LATENCY, POWER_OF_TWO                                      │    │
│  │                                                                     │    │
│  │  PriorityStrategy ── 评分:                                         │    │
│  │  base=100, priority×-15, load×-20, success×+10,                   │    │
│  │  latency违规-50, local+30/cloud-100, hint+25                      │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 6. Stream Service & LiteLLM Proxy 内部架构

### 6.1 Stream Service (:8084)

```
┌──────────────────────────────────────────────────────────────────────────┐
│                Stream Service (stream_service.py, :8084)                 │
│                                                                          │
│  ┌─── WebSocket /v1/stream/ws ────────────────────────────────────────┐ │
│  │  双向通信:                                                          │ │
│  │  前端 → JSON文本消息 或 Binary音频数据                               │ │
│  │  前端 ← ASR文本 + LLM分块 + llm_done                                │ │
│  │                                                                     │ │
│  │  音频处理: _pcm_to_wav() → WAV头封装 → ASR转录                     │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── WebSocket /v1/stream/asr ──────────────────────────────────────┐ │
│  │  实时流式ASR:                                                       │ │
│  │  前端 → PCM音频chunks (持续流式)                                    │ │
│  │  前端 ← asr_partial / asr_final                                     │ │
│  │                                                                     │ │
│  │  缓冲策略:                                                          │ │
│  │  MIN_BYTES=32000 (~1s) → 开始转录                                  │ │
│  │  MAX_BYTES=320000 (~10s) → 强制转录                                │ │
│  │  INTERVAL=2.0s → 定期转录                                          │ │
│  │                                                                     │ │
│  │  Auto-LLM: auto_llm=true → ASR文本积累后自动触发LLM流式回复        │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── SSE /v1/stream/sse ────────────────────────────────────────────┐ │
│  │  单向推流:                                                          │ │
│  │  GET请求 + prompt/model查询参数                                     │ │
│  │  ← SSE流式LLM回复 (data: {chunk}\n\n)                              │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── HTTP /v1/stream/asr/file ──────────────────────────────────────┐ │
│  │  文件上传ASR: multipart form上传 → ASR转录 → JSON结果              │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── ASR级联 (优先级从高到低) ──────────────────────────────────────┐ │
│  │                                                                     │ │
│  │  1. FunASR本地 (:8199)                                             │ │
│  │     SenseVoice / Paraformer-online                                  │ │
│  │     OpenAI兼容 /v1/audio/transcriptions                            │ │
│  │     _clean_funasr_text() → 去除特殊标签(语言/情感/说话人/BGM)      │ │
│  │                                                                     │ │
│  │  2. 豆包(火山引擎) 云端ASR                                         │ │
│  │     需要 DOUBAO_ASR_KEY                                             │ │
│  │                                                                     │ │
│  │  3. Ollama Whisper fallback                                        │ │
│  │     /api/generate with audio                                       │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── LLM流式回复 ──────────────────────────────────────────────────┐ │
│  │  stream_llm_response():                                             │ │
│  │  优先级: External LiteLLM Proxy (:4000) → Ollama直连 (:11434)     │ │
│  │  httpx.AsyncClient.stream() → SSE chunk消费                       │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

### 6.2 LiteLLM Proxy (:4000)

```
┌──────────────────────────────────────────────────────────────────────────┐
│              LiteLLM Proxy (litellm_proxy.py, :4000)                     │
│              ★ OpenAI兼容API ★ 水平扩展 (HPA 1-5副本) ★                  │
│                                                                          │
│  ┌─── 对话/补全 ──────────────────────────────────────────────────────┐ │
│  │  /v1/chat/completions                                               │ │
│  │  ├─ 检测image_url → 自动切换vision模型                             │ │
│  │  ├─ 附件预处理 (PDF→文本, Word→文本, 音频→ASR)                    │ │
│  │  ├─ _resolve_image_urls() → base64 data URI                       │ │
│  │  └─ call_litellm_chat() → External LiteLLM → Ollama fallback      │ │
│  │                                                                     │ │
│  │  /v1/completions → 遗留补全API                                     │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── 流式响应 ──────────────────────────────────────────────────────┐ │
│  │  /v1/chat/completions (stream=true)                                │ │
│  │  call_litellm_chat_stream() → SSE streaming                        │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── Vision 多模态 ────────────────────────────────────────────────┐ │
│  │  /v1/multimodal/chat                                                │ │
│  │  自动路由: image→vision, audio→ASR, pdf→提取, doc→提取             │ │
│  │  preprocess_attachments() → 格式转换+拼接                          │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── ASR 语音识别 ──────────────────────────────────────────────── ─┐ │
│  │  /v1/audio/transcriptions                                           │ │
│  │  级联: FunASR → External LiteLLM → 豆包 → Ollama Whisper          │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── Embedding 向量 ────────────────────────────────────────────── ─┐ │
│  │  /v1/embeddings                                                     │ │
│  │  → Ollama /api/embeddings (nomic-embed-text)                       │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── 模型路由策略 ──────────────────────────────────────────────── ─┐ │
│  │  latency-based-routing → 响应最快的模型优先                         │ │
│  │  模型组别名: chat→qwen2.5, vision→llava,                           │ │
│  │              asr→funasr, embedding→bge                              │ │
│  │  重试: 2次, 冷却60s (3次允许失败后)                                │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌─── 配置 (litellm_config.yaml) ────────────────────────────────────┐ │
│  │  模型列表:                                                          │ │
│  │  qwen2.5    → ollama/qwen2.5:7b    (:11434)  timeout=180s         │ │
│  │  llava       → ollama/llava:7b      (:11434)  timeout=180s         │ │
│  │  deepseek    → deepseek/deepseek-chat (cloud)  timeout=120s       │ │
│  │  moonshot    → moonshot/moonshot-v1-8k (cloud)  timeout=120s      │ │
│  │  doubao-asr  → 豆包ASR代理          timeout=60s                    │ │
│  │  funasr      → FunASR (:8199)       timeout=60s                    │ │
│  │  bge         → ollama/nomic-embed-text (:11434) timeout=30s       │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

## 7. Docker/K8S 部署架构

### 7.1 Docker Compose 部署拓扑

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Docker Compose 部署 (单机模式)                            │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────┐      │
│  │                      Nginx LB (:8080→:80)                        │      │
│  │  least_conn: litellm_backend (keepalive=32)                     │      │
│  │  least_conn: hermes_backend (keepalive=16)                      │      │
│  │  ip_hash:    stream_backend                                      │      │
│  │  round-robin: stream_ws_backend                                  │      │
│  └──────────────┬────────────────────┬────────────────────┬─────────┘      │
│                 │                    │                    │                 │
│                 ▼                    ▼                    ▼                 │
│  ┌──────────────────┐ ┌──────────────────┐ ┌───────────────────────┐      │
│  │ litellm (:4000)  │ │ hermes (:8082)   │ │ stream-service (:8084)│      │
│  │ ★ 可扩展 ★       │ │ 核心调度层       │ │ 内部端口(expose)      │      │
│  │ --scale litellm=N │ │                  │ │ WebSocket/SSE/ASR     │      │
│  │                  │ │ env:              │ │                       │      │
│  │ env:             │ │ OLLAMA_URL        │ │ env:                  │      │
│  │ LITELLM_MASTER_  │ │ LITELLM_INTERNAL  │ │ OLLAMA_URL            │      │
│  │ KEY              │ │ _URL              │ │ EXTERNAL_LITELLM_URL  │      │
│  │ DATABASE_URL     │ │ STREAM_INTERNAL   │ │ LITELLM_MASTER_KEY   │      │
│  │ STORE_MODEL_IN_DB │ │ _URL             │ │ FUNASR_URL            │      │
│  │                  │ │ LITELLM_MASTER_KEY│ │                       │      │
│  │ depends_on:      │ │ QUEUE_BACKEND=    │ │ depends_on:           │      │
│  │ litellm-db       │ │ inprocess         │ │ ollama + litellm     │      │
│  │ ollama           │ │                   │ │                       │      │
│  │                  │ │ depends_on:       │ │                       │      │
│  │                  │ │ ollama            │ │                       │      │
│  └──────────┬───────┘ └──────────┬───────┘ └──────────┬────────────┘      │
│             │                    │                    │                     │
│             ▼                    │                    ▼                     │
│  ┌──────────────────┐           │           ┌───────────────────────┐      │
│  │ litellm-db        │           │           │ funasr (:8199)        │      │
│  │ PostgreSQL 16     │           │           │ 内部端口(expose)      │      │
│  │ (:5432内部)       │           │           │ SenseVoice ASR       │      │
│  │                  │           │           │ cpu模式, 2-4Gi内存    │      │
│  └──────────────────┘           │           │ 120s初始延迟          │      │
│                                 │           └──────────┬────────────┘      │
│                                 │                      │                     │
│                                 ▼                      │                     │
│  ┌──────────────────────────────────────────┐          │                     │
│  │        Ollama (:11434)                   │◄─────────┘                     │
│  │        本地模型推理                       │                                 │
│  │        12Gi内存限制                       │                                 │
│  │        volumes: ollama_data              │                                 │
│  │        health: ollama list               │                                 │
│  └──────────────────────────────────────────┘                                 │
│                                                                                │
│  ┌──────────────────────────────────────────┐                                 │
│  │        MinIO (:9000 API / :9001 Console) │                                 │
│  │        S3兼容对象存储                     │                                 │
│  │        volumes: minio_data               │                                 │
│  └──────────────────────────────────────────┘                                 │
│                                                                                │
│  ★ 扩展模式:                                                                  │
│  docker compose up -d --scale litellm=3   → 3个LiteLLM实例                    │
│  docker compose up -d --scale stream=2    → 2个Stream实例                    │
│                                                                                │
│  ★ 端口映射:                                                                  │
│  8080→Nginx:80, 4000→LiteLLM, 8082→Hermes, 11434→Ollama                      │
│  9000→MinIO API, 9001→MinIO Console                                          │
│  Stream/FunASR/Postgres → 内部网络(未映射到host)                              │
└────────────────────────────────────────────────────────────────────────────────┘
```

### 7.2 K8S 部署架构

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Kubernetes 部署 (namespace: openclaw)                     │
│                    Kind本地集群 / 真实K8S集群                                │
│                                                                             │
│  ┌─ 资源清单 (14 YAML, 顺序编号00-11) ──────────────────────────────────┐ │
│  │  00-namespace.yaml      → Namespace: openclaw                       │ │
│  │  01-secrets.yaml        → Secret: API keys, master key, pg密码       │ │
│  │  02-configmaps.yaml     → ConfigMap: litellm-config + nginx-config   │ │
│  │  03-storage.yaml        → 3 PV/PVC: ollama(50Gi), minio(20Gi),      │ │
│  │                            postgres(5Gi), local-storage class        │ │
│  │  04-ollama.yaml         → Deployment+Service+NodePort(31143)        │ │
│  │                            Recreate策略, 1-12Gi memory               │ │
│  │  05-litellm-db.yaml     → Deployment+Service (PostgreSQL 16)        │ │
│  │                            Recreate策略, PVC-backed                  │ │
│  │  06-litellm.yaml        → Deployment+Service+HPA                    │ │
│  │                            min1 max5, CPU70%/Mem80%                  │ │
│  │  07-hermes.yaml         → Deployment+Service+HPA                    │ │
│  │                            min1 max5, CPU70%                         │ │
│  │  08-stream-service.yaml → Deployment+Service+HPA                    │ │
│  │                            min1 max5, CPU60%, sessionAffinity        │ │
│  │                            600s scaleDown stabilization              │ │
│  │  09-funasr.yaml         → Deployment+Service                        │ │
│  │                            Recreate策略, 2-4Gi memory               │ │
│  │  10-minio.yaml          → Deployment+Service+NodePort(30001)        │ │
│  │                            Recreate策略, PVC-backed                  │ │
│  │  11-nginx.yaml          → Deployment+Service+NodePort(30080)        │ │
│  │                            ConfigMap挂载nginx配置                   │ │
│  │  kind-config.yaml       → Kind集群: 单control-plane节点             │ │
│  │                            extraPortMappings + hostPath mounts       │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌─ HPA 自动伸缩 ───────────────────────────────────────────────────────┐ │
│  │                                                                     │ │
│  │  LiteLLM:   min=1, max=5, CPU≥70% / Mem≥80% → 扩容               │ │
│  │  Hermes:    min=1, max=5, CPU≥70% → 扩容                          │ │
│  │  Stream:    min=1, max=5, CPU≥60% → 扩容                          │ │
│  │             600s stabilization → 缩容延迟(避免WebSocket断连)       │ │
│  │                                                                     │ │
│  │  ★ 无状态服务可伸缩, 有状态服务Recreate策略(单副本)                 │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌─ Kind主机端口映射 (避免与Docker Compose冲突) ───────────────────────┐ │
│  │  Host:8090 → NodePort:30080 → Nginx (Docker用8080)                │ │
│  │  Host:11435 → NodePort:31143 → Ollama (Docker用11434)             │ │
│  │  Host:9002 → NodePort:30001 → MinIO Console (Docker用9001)        │ │
│  │                                                                     │ │
│  │  ★ Kind和Docker Compose可同时运行在同一主机                         │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌─ Stream Service会话亲和性 ──────────────────────────────────────────┐ │
│  │  K8S: sessionAffinity: ClientIP, timeout=3600s                    │ │
│  │  Nginx: ip_hash upstream                                          │ │
│  │  ★ WebSocket客户端始终路由到同一Pod                                │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌─ 服务发现 ──────────────────────────────────────────────────────────┐ │
│  │  K8S DNS: <svc>.openclaw.svc.cluster.local                        │ │
│  │  同Docker Compose服务名 → 配置可移植                               │ │
│  │  http://ollama:11434, http://litellm:4000,                        │ │
│  │  http://hermes:8082, http://stream-service:8084,                  │ │
│  │  http://funasr:8199, http://minio:9000                            │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 8. 配置体系架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       配置体系 (三源驱动)                                    │
│                                                                             │
│  ┌─── openclaw.json (主配置) ────────────────────────────────────────────┐ │
│  │                                                                     │ │
│  │  ┌─ agent ──────────┐  全局Agent默认: primary model, fallbacks,    │ │
│  │  │                  │  thinkingLevel, workspace                     │ │
│  │  └──────────────────┘                                              │ │
│  │                                                                     │ │
│  │  ┌─ agents ─────────┐  4个Agent定义:                               │ │
│  │  │  list:           │  main, local-dispatcher,                     │ │
│  │  │  defaults:       │  cloud-dispatcher, code-executor             │ │
│  │  └──────────────────┘  每个Agent: primary model + fallbacks         │ │
│  │                                                                     │ │
│  │  ┌─ models ─────────┐                                              │ │
│  │  │  catalog:        │  local: [ollama/qwen2.5:3b]                 │ │
│  │  │                  │  cloud: [moonshot/kimi, deepseek-chat]       │ │
│  │  │  smartRouter:    │  threshold=40, typeWeights,                  │ │
│  │  │                  │  promptLengthThresholds, keywords            │ │
│  │  │  routing:        │  adaptive策略, priority策略                  │ │
│  │  └──────────────────┘                                              │ │
│  │                                                                     │ │
│  │  ┌─ gateway ────────┐  port=3000, host=0.0.0.0, auth disabled      │ │
│  │  └──────────────────┘                                              │ │
│  │                                                                     │ │
│  │  读取方: Bridge (smartRouter + agents + catalog)                   │ │
│  │         Gateway (catalog + agents)                                  │ │
│  │         ★ Scheduler不从openclaw.json读取 (硬编码初始化)            │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌─── .env (环境变量) ───────────────────────────────────────────────────┐ │
│  │  API Keys: MOONSHOT_API_KEY, DEEPSEEK_API_KEY, OPENCLAW_TOKEN     │ │
│  │  Ports: GATEWAY_PORT=3000, BRIDGE_PORT=3001, HERMES_PORT=8082     │ │
│  │  Routing: DEFAULT_ROUTE_MODE=smart, SMART_ROUTER_THRESHOLD=40     │ │
│  │  Hermes: COMPLEXITY_THRESHOLD=40, EXPLORATION_RATE=0.1,           │ │
│  │          GEPA_ENABLED=true                                          │ │
│  │  Rate: RATE_LIMIT_RPM=60, MAX_RETRIES=2                            │ │
│  │                                                                     │ │
│  │  Python服务: python-dotenv自动加载 (load_dotenv()在模块顶部)       │ │
│  │  Node.js服务: start-all.sh中 set -a; source .env; set +a          │ │
│  │  Docker: docker-compose.yml引用${VAR}语法                          │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌─── Docker/K8S专属配置 ───────────────────────────────────────────────┐ │
│  │  docker/litellm_config.yaml → LiteLLM模型路由+策略                  │ │
│  │  docker/nginx.conf           → Nginx反向代理路由规则                │ │
│  │  k8s/02-configmaps.yaml      → K8S ConfigMap (litellm+nginx内嵌)   │ │
│  │  hermes/watchdog_config.json → 服务监控+自动重启                    │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌─── ★ 配置漂移风险 ★ ────────────────────────────────────────────────┐ │
│  │                                                                     │ │
│  │  三份独立的模型目录:                                                │ │
│  │  ├─ openclaw.json models.catalog (Bridge + Gateway读取)            │ │
│  │  ├─ Bridge硬编码MODEL_CATALOG (orchestrator.mjs 84-130行)         │ │
│  │  └─ Scheduler硬编码_init_default_models() (main.py 50-103行)      │ │
│  │                                                                     │ │
│  │  /route/reload 仅更新Bridge的smartRouter规则                       │ │
│  │  不更新Bridge硬编码MODEL_CATALOG                                   │ │
│  │  不影响Scheduler硬编码模型                                         │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌─── 自学习记忆配置 ────────────────────────────────────────────────────┐ │
│  │  .hermes/memories/MEMORY.md → 路由规则+延迟统计+反馈历史           │ │
│  │  .hermes/queues/*.jsonl     → 消息队列持久化                        │ │
│  │  RoutingMemory SQLite DB   → routing_history(FTS5)+skills+state    │ │
│  │  .hermes/routing_feedback/  → feedback.jsonl 即时反馈记录           │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 9. 服务端口与依赖关系总表

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      服务依赖与端口映射总览                                  │
│                                                                             │
│  ┌─── 服务端口 ──────────────────────────────────────────────────────────┐ │
│  │                                                                     │ │
│  │  服务名          开发模式端口    Docker端口    K8S NodePort          │ │
│  │  ────────────  ────────────  ───────────  ─────────────            │ │
│  │  Nginx LB       -             8080→80       8090→30080              │ │
│  │  Hermes Agent   8082          8082          ClusterIP only          │ │
│  │  LiteLLM Proxy  4000          4000          ClusterIP only          │ │
│  │  Stream Service 8084          8084(内部)    ClusterIP only          │ │
│  │  Ollama         11434         11434         11435→31143             │ │
│  │  Official GW    3005          3005(可选)    -                       │ │
│  │  Bridge         3001          -             -                       │ │
│  │  Gateway        3000          -             -                       │ │
│  │  Scheduler      8000          -             -                       │ │
│  │  FunASR         8199          8199(内部)    ClusterIP only          │ │
│  │  PostgreSQL     5432          5432(内部)    ClusterIP only          │ │
│  │  MinIO API      9000          9000          ClusterIP only          │ │
│  │  MinIO Console  9001          9001          9002→30001              │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌─── 服务依赖 (Docker/K8S) ────────────────────────────────────────────┐ │
│  │                                                                     │ │
│  │  Nginx ──depends──→ litellm, hermes, stream-service               │ │
│  │  litellm ──depends──→ litellm-db(healthy), ollama(healthy)         │ │
│  │  hermes ──depends──→ ollama(healthy)                                │ │
│  │  stream ──depends──→ ollama(healthy), litellm(healthy)             │ │
│  │                                                                     │ │
│  │  开发模式依赖 (start-all.sh):                                       │ │
│  │  Hermes ──calls──→ Ollama(:11434), OfficialGW(:3005),              │ │
│  │                 LiteLLM(:4000), Stream(:8084)                       │ │
│  │  OfficialGW ──optional──→ Ollama, Gateway                          │ │
│  │  Bridge ──calls──→ Gateway(:3000), OfficialGW(:3005), Hermes(:8082)│ │
│  │  Gateway ──calls──→ Ollama, Moonshot API, DeepSeek API             │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  ┌─── 内部连接 (Hermes视角) ────────────────────────────────────────────┐ │
│  │                                                                     │ │
│  │  Hermes server.py ──→                                               │ │
│  │    ├─ LLMEnhancer ──→ Ollama (:11434) 意图分类                     │ │
│  │    ├─ HermesRouter ──→ HermesAgent, LLMEnhancer, OfficialAdapter   │ │
│  │    ├─ OfficialAdapter ──→ Ollama (:11434) 路由决策                  │ │
│  │    │                   ──→ OfficialGW (:3005) Agent路由              │ │
│  │    │                   ──→ MEMORY.md 规则注入                       │ │
│  │    ├─ DispatchWorker ──→ Ollama (:11434) 推理                      │ │
│  │    │                  ──→ OfficialGW (:3005) 推理                   │ │
│  │    │                  ──→ OpenClaw K8S Plugin                      │ │
│  │    ├─ /v1/chat ──→ LiteLLM Proxy (:4000) 对话/向量                 │ │
│  │    │             ──→ Stream Service (:8084) ASR/推流                │ │
│  │    └─ Watchdog ──→ hermesAgent(:8642), officialGW(:3005),          │ │
│  │                   ollama(:11434) 健康检查+自动重启                  │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```
