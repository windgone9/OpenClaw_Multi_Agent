# OpenClaw k8s-integration 插件 HTTP 接口规范

**版本**：v2（在 v1 基础上新增 Hermes Agent 集成层）
**插件 ID**：`k8s-integration`
**面向**：Hermes 路由插件作者，以及任何需要通过 OpenClaw 网关向 K8s
提交 AIWorkload 的程序化调用方。

本规范定义了：
1. 通过 OpenClaw 网关直接提交 / 查询 / 删除 AIWorkload 的 HTTP 接口（v1，不变）
2. **新增**：通过 Hermes Agent 路由层提交 AIWorkload 的集成接口（v2）
3. **新增**：Hermes 请求 JSON 与返回 Result JSON 的字段规范

---

## 1. 通用约定

### 1.1 Base URL

| 入口 | URL | 说明 |
|------|-----|------|
| OpenClaw 网关直连 | `http://<openclaw-gateway-host>:<port>` | 本机 `http://localhost:18789` |
| Hermes Agent 路由 | `http://<hermes-host>:8082` | 本机 `http://localhost:8082` |

### 1.2 认证

**OpenClaw 网关直连**：所有 `/plugins/k8s/v1/*` 请求必须携带 Bearer Token：

```
Authorization: Bearer <shared-secret>
```

**Hermes Agent 路由**：通过 Hermes 的 `/queue/submit` 或 `/k8s/workloads` 接口提交，
Hermes 内部转发时自动注入 `OPENCLAW_TOKEN`。

### 1.3 内容类型

- 请求：`Content-Type: application/json`
- 响应：`Content-Type: application/json; charset=utf-8`
- 请求体最大 1 MiB

### 1.4 响应外壳

**OpenClaw 网关直连**（不变）：

```json
{ "ok": true,  "payload": { ... }, "duplicate": false }
{ "ok": false, "error": { "code": "...", "field": "...", "message": "..." } }
```

**Hermes Agent 路由**（兼容 Hermes 现有结果结构）：

```json
{
  "request_id": "...",
  "appid": "...",
  "status": "success" | "failed" | "timeout",
  "timestamp": "ISO8601",
  "total_latency_ms": 0,
  "routing": { ... },
  "result": { ... },
  "error": null
}
```

### 1.5 错误码表

| HTTP | code | 含义 |
|---|---|---|
| 400 | `InvalidInput` | payload schema 校验失败；`field` 给出具体路径 |
| 400 | `namespace_not_found` | 由 `tenant.id` 解析得到的目标命名空间不存在 |
| 401 | `Unauthorized` | Bearer Token 缺失或不匹配 |
| 404 | `NotFound` | 工作负载不存在 |
| 405 | `MethodNotAllowed` | HTTP 方法不被支持 |
| 409 | `AlreadyExists` | K8s 侧返回 conflict |
| 413 | `InvalidInput` | 请求体超过 1 MiB |
| 500 | `Internal` | 插件内部错误 |
| 502 | `KubernetesUnavailable` | K8s API 调用失败 |
| 503 | `NotReady` | 插件初始化未完成 |

---

## 2. OpenClaw 网关直连接口（v1，不变）

### 2.1 提交工作负载

```
POST /plugins/k8s/v1/workloads
```

**请求体**：

```jsonc
{
  "requestId": "hermes-req-abc123",        // 必填，幂等键
  "tenant":    { "id": "team-a", "user": "u42" },   // user 可选
  "taskType":  "batch-inference",          // 必填，决定 intent 里使用哪个 slice
  "intent": {
    "batchInference": {                    // slice key 必须与 taskType 对应
      // 见 §6 每种 taskType 的 slice schema
    }
  },
  "sla": {                                 // 可选
    "priority": 70,                        // 0-100，默认 50
    "deadline": "2026-06-11T18:00:00Z"     // ISO-8601
  },
  "callback": {                            // 可选
    "onStatusChange": "https://hermes/internal/wl-events",
    "secret": "hmac-key"
  }
}
```

**响应**（成功，`202 Accepted`）：

```json
{
  "ok": true,
  "payload": {
    "workloadId": "wl-hermes-req-abc123-3f9a2b",
    "namespace":  "default",
    "aiworkloadRef": {
      "apiVersion": "ai.aischeduler.io/v1alpha1",
      "kind":       "AIWorkload",
      "name":       "wl-hermes-req-abc123-3f9a2b",
      "namespace":  "default"
    },
    "status": "Pending",
    "jobRef": null
  }
}
```

