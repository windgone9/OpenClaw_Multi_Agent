#!/usr/bin/env bash
# ============================================
# deploy-server.sh — 部署到测试服务器 (方案A: vLLM 复用 GPUStack, 直连)
# ============================================
# 服务器 K8S 只跑 OpenClaw pod (proxy/stream/funasr/hermes/minio/nginx);
# vLLM 由 GPUStack 托管于 http://192.168.0.151/v1 (集群外), litellm 直连 + API key。
# 无 Ollama (保留占位 ollama Service 仅供 nginx 启动)。
#
# ★ 复用现有 LiteLLM: 若 default ns 已有 litellm Service (REUSE_EXISTING_LITELLM=auto
#   自动检测, 或 =true 强制), 则跳过我们的 05-litellm-db + 06-litellm, 改用 14-litellm-bridge
#   (ExternalName → lite-helm-litellm.default) 桥接, 并经 litellm API 把模型注册到现有 litellm。
#   需额外提供 LITELLM_MASTER_KEY (现有 litellm 的 master key, UI 鉴权用)。
#
# 前置:
#   - kubectl 已指向目标集群 (kubectl config get-contexts)
#   - 集群节点能访问 192.168.0.151:80 (GPUStack 同网段; 跨网段需额外网络配置)
#   - GPUStack 已跑: deepseek-r1-distill-qwen-32b + qwen3-vl-32b-instruct
#   - 集群节点能拉到/加载 openclaw-* 镜像 (单节点直接 build; 多节点设 REGISTRY 推 registry)
#
# 用法:
#   # 全新部署 (我们自己装 litellm+db):
#   VLLM_API_KEY=gpustack_xxx ./k8s/deploy-server.sh
#   # 复用现有 litellm (default ns) — 需 LITELLM_MASTER_KEY:
#   VLLM_API_KEY=gpustack_xxx LITELLM_MASTER_KEY=sk-xxx ./k8s/deploy-server.sh
#   VLLM_API_KEY=gpustack_xxx LITELLM_MASTER_KEY=sk-xxx ./k8s/deploy-server.sh --skip-build
#
# ★ VLLM_API_KEY / LITELLM_MASTER_KEY 经环境变量传入, 写入 K8S secret, 不落 git。
# 部署后访问: http://<节点IP>:30080/new_dashboard.html  (Dashboard)
#             http://<节点IP>:30080/ui/                  (LiteLLM UI, 需 master key)
# ============================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER_DIR="$SCRIPT_DIR/server"
NAMESPACE="${NAMESPACE:-openclaw}"
REGISTRY="${REGISTRY:-}"
PYTHON="${PYTHON:-python3}"
# 复用现有 litellm: auto(检测 default ns lite-helm-litellm) / true(强制) / false(自己装)
REUSE_LITELLM="${REUSE_EXISTING_LITELLM:-auto}"
# 现有 litellm 所在 namespace + Service 名 (用于桥接 externalName, 仅 REUSE 时生效)
EXISTING_LITELLM_NS="${EXISTING_LITELLM_NS:-default}"
EXISTING_LITELLM_SVC="${EXISTING_LITELLM_SVC:-lite-helm-litellm}"
EXISTING_LITELLM_DEPLOY="${EXISTING_LITELLM_DEPLOY:-lite-helm-litellm}"

# 颜色
G() { printf '\033[32m%s\033[0m\n' "$1"; }
Y() { printf '\033[33m%s\033[0m\n' "$1"; }
B() { printf '\033[36m%s\033[0m\n' "$1"; }
R() { printf '\033[31m%s\033[0m\n' "$1"; }

cd "$PROJECT_DIR"

# ---------- 0. 前置检查 ----------
command -v kubectl >/dev/null 2>&1 || { R "kubectl 未安装"; exit 1; }
kubectl cluster-info >/dev/null 2>&1 || { R "kubectl 连不上集群, 检查 kubeconfig"; exit 1; }
B "目标集群: $(kubectl config current-context 2>/dev/null || echo '?')"

if [[ -z "${VLLM_API_KEY:-}" ]]; then
  R "缺少 VLLM_API_KEY 环境变量 (GPUStack http://192.168.0.151/ 的 API key)。"
  R "用法: VLLM_API_KEY=gpustack_xxx $0"
  exit 1
fi

# ---------- 0.5 复用现有 LiteLLM 检测 ----------
LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-}"
REUSE=0
if [[ "$REUSE_LITELLM" == "true" ]]; then
  REUSE=1
elif [[ "$REUSE_LITELLM" == "auto" ]]; then
  if kubectl -n "$EXISTING_LITELLM_NS" get svc "$EXISTING_LITELLM_SVC" >/dev/null 2>&1; then
    REUSE=1
  fi
