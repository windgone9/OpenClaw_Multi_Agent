# 服务器部署 Handoff — feature_deploytoTestServer (2026-07-01)

## 最终状态 ✅ 部署成功

测试服务器 `192.168.0.151`(4×H100, kubeadm 多节点集群, K8S v1.33.4, runtime=docker://28.5.2)上,OpenClaw Multi-Agent 以 K8S 部署完成,复用现有 LiteLLM,vLLM 复用 GPUStack。

### 端到端验证
- **chat**: `qwen2.5` → deepseek-r1-distill-qwen-32b 回复 ✓ (`vllm-0.22.1-tp2`)
- **vision**: `qwen2.5`+图 → qwen3-vl-32b-instruct 回复 `"红色背景上有一个黄色圆圈。"` ✓
- **LiteLLM UI**: `http://192.168.0.151:30080/ui/` 可打开 ✓
- **Dashboard**: `http://192.168.0.151:30080/new_dashboard.html`

### 拓扑
```
Mac/浏览器 → http://192.168.0.151:30080 (nginx NodePort)
  → nginx pod (openclaw, worker03)
  → proxy pod (openclaw, master02)  [图片: minio→base64, 自动切视觉模型]
  → litellm (default ns, lite-helm-litellm, 复用)  [经 14-bridge: openclaw/litellm → lite-helm-litellm.default]
  → GPUStack vLLM (192.168.0.151:80, 集群外, 4×H100 两模型常驻 tp2)
      deepseek-r1-distill-qwen-32b (文本) + qwen3-vl-32b-instruct (视觉)
```

### Pod 状态
| Pod | ns | 节点 | 状态 |
|-----|----|----|------|
| hermes / proxy / stream-service / funasr | openclaw | k8s-master02-ceph-01 (钉节点) | Running |
| minio / nginx | openclaw | k8s-worker03-aixn | Running |
| lite-helm-litellm | default | (复用, helm 装) | Running 1/1, STORE_MODEL_IN_DB=True |
| postgresql-standalone | default | (复用, litellm 的 DB) | Running |

### LiteLLM 模型注册 (DB, 经 /model/new API)
`qwen2.5` / `deepseek-r1-distill-qwen-32b` → http://192.168.0.151/v1 (文本)
`llava` / `qwen3-vl-32b-instruct` → http://192.168.0.151/v1 (视觉)
`funasr` → http://funasr:8199/v1 (本地 ASR)

### MinIO
bucket `openclaw-test` (public-read) + `test_image.png` + `speech_test.wav` 已灌入。

---

## 部署过程踩坑与修复 (按出现顺序)

| # | 问题 | 根因 | 修复 |
|---|------|------|------|
| 1 | `current-context is not set` | ubuntu 用户无 kubeconfig | `sudo cp /etc/kubernetes/admin.conf ~/.kube/config` + `sudo usermod -aG docker ubuntu` |
| 2 | build 镜像报 `failed size validation` (python:3.11-slim) | buildkit 元数据校验 bug (镜像加速器返回坏 manifest) | `DOCKER_BUILDKIT=0` 跑 deploy (传统 builder 走能用的 docker pull 路径) |
| 3 | ` namespaces "openclaw" not found` (apply 01-secrets) | step2 先 apply secret 但 namespace 在 step3 才建 | 改 deploy-server.sh: step2 先 apply 00-namespace 再 01-secrets (已提交 a69ca02) |
| 4 | ImagePullBackOff (hermes/proxy/stream/funasr) | 多节点集群, docker build 镜像只在 master02 docker store, pod 调度到 worker 节点拉不到 (runtime=docker, 镜像不跨节点) | `bash k8s/server/pin-to-node.sh k8s-master02-ceph-01` 把 4 个 deployment 钉到 master02 + control-plane toleration |
| 5 | LiteLLM UI 打不开 (原始诉求) | 现有 litellm 在 default ns, openclaw 的 nginx/proxy 引用 litellm:4000 解析不到 + master key 未注入 | 14-litellm-bridge (ExternalName litellm → lite-helm-litellm.default) + 注入 LITELLM_MASTER_KEY 到 secret (nginx envsubst 注入 Authorization) |
| 6 | `litellm_configure.py` 注册模型报 `Set STORE_MODEL_IN_DB=True` | 现有 litellm (helm, 配置文件模式) 未开 DB 模型存储 | `kubectl set env deploy/lite-helm-litellm STORE_MODEL_IN_DB=True` + 重启, 再经 /model/new 注册 |
| 7 | `kubectl port-forward` 报 `socat not found` | 节点未装 socat (port-forward 依赖它) | 绕开 port-forward: 注册模型用 `kubectl exec` 进 proxy pod (经 14-bridge 直达 litellm); minio 灌数据用 ClusterIP 直连 |
| 8 | `kubectl cp deploy/proxy` 报 `pods "proxy" not found` | kubectl cp 不解析 deployment 名 | 改 stdin 管道: `kubectl exec -i deploy/proxy -- python3 - < script` (exec 解析 deployment) |
| 9 | vision 报 `图片加载失败 403` | minio_setup.py 走 port-forward 失败 (socat) → bucket 未建/未传图 → proxy 取不到图 → URL 直送 vLLM → 403 | minio_setup.py 改经 minio ClusterIP 直连 (节点能达 ClusterIP): `MINIO_ENDPOINT=http://<minio-clusterIP>:9000 python3 scripts/minio_setup.py` |

---

## 运维命令 (在测试服务器 192.168.0.151, ssh ubuntu@192.168.0.151)

### 一次性环境 (已做, 重装时才需)
```bash
# kubeconfig + docker 组
mkdir -p $HOME/.kube && sudo cp -i /etc/kubernetes/admin.conf $HOME/.kube/config && sudo chown $(id -u):$(id -g) $HOME/.kube/config
sudo usermod -aG docker ubuntu && exit   # 重登录
```

### 部署 (复用模式, 镜像已 build 可加 --skip-build)
```bash
cd ~/OpenClaw_Multi_Agent && git pull
DOCKER_BUILDKIT=0 \
VLLM_API_KEY=gpustack_d99986c2981d21f6_993b438a4f4cd13aaa8adde5601f07e4 \
LITELLM_MASTER_KEY=sk-1234 \
./k8s/deploy-server.sh --skip-build 2>&1 | tee /tmp/deploy.log

# 钉节点 (镜像在 master02, 让 4 个 openclaw deployment 跑 master02)
bash k8s/server/pin-to-node.sh k8s-master02-ceph-01
```

### 注册模型到现有 litellm (复用模式, 幂等)
```bash
export VLLM_API_KEY=gpustack_d99986c2981d21f6_993b438a4f4cd13aaa8adde5601f07e4
export LITELLM_MASTER_KEY=sk-1234
bash scripts/register-models-incluster.sh   # kubectl exec 进 proxy pod, 经 14-bridge 注册
```

### 灌 MinIO 测试数据 (经 ClusterIP, 不用 port-forward)
```bash
python3 -c "import boto3" 2>/dev/null || pip3 install --user boto3
MI=$(kubectl -n openclaw get svc minio -o jsonpath='{.spec.clusterIP}')
MINIO_ENDPOINT=http://$MI:9000 python3 scripts/minio_setup.py
```

### 端到端测试
```bash
bash scripts/test-chat.sh     # 文本: qwen2.5 → deepseek-r1
bash scripts/test-vision.sh   # 视觉: qwen2.5+图 → qwen3-vl
```

### 访问
- Dashboard: http://192.168.0.151:30080/new_dashboard.html
- LiteLLM UI: http://192.168.0.151:30080/ui/
- API: http://192.168.0.151:30080/v1/chat/completions

### 回归 (本机 Mac, 指向服务器)
```bash
DASHBOARD_URL=http://192.168.0.151:30080/new_dashboard.html \
  ~/MyWork/Multi-Agent/venv/bin/python tests/e2e_full_playwright.py   # 期望 10/10
```

---

## 关键文件 (feature_deploytoTestServer 分支)

| 文件 | 作用 |
|------|------|
| `k8s/deploy-server.sh` | 一键部署 (复用检测 + 桥接 + 注入 key + minio) |
| `k8s/server/14-litellm-bridge.yaml` | ExternalName litellm → lite-helm-litellm.default |
| `k8s/server/13-inference-endpoints.yaml` | 占位 ollama Service (nginx 启动兼容) |
| `k8s/server/{06,07,08,09,12}-*-server.yaml` | server 版 deployment (IfNotPresent, OLLAMA_URL="", STRIP_REASONING_TAGS) |
| `k8s/server/pin-to-node.sh` | 钉 deployment 到镜像所在节点 (+ toleration) |
| `scripts/register-models-incluster.sh` | kubectl exec 进 proxy pod 注册模型 (绕 port-forward) |
| `scripts/litellm_configure.py` | 经 /model/new 注册模型 (幂等) |
| `scripts/minio_setup.py` | 建 bucket + public-read + 上传测试文件 |
| `scripts/test-chat.sh` / `test-vision.sh` | 端到端验证 (JSON 走文件) |

---

## 已知限制 / 待办

1. **deepseek-r1 内联推理未剥离**: GPUStack 的 vLLM 返回 r1 推理为内联文本 (无 ` Wooden` 标签), proxy 的 `STRIP_REASONING_TAGS` (标签剥离) 不作用; content 含推理 (非空, 不影响功能)。若要干净输出, 需在 proxy 加 "无标签推理" 的启发式剥离, 或 vLLM 侧开 `reasoning_content` 分离。
2. **pod 钉 master02**: 4 个 openclaw deployment 集中在 master02 (镜像在该节点 docker store)。要分散到 worker 需: `docker save` + 跨节点 `docker load` (runtime=docker), 或搭 registry + REGISTRY 前缀。
3. **节点缺 socat**: `kubectl port-forward` 不可用 (已用 ClusterIP/exec 绕开)。若要恢复 port-forward: `sudo apt install -y socat` 各节点。
4. **deploy-server.sh 未烘焙所有修复**: 当前 deploy 仍需手动补三步 (pin-to-node, register-models-incluster, minio ClusterIP 灌数据)——因 socat 缺失致 deploy 内置的 port-forward 步骤失效。后续可把这三步改进 deploy-server.sh (用 ClusterIP/exec 替代 port-forward) 实现真正一键。
5. **max_model_len≈8k**: GPUStack 的 vLLM 上下文上限 ~8192 (deepseek-r1 原生 128k, GPUStack 限了)。长输入 (>8k token) 报 400。需长上下文时调高 vLLM `max_model_len`。
6. **H100 容量 (本机验证数据)**: 文本 ~760 t/s @40并发, 视觉 ~660 t/s @80并发, 低延迟甜点 ≤20并发。服务器配置输入见 `docs/NEXT_STEPS.md`。

---

## 相关分支
- `feature_deploytoTestServer` (本分支, 服务器部署) — 已推 origin, 服务器已部署并验证。
- `feature_localUsingvLLM` (本地 Kind + GPUStack 验证 + 容量压测) — 验证 vLLM 兼容性/性能, 产出 H100 容量曲线。
- `feature_OpenAI_compatible` (本机 Ollama 主开发线)。
