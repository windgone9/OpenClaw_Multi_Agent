#!/usr/bin/env python3
"""Comprehensive test suite for OpenClaw Multi-Agent project."""
import json, time, httpx, sys

BASE = "http://localhost:8082"
BRIDGE = "http://localhost:3001"
GATEWAY = "http://localhost:3000"
OFFICIAL_GW = "http://localhost:3005"
OLLAMA = "http://localhost:11434"

results = []
def record(name, ok, detail=""):
    results.append({"test": name, "pass": ok, "detail": detail})
    icon = "PASS" if ok else "FAIL"
    print(f"  [{icon}] {name}: {detail}")

# ── Test 1: direct_local (simple chat) ──
print("\n=== Test 1: direct_local (simple chat) ===")
try:
    r = httpx.post(f"{BASE}/queue/submit-sync?timeout=30", json={
        "appid": "test", "type": "chat", "prompt": "你好", "priority": 3
    }, timeout=35)
    d = r.json()
    ok = d["status"] == "success" and d["routing"]["route_path"] == "direct_local"
    record("direct_local routing", ok, f'route={d["routing"]["route_path"]} score={d["routing"]["complexity_score"]}')
    s = d.get("result") or {}
    record("direct_local model", s.get("provider") == "ollama", f'provider={s.get("provider")} model={s.get("model_name")}')
    record("direct_local via_gateway", s.get("routed_via_gateway") == True, f'via_gw={s.get("routed_via_gateway")}')
    record("direct_local has_output", bool(s.get("output")), f'output_len={len(s.get("output",""))}')
    record("direct_local latency", d.get("total_latency_ms",0) < 10000, f'latency={d.get("total_latency_ms")}ms')
except Exception as e:
    record("direct_local", False, str(e))

# ── Test 2: direct_local (English greeting) ──
print("\n=== Test 2: direct_local (English greeting) ===")
try:
    r = httpx.post(f"{BASE}/queue/submit-sync?timeout=30", json={
        "appid": "test", "type": "chat", "prompt": "hello, how are you?", "priority": 3
    }, timeout=35)
    d = r.json()
    ok = d["status"] == "success" and d["routing"]["route_path"] == "direct_local"
    record("direct_local_en routing", ok, f'route={d["routing"]["route_path"]} score={d["routing"]["complexity_score"]}')
except Exception as e:
    record("direct_local_en", False, str(e))

# ── Test 3: agent_chain (code generation) ──
print("\n=== Test 3: agent_chain (code generation, async) ===")
try:
    r = httpx.post(f"{BASE}/queue/submit", json={
        "appid": "test", "type": "chat", "prompt": "Write a Python data pipeline with Volcano GPU scheduling", "priority": 1
    }, timeout=10)
    d = r.json()
    req_id = d["request_id"]
    record("agent_chain submit", bool(req_id), f'request_id={req_id}')
    
    # Poll for result
    for i in range(30):
        time.sleep(5)
        r2 = httpx.get(f"{BASE}/queue/results/{req_id}", timeout=5)
        d2 = r2.json()
        if d2["status"] in ("success", "failed"):
            break
    
    ok = d2["status"] == "success" and d2["routing"]["route_path"] == "agent_chain"
    record("agent_chain routing", ok, f'route={d2["routing"]["route_path"]} score={d2["routing"]["complexity_score"]}')
    s = d2.get("result") or {}
    record("agent_chain openclaw", s.get("openclaw_official") == True, f'openclaw={s.get("openclaw_official")} model={s.get("model_name")}')
    record("agent_chain has_output", bool(s.get("output")), f'output_len={len(s.get("output",""))}')
    record("agent_chain latency", d2.get("total_latency_ms",0) < 120000, f'latency={d2.get("total_latency_ms")}ms')
    record("agent_chain no_fallback", s.get("fallback_used") is None, f'fallback={s.get("fallback_used")}')
except Exception as e:
    record("agent_chain", False, str(e))