**幂等行为**：同一个 `requestId` 反复 POST 不会创建多个工作负载。命中重复时返回 `202` + `"duplicate": true`。

**命名空间解析**：POST 请求不接受 `namespace` 字段。目标命名空间由 `tenant.id` 通过插件配置 `tenantNamespace` 解析得到。解析顺序：

1. `tenantNamespace.overrides[tenant.id]` — 显式映射
2. `tenantNamespace.template`，将 `{id}` 替换为 `tenant.id`
3. `tenantNamespace.fallback`：`reject`（默认）或 `defaultNamespace`

**`workloadId` 命名规则**：

```
wl-<sanitize(lowercase(requestId)) 截断至 40 字符>-<6 字符随机后缀>
```

### 2.2 查询状态

```
GET /plugins/k8s/v1/workloads/{workloadId}[?namespace=<ns>|tenantId=<id>]
```

**响应**（`200 OK`）：

```json
{
  "ok": true,
  "payload": {
    "workloadId": "wl-...",
    "namespace":  "default",
    "aiworkloadRef": { ... },
    "status":  "Running",
    "jobRef":  { "apiVersion": "batch.volcano.sh/v1alpha1", "kind": "Job",
                 "name": "wl-...-job", "namespace": "default" }
  }
}
```

`status` 可能值：`Pending` / `Scheduled` / `Running` / `Completed` / `Failed` / `Cancelled`。

### 2.3 取消 / 删除

```
DELETE /plugins/k8s/v1/workloads/{workloadId}[?namespace=<ns>|tenantId=<id>]
```

**响应**（`200 OK`）：

```json
{
  "ok": true,
  "payload": { "workloadId": "wl-...", "namespace": "default", "status": "Deleting" }
}
```

---

## 3. Hermes Agent 集成接口（v2 新增）

### 3.1 设计原则

1. **双入口**：支持 Hermes 自动路由（chat 接口）和独立 API（直接操作 K8S 资源）
2. **结果结构兼容**：K8S 请求的返回 Result JSON 保持与现有 Hermes 结果结构一致
3. **异步优先**：K8S AIWorkload 提交后立即返回 workloadId + status，通过轮询或 webhook 获取最终结果
4. **路由透明**：Hermes 路由层自动识别 K8S 相关意图，转发到 `/plugins/k8s/v1/workloads`

### 3.2 调用方式一：Hermes 自动路由（chat 接口扩展）

通过现有的 `/queue/submit` 或 `/queue/submit-sync` 接口提交请求，Hermes 路由层
自动识别 K8S 相关意图并转发到 OpenClaw 网关的 `/plugins/k8s/v1/workloads`。

**请求 JSON**（在现有 `QueueSubmitRequest` 基础上扩展）：

```jsonc
{
  // ── 现有字段（与 QueueSubmitRequest 完全兼容）──
  "appid":    "my-app",              // 可选，默认 "default"
  "type":     "k8s_workload",        // 新增类型，触发 K8S 路由
  "prompt":   "提交批量推理任务...",   // 必填，自然语言描述
  "priority": 3,                     // 可选，1-5

  // ── K8S 扩展字段（type=k8s_workload 时有效）──
  "k8s_workload": {                  // 可选；提供时直接构造 AIWorkload，不提供时由 Hermes 从 prompt 推断
    "requestId": "my-req-001",       // 可选，默认使用 Hermes request_id
    "tenant":    { "id": "team-a", "user": "alice" },  // 可选，默认从 appid 推导
    "taskType":  "batch-inference",  // 必填（当 k8s_workload 提供时）
    "intent": {
      "batchInference": {            // 与 §2.1 的 intent 完全一致
        "model":  { "name": "resnet50", "framework": "pytorch" },
        "input":  { "uri": "s3://datasets/imgs/" },
        "output": { "uri": "s3://results/demo/" },
        "scale":  { "items": 100000, "batchSize": 32 }
      }
    },
    "sla": { "priority": 70, "deadline": "2026-06-11T18:00:00Z" },
    "callback": { "onStatusChange": "https://hermes:8082/k8s/callback", "secret": "hmac-key" }
  },

  // ── 现有可选字段（保持兼容）──
  "model_hint":  null,
  "parameters":  null,
  "context":     null,
  "tools":       null,
  "tool_choice": null,
  "constraints": null
}
```

