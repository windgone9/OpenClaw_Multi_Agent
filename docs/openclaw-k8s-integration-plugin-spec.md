# OpenClaw k8s-integration 插件 HTTP 接口规范

**版本**：v1
**插件 ID**：`k8s-integration`
**面向**：Hermes 路由插件作者，以及任何需要通过 OpenClaw 网关向 K8s
提交 AIWorkload 的程序化调用方。

本规范定义了通过 OpenClaw 网关提交 / 查询 / 删除 AIWorkload 所需的全部
HTTP 接口、payload 结构、响应格式与错误约定。

---

## 1. 通用约定

### 1.1 Base URL

```
http://<openclaw-gateway-host>:<port>
```

- 本机开发：`http://localhost:18789`
- 集群内：取决于部署形态（Service ClusterIP / Ingress / 自定义）

### 1.2 认证

所有 `/plugins/k8s/v1/*` 请求**必须**携带 Bearer Token：

```
Authorization: Bearer <shared-secret>
```

该 token 即 OpenClaw 配置 `plugins.entries.k8s-integration.config.secret`
中配置的值。校验失败返回 `401 Unauthorized`。

### 1.3 内容类型

- 请求：建议使用 `Content-Type: application/json`；当前实现不强制校验该
  header，但会按 JSON 解析请求体（非 JSON 时返回 400）
- 响应：`Content-Type: application/json; charset=utf-8`
- 请求体最大 1 MiB（超出返回 `413 InvalidInput`，body 同样是 JSON 错误外壳）

### 1.4 响应外壳

**成功**：

```json
{ "ok": true,  "payload": { ... }, "duplicate": false }
```

`duplicate: true` 仅在创建接口因幂等命中已存在工作负载时出现。

**失败**：

```json
{ "ok": false, "error": { "code": "...", "field": "...", "message": "..." } }
```

| 字段 | 含义 |
|---|---|
| `code` | 机器可读错误码，见下方"错误码表" |
| `field` | 错误关联的 payload 字段路径（仅 `InvalidInput` 类有） |
| `message` | 人类可读说明 |

### 1.5 错误码表

| HTTP | code | 含义 |
|---|---|---|
| 400 | `InvalidInput` | payload schema 校验失败；`field` 给出具体路径 |
| 400 | `namespace_not_found` | 由 `tenant.id` 解析得到的目标命名空间不存在；`field=tenant.id`。租户命名空间需要由集群管理员预先创建，插件不会自动创建 |
| 401 | `Unauthorized` | Bearer Token 缺失或不匹配 |
| 404 | `NotFound` | 工作负载不存在 |
| 405 | `MethodNotAllowed` | HTTP 方法不被支持 |
| 409 | `AlreadyExists` | K8s 侧返回 conflict（非幂等命中场景下罕见） |
| 413 | `InvalidInput` | 请求体超过 1 MiB |
| 500 | `Internal` | 插件内部错误 |
| 502 | `KubernetesUnavailable` | K8s API 调用失败 |
| 503 | `NotReady` | 插件初始化未完成（如 secret 解析失败） |

---

## 2. 接口

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
      // 见 §3 每种 taskType 的 slice schema
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

**幂等行为**：

- 同一个 `requestId` 反复 POST 不会创建多个工作负载。
- 命中重复时返回 `202` + `"duplicate": true`，`payload` 指向已存在记录。
- 实现机制：通过 AIWorkload 上的 annotation `openclaw.io/request-id` 查询。
- **幂等的作用域是命名空间**：不同 `tenant.id` 落到不同命名空间时，相同
  `requestId` 不会跨命名空间去重；同一 `tenant.id` 下相同 `requestId` 才会
  命中已有记录。

**命名空间解析**：

POST 请求**不接受** `namespace` 字段。目标命名空间由 `tenant.id` 通过插件
配置 `tenantNamespace` 解析得到（参见 `deployment-guide.md`）。解析顺序：

