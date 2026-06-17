# OpenClaw Multi-Agent 同步直调架构改造技术方案

## 1. 背景与目标

### 1.1 现状

当前 Hermes Agent 采用**消息队列 + 异步分发**架构：

```
前端 AI 应用 → POST /queue/submit → 请求队列 → DispatchWorker(线程) → 路由决策 → 下游模型
                                    ↓
前端 AI 应用 ← GET /queue/results ← 结果队列 ← DispatchWorker ← 模型响应
```

**核心瓶颈**：

| 瓶颈点 | 原因 | 影响 |
|--------|------|------|
| Router 串行处理 | 单进程 + 信号量限制（`_OLLAMA_MAX_CONCURRENT=2`, `_ROUTING_MAX_CONCURRENT=1`） | 高并发下请求排队，延迟飙升 |
| 队列轮询开销 | `submit-sync` 用 `threading.Event` + 0.1s 轮询 + 10s peek 兜底 | CPU 空转，延迟不可控 |
| GPU 争用 | 路由决策和请求执行共享同一 GPU 信号量 | 路由占 GPU 时，请求执行被阻塞 |
| 无水平扩展 | 单实例 Hermes，无 Worker 缩扩容机制 | 无法应对流量峰值 |

### 1.2 目标

1. **取消消息队列**，前端 AI 应用直接同步调用 REST API
2. **对话类请求**：封装本地模型为 OpenAI 兼容 REST API，前端直接调用
3. **多模态请求**：提供 REST API，`attachments` 字段支持语音/图片/PDF/Word 文件 URL
4. **动态推流请求**：提供 WebSocket/SSE 动态 URL，支持实时语音转文字 + 模型处理（豆包云端 ASR）
5. **Router 缩扩容**：解决单点瓶颈，支持水平扩展

---

## 2. 目标架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        前端 AI 应用                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ 对话请求  │  │ 多模态    │  │ K8S 任务  │  │ 实时推流(Stream)  │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────────┬──────────┘  │
└───────┼─────────────┼─────────────┼─────────────────┼─────────────┘
        │             │             │                 │
        ▼             ▼             ▼                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     API Gateway / Load Balancer                     │
│               (Nginx / Traefik / K8S Ingress)                      │
│               路径路由 + 限流 + 负载均衡 + 健康检查                    │
└───┬─────────────┬─────────────┬─────────────────┬─────────────────┘
    │             │             │                 │
    ▼             ▼             ▼                 ▼
┌────────┐  ┌────────┐  ┌────────┐  ┌──────────────────────────────┐
│ Chat   │  │Multi-  │  │ K8S    │  │ Stream Service               │
│ Worker │  │modal   │  │ Worker │  │ (WebSocket/SSE)              │
│ Pool   │  │Worker  │  │ Pool   │  │ ┌──────────┐ ┌────────────┐  │
│(N实例) │  │Pool    │  │(N实例) │  │ │ ASR Worker│ │ LLM Worker │  │
│        │  │(N实例) │  │        │  │ │(豆包云端) │ │(Ollama/vLLM)│  │
└───┬────┘  └───┬────┘  └───┬────┘  │ └──────────┘ └────────────┘  │
    │           │           │        └──────────────┬───────────────┘
    ▼           ▼           ▼                       ▼