**路由决策逻辑**：

| 条件 | 路由目标 | 说明 |
|------|----------|------|
| `type == "k8s_workload"` | `gateway` + K8S 插件 | 直接走 K8S 路径 |
| `type == "chat"` 且 prompt 含 K8S 关键词 | `gateway` + K8S 插件 | 自动识别 |
| `type == "chat"` 且 prompt 含 Volcano/调度关键词 | `gateway`（原有逻辑） | 走 chat/completions |
| `k8s_workload` 字段提供 | `gateway` + K8S 插件 | 显式指定 |

**K8S 关键词列表**（触发自动路由）：

```
AIWorkload, aiworkload, 批量推理, batch-inference, 分布式训练,
K8S提交, k8s workload, 提交工作负载, 提交任务到集群
```

**返回 Result JSON**（保持 Hermes 现有结构，在 `result` 内扩展 K8S 字段）：

```json
{
  "request_id": "a1b2c3d4-...",
  "appid": "my-app",
  "status": "success",
  "timestamp": "2026-06-15T15:20:30.123456",
  "total_latency_ms": 1523,
  "routing": {
    "route_path": "gateway",
    "complexity_score": 75.0,
    "selected_model": "openclaw/default",
    "reason": "K8S AIWorkload 提交请求，路由到 OpenClaw 网关 k8s-integration 插件",
    "memory_context_used": true,
    "post_validated": false
  },
  "result": {
    // ── Hermes 标准字段（与现有 gateway 路由一致）──
    "model_name": "openclaw/default",
    "model_type": "openclaw-agent",
    "output": "已提交 K8S AIWorkload: wl-my-req-001-3f9a2b，状态: Pending",
    "latency_ms": 1200,
    "finish_reason": "stop",
    "routed_via": "hermes_k8s_gateway",
    "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 },

    // ── K8S 扩展字段（仅 K8S 路由时出现）──
    "k8s_workload": {
      "workloadId": "wl-my-req-001-3f9a2b",
      "namespace": "default",
      "aiworkloadRef": {
        "apiVersion": "ai.aischeduler.io/v1alpha1",
        "kind": "AIWorkload",
        "name": "wl-my-req-001-3f9a2b",
        "namespace": "default"
      },
      "status": "Pending",
      "jobRef": null
    }
  },
  "error": null
}
```

### 3.3 调用方式二：独立 API（直接操作 K8S 资源）

Hermes 新增 K8S 专用 endpoint，直接映射到 OpenClaw 网关的 K8S 插件接口。

#### 3.3.1 提交工作负载

```
POST /k8s/workloads
```

**请求 JSON**：

```jsonc
{
  // ── 必填字段 ──
  "requestId": "my-req-001",              // 幂等键
  "tenant":    { "id": "team-a" },         // tenant.id 必填，user 可选
  "taskType":  "batch-inference",          // 必填

  // ── 任务定义 ──
  "intent": {
    "batchInference": {
      "model":  { "name": "resnet50", "framework": "pytorch" },
      "input":  { "uri": "s3://datasets/imgs/" },
      "output": { "uri": "s3://results/demo/" },
      "scale":  { "items": 100000, "batchSize": 32 }
    }
  },

  // ── 可选字段 ──
  "sla": { "priority": 70, "deadline": "2026-06-11T18:00:00Z" },
  "callback": { "onStatusChange": "https://hermes:8082/k8s/callback", "secret": "hmac-key" }
}
```

**字段说明**：

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `requestId` | string | **是** | 幂等键，同一 tenant.id 下唯一 |
| `tenant` | object | **是** | `tenant.id` 必填，`tenant.user` 可选 |
| `taskType` | string | **是** | 任务类型，决定 intent slice |
| `intent` | object | **是** | 任务定义，结构由 taskType 决定 |
| `sla` | object | 否 | SLA 约束（priority 0-100，deadline ISO-8601） |
| `callback` | object | 否 | 状态变更回调（onStatusChange URL + secret） |

**响应**（`202 Accepted`）：