fi
if [[ $REUSE -eq 1 ]]; then
  B "复用现有 LiteLLM: ${EXISTING_LITELLM_NS}/${EXISTING_LITELLM_SVC}:4000 (跳过 05-litellm-db + 06-litellm, 用 14-litellm-bridge 桥接)"
  if [[ -z "$LITELLM_MASTER_KEY" ]]; then
    R "复用模式需 LITELLM_MASTER_KEY (现有 litellm 的 master key, UI 鉴权用)。"
    R "用法: VLLM_API_KEY=gpustack_xxx LITELLM_MASTER_KEY=sk-xxx $0"
    exit 1
  fi
else
  Y "未检测到现有 litellm (或 REUSE_EXISTING_LITELLM=false) → 部署我们自己的 05-litellm-db + 06-litellm"
  LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-litellm-local}"
fi

SKIP_BUILD=0
[[ "${1:-}" == "--skip-build" ]] && SKIP_BUILD=1

# ---------- 1. 构建镜像 ----------
IMAGES=(
  "openclaw-litellm:Dockerfile.litellm:."          # name:Dockerfile:context(rel to docker/)
  "openclaw-hermes:Dockerfile.hermes:.."
  "openclaw-proxy:Dockerfile.proxy:.."
  "openclaw-stream:Dockerfile.stream:.."
  "openclaw-funasr:Dockerfile.funasr:.."
)
if [[ $SKIP_BUILD -eq 0 ]]; then
  B "==== [1/7] 构建镜像 ===="
  cd "$PROJECT_DIR/docker"
  for entry in "${IMAGES[@]}"; do
    IFS=':' read -r name df ctx <<< "$entry"
    # 复用模式不部署自己的 litellm → 跳过 openclaw-litellm 构建 (省时)
    if [[ $REUSE -eq 1 && "$name" == "openclaw-litellm" ]]; then
      Y "skip build $name (复用现有 litellm)"
      continue
    fi
    G "build $name (-f $df ctx=$ctx)"
    docker build -t "$name:latest" -f "$df" "$ctx" 2>&1 | tail -1
    # 多节点: retag + push 到 registry
    if [[ -n "$REGISTRY" ]]; then
      docker tag "$name:latest" "${REGISTRY}${name}:latest"
      docker push "${REGISTRY}${name}:latest" 2>&1 | tail -1
      G "pushed ${REGISTRY}${name}:latest"
    fi
  done
  cd "$PROJECT_DIR"
else
  Y "==== [1/7] 跳过 build (--skip-build) ===="
fi

if [[ -z "$REGISTRY" ]]; then
  Y "提示: REGISTRY 未设 — 镜像须已存在于各节点 (单节点 build 即可; 多节点请设 REGISTRY 推 registry)"
fi

# ---------- 2. 先 apply namespace + secret + 注入 VLLM_API_KEY + LITELLM_MASTER_KEY (不落 git) ----------
B "==== [2/7] apply namespace + secret + 注入 VLLM_API_KEY / LITELLM_MASTER_KEY (不落 git) ===="
# 必须先建 namespace (00), 再建 secret (01, 引用 namespace openclaw), 再 patch 真实 key,
# 保证后续 litellm (自己装) / nginx (envsubst) 启动时 key 已就位。
kubectl apply -f "$SCRIPT_DIR/00-namespace.yaml" 2>&1 | sed 's/^/    /'
kubectl apply -f "$SCRIPT_DIR/01-secrets.yaml" 2>&1 | sed 's/^/    /'
kubectl patch secret openclaw-secrets -n "$NAMESPACE" \
  -p "{\"stringData\":{\"vllm-api-key\":\"$VLLM_API_KEY\",\"litellm-master-key\":\"$LITELLM_MASTER_KEY\"}}" >/dev/null 2>&1 \
  && G "secret openclaw-secrets 已更新 (vllm-api-key + litellm-master-key)" \
  || { R "patch secret 失败"; exit 1; }

