#!/usr/bin/env bash
# ============================================
# ramp-litellm-only.sh — litellm-only 并发压测 (隔离 proxy vs litellm 开销)
# ============================================
# 经 kubectl exec 进 proxy pod, 直打 litellm:4000 (经 14-bridge),
# 绕过 proxy 自身 + nginx, 保留 litellm → vLLM。对比:
#   直连 vLLM (--direct, GPUStack) = vLLM 自身
#   litellm-only (本脚本)          = litellm + vLLM
#   全链路 (KIND_BASE=:30080)       = proxy + litellm + vLLM
# 用法 (在测试服务器上): bash scripts/ramp-litellm-only.sh
# ============================================
set -e
cd "$(dirname "$0")/.."

echo "=== litellm-only ramp (经 proxy pod 直打 litellm:4000) ==="
kubectl -n openclaw exec -i deploy/proxy -- sh -c \
  'DIRECT_BASE=http://litellm:4000 VLLM_API_KEY=sk-1234 python3 - --direct --ramp --levels 10,20,40 --ramp-mt 50' \
  < scripts/vllm_perf.py