```json
{
  "request_id": "my-req-001",
  "appid": "team-a",
  "status": "success",
  "timestamp": "2026-06-15T15:20:30.123456",
  "total_latency_ms": 1523,
  "routing": {
    "route_path": "gateway",
    "complexity_score": 75.0,
    "selected_model": "openclaw/default",
    "reason": "K8S AIWorkload direct submit",
    "memory_context_used": false,
    "post_validated": false
  },
  "result": {
    "model_name": "openclaw/default",
    "model_type": "openclaw-agent",
    "output": "已提交 K8S AIWorkload: wl-my-req-001-3f9a2b，状态: Pending",
    "latency_ms": 1200,
    "finish_reason": "stop",
    "routed_via": "hermes_k8s_gateway",
    "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 },
    "k8s_workload": {
      "workloadId": "wl-my-req-001-3f9a2b",
      "namespace": "default",
      "aiworkloadRef": {
        "apiVersion": "ai.aischeduler.io/v1alpha1",
        "kind": "AIWorkload",
        "name": "wl-my-req-001-3f9a2b",
        "namespace": "default"
      },
      "status": "Pending",
      "jobRef": null
    }
  },
  "error": null
}
```

#### 3.3.2 查询工作负载状态

```
GET /k8s/workloads/{workloadId}[?namespace=<ns>|tenantId=<id>]
```

**响应**（`200 OK`）：

```json
{
  "request_id": "query-wl-my-req-001",
  "appid": "team-a",
  "status": "success",
  "timestamp": "2026-06-15T15:25:30.123456",
  "total_latency_ms": 230,
  "routing": {
    "route_path": "gateway",
    "complexity_score": 0,
    "selected_model": "openclaw/default",
    "reason": "K8S workload status query",
    "memory_context_used": false,
    "post_validated": false
  },
  "result": {
    "model_name": "openclaw/default",
    "model_type": "openclaw-agent",
    "output": "工作负载 wl-my-req-001-3f9a2b 状态: Running",
    "latency_ms": 200,
    "finish_reason": "stop",
    "routed_via": "hermes_k8s_gateway",
    "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 },
    "k8s_workload": {
      "workloadId": "wl-my-req-001-3f9a2b",
      "namespace": "default",
      "aiworkloadRef": {
        "apiVersion": "ai.aischeduler.io/v1alpha1",
        "kind": "AIWorkload",
        "name": "wl-my-req-001-3f9a2b",
        "namespace": "default"
      },
      "status": "Running",
      "jobRef": {
        "apiVersion": "batch.volcano.sh/v1alpha1",
        "kind": "Job",
        "name": "wl-my-req-001-3f9a2b-job",
        "namespace": "default"
      }
    }
  },
  "error": null
}
```

#### 3.3.3 取消 / 删除工作负载

```
DELETE /k8s/workloads/{workloadId}[?namespace=<ns>|tenantId=<id>]
```

**响应**（`200 OK`）：

```json
{
  "request_id": "delete-wl-my-req-001",
  "appid": "team-a",
  "status": "success",
  "timestamp": "2026-06-15T15:30:30.123456",
  "total_latency_ms": 350,
  "routing": {
    "route_path": "gateway",
    "complexity_score": 0,
    "selected_model": "openclaw/default",
    "reason": "K8S workload delete",
    "memory_context_used": false,
    "post_validated": false
  },
  "result": {
    "model_name": "openclaw/default",
    "model_type": "openclaw-agent",
    "output": "工作负载 wl-my-req-001-3f9a2b 已标记删除",
    "latency_ms": 300,
    "finish_reason": "stop",
    "routed_via": "hermes_k8s_gateway",
    "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 },
    "k8s_workload": {
      "workloadId": "wl-my-req-001-3f9a2b",
      "namespace": "default",
      "status": "Deleting"
    }
  },
  "error": null
}
```

#### 3.3.4 K8S 状态回调接收

Hermes 提供 webhook 接收端，供 OpenClaw k8s-integration 插件回调状态变更：

```
POST /k8s/callback
```

**请求体**（来自 OpenClaw 插件的回调，见 §5）：

```json
{
  "workloadId": "wl-...",
  "namespace": "default",
  "phase": "Running",
  "jobRef": { "apiVersion": "...", "kind": "Job", "name": "...", "namespace": "default" },
  "requestId": "hermes-req-abc123",
  "timestamp": "2026-06-11T09:49:12.918Z"
}
```

**Hermes 处理逻辑**：

1. 验证 HMAC 签名（`X-OpenClaw-Signature` header）
2. 根据 `requestId` 查找对应的 pending sync waiter，触发 `event.set()`
3. 更新 `_stats` 中的 K8S 工作负载统计
4. 将状态变更记录到 feedback queue → MEMORY.md 自学习闭环