# ---------- 3. apply 其余清单 (跳过 04-ollama; 用 server 版替换 06/07/08/09/12) ----------
B "==== [3/7] apply K8S 清单 (server, vLLM 直连 GPUStack, 无 Ollama) ===="
# 顺序: [namespace+secret 已在 step 2] -> base configmaps(含 nginx) -> storage
#       -> [05-litellm-db 仅自装模式] -> server litellm-config/hermes/stream/funasr/proxy
#       -> nginx -> 占位 ollama Service (nginx 启动兼容)
# 注: 00-namespace + 01-secrets 已在 step 2 apply; 不 apply base 04-ollama / 06 / 07 / 08 / 09 / 12 (用 server 版替代)
BASE_AGG=(
  "$SCRIPT_DIR/02-configmaps.yaml"
  "$SCRIPT_DIR/03-storage.yaml"
)
SERVER_AGG=(
  "$SERVER_DIR/07-hermes-server.yaml"
  "$SERVER_DIR/08-stream-service-server.yaml"   # OLLAMA_URL="" (走 litellm, 复用时经桥接)
  "$SERVER_DIR/09-funasr-server.yaml"
  "$SERVER_DIR/12-proxy-server.yaml"            # OLLAMA_URL="", STRIP_REASONING_TAGS=true
  "$SERVER_DIR/13-inference-endpoints.yaml"     # 占位 ollama Service (nginx 启动兼容)
  "$SCRIPT_DIR/10-minio.yaml"
  "$SCRIPT_DIR/11-nginx.yaml"
)
if [[ $REUSE -eq 1 ]]; then
  # 复用现有 litellm: 不装 05-litellm-db / 06-litellm / 02-litellm-config-server (现有 litellm 有自己的配置);
  # 改用 14-litellm-bridge 把 openclaw 的 "litellm" DNS 桥接到现有 litellm。
  # 现有 litellm 的模型路由由后续 litellm_configure.py 经 API 注册到其 DB。
  SERVER_AGG+=("$SERVER_DIR/14-litellm-bridge.yaml")
  Y "复用模式: 跳过 05-litellm-db / 06-litellm / 02-litellm-config-server; apply 14-litellm-bridge"
else
  # 自装模式: 部署自己的 litellm-db + litellm + litellm-config (→ 192.168.0.151)
  BASE_AGG+=("$SCRIPT_DIR/05-litellm-db.yaml")
  SERVER_AGG=("$SERVER_DIR/02-litellm-config-server.yaml" "${SERVER_AGG[@]}")
  SERVER_AGG=("$SERVER_DIR/06-litellm-server.yaml" "${SERVER_AGG[@]}")
fi
for f in "${BASE_AGG[@]}" "${SERVER_AGG[@]}"; do
  G "apply $f"
  kubectl apply -f "$f" 2>&1 | sed 's/^/    /'
done

# ---------- 4. 钉节点 (单节点镜像分发: 镜像只在 build 节点 docker store) ----------
# 多节点集群 + runtime=docker + 未设 REGISTRY → docker build 的镜像只在本节点,
# pod 调度到别的节点会 ImagePullBackOff。把 4 个 openclaw deployment 钉到本节点
# (+ control-plane toleration, 若本节点是 master)。设了 REGISTRY (推 registry) 则跳过。
if [[ -z "$REGISTRY" ]]; then
  B "==== [4/7] 钉 openclaw deployment 到本节点 (镜像在本节点 docker store) ===="
  PIN_NODE="${PIN_NODE:-$(hostname)}"
  Y "钉 hermes/proxy/stream-service/funasr -> ${PIN_NODE}"
  bash "$SERVER_DIR/pin-to-node.sh" "$PIN_NODE" 2>&1 | sed 's/^/    /' || R "pin-to-node 失败 (可手动: bash $SERVER_DIR/pin-to-node.sh $PIN_NODE)"
else
  Y "==== [4/7] 跳过钉节点 (REGISTRY 已设, 镜像推 registry, 各节点可拉) ===="
fi

# ---------- 5. 复用模式: 注册模型到现有 litellm (incluster, 绕 port-forward/socat) ----------
if [[ $REUSE -eq 1 ]]; then
  B "==== [5/7] 注册模型到现有 litellm (经 proxy pod incluster, 绕 port-forward) ===="
  # 5.1 确保 STORE_MODEL_IN_DB=True + 重启 (litellm 须开此才能 /model/new)
  Y "确保 litellm STORE_MODEL_IN_DB=True + 重启..."
  kubectl -n "$EXISTING_LITELLM_NS" set env deploy/"$EXISTING_LITELLM_DEPLOY" STORE_MODEL_IN_DB=True >/dev/null 2>&1
  kubectl -n "$EXISTING_LITELLM_NS" rollout status deploy/"$EXISTING_LITELLM_DEPLOY" --timeout=180s 2>&1 | sed 's/^/    /'
  # 5.2 等 proxy pod Ready (kubectl exec 目标)
  Y "等 proxy pod Ready..."
  kubectl -n "$NAMESPACE" rollout status deploy/proxy --timeout=180s 2>&1 | sed 's/^/    /'
  # 5.3 kubectl exec 进 proxy pod, stdin 管道传脚本, 经 litellm:4000 (14-bridge) 注册
  #     (不依赖 socat/port-forward; proxy pod 的 LITELLM_MASTER_KEY 来自 secret, 此处仍内联双 key 保险)
  G "经 proxy pod 注册模型 (qwen2.5/llava/funasr → 192.168.0.151)..."
  kubectl -n "$NAMESPACE" exec -i deploy/proxy -- sh -c "VLLM_API_KEY=${VLLM_API_KEY} LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY} python3 - --base http://litellm:4000" < "$PROJECT_DIR/scripts/litellm_configure.py" 2>&1 | sed 's/^/    /' \
    || Y "模型注册部分失败 (可能已存在或需 UI 手动加, 见日志)"
