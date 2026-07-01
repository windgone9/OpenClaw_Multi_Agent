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

echo "[2/3] port-forward ${SVC}:4000 -> localhost:4000 ..."
kubectl -n "${NS}" port-forward svc/"${SVC}" 4000:4000 > /tmp/lpf.log 2>&1 &
PF=$!
sleep 4
trap 'kill $PF 2>/dev/null || true' EXIT

echo "[3/3] 经 API 注册模型到现有 litellm DB (幂等)..."
python3 scripts/litellm_configure.py --base http://localhost:4000