**响应**（`200 OK`）：

```json
{ "ok": true }
```

---

## 4. 请求 JSON 字段规范（完整）

### 4.1 通过 Hermes 队列提交（`/queue/submit`、`/queue/submit-sync`）

| 字段 | 类型 | 必须 | 默认值 | 说明 |
|------|------|------|--------|------|
| `prompt` | string | **是** | — | 用户输入的提示文本 |
| `appid` | string | 否 | `"default"` | 应用标识 |
| `type` | string | 否 | `"chat"` | 请求类型：`chat` / `code` / `embedding` / **`k8s_workload`** |
| `priority` | int | 否 | `3` | 优先级 1-5 |
| `model_hint` | string | 否 | `null` | 模型偏好提示 |
| `parameters` | object | 否 | `null` | 模型参数覆盖 |
| `context` | array | 否 | `null` | 上下文消息列表 |
| `tools` | array | 否 | `null` | 可调用的工具定义 |
| `tool_choice` | string | 否 | `null` | 工具选择策略 |
| `constraints` | object | 否 | `null` | 路由约束 |
| `attachments` | array | 否 | `null` | 附件列表 |
| **`k8s_workload`** | object | 否 | `null` | **K8S 工作负载定义（v2 新增）** |

### 4.2 `k8s_workload` 子结构

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `requestId` | string | 否 | 幂等键，默认使用 Hermes `request_id` |
| `tenant` | object | 否 | `{ "id": "...", "user": "..." }`，id 默认从 `appid` 推导 |
| `taskType` | string | **是** | 任务类型：`batch-inference`（后续扩展 `distributed-training` 等） |
| `intent` | object | **是** | 任务定义，结构由 taskType 决定（见 §6） |
| `sla` | object | 否 | `{ "priority": 0-100, "deadline": "ISO-8601" }` |
| `callback` | object | 否 | `{ "onStatusChange": "url", "secret": "key" }` |

### 4.3 通过独立 API 提交（`/k8s/workloads`）

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `requestId` | string | **是** | 幂等键 |
| `tenant` | object | **是** | `{ "id": "...", "user": "..." }`，id 必填 |
| `taskType` | string | **是** | 任务类型 |
| `intent` | object | **是** | 任务定义 |
| `sla` | object | 否 | SLA 约束 |
| `callback` | object | 否 | 状态变更回调 |

---

## 5. 返回 Result JSON 字段规范（完整）

### 5.1 顶层结构（与现有 Hermes 结果完全兼容）

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `request_id` | string | **是** | 请求唯一 ID |
| `appid` | string | **是** | 应用标识（回显） |
| `status` | string | **是** | `success` / `failed` / `timeout` |
| `timestamp` | string | **是** | ISO8601 时间戳 |
| `total_latency_ms` | int | **是** | 总耗时（含路由+调度+执行） |
| `routing` | object | **是** | 路由决策信息 |
| `result` | object | 否* | 成功时的执行结果 |
| `error` | object | 否* | 失败时的错误信息 |

### 5.2 `routing` 子结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `route_path` | string | 路由路径：`direct_local` / `gateway` / `multimodal` / `local_inference` |
| `complexity_score` | float | 复杂度评分（0-100） |
| `selected_model` | string | 选中的模型名 |
| `reason` | string | 路由原因说明 |
| `memory_context_used` | bool | 是否使用了 MEMORY.md 上下文 |
| `post_validated` | bool | 是否经过后验证修正 |

### 5.3 `result` 子结构（K8S 路由时）

| 字段 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `model_name` | string | **是** | 实际使用的模型名（K8S 路由时为 `"openclaw/default"`） |
| `model_type` | string | **是** | 模型类型：`local` / `openclaw-agent` / `multimodal` |
| `output` | string | **是** | 人类可读的输出描述 |
| `latency_ms` | int | **是** | 调度执行耗时 |
| `finish_reason` | string | **是** | 结束原因：`stop` / `length` / `tool_call` |
| `routed_via` | string | **是** | 路由标签：`hermes_k8s_gateway` |
| `usage` | object | **是** | Token 用量（K8S 路由时为 0） |
| **`k8s_workload`** | object | 否 | **K8S 工作负载信息（仅 K8S 路由时出现）** |