1. `tenantNamespace.overrides[tenant.id]` —— 显式映射
2. `tenantNamespace.template`，将 `{id}` 替换为 `tenant.id`（如 `tenant-{id}`）
3. `tenantNamespace.fallback`：
   - `reject`（默认）：返回 `400 InvalidInput`，`field=tenant.id`
   - `defaultNamespace`：回退到旧的 `defaultNamespace` 配置

未配置 `tenantNamespace` 时保留旧行为：所有租户均落到 `defaultNamespace`。

插件在 create 之前会预检命名空间是否存在；不存在时返回 `400
namespace_not_found`。租户命名空间应由管理员预先创建（含 RBAC、配额等），
插件本身没有创建命名空间的权限。

**`workloadId` 命名规则**：

插件根据 `requestId` 生成符合 RFC 1123 的 K8s 资源名：

```
wl-<sanitize(lowercase(requestId)) 截断至 40 字符>-<6 字符随机后缀>
```

`sanitize` 把非 `[a-z0-9-]` 的字符替换为 `-`。例如
`requestId = "Hermes-Req:Abc123"` →
`workloadId = "wl-hermes-req-abc123-3f9a2b"`。

### 2.2 查询状态

```
GET /plugins/k8s/v1/workloads/{workloadId}[?namespace=<ns>|tenantId=<id>]
```

`namespace` 与 `tenantId` 二选一：传 `tenantId` 时插件会按 `tenantNamespace`
配置解析得到命名空间；同时传 `namespace` 时以 `namespace` 为准（管理员/调试
用途）。两者都不传时回退到旧的 `defaultNamespace`。

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

`status` 字段可能值：`Pending` / `Scheduled` / `Running` / `Completed` /
`Failed` / `Cancelled`。来源是 AIWorkload CR 的 `status.phase`。

### 2.3 取消 / 删除

```
DELETE /plugins/k8s/v1/workloads/{workloadId}[?namespace=<ns>|tenantId=<id>]
```

命名空间解析规则与 §2.2 相同。

**响应**（`200 OK`）：

```json
{
  "ok": true,
  "payload": { "workloadId": "wl-...", "namespace": "default", "status": "Deleting" }
}
```

删除 AIWorkload 会触发 K8s 的 OwnerReference 级联删除，对应的 Volcano Job
与 Pod 会被自动清理，无需调用方额外处理。

---

## 3. 各 taskType 的 intent slice

