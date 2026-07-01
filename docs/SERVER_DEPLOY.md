# 服务器部署指南 (feature_deploytoTestServer)

把 OpenClaw Multi-Agent 部署到**已有真实 K8S 集群**的测试服务器。服务器用 **vLLM** 跑两个模型 (替代本机 Ollama 的 qwen2.5:3b + llava:7b):

| 用途 | 服务器模型 (vLLM) | 替代本机 | LiteLLM model_name 别名 |
|------|-------------------|----------|--------------------------|
| 文本 | `deepseek-r1-distill-qwen-32b` | qwen2.5:3b | `qwen2.5` / `deepseek-r1-distill-qwen-32b` |
| 视觉 | `qwen3-vl-32b-instruct` | llava:7b | `llava` / `qwen3-vl-32b-instruct` |

**核心思路 (方案A)**: vLLM 由 **GPUStack** 托管于 `http://192.168.0.151/v1` (K8S 集群外), 服务器 K8S 只跑 OpenClaw 自身 pod; litellm 直连 GPUStack + API key。保留 `qwen2.5`/`llava` 作为 LiteLLM 别名, 使 proxy / stream-service / dashboard / server.py **零代码改动**; 移除 Ollama (仅留占位 Service 供 nginx 启动)。

## 架构 (方案A: vLLM 复用 GPUStack)

```
本机 kind:   nginx(8090) → proxy → litellm → ollama(qwen2.5:3b + llava:7b)
服务器:       nginx(30080) → proxy → litellm --直连--> GPUStack vLLM (192.168.0.151:80)
                                                       deepseek-r1-distill-qwen-32b + qwen3-vl-32b-instruct
                                          (K8S 外, 不由本仓管理; 4×H100, 两模型常驻 tp2)
```

- **不部署 Ollama** (`04-ollama.yaml` 不 apply); 保留占位 `ollama` Service 仅供 nginx 启动 (`/svc/ollama/` location 解析用)。
- LiteLLM 配置 (`02-litellm-config-server.yaml`) 把 `qwen2.5`/`llava` 别名路由到 `http://192.168.0.151/v1`, `api_key: os.environ/VLLM_API_KEY`。
- `13-inference-endpoints.yaml` 仅含占位 ollama Service (vLLM 在 K8S 外, 无需 in-cluster Service)。
- proxy/stream `OLLAMA_URL` 置空, 直走 LiteLLM; proxy `STRIP_REASONING_TAGS=true` (去 r1 推理块)。
- 镜像 `imagePullPolicy=IfNotPresent` (真实集群, 非 kind load)。

## 前置条件

1. **kubectl** 已指向目标集群: `kubectl config current-context`。
2. **GPUStack 已就绪** (`http://192.168.0.151/v1`), 跑两模型:
   - `deepseek-r1-distill-qwen-32b` (文本) + `qwen3-vl-32b-instruct` (视觉, 支持 `image_url`)
   - 拿到 GPUStack API key (用于 `VLLM_API_KEY`)。
3. **网络**: 服务器 K8S 节点能访问 `192.168.0.151:80` (同网段直连; 跨网段需额外 egress/NAT)。
4. **镜像分发**:
   - 单节点集群: 在该节点上跑 deploy-server.sh, build 出来的镜像本地可用。
   - 多节点集群: 设 `REGISTRY=your-registry.example.com/openclaw-`, 脚本会 retag+push。

## 部署步骤

### 1. 一键部署

**情况 A — 全新部署 (我们自己装 litellm+db):**
```bash
cd <repo>
VLLM_API_KEY=gpustack_xxx ./k8s/deploy-server.sh
# 跳过 build:   VLLM_API_KEY=gpustack_xxx ./k8s/deploy-server.sh --skip-build
# 多节点:       VLLM_API_KEY=gpustack_xxx REGISTRY=your-registry/openclaw- ./k8s/deploy-server.sh
```

**情况 B — 复用现有 LiteLLM (服务器已独立装好 litellm+db, 在 default ns, Service lite-helm-litellm:4000):**
```bash
cd <repo>
VLLM_API_KEY=gpustack_xxx LITELLM_MASTER_KEY=sk-xxx ./k8s/deploy-server.sh
# 强制复用 (跳过自动检测): REUSE_EXISTING_LITELLM=true ...
# 现有 litellm 在其它 ns/Service: EXISTING_LITELLM_NS=... EXISTING_LITELLM_SVC=... (并改 14-litellm-bridge.yaml 的 externalName)
```