### 5.4 `result.k8s_workload` 子结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `workloadId` | string | K8S 工作负载 ID |
| `namespace` | string | K8S 命名空间 |
| `aiworkloadRef` | object | AIWorkload CR 引用（apiVersion, kind, name, namespace） |
| `status` | string | 工作负载状态：`Pending` / `Scheduled` / `Running` / `Completed` / `Failed` / `Cancelled` / `Deleting` |
| `jobRef` | object | Volcano Job 引用（null 直到调度完成） |

### 5.5 `error` 子结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | string | 错误码：`DISPATCH_ERROR` / `TIMEOUT` / `K8S_SUBMIT_ERROR` / `K8S_NOT_FOUND` / `K8S_UNAVAILABLE` |
| `message` | string | 错误详情 |

---

## 6. 各 taskType 的 intent slice

### 6.1 `taskType: "batch-inference"`

**slice key**：`intent.batchInference`

```jsonc
{
  "model":  {
    "name":      "resnet50",              // 必填
    "framework": "pytorch",               // 可选；用于选择默认运行时镜像
    "version":   "1.0"                    // 可选
  },
  "input":  {
    "type": "s3",                         // 可选；元数据，仅参考
    "uri":  "s3://datasets/imgs/"         // 必填
  },
  "output": {
    "type": "s3",
    "uri":  "s3://results/run-abc/"       // 必填
  },
  "scale":  {
    "items":     1000000,                 // 必填，>= 1，用于推算 replicas
    "batchSize": 32                       // 必填，>= 1
  },
  "resources": {                          // 全部可选；省略时见下方默认值
    "gpus":         1,                    // >= 0；0 表示纯 CPU 工作负载
    "gpuType":      "A100",
    "memoryPerGpu": "16Gi",
    "cpus":         "8",
    "memory":       "32Gi"
  },
  "runtime": {                            // 全部可选；不填则用 framework 的默认镜像
    "image":   "custom-image:tag",
    "command": ["python", "infer.py"],
    "args":    ["--batch-size=32"],
    "env":     [{ "name": "FOO", "value": "bar" }]
  }
}
```

> **resources 省略时的默认值**：如果整个 `resources` 块不提供，插件会回退到
> 默认 GPU 配置（`gpus: 1`、`gpuType: "A100"`、`memoryPerGpu: "16Gi"`、
> `cpus: "8"`、`memory: "32Gi"`）。因此**纯 CPU 任务必须显式给出**
> `"resources": { "gpus": 0, "cpus": "...", "memory": "..." }`。

**关键行为说明**：

| 字段 | 翻译为 K8s | 备注 |
|---|---|---|
| `model.framework` | 默认容器镜像 | `pytorch` → `pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime` 等；被 `runtime.image` 覆盖 |
| `input.uri` 以 `s3://` 开头 | Operator 自动注入 `aws s3 cp` init container | 否则视为节点本地路径 |
| `scale.items + batchSize` | `task.batch.replicas` 与 `parallelism` | 启发式：`ceil(items / (batchSize × 10000))`，上限 64 |
| `resources.gpus: 0` | 不注入 vGPU 资源、不挂 nvidia-libs、不加 GPU 容忍度 | 通过 Operator 的条件分支实现 |

### 6.2 历史兼容形态

旧版本调用方可把 `model`/`input`/`output`/`scale`/`resources`/`runtime`
直接放在 `intent.{...}` 下（无 `batchInference` 包装）。插件会自动转换为
canonical 形态。**新代码请勿再使用此形态**，未来版本将弃用。

---

## 7. 状态回调（webhook）

如果 `POST` 请求体中携带了 `callback.onStatusChange`，插件会在 AIWorkload
的 `status.phase` 发生变化时，向该 URL 发起回调。

**回调请求**：

```
POST <onStatusChange>
Content-Type: application/json
X-OpenClaw-Signature: sha256=<HMAC-SHA256 of body using callback.secret>

{
  "workloadId": "wl-...",
  "namespace":  "default",
  "phase":      "Running",
  "jobRef":     { "apiVersion": "...", "kind": "Job", "name": "...", "namespace": "default" },
  "requestId":  "hermes-req-abc123",
  "timestamp":  "2026-06-11T09:49:12.918Z"
}
```

**验签**：

```python
import hmac, hashlib
expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, request.headers["X-OpenClaw-Signature"]):
    raise Unauthorized
```

**重试策略**：

- 默认最大 5 次重试，初始退避 1s，每次翻倍
- 收到 4xx（除 408/429）视为终态，不重试
- 5xx / 网络错误持续重试至上限