# ── Test 4: agent_chain (Chinese code request) ──
print("\n=== Test 4: agent_chain (Chinese code request, async) ===")
try:
    r = httpx.post(f"{BASE}/queue/submit", json={
        "appid": "test", "type": "chat", "prompt": "请写一个Python代码实现快速排序算法", "priority": 1
    }, timeout=10)
    d = r.json()
    req_id = d["request_id"]
    record("agent_chain_cn submit", bool(req_id), f'request_id={req_id}')
    
    for i in range(30):
        time.sleep(5)
        r2 = httpx.get(f"{BASE}/queue/results/{req_id}", timeout=5)
        d2 = r2.json()
        if d2["status"] in ("success", "failed"):
            break
    
    ok = d2["status"] == "success" and d2["routing"]["route_path"] == "agent_chain"
    record("agent_chain_cn routing", ok, f'route={d2["routing"]["route_path"]} score={d2["routing"]["complexity_score"]}')
    s = d2.get("result") or {}
    record("agent_chain_cn has_output", bool(s.get("output")), f'output_len={len(s.get("output",""))}')
except Exception as e:
    record("agent_chain_cn", False, str(e))

# ── Test 5: Bridge direct dispatch ──
print("\n=== Test 5: Bridge direct dispatch ===")
try:
    r = httpx.post(f"{BRIDGE}/dispatch", json={
        "appid": "test", "type": "chat", "prompt": "hello",
        "route_mode": "gateway", "constraints": {"require_local": True}
    }, timeout=30)
    d = r.json()
    ok = d["status"] == "success"
    record("bridge gateway dispatch", ok, f'status={d["status"]}')
    s = d.get("result") or {}
    record("bridge gateway model", s.get("provider") == "ollama", f'provider={s.get("provider")}')
except Exception as e:
    record("bridge gateway dispatch", False, str(e))

# ── Test 6: Bridge agent dispatch ──
print("\n=== Test 6: Bridge agent dispatch (direct) ===")
try:
    r = httpx.post(f"{BRIDGE}/dispatch", json={
        "appid": "test", "type": "chat", "prompt": "Write a sorting algorithm",
        "route_mode": "agent"
    }, timeout=120)
    d = r.json()
    ok = d["status"] == "success"
    record("bridge agent dispatch", ok, f'status={d["status"]} strategy={d.get("strategy_name")}')
    s = d.get("result") or {}
    record("bridge agent openclaw", s.get("openclaw_official") == True, f'openclaw={s.get("openclaw_official")}')
except Exception as e:
    record("bridge agent dispatch", False, str(e))

# ── Test 7: Gateway local model ──
print("\n=== Test 7: Gateway local model ===")
try:
    r = httpx.post(f"{GATEWAY}/v1/chat/completions", json={
        "model": "qwen2.5:3b", "messages": [{"role": "user", "content": "hi"}], "stream": False
    }, timeout=30)
    d = r.json()
    ok = bool(d.get("choices"))
    record("gateway local model", ok, f'model={d.get("model")} choices={len(d.get("choices",[]))}')
except Exception as e:
    record("gateway local model", False, str(e))

# ── Test 8: Official GW models list ──
print("\n=== Test 8: Official GW ===")
try:
    r = httpx.get(f"{OFFICIAL_GW}/v1/models", timeout=10)
    d = r.json()
    models = [m["id"] for m in d.get("data", [])]
    record("official gw models", len(models) >= 3, f'models={models}')
except Exception as e:
    record("official gw models", False, str(e))

try:
    r = httpx.post(f"{OFFICIAL_GW}/v1/chat/completions", json={
        "model": "openclaw/default", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50
    }, timeout=120)
    d = r.json()
    ok = bool(d.get("choices"))
    record("official gw inference", ok, f'model={d.get("model")} finish={d.get("choices",[{}])[0].get("finish_reason")}')
except Exception as e:
    record("official gw inference", False, str(e))