★ `VLLM_API_KEY` / `LITELLM_MASTER_KEY` 经环境变量传入, 写入 K8S secret, **不落 git**。

#### 复用模式如何工作 (情况 B)
- `deploy-server.sh` 自动检测 `default` ns 有无 `litellm` Service (`REUSE_EXISTING_LITELLM=auto`), 或 `=true` 强制。
- 复用时: **跳过** `05-litellm-db` + `06-litellm` + `02-litellm-config-server` (现有 litellm 有自己的配置/DB); **改用** `14-litellm-bridge.yaml` (ExternalName Service `litellm` in openclaw → `lite-helm-litellm.default.svc.cluster.local`), 使 nginx/proxy/stream 的 `litellm:4000` 解析到现有 litellm。
- 注入 `LITELLM_MASTER_KEY` 到 secret → nginx init container envsubst → `/ui/`、`/sso/` 等注入正确 `Authorization` (**UI 鉴权关键**)。
- 经 litellm API 把 `qwen2.5`/`llava`/`deepseek-r1`/`qwen3-vl`/`funasr` 模型路由注册到现有 litellm 的 DB (`scripts/litellm_configure.py`, 幂等) — 否则现有 litellm 不认识这些 model_name, proxy 调用会 model not found。
- 跳过 `openclaw-litellm` 镜像构建 (省时)。

#### 自装模式 (情况 A) 如何工作
- 部署 `05-litellm-db` (postgres) + `06-litellm` (我们的 litellm) + `02-litellm-config-server` (configmap, qwen2.5/llava → 192.168.0.151)。
- `LITELLM_MASTER_KEY` 默认 `sk-litellm-local` (可经 env 覆盖)。

脚本会: build 5 镜像 → 注入 `VLLM_API_KEY` 到 secret → 按序 apply 清单 (跳过 ollama, 用 server 版替换 06/07/08/09/12) → 等 minio Ready → 端口转发灌入测试数据 (bucket + public-read + test_image.png + speech_test.wav) → 打印下一步。

> 若改过 `02-litellm-config-server.yaml` (如 GPUStack 地址变更) 后想单独重 apply: `kubectl apply -f k8s/server/02-litellm-config-server.yaml && kubectl rollout restart deployment/litellm -n openclaw`

### 2. 等 Pod Ready

```bash
kubectl get pods -n openclaw -w
# 期望: litellm / litellm-db / proxy / stream-service / funasr / hermes / minio / nginx 全 Running
# 注意: 不应有 ollama pod (服务器无 Ollama; vLLM 在 K8S 外经 GPUStack 提供)
```

## 访问

服务器 nginx 是 NodePort **30080** (本机 kind 的 8090 是 kind extraPortMapping 映射, 真实集群直接用 NodePort):

- **Dashboard**: `http://<节点IP>:30080/new_dashboard.html`
- **LiteLLM UI**: `http://<节点IP>:30080/ui/` (nginx 注入 master key, 可直接访问; 复用模式需 `LITELLM_MASTER_KEY` 正确)
- **API**: `http://<节点IP>:30080/v1/chat/completions`

也可端口转发: `kubectl port-forward svc/nginx -n openclaw 30080:80` → `http://localhost:30080`。
直连现有 litellm (复用模式): `kubectl -n default port-forward svc/lite-helm-litellm 4000:4000` → `http://localhost:4000/ui/`。

## 验证

### 文本模型 (qwen2.5 → deepseek-r1)
```bash
curl http://<节点IP>:30080/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5","messages":[{"role":"user","content":"你好"}]}'
# 期望: deepseek-r1 回复 (proxy 默认 STRIP_REASONING_TAGS=true, 已去 think 块)
```

### 视觉模型 (llava → qwen3-vl)
```bash
curl http://<节点IP>:30080/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5","messages":[{"role":"user","content":[
    {"type":"text","text":"图里有什么"},
    {"type":"image_url","image_url":{"url":"http://minio:9000/openclaw-test/test_image.png"}}]}]}'
# 期望: qwen3-vl 描述图片 (红底黄圆)
```

### Playwright E2E (10/10)
本机跑, 指向服务器:
```bash
# 方式A: 端口转发
kubectl port-forward svc/nginx -n openclaw 30080:80
DASHBOARD_URL=http://localhost:30080/new_dashboard.html \
  ~/MyWork/Multi-Agent/venv/bin/python tests/e2e_full_playwright.py
# 方式B: 直接用节点 IP
DASHBOARD_URL=http://<节点IP>:30080/new_dashboard.html \
  ~/MyWork/Multi-Agent/venv/bin/python tests/e2e_full_playwright.py
```
期望 10/10 PASS: chat / ws_chat / vision / multimodal / asr / ws_asr / 附件 / minio_presign。