---

## 8. Hermes 内部转发流程

### 8.1 数据流

```
调用方                     Hermes Agent                        OpenClaw 网关
  │                           │                                    │
  │  POST /queue/submit       │                                    │
  │  type=k8s_workload        │                                    │
  │──────────────────────────>│                                    │
  │                           │  路由决策: gateway + k8s            │
  │                           │  (识别 type 或 k8s_workload 字段)   │
  │                           │                                    │
  │                           │  POST /plugins/k8s/v1/workloads    │
  │                           │  Authorization: Bearer <token>     │
  │                           │───────────────────────────────────>│
  │                           │                                    │
  │                           │  202 Accepted                      │
  │                           │  { ok, payload: { workloadId } }   │
  │                           │<───────────────────────────────────│
  │                           │                                    │
  │  Result JSON              │  (转换 OpenClaw 响应为 Hermes 格式) │
  │  result.k8s_workload      │                                    │
  │<──────────────────────────│                                    │
  │                           │                                    │
  │                           │  (异步: 状态变更回调)               │
  │                           │<──── POST /k8s/callback ───────────│
  │                           │  (更新 stats + feedback)            │
```

### 8.2 Hermes `_dispatch_to_k8s_gateway` 逻辑

当路由决策识别到 K8S 请求时，`dispatch_worker.py` 的 `_dispatch` 方法
新增 `k8s_gateway` 分支：

```python
def _dispatch(self, request, routing):
    route_path = routing["route_path"]
    if route_path == "direct_local":
        return self._dispatch_to_local(request, routing)
    elif route_path == "gateway":
        # 检查是否为 K8S 工作负载请求
        if request.get("type") == "k8s_workload" or request.get("k8s_workload"):
            return self._dispatch_to_k8s_gateway(request, routing)
        return self._dispatch_to_official_gw(request, routing)
    elif route_path == "multimodal":
        return self._dispatch_to_multimodal(request, routing)
    elif route_path == "local_inference":
        return self._dispatch_to_local(request, routing, privacy=True)
    ...
```

### 8.3 OpenClaw 响应 → Hermes Result 转换规则

| OpenClaw 响应字段 | Hermes Result 字段 | 转换规则 |
|---|---|---|
| `payload.workloadId` | `result.k8s_workload.workloadId` | 直接映射 |
| `payload.namespace` | `result.k8s_workload.namespace` | 直接映射 |
| `payload.aiworkloadRef` | `result.k8s_workload.aiworkloadRef` | 直接映射 |
| `payload.status` | `result.k8s_workload.status` | 直接映射 |
| `payload.jobRef` | `result.k8s_workload.jobRef` | 直接映射 |
| — | `result.model_name` | 固定 `"openclaw/default"` |
| — | `result.model_type` | 固定 `"openclaw-agent"` |
| — | `result.output` | 人类可读描述：`"已提交 K8S AIWorkload: {workloadId}，状态: {status}"` |
| — | `result.routed_via` | 固定 `"hermes_k8s_gateway"` |
| — | `result.finish_reason` | 固定 `"stop"` |
| — | `result.usage` | 固定 `{ prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 }` |
| `error.code` | `error.code` | 映射：`InvalidInput` → `K8S_SUBMIT_ERROR`，`NotFound` → `K8S_NOT_FOUND`，`KubernetesUnavailable` → `K8S_UNAVAILABLE` |
| `error.message` | `error.message` | 直接映射 |

---

## 9. AIWorkload 上的元信息

插件创建的每个 AIWorkload 都带有以下 labels / annotations：

| key | 来源 |
|---|---|
| label `openclaw.io/tenant` | `tenant.id` |
| label `openclaw.io/user` | `tenant.user`（若提供） |
| annotation `openclaw.io/request-id` | `requestId` |
| annotation `openclaw.io/callback-url` | `callback.onStatusChange`（若提供） |
| annotation `openclaw.io/callback-secret-hash` | `sha256(callback.secret)` |

---

## 10. 调用示例

### 10.1 通过 Hermes 队列提交（自动路由）

