#!/usr/bin/env python3
import json, time, urllib.request, urllib.error, sys

SCHEDULER_URL = "http://localhost:8000"
BRIDGE_URL = "http://localhost:3001"
HERMES_URL = "http://localhost:8082"

SAMPLE_TOOLS = [
    {"type": "function", "function": {"name": "search", "description": "Search the web", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}
]

results = []

def post(url, body, timeout=90):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"status": "error", "error": str(e)}

def test(label, url, body, category):
    print(f"  \u25b6 {label} [{category}]...", end=" ", flush=True)
    start = time.time()
    data = post(url, body)
    elapsed = int((time.time() - start) * 1000)
    status = data.get("status", "unknown")

    if category == "hermes":
        routing = data.get("routing", {})
        path = routing.get("route_path", "?")
        score = routing.get("complexity_score", "?")
        model = routing.get("selected_model", "?")
        skill = routing.get("skill_matched")
        memory = routing.get("memory_match", False)
        llm_enhanced = routing.get("llm_enhanced", False)
        llm_intent = routing.get("llm_intent", "-")
        llm_conf = routing.get("llm_confidence", "-")
        llm_lat = routing.get("llm_latency_ms", "-")
        agent_routed = routing.get("agent_routed", False)
        agent_decision = routing.get("agent_decision", "-")
        agent_conf = routing.get("agent_confidence", "-")
        enh_tag = f", llm={llm_intent}({llm_conf})" if llm_enhanced else ""
        agent_tag = f", agent={agent_decision}({agent_conf})" if agent_routed else ""
        print(f"\u2713 status={status}, path={path}, score={score}, model={model}, skill={skill}, memory={memory}{enh_tag}{agent_tag}, latency={elapsed}ms")
        results.append({"label": label, "category": category, "status": status, "path": path, "score": score, "model": model, "skill": skill, "memory": memory, "latency_ms": elapsed, "llm_enhanced": llm_enhanced, "llm_intent": llm_intent, "llm_confidence": llm_conf, "llm_latency_ms": llm_lat, "agent_routed": agent_routed, "agent_decision": agent_decision, "agent_confidence": agent_conf})
    elif category in ("bridge", "agent"):
        result = data.get("result", {})
        model = result.get("model_name", "?") if result else "?"
        routing_src = data.get("routing_source", "-")
        smart = data.get("smart_routing")
        hermes = data.get("hermes_routing")
        extra = ""
        if smart:
            extra += f", smart={smart['decision']}"
        if hermes:
            extra += f", hermes={hermes['route_path']}"
        print(f"\u2713 status={status}, model={model}, src={routing_src}{extra}, latency={elapsed}ms")
        results.append({"label": label, "category": category, "status": status, "model": model, "latency_ms": elapsed})
    else:
        result = data.get("result", {})
        model = result.get("model_name", "?") if result else "?"
        print(f"\u2713 status={status}, model={model}, latency={elapsed}ms")
        results.append({"label": label, "category": category, "status": status, "model": model, "latency_ms": elapsed})

    time.sleep(0.3)

appid = "batch-test"

print("\n" + "="*70)
print("  OpenClaw Batch Test (12 strategies)")
print("="*70)

print("\n--- Path 1: Python Direct (3) ---")
test("1. auto-chat", f"{SCHEDULER_URL}/dispatch", {"appid": appid, "type": "chat", "prompt": "Introduce artificial intelligence", "priority": 3}, "direct")
test("2. tool-call-direct", f"{SCHEDULER_URL}/dispatch", {"appid": appid, "type": "tool_call", "prompt": "Search AI news with tool", "priority": 3, "tools": SAMPLE_TOOLS, "tool_choice": "auto"}, "direct")
test("3. privacy-local", f"{SCHEDULER_URL}/dispatch", {"appid": appid, "type": "chat", "prompt": "Process sensitive data: user info encryption", "priority": 3, "constraints": {"require_local": True}}, "direct")