┌────────┐  ┌────────┐  ┌──────────────────────────────────────────┐
│Ollama/ │  │Ollama  │  │         OpenClaw Gateway (:3005)         │
│vLLM    │  │多模态   │  │         K8S Plugin / Agent Chain         │
│(本地)  │  │(本地)   │  └──────────────────────────────────────────┘
└────────┘  └────────┘
```

**核心变化**：

- 消息队列 → 直接同步 REST API（OpenAI 兼容格式）
- 单 Router → 按请求类型拆分为独立 Worker Pool，各自缩扩容
- 新增 Stream Service 处理实时推流
- API Gateway 层负责路径路由 + 负载均衡

---

## 3. 三种技术方案对比

### 3.1 方案概览

| 维度 | 方案 A：Hermes 多 Worker 扩展 | 方案 B：LiteLLM Proxy | 方案 C：OpenClaw Gateway 增强 |
|------|------|------|------|
| 核心思路 | 在现有 Hermes 上拆分 API 端点，多进程部署 | 用 LiteLLM 作为统一代理层，替换 Hermes Router | 在 OpenClaw Gateway 中实现路由+缩扩容 |
| 代码改动量 | 中（重构 Hermes，新增 Stream Service） | 大（引入新组件，适配现有接口） | 大（修改 OpenClaw 核心代码） |
| Router 瓶颈解决 | 按类型拆分 Worker Pool，各自独立扩展 | LiteLLM 本身无状态，水平扩展天然支持 | 依赖 OpenClaw 插件机制扩展 |
| OpenAI 兼容性 | 需手动封装 | 原生支持（核心功能） | 需手动封装 |
| 多模态支持 | 需手动封装 | 部分支持（视觉模型），附件需自定义 | 需自定义插件 |
| 流式推流 | 需自建 WebSocket/SSE 服务 | 原生支持 SSE streaming | 需自定义 |
| 语音 ASR | 需集成豆包 API | 不支持 | 需自定义插件 |
| 运维复杂度 | 中（多进程管理） | 低（单进程，配置驱动） | 高（依赖 OpenClaw 生态） |
| 社区生态 | 无（自研） | 活跃（11k+ GitHub Stars） | 小众 |
| K8S 部署友好度 | 中 | 高（官方提供 Docker/K8S 配置） | 中 |

### 3.2 方案 A：Hermes 多 Worker 扩展

#### 架构

```
                    ┌─────────────────────┐
                    │   Nginx/Traefik     │
                    │   :80 (入口)         │
                    └──┬──────┬──────┬────┘
                       │      │      │
           ┌───────────┘      │      └───────────┐
           ▼                  ▼                  ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │ Chat Worker  │  │ MM Worker    │  │ Stream Worker │
    │ :8082        │  │ :8083        │  │ :8084         │
    │ (x N 实例)   │  │ (x M 实例)   │  │ (x K 实例)    │
    └──────┬───────┘  └──────┬───────┘  └──────┬────────┘
           │                 │                  │
           ▼                 ▼                  ▼
       Ollama/vLLM      Ollama多模态      豆包ASR + Ollama
```

#### 实现要点

1. **拆分 Hermes 为 3 个独立 Service**：
   - `ChatService`：对话/补全，OpenAI `/v1/chat/completions` 兼容
   - `MultimodalService`：多模态，支持 `attachments` 字段
   - `StreamService`：WebSocket/SSE 实时推流，集成豆包 ASR

2. **缩扩容机制**：
   - 每个服务独立进程，通过 `gunicorn -w N` 或多实例部署
   - Nginx upstream 配置负载均衡（round-robin / least_conn）
   - 基于队列深度/CPU 的自动缩扩容脚本

3. **优点**：
   - 复用现有代码，改动最小
   - 完全可控，无第三方依赖
   - 各服务独立扩展，互不影响

4. **缺点**：
   - 需要自建负载均衡和健康检查
   - OpenAI 兼容需手动封装
   - 运维多套服务

#### 缩扩容可行性

| 方式 | 可行性 | 说明 |
|------|--------|------|
| 多进程（gunicorn） | ✅ | 每个 Worker 独立处理请求，无共享状态 |
| 多实例 + Nginx | ✅ | 无状态服务，水平扩展天然支持 |
| K8S HPA | ✅ | 基于 CPU/RPS 指标自动扩缩 |
| GPU 感知调度 | ⚠️ | 需自建，Ollama 本身有并发限制 |

---

### 3.3 方案 B：LiteLLM Proxy

#### 架构

```
                    ┌─────────────────────┐
                    │   Nginx/Traefik     │
                    │   :80 (入口)         │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │   LiteLLM Proxy     │
                    │   :4000             │
                    │   (x N 实例)        │
                    │                     │
                    │  /v1/chat/completions  (对话)     │
                    │  /v1/completions        (补全)     │
                    │  /v1/embeddings         (向量)     │
                    │  /v1/audio/transcriptions (ASR)   │
                    │  /v1/images/generations  (图像)    │
                    └──┬──────┬──────┬─────┘
                       │      │      │
           ┌───────────┘      │      └───────────┐
           ▼                  ▼                  ▼
       Ollama/vLLM      豆包/云ASR         OpenClaw GW
       (本地模型)       (语音识别)          (Agent链/K8S)