```bash
# 方式一：type=k8s_workload + k8s_workload 字段（显式）
curl -sS -X POST http://localhost:8082/queue/submit \
  -H 'Content-Type: application/json' \
  -d '{
    "appid": "team-a",
    "type": "k8s_workload",
    "prompt": "提交批量推理任务",
    "k8s_workload": {
      "requestId": "demo-1",
      "tenant": { "id": "team-a", "user": "alice" },
      "taskType": "batch-inference",
      "intent": {
        "batchInference": {
          "model":  { "name": "resnet50", "framework": "pytorch" },
          "input":  { "uri": "s3://datasets/imgs/" },
          "output": { "uri": "s3://results/demo/" },
          "scale":  { "items": 100000, "batchSize": 32 }
        }
      },
      "sla": { "priority": 70 }
    }
  }'

# 方式二：自然语言 + 关键词（自动识别）
curl -sS -X POST http://localhost:8082/queue/submit \
  -H 'Content-Type: application/json' \
  -d '{
    "appid": "team-a",
    "type": "chat",
    "prompt": "请提交一个AIWorkload批量推理任务，模型resnet50，数据集s3://datasets/imgs/"
  }'
```

### 10.2 通过 Hermes 独立 API 提交

```bash
# 提交
WL=$(curl -sS -X POST http://localhost:8082/k8s/workloads \
  -H 'Content-Type: application/json' \
  -d '{
    "requestId": "demo-1",
    "tenant":    { "id": "team-a", "user": "alice" },
    "taskType":  "batch-inference",
    "intent": {
      "batchInference": {
        "model":  { "name": "resnet50", "framework": "pytorch" },
        "input":  { "uri": "s3://datasets/imgs/" },
        "output": { "uri": "s3://results/demo/" },
        "scale":  { "items": 100000, "batchSize": 32 }
      }
    },
    "sla": { "priority": 70 }
  }' | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["result"]["k8s_workload"]["workloadId"])')
echo "created: $WL"

# 查询
curl -sS "http://localhost:8082/k8s/workloads/$WL?tenantId=team-a"

# 删除
curl -sS -X DELETE "http://localhost:8082/k8s/workloads/$WL?tenantId=team-a"
```

### 10.3 直接调用 OpenClaw 网关（v1，不变）

```bash
SECRET="<openclaw.json 中配置的 secret>"

WL=$(curl -sS -X POST http://localhost:18789/plugins/k8s/v1/workloads \
  -H "Authorization: Bearer $SECRET" \
  -H 'Content-Type: application/json' \
  -d '{
    "requestId": "demo-1",
    "tenant":    { "id": "team-a", "user": "alice" },
    "taskType":  "batch-inference",
    "intent": {
      "batchInference": {
        "model":  { "name": "resnet50", "framework": "pytorch" },
        "input":  { "uri": "s3://datasets/imgs/" },
        "output": { "uri": "s3://results/demo/" },
        "scale":  { "items": 100000, "batchSize": 32 }
      }
    },
    "sla": { "priority": 70 }
  }' | python3 -c 'import sys,json;print(json.load(sys.stdin)["payload"]["workloadId"])')
echo "created: $WL"

# 查询
curl -sS "http://localhost:18789/plugins/k8s/v1/workloads/$WL" \
  -H "Authorization: Bearer $SECRET"

# 删除
curl -sS -X DELETE "http://localhost:18789/plugins/k8s/v1/workloads/$WL" \
  -H "Authorization: Bearer $SECRET"
```

---

## 11. 版本与变更策略

- OpenClaw 网关直连路径 `/plugins/k8s/v1/...` 为 v1 契约（不变）
- Hermes 集成接口 `/k8s/workloads` 为 v2 契约
- **向后兼容承诺**：
  - 不会移除已有字段，不会缩小已接受值的范围
  - 不会添加新的必填字段
  - `result.k8s_workload` 为新增可选字段，不影响现有调用方
- 新增 `taskType` 通过添加新的 slice key 实现
- 不兼容变更会引入新的路径前缀（如 `/k8s/v2/...`），并保留旧版本至少一个完整发布周期

---

## 12. 相关文档

- [`deployment-guide.md`](./deployment-guide.md) — 插件部署与配置
- [`architecture-diagram.md`](./architecture-diagram.md) — 组件关系与端到端时序
- [`developer-guide-new-scenario.md`](./developer-guide-new-scenario.md) — 新增 taskType 的开发指南
- [`aiworkload-operator-design.md`](./aiworkload-operator-design.md) — AIWorkload Operator 设计
- [`technical_spec_v4.md`](./technical_spec_v4.md) — Hermes Agent 技术规范 v4
- 插件源码：[`../openclaw-k8s-integration/`](../openclaw-k8s-integration/)
