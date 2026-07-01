#!/usr/bin/env bash
# ============================================
# register-models-incluster.sh — 在 proxy pod 内注册模型 (绕开 port-forward)
# ============================================
# 节点缺 socat → kubectl port-forward 不可用。改用 kubectl exec 进 proxy pod,
# 经 14-bridge (litellm:4000 → lite-helm-litellm.default) 直达现有 litellm 注册模型。
#
# 用法 (需先 export 两个 key):
#   export VLLM_API_KEY=gpustack_xxx
#   export LITELLM_MASTER_KEY=sk-1234
#   bash scripts/register-models-incluster.sh
# ============================================
set -e
cd "$(dirname "$0")/.."

: "${VLLM_API_KEY:?需 export VLLM_API_KEY (GPUStack key)}"
: "${LITELLM_MASTER_KEY:?需 export LITELLM_MASTER_KEY (现有 litellm master key)}"

# 确保 STORE_MODEL_IN_DB 已开 (litellm 须开此才能 /model/new)
echo "[1/2] 确保 litellm 开 STORE_MODEL_IN_DB=True..."
kubectl -n default set env deploy/lite-helm-litellm STORE_MODEL_IN_DB=True
kubectl -n default rollout status deploy/lite-helm-litellm --timeout=180s

echo "[2/2] 拷脚本进 proxy pod + 注册模型 (经 litellm:4000 桥接)..."
kubectl -n openclaw cp scripts/litellm_configure.py deploy/proxy:/tmp/lc.py
# env 内联传 key ($var 在本地展开), proxy pod 经 litellm:4000 (14-bridge) 直达现有 litellm
kubectl -n openclaw exec deploy/proxy -- sh -c "VLLM_API_KEY=${VLLM_API_KEY} LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY} python3 /tmp/lc.py --base http://litellm:4000"
