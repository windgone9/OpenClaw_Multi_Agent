#!/usr/bin/env bash
# ============================================
# deploy-server.sh — 部署到测试服务器 (已有真实 K8S 集群, vLLM, 无 Ollama)
# ============================================
# 前置:
#   - kubectl 已指向目标集群 (kubectl config get-contexts)
#   - 服务器上 vLLM 已跑: deepseek-r1-distill-qwen-32b (文本) + qwen3-vl-32b-instruct (视觉)
#   - 集群节点能拉到/加载 openclaw-* 镜像 (单节点直接 build; 多节点设 REGISTRY 推 registry)
#
# 用法:
#   ./k8s/deploy-server.sh              # build + apply + minio 灌数据
#   ./k8s/deploy-server.sh --skip-build # 跳过 build
#   REGISTRY=registry.example.com/openclaw- ./k8s/deploy-server.sh   # 多节点: retag+push
#
# 部署后访问: http://<节点IP>:30080/new_dashboard.html
# (本机 kind 是 8090, 因 kind extraPortMapping 8090->30080; 真实集群直接用 NodePort 30080)
# ============================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER_DIR="$SCRIPT_DIR/server"
NAMESPACE="${NAMESPACE:-openclaw}"
REGISTRY="${REGISTRY:-}"
PYTHON="${PYTHON:-python3}"

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
  B "==== [1/4] 构建镜像 ===="
  cd "$PROJECT_DIR/docker"
  for entry in "${IMAGES[@]}"; do
    IFS=':' read -r name df ctx <<< "$entry"
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
  Y "==== [1/4] 跳过 build (--skip-build) ===="
fi

if [[ -z "$REGISTRY" ]]; then
  Y "提示: REGISTRY 未设 — 镜像须已存在于各节点 (单节点 build 即可; 多节点请设 REGISTRY 推 registry, 并取消 kustomization images: 注释或手改清单)"
fi

# ---------- 2. apply 清单 (跳过 04-ollama; 用 server 版替换 06/07/08/09/12) ----------
B "==== [2/4] apply K8S 清单 (server, 无 Ollama) ===="
# 顺序: namespace -> secrets -> base configmaps(含 nginx) -> storage -> litellm-db
#       -> server litellm-config(覆盖 config.yaml) -> server litellm/hermes/stream/funasr/proxy
#       -> nginx -> inference endpoints
BASE_AGG=(
  "$SCRIPT_DIR/00-namespace.yaml"
  "$SCRIPT_DIR/01-secrets.yaml"
  "$SCRIPT_DIR/02-configmaps.yaml"
  "$SCRIPT_DIR/03-storage.yaml"
  "$SCRIPT_DIR/05-litellm-db.yaml"
)
SERVER_AGG=(
  "$SERVER_DIR/02-litellm-config-server.yaml"   # 覆盖 litellm config.yaml (vLLM)
  "$SERVER_DIR/06-litellm-server.yaml"
  "$SERVER_DIR/07-hermes-server.yaml"
  "$SERVER_DIR/08-stream-service-server.yaml"
  "$SERVER_DIR/09-funasr-server.yaml"
  "$SERVER_DIR/12-proxy-server.yaml"
  "$SERVER_DIR/13-inference-endpoints.yaml"
  "$SCRIPT_DIR/10-minio.yaml"
  "$SCRIPT_DIR/11-nginx.yaml"
)
# 注: 不 apply base 04-ollama / 06 / 07 / 08 / 09 / 12 (用 server 版替代)
for f in "${BASE_AGG[@]}" "${SERVER_AGG[@]}"; do
  G "apply $f"
  kubectl apply -f "$f" 2>&1 | sed 's/^/    /'
done

# ---------- 3. 等 minio Ready, 灌测试数据 ----------
B "==== [3/4] 等待 MinIO Ready 并灌入测试数据 ===="
Y "等待 minio pod Ready (最长 180s)..."
kubectl wait --for=condition=Ready pod -l app=minio -n "$NAMESPACE" --timeout=180s 2>&1 | sed 's/^/    /' || R "minio 未就绪, 跳过灌数据 (可后续手动跑 scripts/minio_setup.py)"

# 端口转发 minio 到本地 9000, 跑 minio_setup.py
Y "端口转发 minio:9000 -> localhost:9000 ..."
kubectl port-forward svc/minio -n "$NAMESPACE" 9000:9000 >/tmp/minio-pf-server.log 2>&1 &
PF_PID=$!
sleep 4
if curl -s -o /dev/null http://localhost:9000/minio/health/live 2>/dev/null; then
  G "灌入测试数据 (bucket + public-read + test_image.png + speech_test.wav)..."
  MINIO_ENDPOINT=http://localhost:9000 MINIO_BUCKET=openclaw-test \
    "$PYTHON" "$PROJECT_DIR/scripts/minio_setup.py" 2>&1 | sed 's/^/    /' || R "minio_setup.py 失败"
else
  R "minio 端口转发失败, 跳过灌数据. 请手动: kubectl port-forward svc/minio -n $NAMESPACE 9000:9000 && $PYTHON scripts/minio_setup.py"
fi
kill "$PF_PID" 2>/dev/null || true

# ---------- 4. 完成 + 下一步 ----------
B "==== [4/4] 部署完成 ===="
G "已 apply 全部 server 清单 (无 Ollama). Pod 状态:"
kubectl get pods -n "$NAMESPACE" 2>&1 | sed 's/^/    /'

cat <<EOF

${G}下一步:${R}
${G}1.${R} ★ 填 vLLM 端点: 编辑 $SERVER_DIR/13-inference-endpoints.yaml,
   把 REPLACE_TEXT_VLLM_IP / REPLACE_VISION_VLLM_IP 改为服务器 vLLM 真实 IP, 端口按实际改;
   然后: kubectl apply -f $SERVER_DIR/13-inference-endpoints.yaml

${G}2.${R} 等 pod Ready: kubectl get pods -n $NAMESPACE -w

${G}3.${R} 验证文本模型:
   curl http://<节点IP>:30080/v1/chat/completions -H 'Content-Type: application/json' \\
     -d '{"model":"qwen2.5","messages":[{"role":"user","content":"你好"}]}'

${G}4.${R} 验证视觉模型:
   curl http://<节点IP>:30080/v1/chat/completions -H 'Content-Type: application/json' \\
     -d '{"model":"qwen2.5","messages":[{"role":"user","content":[{"type":"text","text":"图里有什么"},{"type":"image_url","image_url":{"url":"http://minio:9000/openclaw-test/test_image.png"}}]}]}'

${G}5.${R} Dashboard: http://<节点IP>:30080/new_dashboard.html

${G}6.${R} Playwright E2E (本机跑, 指向服务器):
   DASHBOARD_URL 不支持覆盖; 可改 tests/e2e_full_playwright.py 的 DASHBOARD_URL
   或临时: kubectl port-forward svc/nginx -n $NAMESPACE 30080:80
   ~/MyWork/Multi-Agent/venv/bin/python tests/e2e_full_playwright.py  (DASHBOARD_URL 改 http://localhost:30080)

详见 docs/SERVER_DEPLOY.md
EOF