### 3.1 `taskType: "batch-inference"`

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
  "resources": {                          // 全部可选；省略时见下方"resources 省略时的默认值"
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
> `"resources": { "gpus": 0, "cpus": "...", "memory": "..." }`，否则会被
> 排到 GPU 资源不足而 Pending。

> **当前校验严格度**：必填字段（`model.name`、`input.uri`、`output.uri`、
> `scale.items`、`scale.batchSize`）和未知字段会被严格拒绝；
> `model.framework`、`resources.{gpuType,memoryPerGpu,cpus,memory}` 若提供则
> 校验类型；`model.version`、`input.type`、`output.type` 仅作为允许字段透传
> 不做类型校验；`runtime.{image,command,args,env}` 仅在出现未知键时拒绝，
> 不深度校验各字段类型。后续可能加强这部分校验。

**关键行为说明**：

| 字段 | 翻译为 K8s | 备注 |
|---|---|---|
| `model.framework` | 默认容器镜像 | `pytorch` → `pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime` 等；被 `runtime.image` 覆盖 |
| `input.uri` 以 `s3://` 开头 | Operator 自动注入 `aws s3 cp` init container | 否则视为节点本地路径 |
| `scale.items + batchSize` | `task.batch.replicas` 与 `parallelism` | 启发式：`ceil(items / (batchSize × 10000))`，上限 64 |
| `resources.gpus: 0` | 不注入 vGPU 资源、不挂 nvidia-libs、不加 GPU 容忍度 | 通过 Operator 的条件分支实现 |

### 3.2 历史兼容形态

旧版本调用方可把 `model`/`input`/`output`/`scale`/`resources`/`runtime`
直接放在 `intent.{...}` 下（无 `batchInference` 包装）。插件会自动转换为
canonical 形态。**新代码请勿再使用此形态**，未来版本将弃用。

---

## 4. 状态回调（webhook）

如果 `POST` 请求体中携带了 `callback.onStatusChange`，插件会在 AIWorkload
的 `status.phase` 发生变化时，向该 URL 发起回调：

**请求**：

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

**验签**（调用方应执行）：

```python
import hmac, hashlib
expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, request.headers["X-OpenClaw-Signature"]):
    raise Unauthorized
```

**重试策略**：

- 默认最大 5 次重试，初始退避 1s，每次翻倍
- 收到 4xx（除 408/429）视为终态，不重试
- 5xx / 网络错误持续重试至上限，最终在网关日志中告警并放弃

> **多命名空间状态监听**：status-watcher 维护一个 per-namespace 的监听池，
> 启动时会针对所有已配置的租户命名空间（`tenantNamespace.overrides` 中的
> 目标命名空间，加上 `defaultNamespace`，如果存在的话）建立独立的 watch
> 连接，并在每次 `subscribe()` 调用时按需补建 watch。当 watch 因网络/API
> Server 抖动断开时，会以 2s 退避自动重连。`tenantNamespace.template` 形态
> 的命名空间无法在启动时枚举，会在首个回调订阅落到该命名空间时按需建立
> watch。

**插件重启的影响**：

- 回调订阅当前只保存在进程内存中；插件/网关重启会丢失订阅
- 重启后会在 AIWorkload 上发现 `openclaw.io/callback-url` 注解，但由于
  secret 仅以哈希存档，无法自动恢复签名能力——日志中会打印告警
- **当前实现下**，重复 POST 同一个 `requestId` 会因幂等检查命中并直接返回，
  **不会**重新登记回调；如需在重启后恢复订阅，调用方应改用不同的
  `requestId` 重新提交（创建新工作负载），或改用轮询 `GET .../{id}` 检测
  状态变化。后续版本可能改为"命中幂等时也重新登记回调"

---

## 5. AIWorkload 上的元信息

插件创建的每个 AIWorkload 都带有以下 labels / annotations，便于调用方
（和运维）后续按 tenant / 请求 ID 检索：

| key | 来源 |
|---|---|
| label `openclaw.io/tenant` | `tenant.id` |
| label `openclaw.io/user` | `tenant.user`（若提供） |
| annotation `openclaw.io/request-id` | `requestId` |
| annotation `openclaw.io/callback-url` | `callback.onStatusChange`（若提供） |
| annotation `openclaw.io/callback-secret-hash` | `sha256(callback.secret)` |

---

## 6. 调用示例

```bash
SECRET="<openclaw.json 中配置的 secret>"

# 提交
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

## 7. 版本与变更策略

- 当前路径 `/plugins/k8s/v1/...` 即 v1 契约
- **向后兼容承诺**：
  - 不会移除已有字段，不会缩小已接受值的范围
  - 不会添加新的必填字段
- 新增 `taskType` 通过添加新的 slice key 实现，不影响已有 taskType 的调用方
- 不兼容变更会引入新的路径前缀（如 `/plugins/k8s/v2/...`），并保留 v1 至少
  一个完整发布周期

---

## 8. 相关文档

- [`deployment-guide.md`](./deployment-guide.md) — 插件部署与配置
- [`architecture-diagram.md`](./architecture-diagram.md) — 组件关系与端到端时序
- [`developer-guide-new-scenario.md`](./developer-guide-new-scenario.md) — 新增 taskType 的开发指南
- [`aiworkload-operator-design.md`](./aiworkload-operator-design.md) — AIWorkload Operator 设计
- 插件源码：[`../openclaw-k8s-integration/`](../openclaw-k8s-integration/)

---

## 9. Hermes Agent 集成接口（v2 新增）

**版本**：v2（基于 v1 扩展，完全向后兼容）
**面向**：Hermes Agent 开发者，以及通过 Hermes 队列提交 K8S AIWorkload 的调用方

本节定义 Hermes Agent 如何集成 K8S AIWorkload 提交功能，包括两种调用方式、
请求/响应 JSON 格式、路由决策逻辑和内部转发流程。

### 9.1 设计原则

1. **双入口**：支持 Hermes 自动路由（队列接口）和独立 API（直接操作 K8S 资源）
2. **结果结构兼容**：K8S 请求的返回 Result JSON 保持与现有 Hermes 结果结构一致
3. **异步优先**：K8S AIWorkload 提交后立即返回 workloadId + status，通过轮询或 webhook 获取最终结果
4. **路由透明**：Hermes 路由层自动识别 K8S 相关意图，跳过 LLM 路由直接转发到 k8s_gateway
5. **零侵入**：原有 chat/completion/code 等请求类型不受影响

### 9.2 调用方式一：Hermes 自动路由（队列接口扩展）

通过现有的 `POST /queue/submit` 或 `POST /queue/submit-sync` 提交请求，
设置 `type=k8s_workload` 触发 K8S 路由。

**请求 JSON**（在现有 `QueueSubmitRequest` 基础上扩展）：

```jsonc
{
  // ── 现有字段（与 QueueSubmitRequest 完全兼容）──
  "appid":    "my-app",              // 可选，默认 "default"
  "type":     "k8s_workload",        // 必填，触发 K8S 路由（新增类型）
  "prompt":   "提交批量推理任务...",   // 必填，自然语言描述
  "priority": 3,                     // 可选，1-5

  // ── K8S 扩展字段（type=k8s_workload 时有效）──
  "k8s_workload": {                  // 可选；提供时直接构造 AIWorkload
    "action":    "submit",           // 可选，默认 "submit"；可选 "get"/"delete"
    "requestId": "my-req-001",       // 可选，默认使用 Hermes request_id
    "tenant":    { "id": "team-a", "user": "alice" },  // 可选
    "taskType":  "batch-inference",  // 必填（当 action=submit 时）
    "intent": {
      "batchInference": {            // 与 §2.1 的 intent 完全一致
        "model":  { "name": "resnet50", "framework": "pytorch" },
        "input":  { "uri": "s3://datasets/imgs/" },
        "output": { "uri": "s3://results/demo/" },
        "scale":  { "items": 100000, "batchSize": 32 }
      }
    },
    "sla": { "priority": 70 },       // 可选
    "callback": {                     // 可选
      "onStatusChange": "http://hermes:8082/k8s/callback",
      "secret": "hmac-key"
    }
  }
}
```

**路由决策逻辑**：

当 `type=k8s_workload` 或请求中包含 `k8s_workload` 字段时：
1. 跳过 LLM 路由决策（`k8s_fast_path`），直接返回 `route_path=k8s_gateway`
2. 不经过 `_post_validate_route` 关键词修正
3. 不消耗 Ollama GPU 资源进行路由推理
4. 路由结果：`complexity_score=75.0, selected_model=openclaw/default`

**返回 Result JSON**（与现有 Hermes 结果结构兼容）：

```json
{
  "request_id": "3e4bc3eb-6309-4a8f-94f9-53cc81b54e1c",
  "appid": "my-app",
  "status": "success",
  "routing": {
    "route_path": "k8s_gateway",
    "complexity_score": 75.0,
    "selected_model": "openclaw/default",
    "reason": "K8S AIWorkload request — direct k8s_gateway routing",
    "agent_decision": "k8s_fast_path",
    "memory_context_used": false,
    "post_validated": false
  },
  "result": {
    "model_name": "openclaw/default",
    "model_type": "openclaw-agent",
    "output": "已提交 K8S AIWorkload: wl-my-req-001-3f9a2b，状态: Pending",
    "latency_ms": 1200,
    "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 },
    "finish_reason": "stop",
    "routed_via": "hermes_k8s_gateway",
    "dispatch_latency_ms": 1200,
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
  "error": null,
  "total_latency_ms": 1523,
  "timestamp": "2026-06-15T17:46:07.186414"
}
```

### 9.3 调用方式二：独立 API（直接操作 K8S 资源）

不经过 Hermes 路由决策，直接操作 K8S 资源。

#### 9.3.1 提交 AIWorkload

```
POST /k8s/workloads?appid=<appid>
```

**请求体**：

```json
{
  "requestId": "my-req-001",         // 可选
  "tenant": { "id": "team-a" },      // 可选，默认使用 appid
  "taskType": "batch-inference",     // 必填
  "intent": {
    "batchInference": { ... }        // 与 §2.1 的 intent 完全一致
  },
  "sla": { "priority": 70 },         // 可选
  "callback": { ... }                // 可选
}
```

**响应**（`202 Accepted`）：

```json
{
  "request_id": "my-req-001",
  "appid": "team-a",
  "status": "success",
  "timestamp": "2026-06-15T15:20:30.123456",
  "total_latency_ms": 1523,
  "routing": {
    "route_path": "k8s_gateway",
    "complexity_score": 75.0,
    "selected_model": "openclaw/default",
    "reason": "K8S AIWorkload direct submit"
  },
  "result": {
    "model_name": "openclaw/default",
    "model_type": "openclaw-agent",
    "output": "已提交 K8S AIWorkload: wl-my-req-001-3f9a2b，状态: Pending",
    "latency_ms": 1200,
    "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 },
    "finish_reason": "stop",
    "routed_via": "hermes_k8s_gateway",
    "k8s_workload": {
      "workloadId": "wl-my-req-001-3f9a2b",
      "namespace": "default",
      "aiworkloadRef": { ... },
      "status": "Pending",
      "jobRef": null
    }
  },
  "error": null
}
```

#### 9.3.2 查询 AIWorkload 状态

```
GET /k8s/workloads/{workloadId}?namespace=<ns>&tenantId=<id>
```

**响应**（`200 OK`）：

```json
{
  "request_id": "abc12345",
  "status": "success",
  "timestamp": "2026-06-15T15:21:00.123456",
  "total_latency_ms": 200,
  "result": {
    "model_name": "openclaw/default",
    "model_type": "openclaw-agent",
    "output": "工作负载 wl-xxx 状态: Running",
    "latency_ms": 180,
    "finish_reason": "stop",
    "routed_via": "hermes_k8s_gateway",
    "k8s_workload": {
      "workloadId": "wl-xxx",
      "namespace": "default",
      "status": "Running",
      "jobRef": { ... }
    }
  },
  "error": null
}
```

#### 9.3.3 删除 AIWorkload

```
DELETE /k8s/workloads/{workloadId}?namespace=<ns>&tenantId=<id>
```

**响应**（`200 OK`）：

```json
{
  "request_id": "abc12345",
  "status": "success",
  "timestamp": "2026-06-15T15:22:00.123456",
  "total_latency_ms": 150,
  "result": {
    "model_name": "openclaw/default",
    "model_type": "openclaw-agent",
    "output": "工作负载 wl-xxx 已标记删除",
    "latency_ms": 130,
    "finish_reason": "stop",
    "routed_via": "hermes_k8s_gateway",
    "k8s_workload": {
      "workloadId": "wl-xxx",
      "namespace": "default",
      "status": "Deleting"
    }
  },
  "error": null
}
```

#### 9.3.4 K8S 状态回调

OpenClaw Gateway 在 AIWorkload 状态变化时回调此端点：

```
POST /k8s/callback
```

**请求体**（来自 OpenClaw 的回调 payload）：

```json
{
  "workloadId": "wl-xxx",
  "namespace": "default",
  "phase": "Running",
  "jobRef": { ... },
  "requestId": "hermes-req-abc123",
  "timestamp": "2026-06-11T09:49:12.918Z"
}
```

**响应**：

```json
{ "status": "ok", "workloadId": "wl-xxx" }
```

回调数据会被推入 Hermes 结果队列，可通过 `GET /queue/results` 轮询获取。

### 9.4 内部转发流程

```
┌─────────────────────────────────────────────────────────────────┐
│ 方式一：Hermes 自动路由                                          │
│                                                                 │
│  Client → POST /queue/submit-sync                               │
│           type=k8s_workload                                     │
│           ↓                                                     │
│  DispatchWorker._process_one()                                  │
│           ↓                                                     │
│  route_via_agent() → k8s_fast_path (跳过LLM)                    │
│           ↓                                                     │
│  _dispatch() → _dispatch_to_k8s_gateway()                       │
│           ↓                                                     │
│  OpenClaw GW: POST /plugins/k8s/v1/workloads                    │
│           ↓                                                     │
│  返回 Result JSON (含 k8s_workload 扩展字段)                     │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ 方式二：独立 API                                                 │
│                                                                 │
│  Client → POST /k8s/workloads                                   │
│           ↓                                                     │
│  k8s_submit_workload() → dispatch_worker._dispatch_to_k8s_gateway() │
│           ↓                                                     │
│  OpenClaw GW: POST /plugins/k8s/v1/workloads                    │
│           ↓                                                     │
│  返回 202 Accepted (含完整 Hermes Result 结构)                   │
└─────────────────────────────────────────────────────────────────┘
```

### 9.5 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENCLAW_K8S_GATEWAY_URL` | 同 `OPENCLAW_OFFICIAL_GATEWAY_URL` | K8S 插件网关地址，可独立配置 |
| `OPENCLAW_OFFICIAL_GATEWAY_URL` | `http://127.0.0.1:3005` | OpenClaw 官方网关地址（默认复用） |
| `OPENCLAW_TOKEN` | 空 | Bearer Token，用于认证 OpenClaw 请求 |

### 9.6 错误处理

当 OpenClaw Gateway 返回错误时，Hermes 会将错误包装为标准 Hermes 错误格式：

```json
{
  "status": "failed",
  "error": {
    "code": "K8S_GATEWAY_ERROR",
    "message": "Client error '404 Not Found' for url 'http://127.0.0.1:3005/plugins/k8s/v1/workloads'"
  }
}
```

常见错误场景：

| 场景 | HTTP 状态码 | error.code | 说明 |
|---|---|---|---|
| OpenClaw 未启动 | 502 | K8S_GATEWAY_ERROR | 连接被拒绝 |
| K8S 插件未注册 | 502 | K8S_GATEWAY_ERROR | 404 Not Found |
| 认证失败 | 502 | K8S_GATEWAY_ERROR | 401 Unauthorized |
| 请求超时 | 502 | K8S_GATEWAY_ERROR | 超过 OFFICIAL_GW_TIMEOUT |
| 命名空间不存在 | 502 | K8S_GATEWAY_ERROR | 400 namespace_not_found |

### 9.7 Dashboard 集成

Hermes Dashboard 已集成 K8S 工作负载提交功能：

1. **请求类型选择**：`req-type` 下拉框新增 `k8s_workload` 选项
2. **批量测试场景**：新增 `☸ K8S工作负载` 按钮，包含 3 个测试用例：
   - K8S-批量推理（batch-inference）
   - K8S-分布式训练（distributed-training）
   - K8S-超参调优（hyperparameter-tuning）
3. **结果卡片**：K8S 请求的结果卡片显示 `☸ K8S 工作负载` 区域，包含：
   - Workload ID
   - 状态（Pending/Running/Completed/Failed，带颜色标识）
   - 命名空间
   - 资源引用（Kind/Name）
4. **路由标识**：`k8s_gateway` 路由使用 `☸` 图标标识
5. **全场景批量**：`📦 全场景批量` 按钮已包含 K8S 场景