## 关键文件

| 文件 | 作用 |
|------|------|
| `k8s/server/13-inference-endpoints.yaml` | 占位 ollama Service (nginx 启动兼容; vLLM 在 K8S 外) |
| `k8s/server/02-litellm-config-server.yaml` | LiteLLM 路由: qwen2.5/llava → vLLM |
| `k8s/server/06-litellm-server.yaml` | litellm deployment (+VLLM_API_KEY, IfNotPresent) |
| `k8s/server/12-proxy-server.yaml` | proxy (OLLAMA_URL="", STRIP_REASONING_TAGS=true) |
| `k8s/server/08-stream-service-server.yaml` | stream (OLLAMA_URL="") |
| `k8s/server/07-hermes-server.yaml` / `09-funasr-server.yaml` | IfNotPresent 版 |
| `k8s/deploy-server.sh` | 一键部署脚本 |
| `scripts/minio_setup.py` | MinIO 测试数据初始化 |
| `tests/fixtures/{test_image.png,speech_test.wav}` | E2E 测试素材 |

## 排错

- **vision 报 `Failed to load image`**: 测试图过小/损坏 → 用 `tests/fixtures/test_image.png` (256x256); 跑 `scripts/minio_setup.py` 灌入。
- **ws_asr 报 `code=1006`**: 音频是静音 → FunASR 返回空 → 用 `tests/fixtures/speech_test.wav` (真实语音); 跑 `scripts/minio_setup.py`。
- **chat 内容是 think 推理 gibberish**: `STRIP_REASONING_TAGS` 未生效 → 确认 proxy env `STRIP_REASONING_TAGS=true` (server 清单默认 true)。
- **litellm 报 model not found**: vLLM 模型名与 LiteLLM 配置里 `model: openai/<name>` 不一致 → 改 `02-litellm-config-server.yaml` 的 model 字段为 vLLM 实际 served name。
- **litellm 报 `connection refused 192.168.0.151:80`**: 节点访问不到 GPUStack → 检查 K8S 节点到 192.168.0.151 的网络/防火墙; 确认 GPUStack 在线 (`curl http://192.168.0.151/v1/models`)。本机验证时偶发此错是 GPUStack 瞬态 (worker 重启), 自愈后恢复。
- **长上下文报 400 Bad Request**: GPUStack 的 vLLM `max_model_len`≈8192 (实测), 输入超 8k 即 400。若业务需更长上下文, 服务器侧 vLLM 调高 `max_model_len` (2×H100 跑 32B 可支持 32k+)。
- **ImagePullBackOff**: 镜像不在节点上 → 单节点在该节点 build; 多节点设 `REGISTRY` 推 registry。
- **LiteLLM UI 打不开 / 401**: 复用模式下最常见。① 确认 `LITELLM_MASTER_KEY` 是现有 litellm 的真实 master key (不是 sk-litellm-local); ② 确认 `14-litellm-bridge` 已 apply 且 `externalName` 指向正确的现有 litellm (`lite-helm-litellm.default.svc.cluster.local`); ③ nginx pod 重启使 envsubst 生效: `kubectl rollout restart deploy/nginx -n openclaw`; ④ 直连验证: `kubectl -n default port-forward svc/lite-helm-litellm 4000:4000` → `http://localhost:4000/ui/` 能开说明 litellm 本身 OK, 问题在 nginx 桥接/key。
- **proxy 报 model not found (复用模式)**: 现有 litellm 未注册 qwen2.5/llava → 重跑 `scripts/litellm_configure.py` (经 port-forward), 或在 LiteLLM UI 手动加模型。`kubectl logs deploy/proxy -n openclaw` 看具体 model_name。
- **nginx 启动报 `host not found in upstream "litellm"`**: 复用模式下 14-bridge 未 apply 或 externalName 错 → `kubectl apply -f k8s/server/14-litellm-bridge.yaml` 并核对 externalName。
- **ollama 服务卡片显示异常**: 服务器无 ollama, dashboard 服务监控的 ollama 卡片会显示 down — 属已知 cosmetic, 不影响 E2E (smartRecover 的 warmup 已改走 litellm)。
