#!/usr/bin/env bash
# ============================================
# post-reboot-check.sh — 服务器重启后自动检查+恢复
# ============================================
# 覆盖重启后的 4 个常见问题:
#   1. pod 网络命名空间 stale (Calico 恢复慢) → 删 CrashLoopBackOff pod 重建
#   2. minio 数据丢失 (hostPath PV 跨节点) → 检查+重新灌入
#   3. litellm 模型 (已写 config, 检查是否加载) → 报警
#   4. GPUStack vLLM 没自启 → 报警 (需 GPUStack UI 手动拉起)
#
# 用法: bash scripts/post-reboot-check.sh
# ============================================
set -e
cd "$(dirname "$0")/.."
NS="${NAMESPACE:-openclaw}"
GPUSTACK_KEY="${VLLM_API_KEY:-gpustack_d99986c2981d21f6_993b438a4f4cd13aaa8adde5601f07e4}"

G() { printf '\033[32m%s\033[0m\n' "$1"; }
Y() { printf '\033[33m%s\033[0m\n' "$1"; }
R() { printf '\033[31m%s\033[0m\n' "$1"; }
B() { printf '\033[36m%s\033[0m\n' "$1"; }

echo "===== 1. 检查 + 重建 stale pods ====="
for app in nginx proxy stream-service funasr hermes minio; do
  STATUS=$(kubectl -n "$NS" get pod -l app="$app" -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "NotFound")
  READY=$(kubectl -n "$NS" get pod -l app="$app" -o jsonpath='{.items[0].status.containerStatuses[0].ready}' 2>/dev/null || echo "false")
  if [[ "$STATUS" != "Running" ]] || [[ "$READY" != "true" ]]; then
    Y "  $app: $STATUS (ready=$READY) → 删重建"
    kubectl -n "$NS" delete pod -l app="$app" 2>/dev/null || true
  else
    G "  $app: Running ✓"
  fi
done
sleep 15

echo ""
echo "===== 2. 检查 GPUStack vLLM ====="
if curl -s -m 10 http://192.168.0.151/v1/models -H "Authorization: Bearer $GPUSTACK_KEY" 2>/dev/null | grep -q "deepseek-r1"; then
  G "  GPUStack 模型在线 ✓"
else
  R "  ✗ GPUStack 模型未运行! 请在 GPUStack UI (http://192.168.0.151/) 拉起模型实例"
  R "    (deepseek-r1-distill-qwen-32b + qwen3-vl-32b-instruct, 各 2×H100 tp2)"
fi

echo ""
echo "===== 3. 检查 litellm 模型 (从 config 加载, 重启不丢) ====="
if curl -s -m 10 http://192.168.0.151:30080/model/info 2>/dev/null | grep -q "deepseek-r1"; then
  G "  litellm 模型就绪 ✓"
else
  R "  ✗ litellm 模型未加载! 检查: kubectl -n default get cm lite-helm-litellm-config -o jsonpath='{.data.config\\.yaml}'"
fi

echo ""
echo "===== 4. 检查 minio 数据 (灌入如果缺失) ====="
MI=$(kubectl -n "$NS" get svc minio -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
if [[ -z "$MI" ]]; then
  R "  ✗ minio Service 未找到"
else
  # 检查 minio 是否可达
  if ! curl -s -m 5 -o /dev/null "http://$MI:9000/minio/health/live" 2>/dev/null; then
    Y "  minio 不可达 (ClusterIP $MI) → 删 pod 重建..."
    kubectl -n "$NS" delete pod -l app=minio 2>/dev/null || true
    sleep 15
    MI=$(kubectl -n "$NS" get svc minio -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
  fi
  # 检查 bucket/文件
  if curl -s -m 5 -o /dev/null -w "%{http_code}" "http://$MI:9000/openclaw-test/test_image.png" 2>/dev/null | grep -q "200"; then
    G "  minio 数据就绪 ✓ (test_image.png 存在)"
  else
    Y "  minio bucket/文件缺失 → 重新灌入..."
    python3 -c "import boto3" 2>/dev/null || pip3 install --user boto3 2>/dev/null
    MINIO_ENDPOINT="http://$MI:9000" python3 scripts/minio_setup.py 2>&1 | sed 's/^/    /' || R "  灌入失败, 手动: MINIO_ENDPOINT=http://$MI:9000 python3 scripts/minio_setup.py"
  fi
fi

echo ""
echo "===== 5. 快速验证 chat ====="
RESP=$(curl -s -m 30 http://192.168.0.151:30080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-r1-distill-qwen-32b","messages":[{"role":"user","content":"hi"}],"max_tokens":10}' 2>&1)
if echo "$RESP" | grep -q "choices"; then
  G "  chat 正常 ✓"
else
  R "  ✗ chat 异常: $(echo "$RESP" | head -c 150)"
fi

echo ""
echo "===== 6. pod 状态总览 ====="
kubectl get pods -n "$NS" -o wide 2>/dev/null
echo ""
kubectl -n default get pods 2>/dev/null | grep -E "litellm|postgres" || true

echo ""
B "恢复检查完成。"
B "如全部 ✓, 可跑 E2E:"
B "  DASHBOARD_URL=http://192.168.0.151:30080/new_dashboard.html \\"
B "    ~/MyWork/Multi-Agent/venv/bin/python tests/e2e_full_playwright.py"