fi

# ---------- 6. 等 minio Ready, 灌测试数据 (经 ClusterIP, 绕 port-forward/socat) ----------
B "==== [6/7] 等 MinIO Ready 并灌入测试数据 (经 ClusterIP) ===="
Y "等 minio pod Ready (最长 180s)..."
kubectl wait --for=condition=Ready pod -l app=minio -n "$NAMESPACE" --timeout=180s 2>&1 | sed 's/^/    /' || R "minio 未就绪, 跳过灌数据"
MI=$(kubectl -n "$NAMESPACE" get svc minio -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
if [[ -n "$MI" ]]; then
  # 确保 boto3 (minio_setup.py 依赖); 节点能达 ClusterIP (kube-proxy)
  if ! "$PYTHON" -c "import boto3" 2>/dev/null; then
    Y "安装 boto3 (minio_setup.py 依赖)..."
    "$PYTHON" -m pip install --user boto3 2>&1 | tail -1 || R "boto3 安装失败, 可手动: pip3 install boto3 或 sudo apt install python3-boto3"
  fi
  G "灌入测试数据 (bucket + public-read + test_image.png + speech_test.wav) 经 minio ClusterIP ${MI}:9000..."
  MINIO_ENDPOINT="http://${MI}:9000" "$PYTHON" "$PROJECT_DIR/scripts/minio_setup.py" 2>&1 | sed 's/^/    /' || R "minio_setup.py 失败 (可手动: MINIO_ENDPOINT=http://${MI}:9000 $PYTHON scripts/minio_setup.py)"
else
  R "未取到 minio ClusterIP, 跳过灌数据. 手动: MI=\$(kubectl -n $NAMESPACE get svc minio -o jsonpath='{.spec.clusterIP}') && MINIO_ENDPOINT=http://\$MI:9000 $PYTHON scripts/minio_setup.py"
fi

# ---------- 7. 完成 + 下一步 ----------
B "==== [7/7] 部署完成 ===="
if [[ $REUSE -eq 1 ]]; then
  G "复用现有 LiteLLM (${EXISTING_LITELLM_NS}/${EXISTING_LITELLM_SVC}:4000, 经 14-bridge 桥接)。OpenClaw pod:"
else
  G "自装 LiteLLM+DB。OpenClaw pod:"
fi
G "OpenClaw ns pod 状态:"
kubectl get pods -n "$NAMESPACE" 2>&1 | sed 's/^/    /'
if [[ $REUSE -eq 1 ]]; then
  Y "现有 litellm pod (default ns):"
  kubectl get pods -n "$EXISTING_LITELLM_NS" 2>&1 | sed 's/^/    /' | head -8
fi

cat <<EOF

部署已完成 (build + apply + 钉节点 + 注册模型 + minio 灌数据 全自动)。下一步验证:

1. 等 pod Ready: kubectl get pods -n $NAMESPACE -w
   (不应有 ollama pod; vLLM 在 K8S 外经 GPUStack 提供$([[ $REUSE -eq 1 ]] && echo "; litellm 复用 default ns 现有"))

2. LiteLLM UI: http://<节点IP>:30080/ui/   (nginx 注入 master key, 可直接访问)

3. 端到端测试 (在服务器上):
   bash scripts/test-chat.sh        # 文本: qwen2.5 → deepseek-r1
   bash scripts/test-vision.sh      # 视觉: qwen2.5+图 → qwen3-vl

4. Dashboard: http://<节点IP>:30080/new_dashboard.html

5. Playwright E2E 10/10 (本机 Mac, 指向服务器):
   dashboard 只认 8080/8090, 服务器 NodePort 30080 → 用 SSH 隧道映射:
   ssh -N -L 8090:localhost:30080 ubuntu@<节点IP> &   # 后台隧道
   DASHBOARD_URL=http://localhost:8090/new_dashboard.html \\
     ~/MyWork/Multi-Agent/venv/bin/python tests/e2e_full_playwright.py

6. 性能/容量基准 (本机, 指向服务器):
   改 scripts/vllm_perf.py 的 KIND_BASE 为 http://<节点IP>:30080 后:
   python3 scripts/vllm_perf.py --ramp        # 并发阶梯
   python3 scripts/vllm_perf.py --longctx     # 长上下文

详见 docs/SERVER_DEPLOY_HANDOFF.md / docs/SERVER_DEPLOY.md
EOF
