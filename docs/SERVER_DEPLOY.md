# 服务器部署指南 (feature_deploytoTestServer)

把 OpenClaw Multi-Agent 部署到**已有真实 K8S 集群**的测试服务器。服务器用 **vLLM** 跑两个模型 (替代本机 Ollama 的 qwen2.5:3b + llava:7b):

| 用途 | 服务器模型 (vLLM) | 替代本机 | LiteLLM model_name 别名 |
|------|-------------------|----------|--------------------------|
| 文本 | `deepseek-r1-distill-qwen-32b` | qwen2.5:3b | `qwen2.5` / `deepseek-r1-distill-qwen-32b` |
| 视觉 | `qwen3-vl-32b-instruct` | llava:7b | `llava` / `qwen3-vl-32b-instruct` |

**核心思路**: 保留 `qwen2.5`/`llava` 作为 LiteLLM 别名指向 vLLM, 使 proxy / stream-service / dashboard / server.py **零代码改动**; 只改 LiteLLM 路由配置 + 移除 Ollama。

## 架构差异 (vs 本机 kind)

```
本机 kind:   nginx(8090) → proxy → litellm → ollama(qwen2.5:3b + llava:7b)
服务器:       nginx(30080) → proxy → litellm → vLLM(text-inference + vision-inference)
                                          (服务器已部署, 不由本仓管理)
```

- **不部署 Ollama** (`04-ollama.yaml` 不 apply)。
- LiteLLM 配置路由到 `http://text-inference:8000/v1` 与 `http://vision-inference:8000/v1` (集群内 DNS, 由 `13-inference-endpoints.yaml` 桥接)。
- proxy/stream `OLLAMA_URL` 置空, 直走 LiteLLM。
- 镜像 `imagePullPolicy=IfNotPresent` (真实集群, 非 kind load)。

## 前置条件

1. **kubectl** 已指向目标集群: `kubectl config current-context`。
2. **服务器 vLLM 已就绪**, 两个模型各自独立端口, 集群内可达:
   - 文本 vLLM 跑 `deepseek-r1-distill-qwen-32b` (OpenAI 兼容 `/v1`)
   - 视觉 vLLM 跑 `qwen3-vl-32b-instruct` (OpenAI 兼容 `/v1`, 支持 `image_url`)
3. **镜像分发**:
   - 单节点集群: 在该节点上跑 deploy-server.sh, build 出来的镜像本地可用。
   - 多节点集群: 设 `REGISTRY=your-registry.example.com/openclaw-`, 脚本会 retag+push; 或取消 `k8s/server/kustomization.yaml`(已移除, 改用清单直 apply) 里对应注释。

## 部署步骤

### 1. 填 vLLM 端点 (★ 必填)

编辑 `k8s/server/13-inference-endpoints.yaml`, 把两个 Endpoints 的占位 IP 改为服务器 vLLM 真实 IP, 端口按实际改 (默认 8000):

```yaml
subsets:
  - addresses:
      - ip: 10.0.0.20        # ← deepseek-r1 vLLM 真实 IP
    ports:
      - port: 8000           # ← 按实际 vLLM 端口
```

若 vLLM 启动带了 `--api-key`, 把 key 填进 `k8s/01-secrets.yaml` 的 `vllm-api-key` (默认 `EMPTY`)。

### 2. 一键部署

```bash
cd <repo>
./k8s/deploy-server.sh
# 多节点: REGISTRY=your-registry/openclaw- ./k8s/deploy-server.sh
```

脚本会: build 5 镜像 (litellm/proxy/stream/hermes/funasr) → 按序 apply 清单 (跳过 ollama, 用 server 版替换 06/07/08/09/12) → 等 minio Ready → 端口转发灌入测试数据 (bucket + public-read + test_image.png + speech_test.wav) → 打印下一步。

> 若改过 `13-inference-endpoints.yaml` 后想单独重 apply: `kubectl apply -f k8s/server/13-inference-endpoints.yaml`

### 3. 等 Pod Ready

```bash
kubectl get pods -n openclaw -w
# 期望: litellm / litellm-db / proxy / stream-service / funasr / hermes / minio / nginx 全 Running
# 注意: 不应有 ollama pod (服务器无 Ollama)
```

## 访问

服务器 nginx 是 NodePort **30080** (本机 kind 的 8090 是 kind extraPortMapping 映射, 真实集群直接用 NodePort):

- Dashboard: `http://<节点IP>:30080/new_dashboard.html`
- API: `http://<节点IP>:30080/v1/chat/completions`

也可端口转发: `kubectl port-forward svc/nginx -n openclaw 30080:80` → `http://localhost:30080`。

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
| `k8s/server/13-inference-endpoints.yaml` | ★ vLLM 端点 Service+Endpoints (填 IP) |
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
- **ImagePullBackOff**: 镜像不在节点上 → 单节点在该节点 build; 多节点设 `REGISTRY` 推 registry。
- **ollama 服务卡片显示异常**: 服务器无 ollama, dashboard 服务监控的 ollama 卡片会显示 down — 属已知 cosmetic, 不影响 E2E (smartRecover 的 warmup 已改走 litellm)。