```

#### 实现要点

1. **LiteLLM 配置**（`litellm_config.yaml`）：

```yaml
model_list:
  # 对话模型
  - model_name: qwen2.5
    litellm_params:
      model: ollama/qwen2.5:3b
      api_base: http://localhost:11434

  # 多模态模型
  - model_name: llava
    litellm_params:
      model: ollama/llava:7b
      api_base: http://localhost:11434

  # 云端模型
  - model_name: deepseek-chat
    litellm_params:
      model: deepseek/deepseek-chat
      api_key: os.environ/DEEPSEEK_API_KEY

  # 豆包 ASR（通过 OpenAI 兼容接口）
  - model_name: doubao-asr
    litellm_params:
      model: openai/whisper-1
      api_base: https://openspeech.bytedance.com/v1
      api_key: os.environ/DOUBAO_ASR_KEY

router_settings:
  routing_strategy: latency-based-routing  # 基于延迟路由
  num_retries: 2
  timeout: 120
  allowed_fails: 3
  cooldown_time: 60

general_settings:
  drop_params: true
  set_verbose: false
```

2. **自定义多模态附件处理**：
   - LiteLLM 原生支持 OpenAI Vision 格式（`image_url`）
   - PDF/Word/语音需预处理为文本/图片后传入
   - 可通过 LiteLLM `custom_prompt_map` 或前置中间件转换

3. **缩扩容机制**：
   - LiteLLM Proxy 无状态，直接多实例部署
   - 内置负载均衡（latency-based routing）
   - 内置 fallback + cooldown 机制
   - 官方提供 Docker/K8S 部署配置

4. **优点**：
   - **原生 OpenAI 兼容**，前端零适配
   - **内置负载均衡 + fallback + cooldown**
   - **内置 streaming**（SSE）
   - **内置 ASR 接口**（`/v1/audio/transcriptions`）
   - 活跃社区，持续更新
   - 内置成本追踪 + 用量统计

5. **缺点**：
   - 多模态附件（PDF/Word）需前置转换
   - K8S AIWorkload 需保留 Hermes 独立处理
   - 引入新组件，学习成本
   - 自定义路由策略不如自研灵活

#### 缩扩容可行性

| 方式 | 可行性 | 说明 |
|------|--------|------|
| 多实例 + Nginx | ✅ | 无状态，水平扩展 |
| K8S HPA | ✅ | 官方提供 K8S 配置 |
| 内置路由策略 | ✅ | latency-based / cost-based / simple-shuffle |
| GPU 感知调度 | ⚠️ | 需自定义 router_settings |

---

### 3.4 方案 C：OpenClaw Gateway 增强

#### 架构

```
                    ┌─────────────────────┐
                    │   OpenClaw GW       │
                    │   :3005             │
                    │   (x N 实例)        │
                    │                     │
                    │  /v1/chat/completions  (对话)     │
                    │  /v1/audio/transcriptions (ASR)   │
                    │  /plugins/k8s/v1/...    (K8S)     │
                    │  /v1/stream/...         (推流)    │
                    └──┬──────┬──────┬─────┘
                       │      │      │
           ┌───────────┘      │      └───────────┐
           ▼                  ▼                  ▼
       Ollama/vLLM      豆包/云ASR         K8S Plugin
