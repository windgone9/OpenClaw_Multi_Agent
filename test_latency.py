#!/usr/bin/env python3
"""Latency breakdown analysis for agent_chain path."""
import time, httpx, json

print("=== Latency Breakdown Analysis ===\n")

# 1. Ollama routing decision
t0 = time.time()
payload = {
    "model": "qwen2.5:3b",
    "messages": [
        {"role": "system", "content": "You are a router. Output JSON: {route_path, complexity_score, reason}"},
        {"role": "user", "content": "判断路由:\n内容: Write a Python data pipeline with Volcano GPU scheduling\n类型: chat\n优先级: 1"},
    ],
    "stream": False,
    "options": {"num_ctx": 2048, "temperature": 0.0, "num_predict": 128},
    "format": "json",
}
with httpx.Client(timeout=30) as c:
    r = c.post("http://localhost:11434/v1/chat/completions", json=payload)
    r.raise_for_status()
t1 = time.time()
routing_ms = int((t1 - t0) * 1000)
print(f"Step 1 - Ollama routing decision: {routing_ms}ms")

# 2. Bridge HTTP overhead
t2 = time.time()
with httpx.Client(timeout=5) as c:
    r = c.get("http://localhost:3001/health")
t3 = time.time()
bridge_overhead_ms = int((t3 - t2) * 1000)
print(f"Step 2 - Bridge HTTP overhead: {bridge_overhead_ms}ms")

# 3. Official OpenClaw GW inference (short prompt)
t4 = time.time()
payload2 = {
    "model": "openclaw/default",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 50,
}
with httpx.Client(timeout=120) as c:
    r = c.post("http://localhost:3005/v1/chat/completions", json=payload2)
t5 = time.time()
official_gw_ms = int((t5 - t4) * 1000)
print(f"Step 3 - Official GW inference (short prompt, max_tokens=50): {official_gw_ms}ms")

# 4. Ollama direct (same short prompt, for comparison)
t6 = time.time()
payload3 = {
    "model": "qwen2.5:3b",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": False,
}
with httpx.Client(timeout=30) as c:
    r = c.post("http://localhost:11434/v1/chat/completions", json=payload3)
    r.raise_for_status()
t7 = time.time()
ollama_direct_ms = int((t7 - t6) * 1000)
print(f"Step 4 - Ollama direct (same short prompt): {ollama_direct_ms}ms")

# 5. Official GW with longer prompt (simulate agent_chain)
t8 = time.time()
payload4 = {
    "model": "openclaw/default",
    "messages": [{"role": "user", "content": "Write a Python data pipeline with Volcano GPU scheduling"}],
    "max_tokens": 1024,
}
with httpx.Client(timeout=180) as c:
    r = c.post("http://localhost:3005/v1/chat/completions", json=payload4)
t9 = time.time()
official_gw_long_ms = int((t9 - t8) * 1000)
print(f"Step 5 - Official GW inference (long prompt, max_tokens=1024): {official_gw_long_ms}ms")

# 6. Ollama direct with same long prompt
t10 = time.time()
payload5 = {
    "model": "qwen2.5:3b",
    "messages": [{"role": "user", "content": "Write a Python data pipeline with Volcano GPU scheduling"}],
    "stream": False,
}
with httpx.Client(timeout=120) as c:
    r = c.post("http://localhost:11434/v1/chat/completions", json=payload5)
    r.raise_for_status()
t11 = time.time()
ollama_long_ms = int((t11 - t10) * 1000)
print(f"Step 6 - Ollama direct (long prompt): {ollama_long_ms}ms")

print("\n=== Summary ===")
print(f"Hermes routing:          {routing_ms:>6}ms")
print(f"Bridge overhead:         {bridge_overhead_ms:>6}ms")
print(f"Official GW (short):     {official_gw_ms:>6}ms")
print(f"Official GW (long):      {official_gw_long_ms:>6}ms")
print(f"Ollama direct (short):   {ollama_direct_ms:>6}ms")
print(f"Ollama direct (long):    {ollama_long_ms:>6}ms")
print(f"\nOfficial GW overhead vs Ollama (short): {official_gw_ms - ollama_direct_ms}ms")
print(f"Official GW overhead vs Ollama (long):  {official_gw_long_ms - ollama_long_ms}ms")
print(f"\nExpected agent_chain total: ~{routing_ms + bridge_overhead_ms + official_gw_long_ms}ms")
print(f"Actual agent_chain total: 117979ms")