print("\n--- Path 2: Bridge Dispatch (5) ---")
test("4. bridge-gateway", f"{BRIDGE_URL}/dispatch", {"appid": appid, "type": "chat", "prompt": "Introduce artificial intelligence", "priority": 3, "route_mode": "gateway"}, "bridge")
test("5. bridge-agent", f"{BRIDGE_URL}/dispatch", {"appid": appid, "type": "chat", "prompt": "Introduce artificial intelligence", "priority": 3, "route_mode": "agent"}, "agent")
test("6. bridge-smart-simple", f"{BRIDGE_URL}/dispatch", {"appid": appid, "type": "chat", "prompt": "1+1=?", "priority": 3, "route_mode": "smart"}, "bridge")
test("7. bridge-smart-complex", f"{BRIDGE_URL}/dispatch", {"appid": appid, "type": "tool_call", "prompt": "Analyze and compare three approaches", "priority": 2, "tools": SAMPLE_TOOLS, "tool_choice": "auto", "route_mode": "smart"}, "bridge")
test("8. bridge-tool-call", f"{BRIDGE_URL}/dispatch", {"appid": appid, "type": "tool_call", "prompt": "Check Beijing weather with tool", "priority": 3, "tools": SAMPLE_TOOLS, "tool_choice": "auto", "route_mode": "gateway"}, "bridge")

print("\n--- Path 3: Hermes Intelligent Routing (4) ---")
test("9. hermes-simple", f"{HERMES_URL}/route", {"appid": appid, "type": "chat", "prompt": "1+1=?", "priority": 3}, "hermes")
test("10. hermes-moderate", f"{HERMES_URL}/route", {"appid": appid, "type": "chat", "prompt": "Analyze AI development trends and future prospects", "priority": 3}, "hermes")
test("11. hermes-complex", f"{HERMES_URL}/route", {"appid": appid, "type": "tool_call", "prompt": "Analyze and compare three approaches, design a multi-step autonomous execution plan to optimize system performance", "priority": 2, "tools": SAMPLE_TOOLS, "tool_choice": "auto"}, "hermes")
test("12. hermes-privacy", f"{HERMES_URL}/route", {"appid": appid, "type": "chat", "prompt": "Process sensitive data: user info encryption", "priority": 3, "constraints": {"require_local": True}}, "hermes")

print("\n" + "="*70)
print("  Batch Test Summary")
print("="*70)

success_count = sum(1 for r in results if r["status"] == "success")
fail_count = sum(1 for r in results if r["status"] != "success")
print(f"\n  Total: {len(results)} requests, Success: {success_count}, Failed: {fail_count}")

print("\n  By Category:")
for cat in ["direct", "bridge", "agent", "hermes"]:
    cat_results = [r for r in results if r["category"] == cat]
    cat_ok = sum(1 for r in cat_results if r["status"] == "success")
    print(f"    {cat}: {len(cat_results)} total, {cat_ok} success")

print("\n  Hermes Routing Decisions:")
hermes_results = [r for r in results if r["category"] == "hermes"]
for r in hermes_results:
    llm_tag = f", llm={r.get('llm_intent','?')}({r.get('llm_confidence','?')})" if r.get("llm_enhanced") else ""
    agent_tag = f", agent={r.get('agent_decision','?')}({r.get('agent_confidence','?')})" if r.get("agent_routed") else ""
    print(f"    {r['label']}: path={r.get('path','?')}, score={r.get('score','?')}, model={r.get('model','?')}, skill={r.get('skill')}, memory={r.get('memory')}{llm_tag}{agent_tag}, latency={r['latency_ms']}ms")

llm_enhanced_count = sum(1 for r in hermes_results if r.get("llm_enhanced"))
agent_routed_count = sum(1 for r in hermes_results if r.get("agent_routed"))
if llm_enhanced_count > 0:
    llm_latencies = [r.get("llm_latency_ms", 0) for r in hermes_results if r.get("llm_enhanced") and r.get("llm_latency_ms") != "-"]
    avg_llm_lat = sum(llm_latencies) / len(llm_latencies) if llm_latencies else 0
    print(f"\n  LLM Enhancement: {llm_enhanced_count}/{len(hermes_results)} requests enhanced, avg LLM latency={avg_llm_lat:.0f}ms")
if agent_routed_count > 0:
    print(f"  Agent Routing: {agent_routed_count}/{len(hermes_results)} requests agent-routed")

print("\n  Latency Stats:")
for cat in ["direct", "bridge", "agent", "hermes"]:
    cat_results = [r for r in results if r["category"] == cat and r["status"] == "success"]
    if cat_results:
        avg_lat = sum(r["latency_ms"] for r in cat_results) / len(cat_results)
        min_lat = min(r["latency_ms"] for r in cat_results)
        max_lat = max(r["latency_ms"] for r in cat_results)
        print(f"    {cat}: avg={avg_lat:.0f}ms, min={min_lat}ms, max={max_lat}ms")

print()
