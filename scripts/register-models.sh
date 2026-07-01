#!/usr/bin/env bash
# ============================================
# register-models.sh — 给现有 litellm 注册 OpenClaw 模型 (复用模式)
# ============================================
# 现有 litellm (lite-helm, default ns) STORE_MODEL_IN_DB 未开 → /model/new 被拒。
# 本脚本: 开 STORE_MODEL_IN_DB=True + 重启 litellm + port-forward + 经 API 注册
# qwen2.5/llava/funasr → http://192.168.0.151/v1 到现有 litellm 的 DB。
#
# 用法 (需先 export 两个 key):
#   export VLLM_API_KEY=gpustack_xxx
#   export LITELLM_MASTER_KEY=sk-1234
#   bash scripts/register-models.sh
# ============================================
set -e
cd "$(dirname "$0")/.."

NS="${LITELLM_NS:-default}"
SVC="${LITELLM_SVC:-lite-helm-litellm}"
DEPLOY="${LITELLM_DEPLOY:-lite-helm-litellm}"

: "${VLLM_API_KEY:?需 export VLLM_API_KEY (GPUStack key)}"
: "${LITELLM_MASTER_KEY:?需 export LITELLM_MASTER_KEY (现有 litellm master key)}"

echo "[1/3] 开启 STORE_MODEL_IN_DB=True + 重启 ${DEPLOY}..."
kubectl -n "${NS}" set env deploy/"${DEPLOY}" STORE_MODEL_IN_DB=True
kubectl -n "${NS}" rollout status deploy/"${DEPLOY}" --timeout=180s

echo "[2/3] port-forward ${SVC}:4000 -> localhost:${LOCAL_PORT:-14000} ..."
kubectl -n "${NS}" port-forward svc/"${SVC}" ${LOCAL_PORT:-14000}:4000 > /tmp/lpf.log 2>&1 &
PF=$!
sleep 4
trap 'kill $PF 2>/dev/null || true' EXIT

# 等 litellm /health/readiness 就绪 (STORE_MODEL_IN_DB 首启要做 DB 迁移, 较慢)
echo "      等 litellm health 就绪 (最长 90s)..."
ready=0
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:${LOCAL_PORT:-14000}/health/readiness 2>/dev/null || echo 000)
  if [[ "$code" == "200" ]]; then ready=1; break; fi
  sleep 3
done
if [[ $ready -ne 1 ]]; then
  echo "⚠ litellm health 未就绪 (最后 code=$code), 仍尝试注册 (可能失败)..."
else
  echo "      litellm 就绪 ✓"
fi

echo "[3/3] 经 API 注册模型到现有 litellm DB (幂等)..."
python3 scripts/litellm_configure.py --base http://localhost:${LOCAL_PORT:-14000}