```

#### 实现要点

1. 在 OpenClaw Gateway 中注册自定义插件：
   - `chat-plugin`：封装 Ollama 为 OpenAI 兼容接口
   - `multimodal-plugin`：处理附件字段
   - `stream-plugin`：WebSocket/SSE 推流
   - `asr-plugin`：集成豆包 ASR

2. 利用 OpenClaw 的插件机制实现路由和负载均衡

3. **优点**：
   - 统一入口，架构简洁
   - K8S 集成天然支持
   - 插件化扩展

4. **缺点**：
   - **OpenClaw 是第三方项目**，核心代码不可控
   - 插件机制文档有限，自定义路由策略受限
   - 缩扩容依赖 OpenClaw 自身能力，**目前不支持多实例负载均衡**
   - 社区小众，问题排查困难
   - 流式推流、ASR 集成需大量自定义开发

#### 缩扩容可行性

| 方式 | 可行性 | 说明 |
|------|--------|------|
| 多实例 + Nginx | ⚠️ | OpenClaw GW 有状态（插件注册表），多实例需同步 |
| K8S HPA | ⚠️ | 需确保插件状态无冲突 |
| 内置路由策略 | ❌ | 无内置负载均衡，需外挂 |
| GPU 感知调度 | ❌ | 不支持 |

---

## 4. 综合评估

### 4.1 评分矩阵

| 评估维度（权重） | 方案 A：Hermes 多 Worker | 方案 B：LiteLLM Proxy | 方案 C：OpenClaw GW 增强 |
|-----------------|------------------------|---------------------|------------------------|
| Router 瓶颈解决（25%） | 8 | 9 | 5 |
| OpenAI 兼容性（20%） | 5 | 10 | 4 |
| 多模态/附件支持（15%） | 7 | 6 | 5 |
| 流式推流（15%） | 6 | 9 | 4 |
| 缩扩容能力（15%） | 7 | 9 | 3 |
| 实现复杂度（10%） | 6 | 8 | 3 |
| **加权总分** | **6.65** | **8.65** | **4.15** |

### 4.2 推荐方案

**推荐方案 B（LiteLLM Proxy）+ 方案 A（Hermes 保留 K8S/自定义功能）的混合架构**。

理由：

1. **LiteLLM 解决了 80% 的通用问题**：对话、流式、ASR、负载均衡、fallback、OpenAI 兼容
2. **Hermes 保留 20% 的定制能力**：K8S AIWorkload、自学习路由、MEMORY 闭环
3. **避免重复造轮子**：LiteLLM 的负载均衡、cooldown、成本追踪已非常成熟
4. **渐进式迁移**：可先引入 LiteLLM 处理对话/流式，Hermes 逐步退化为 K8S 专用服务

---

## 5. 推荐方案详细设计

### 5.1 整体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                        前端 AI 应用                              │
└───────┬──────────────┬──────────────────┬───────────────────────┘
        │              │                  │
        ▼              ▼                  ▼
┌───────────────┐ ┌──────────────┐ ┌──────────────────────────────┐
│  LiteLLM      │ │  Hermes      │ │  Stream Service              │
│  Proxy        │ │  Agent       │ │  (新建，可集成到 LiteLLM)     │
│  :4000        │ │  :8082       │ │  :8084                       │
│               │ │              │ │                              │
│  对话/补全     │ │  K8S 任务    │ │  WebSocket/SSE              │
│  多模态(Vision)│ │  自学习路由   │ │  豆包 ASR → LLM             │
│  ASR          │ │  MEMORY 闭环 │ │  实时语音转文字+模型回复      │
│  Embedding    │ │              │ │                              │
│  Streaming    │ │              │ │                              │
└───────┬───────┘ └──────┬───────┘ └──────────────┬───────────────┘
        │                │                        │
        ▼                ▼                        ▼
   Ollama/vLLM     OpenClaw GW :3005        豆包 ASR API
   云端模型         K8S Plugin              Ollama/vLLM
```

### 5.2 API 接口设计

#### 5.2.1 对话类 — LiteLLM Proxy（OpenAI 兼容）

```
POST /v1/chat/completions
```

```json
{
  "model": "qwen2.5",
  "messages": [{"role": "user", "content": "1+1等于几"}],
  "stream": false,
  "temperature": 0.7
}
```

**流式响应**：

```
POST /v1/chat/completions
```

```json
{
  "model": "qwen2.5",
  "messages": [{"role": "user", "content": "写一首诗"}],
  "stream": true
}
```

返回 SSE 格式，前端直接消费 `EventSource`。

#### 5.2.2 多模态类 — LiteLLM Proxy（Vision 兼容）

```
POST /v1/chat/completions
```

```json
{
  "model": "llava",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "描述这张图片"},
      {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}}
    ]
  }],
  "stream": false
}
```

**附件字段扩展**（PDF/Word/语音）：

对于非图片附件，增加前置预处理中间件：

```
POST /v1/chat/completions  (自定义扩展)
```

```json
{
  "model": "qwen2.5",
  "messages": [{"role": "user", "content": "总结这个文档"}],
  "attachments": [
    {"type": "pdf", "url": "https://example.com/doc.pdf"},
    {"type": "word", "url": "https://example.com/doc.docx"},
    {"type": "audio", "url": "https://example.com/voice.mp3"}
  ]
}
```

预处理中间件逻辑：
- `pdf` / `word` → 调用文档解析服务提取文本 → 拼接到 prompt
- `audio` → 调用豆包 ASR 转文字 → 拼接到 prompt
- `image` → 转为 `image_url` 格式走 Vision 模型