# ── Test 9: Hermes API endpoints ──
print("\n=== Test 9: Hermes API endpoints ===")
try:
    r = httpx.get(f"{BASE}/health", timeout=5)
    d = r.json()
    record("hermes health", d.get("status") == "healthy", f'status={d.get("status")}')
except Exception as e:
    record("hermes health", False, str(e))

try:
    r = httpx.get(f"{BASE}/stats", timeout=5)
    d = r.json()
    req = d.get("requests", {})
    record("hermes stats", "total" in req, f'total={req.get("total")} success={req.get("success")} failed={req.get("failed")}')
except Exception as e:
    record("hermes stats", False, str(e))

try:
    r = httpx.get(f"{BASE}/queue/status", timeout=5)
    d = r.json()
    record("hermes queue status", "worker" in d or "queue_sizes" in d, f'keys={list(d.keys())[:5]}')
except Exception as e:
    record("hermes queue status", False, str(e))

try:
    r = httpx.get(f"{BASE}/official-agent", timeout=10)
    d = r.json()
    record("hermes official-agent", "available" in d, f'available={d.get("available")} mode={d.get("mode")}')
except Exception as e:
    record("hermes official-agent", False, str(e))

# ── Test 10: Bridge API endpoints ──
print("\n=== Test 10: Bridge API endpoints ===")
try:
    r = httpx.get(f"{BRIDGE}/health", timeout=5)
    d = r.json()
    record("bridge health", d.get("status") == "healthy", f'status={d.get("status")} official_gw={d.get("official_gateway_reachable")}')
except Exception as e:
    record("bridge health", False, str(e))

try:
    r = httpx.get(f"{BRIDGE}/models", timeout=5)
    d = r.json()
    model_count = len(d.get("models", []))
    record("bridge models", model_count > 0, f'count={model_count}')
except Exception as e:
    record("bridge models", False, str(e))

try:
    r = httpx.get(f"{BRIDGE}/stats", timeout=5)
    d = r.json()
    record("bridge stats", "total_requests" in d, f'total={d.get("total_requests")} success={d.get("success_requests")}')
except Exception as e:
    record("bridge stats", False, str(e))

# ── Test 11: Error handling ──
print("\n=== Test 11: Error handling ===")
try:
    r = httpx.get(f"{BASE}/queue/results/nonexistent-id", timeout=5)
    d = r.json()
    record("missing result handling", d.get("status") == "not_found", f'status={d.get("status")}')
except Exception as e:
    record("missing result handling", False, str(e))

try:
    r = httpx.post(f"{BRIDGE}/dispatch", json={
        "appid": "test", "type": "chat", "prompt": "", "route_mode": "gateway"
    }, timeout=30)
    d = r.json()
    # Empty prompt should still work (Gateway handles it)
    record("empty prompt handling", d.get("status") in ("success", "failed"), f'status={d.get("status")}')
except Exception as e:
    record("empty prompt handling", False, str(e))

# ── Test 12: Dashboard / Console ──
print("\n=== Test 12: Dashboard / Console ===")
try:
    r = httpx.get("http://localhost:3000/console", timeout=5, follow_redirects=True)
    record("gateway console", r.status_code == 200, f'status={r.status_code}')
except Exception as e:
    record("gateway console", False, f'Not accessible: {e}')

try:
    r = httpx.get("http://localhost:3005/health", timeout=5)
    d = r.json()
    record("official gw health", d.get("ok") == True, f'ok={d.get("ok")}')
except Exception as e:
    record("official gw health", False, f'Not accessible: {e}')

# ── Summary ──
print("\n" + "=" * 60)
passed = sum(1 for r in results if r["pass"])
failed = sum(1 for r in results if not r["pass"])
total = len(results)
print(f"TOTAL: {total} | PASS: {passed} | FAIL: {failed}")
if failed > 0:
    print("\nFailed tests:")
    for r in results:
        if not r["pass"]:
            print(f"  - {r['test']}: {r['detail']}")
