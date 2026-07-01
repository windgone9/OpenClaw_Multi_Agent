#!/usr/bin/env bash
# 测 chat 端到端 (经 nginx → proxy → litellm → deepseek-r1)
U="${URL:-http://localhost:30080/v1/chat/completions}"
cat > /tmp/oc-chat.json <<'EOF'
{"model":"qwen2.5","messages":[{"role":"user","content":"你好,一句话自我介绍"}]}
EOF
echo "POST $U"
curl -s "$U" -H 'Content-Type: application/json' -d @/tmp/oc-chat.json -o /tmp/oc-chat.resp
echo "--- response (前 500 字节) ---"
head -c 500 /tmp/oc-chat.resp
echo