#### 5.2.3 实时推流 — Stream Service

**WebSocket 接口**：

```
WS /v1/stream/ws?model=qwen2.5&asr_provider=doubao
```

交互流程：

```
前端                          Stream Service                    豆包 ASR           Ollama
 │                                │                              │                  │
 │─── WS Connect ────────────────▶│                              │                  │
 │                                │                              │                  │
 │─── Audio Chunk (binary) ──────▶│─── POST /asr ──────────────▶│                  │
 │                                │◀── text ────────────────────│                  │
 │                                │─── POST /v1/chat/completions─────────────────▶│
 │                                │◀── SSE stream ───────────────────────────────│
 │◀── text delta (WS frame) ─────│                              │                  │
 │                                │                              │                  │
 │─── Audio Chunk (binary) ──────▶│─── POST /asr ──────────────▶│                  │
 │                                │◀── text ────────────────────│                  │
 │                                │─── POST /v1/chat/completions─────────────────▶│
 │◀── text delta (WS frame) ─────│                              │                  │
 │                                │                              │                  │
 │─── WS Close ─────────────────▶│                              │                  │
```

**SSE 接口**（单向推流，前端只接收）：

```
GET /v1/stream/sse?model=qwen2.5&asr_provider=doubao
Content-Type: text/event-stream
```

#### 5.2.4 K8S AIWorkload — Hermes Agent（保留）

```
POST /k8s/workloads?appid=my-team
GET  /k8s/workloads/{id}
DELETE /k8s/workloads/{id}
POST /k8s/callback
```

保持现有接口不变。

### 5.3 缩扩容设计

#### 5.3.1 LiteLLM Proxy 缩扩容

```
┌──────────────────────────────────────────────┐
│              Nginx / K8S Ingress             │
│           :443 (TLS termination)             │
│                                              │
│  upstream litellm {                          │
│    least_conn;                               │
│    server litellm-1:4000;                    │
│    server litellm-2:4000;                    │
│    server litellm-3:4000;  # 动态增减        │
│  }                                           │
└──────────────────┬───────────────────────────┘
                   │
        ┌──────────┼──────────┐
        ▼          ▼          ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐
   │LiteLLM  │ │LiteLLM  │ │LiteLLM  │
   │Worker 1 │ │Worker 2 │ │Worker 3 │
   │:4000    │ │:4000    │ │:4000    │
   └─────────┘ └─────────┘ └─────────┘
        │          │          │
        ▼          ▼          ▼
   Ollama/vLLM  云端API    豆包ASR
```

**扩容策略**：

| 指标 | 阈值 | 动作 |
|------|------|------|
| 平均请求延迟 > 5s | 持续 60s | +1 Worker |
| CPU > 70% | 持续 60s | +1 Worker |
| 请求队列深度 > 10 | 持续 30s | +1 Worker |
| 平均请求延迟 < 1s | 持续 300s | -1 Worker |
| CPU < 30% | 持续 300s | -1 Worker |

**K8S HPA 配置示例**：

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: litellm-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: litellm
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
    - type: Pods
      pods:
        metric:
          name: http_requests_per_second
        target:
          type: AverageValue
          averageValue: "50"
```

#### 5.3.2 Stream Service 缩扩容

Stream Service 是有状态服务（WebSocket 连接），缩扩容需注意：

- **扩容**：新连接路由到新实例，旧连接保持
- **缩容**：等待现有连接关闭后再移除实例（优雅关闭）
- **会话亲和**：Nginx 配置 `ip_hash` 或 K8S SessionAffinity

```nginx
upstream stream_service {
    ip_hash;  # WebSocket 会话亲和
    server stream-1:8084;
    server stream-2:8084;
}
```

#### 5.3.3 Ollama GPU 缩扩容

Ollama 是 GPU 绑定的，缩扩容取决于 GPU 资源：

| 场景 | 方案 |
|------|------|
| 单 GPU | Ollama 并发限制（`OLLAMA_MAX_LOADED=2`），LiteLLM 排队 |
| 多 GPU 同机 | Ollama 自动多 GPU 推理 |
| 多 GPU 多机 | 部署多个 Ollama 实例，LiteLLM 配置多个 `api_base` |

**LiteLLM 多 Ollama 实例配置**：

```yaml
model_list:
  - model_name: qwen2.5
    litellm_params:
      model: ollama/qwen2.5:3b
      api_base: http://ollama-1:11434
  - model_name: qwen2.5
    litellm_params:
      model: ollama/qwen2.5:3b
      api_base: http://ollama-2:11434
  - model_name: qwen2.5
    litellm_params:
      model: ollama/qwen2.5:3b
      api_base: http://ollama-3:11434
