#!/usr/bin/env bash
# ============================================
# deploy-local-vllm.sh — 本地 Kind 切换到 GPUStack vLLM 验证
# ============================================
# feature_localUsingvLLM 分支: 本地 Kind K8S + 远程 GPUStack vLLM (192.168.0.151)。
# 不重建/拆除本地 Ollama (litellm 路由切到 vLLM 后, ollama 仅作未触发的 fallback)。
#
# 用法:
#   VLLM_API_KEY=gpustack_xxx ./k8s/deploy-local-vllm.sh
#
# ★ API key 经环境变量传入, 写入 K8S secret, 不落 git。
# 前置: 本地 Kind 已起 (./k8s/deploy.sh), openclaw namespace 存在, litellm/proxy/nginx Running。
# ============================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCAL_DIR="$SCRIPT_DIR/local-vllm"
NAMESPACE="${NAMESPACE:-openclaw}"
NGINX_PORT="${NGINX_PORT:-8090}"   # 本地 kind extraPortMapping 8090->30080

G() { printf '\033[32m%s\033[0m\n' "$1"; }
Y() { printf '\033[33m%s\033[0m\n' "$1"; }
R() { printf '\033[31m%s\033[0m\n' "$1"; }
B() { printf '\033[36m%s\033[0m\n' "$1"; }

command -v kubectl >/dev/null 2>&1 || { R "kubectl 未安装"; exit 1; }
kubectl cluster-info >/dev/null 2>&1 || { R "kubectl 连不上集群 (本地 Kind 起了吗? ./k8s/deploy.sh)"; exit 1; }

# ---------- 1. 注入 VLLM_API_KEY 到 secret (不落 git) ----------
if [[ -z "${VLLM_API_KEY:-}" ]]; then
  R "缺少 VLLM_API_KEY 环境变量。用法: VLLM_API_KEY=gpustack_xxx $0"
  R "(从 GPUStack http://192.168.0.151/ 获取 API key)"
  exit 1
fi
B "==== [1/4] 注入 VLLM_API_KEY 到 secret (不落 git) ===="
kubectl patch secret openclaw-secrets -n "$NAMESPACE" \
  -p "{\"stringData\":{\"vllm-api-key\":\"$VLLM_API_KEY\"}}" >/dev/null 2>&1 \
  && G "secret openclaw-secrets/vllm-api-key 已更新" \
  || { R "patch secret 失败"; exit 1; }

# ---------- 2. apply litellm 配置 (路由到 192.168.0.151) + env patch ----------
B "==== [2/4] apply litellm 配置 (→ http://192.168.0.151/v1) ===="
kubectl apply -f "$LOCAL_DIR/02-litellm-config-local-vllm.yaml" 2>&1 | sed 's/^/    /'
# 用 strategic-merge patch 给 litellm 容器追加 VLLM_API_KEY env (从 secret 读)。
# 不用 kubectl apply -f (部分 Deployment 清单会误清不可变 selector); patch 只追加 env。
kubectl patch deployment litellm -n "$NAMESPACE" --type=strategic \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"litellm","env":[{"name":"VLLM_API_KEY","valueFrom":{"secretKeyRef":{"name":"openclaw-secrets","key":"vllm-api-key","optional":true}}}]}]}}}}' \
  2>&1 | sed 's/^/    /'

# ---------- 3. 重启 litellm 生效 ----------
B "==== [3/4] 重启 litellm (加载新配置 + VLLM_API_KEY env) ===="
kubectl rollout restart deployment/litellm -n "$NAMESPACE" 2>&1 | sed 's/^/    /'
kubectl rollout status deployment/litellm -n "$NAMESPACE" --timeout=120s 2>&1 | sed 's/^/    /'

# ---------- 4. 连通性检查 ----------
B "==== [4/4] 连通性检查 (经本地 nginx $NGINX_PORT → proxy → litellm → GPUStack) ===="
sleep 3
Y "文本模型 (qwen2.5 → deepseek-r1):"
curl -s -m 60 "http://localhost:$NGINX_PORT/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5","messages":[{"role":"user","content":"你好"}],"max_tokens":40,"stream":false}' \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); c=d.get("choices",[{}])[0].get("message",{}).get("content",""); print("  content:", (c or "(empty)")[:120])' 2>&1 | sed 's/^/    /' || R "  文本模型调用失败"

Y "视觉模型 (llava → qwen3-vl):"
IMG_URL="http://minio:9000/openclaw-test/test_image.png"
curl -s -m 90 "http://localhost:$NGINX_PORT/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"qwen2.5\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"图里有什么?中文简短\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"$IMG_URL\"}}]}],\"max_tokens\":60,\"stream\":false}" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); c=d.get("choices",[{}])[0].get("message",{}).get("content",""); print("  content:", (c or "(empty)")[:120])' 2>&1 | sed 's/^/    /' || R "  视觉模型调用失败"

G ""
G "部署完成。Dashboard: http://localhost:$NGINX_PORT/new_dashboard.html"
G "性能基准: python3 scripts/vllm_perf.py   (经本地 nginx 全链路)"
G "直连 GPUStack 基准: python3 scripts/vllm_perf.py --direct"
G "Playwright E2E (用 vLLM): ~/MyWork/Multi-Agent/venv/bin/python tests/e2e_full_playwright.py"
