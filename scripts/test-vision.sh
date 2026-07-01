#!/usr/bin/env bash
# 测 vision 端到端 (经 nginx → proxy → litellm → qwen3-vl, 图片走 minio)
U="${URL:-http://localhost:30080/v1/chat/completions}"
cat > /tmp/oc-vision.json <<'EOF'
{"model":"qwen2.5","messages":[{"role":"user","content":[{"type":"text","text":"图里有什么?中文简短"},{"type":"image_url","image_url":{"url":"http://minio:9000/openclaw-test/test_image.png"}}]}]}
EOF
echo "POST $U"
curl -s "$U" -H 'Content-Type: application/json' -d @/tmp/oc-vision.json -o /tmp/oc-vision.resp
echo "--- response (前 500 字节) ---"
head -c 500 /tmp/oc-vision.resp
echo