```

LiteLLM 自动在 3 个 Ollama 实例间做负载均衡和 fallback。

### 5.4 附件预处理中间件设计

```
┌──────────────────────────────────────────────────────┐
│               Attachment Preprocessor                │
│                                                      │
│  ┌────────────┐  ┌────────────┐  ┌───────────────┐  │
│  │ PDF Parser │  │ Docx Parser│  │ Audio → ASR   │  │
│  │ (PyMuPDF)  │  │(python-docx│  │ (豆包 API)    │  │
│  └─────┬──────┘  └─────┬──────┘  └──────┬────────┘  │
│        │               │                │            │
│        ▼               ▼                ▼            │
│     [text]          [text]          [text]           │
│        │               │                │            │
│        └───────────────┴────────────────┘            │
│                        │                             │
│                        ▼                             │
│              拼接到 messages.content                  │
│              转发到 LiteLLM Proxy                     │
└──────────────────────────────────────────────────────┘
```

**实现方式**：FastAPI 中间件，在请求到达 LiteLLM 之前拦截处理。

### 5.5 豆包 ASR 集成

**接口**：豆包语音识别服务（火山引擎）

```python
# Stream Service 中集成豆包 ASR
async def asr_with_doubao(audio_chunk: bytes) -> str:
    """调用豆包 ASR 将音频转为文字"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://openspeech.bytedance.com/api/v1/auc/recognize",
            headers={
                "Authorization": f"Bearer {DOUBAO_ASR_KEY}",
                "Content-Type": "application/octet-stream",
            },
            content=audio_chunk,
            params={"format": "wav", "rate": 16000},
        )
        return resp.json()["result"]["text"]
```

**LiteLLM 方式**：通过 `/v1/audio/transcriptions` 接口，配置豆包为 ASR Provider。

---

## 6. 迁移路径

### Phase 1：引入 LiteLLM，对话/流式先行（1-2 周）

1. 部署 LiteLLM Proxy，配置 Ollama + 云端模型
2. 前端对话请求从 `/queue/submit-sync` 迁移到 `/v1/chat/completions`
3. 流式响应从轮询改为 SSE
4. Hermes 保留 K8S 功能不变

### Phase 2：多模态 + 附件中间件（1 周）

1. 开发 Attachment Preprocessor 中间件
2. 支持 PDF/Word/语音附件
3. 图片附件走 LiteLLM Vision

### Phase 3：Stream Service 实时推流（1-2 周）

1. 开发 Stream Service（WebSocket + SSE）
2. 集成豆包 ASR
3. 前端接入实时语音交互

### Phase 4：缩扩容 + 生产加固（1 周）

1. Nginx/K8S Ingress 负载均衡配置
2. HPA 自动缩扩容
3. 健康检查 + 告警
4. 压测验证

---

## 7. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| LiteLLM 与 Ollama 兼容性问题 | 请求失败 | 充分测试，保留 fallback 到 Hermes |
| 豆包 ASR 延迟/限流 | 推流体验差 | 本地 Whisper 模型作为 fallback |
| WebSocket 连接数限制 | 高并发推流受限 | 连接池 + 多实例 + 限流 |
| 附件预处理超时 | 请求阻塞 | 异步处理 + 超时控制 + 文件大小限制 |
| GPU 资源不足 | 模型推理排队 | 请求优先级队列 + 云端模型溢出 |

---

## 8. 技术选型参考

| 组件 | 推荐选型 | 备选 |
|------|---------|------|
| LLM Proxy | LiteLLM (Python) | vLLM Gateway, OpenRouter |
| ASR 服务 | 豆包（火山引擎） | Whisper (本地), Azure Speech |
| 文档解析 | PyMuPDF + python-docx | Unstructured.io, Apache Tika |
| 负载均衡 | Nginx | Traefik, K8S Ingress |
| 进程管理 | Supervisor / systemd | Docker Compose, K8S Deployment |
| 流式协议 | SSE (文本) + WebSocket (语音) | gRPC Streaming |
