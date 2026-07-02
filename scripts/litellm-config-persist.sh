#!/usr/bin/env bash
# ============================================
# litellm-config-persist.sh — 把模型路由写进 litellm config.yaml (一劳永逸, 重启不丢)
# ============================================
# 根因: postgres 无持久卷, 重启丢 DB → DB 里的模型注册(/model/new)全没。
# 本脚本把模型路由写进 litellm 的 config.yaml (ConfigMap, 存 etcd, 重启不丢),
# litellm 启动时从 config 加载模型, 不依赖 DB。同时给 litellm 加 VLLM_API_KEY env。
#
# 用法 (在测试服务器上, 需先 export VLLM_API_KEY):
#   export VLLM_API_KEY=gpustack_xxx
#   bash scripts/litellm-config-persist.sh
# ============================================
set -e
cd "$(dirname "$0")/.."

: "${VLLM_API_KEY:?需 export VLLM_API_KEY (GPUStack key)}"
NS="${LITELLM_NS:-default}"
CM="${LITELLM_CM:-lite-helm-litellm-config}"
DEPLOY="${LITELLM_DEPLOY:-lite-helm-litellm}"

echo "[1/4] 生成含模型路由的 config.yaml..."
cat > /tmp/litellm-config-persist.yaml <<'YAMLEOF'
general_settings:
  master_key: sk-1234
model_list:
# ---------- 原有 (helm 默认) ----------
- litellm_params:
    api_key: eXaMpLeOnLy
    model: gpt-3.5-turbo
  model_name: gpt-3.5-turbo
- litellm_params:
    api_base: https://exampleopenaiendpoint-production.up.railway.app/
    api_key: fake-key
    model: openai/fake
  model_name: fake-openai-endpoint
# ---------- 文本模型 (GPUStack deepseek-r1) ----------
- litellm_params:
    model: openai/deepseek-r1-distill-qwen-32b
    api_base: http://192.168.0.151/v1
    api_key: os.environ/VLLM_API_KEY
    timeout: 300
  model_name: deepseek-r1-distill-qwen-32b
- litellm_params:
    model: openai/deepseek-r1-distill-qwen-32b
    api_base: http://192.168.0.151/v1
    api_key: os.environ/VLLM_API_KEY
    timeout: 300
  model_name: qwen2.5
# ---------- 视觉模型 (GPUStack qwen3-vl) ----------
- litellm_params:
    model: openai/qwen3-vl-32b-instruct
    api_base: http://192.168.0.151/v1
    api_key: os.environ/VLLM_API_KEY
    timeout: 300
  model_name: qwen3-vl-32b-instruct
- litellm_params:
    model: openai/qwen3-vl-32b-instruct
    api_base: http://192.168.0.151/v1
    api_key: os.environ/VLLM_API_KEY
    timeout: 300
  model_name: llava
# ---------- 语音识别 (本地 FunASR) ----------
- litellm_params:
    model: openai/whisper-1
    api_base: http://funasr:8199/v1
    timeout: 60
  model_name: funasr
YAMLEOF
echo "    config.yaml 已生成 (/tmp/litellm-config-persist.yaml)"

echo "[2/4] apply configmap ${CM} (替换, 存 etcd, 重启不丢)..."
kubectl -n "$NS" create cm "$CM" --from-file=config.yaml=/tmp/litellm-config-persist.yaml --dry-run=client -o yaml | kubectl apply -f -

echo "[3/4] 给 litellm 加 VLLM_API_KEY env (config 里 os.environ/VLLM_API_KEY 用)..."
kubectl -n "$NS" set env deploy/"$DEPLOY" VLLM_API_KEY="$VLLM_API_KEY"

echo "[4/4] 重启 litellm (从新 config 加载模型)..."
kubectl -n "$NS" rollout restart deploy/"$DEPLOY"
kubectl -n "$NS" rollout status deploy/"$DEPLOY" --timeout=180s

echo ""
echo "✓ 完成: 模型路由已写入 litellm config.yaml (ConfigMap), 重启不丢。"
echo "  litellm 启动时从 config 加载模型, 不依赖 DB/postgres。"
echo "  验证: kubectl -n $NS get cm $CM -o jsonpath='{.data.config\\.yaml}'"
echo "  模型列表: curl -s http://192.168.0.151:30080/model/info"
