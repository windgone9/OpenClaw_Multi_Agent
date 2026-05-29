#!/usr/bin/env python3
"""Tool Call end-to-end test script for OpenClaw scheduling system.

Tests 6 scenarios:
1. Python direct + tools -> routed to supports_tools=true model (kimi-k2.6)
2. Bridge + tools -> Bridge routes to tool-capable model
3. Bridge + tool_call WITHOUT tools -> degrades to normal chat via Bridge
4. Python direct + tool_call WITHOUT tools -> degrades to normal chat
5. Bridge + normal chat -> Bridge handles regular chat
6. Normal chat (regression test)

Usage:
    ./venv/bin/python test_tool_call.py
"""

import asyncio
import json
import time
import httpx

API_BASE = "http://localhost:8000"
BRIDGE_BASE = "http://localhost:3001"

SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name"},
                    "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}
                },
                "required": ["location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web for information",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "num_results": {"type": "integer", "description": "Number of results"}
                },
                "required": ["query"]
            }
        }
    }
]


async def test_python_direct_with_tools(client: httpx.AsyncClient):
    print("\n" + "=" * 60)
    print("TEST 1: Python Direct + tools -> supports_tools model")
    print("=" * 60)

    payload = {
        "appid": "toolcall-test",
        "type": "tool_call",
        "prompt": "今天北京天气怎么样？请调用天气查询工具。",
        "priority": 3,
        "tools": SAMPLE_TOOLS,
        "tool_choice": "auto",
    }

    start = time.time()
    resp = await client.post(f"{API_BASE}/dispatch", json=payload, timeout=60)
    elapsed = int((time.time() - start) * 1000)
    data = resp.json()

    status = data.get("status", "unknown")
    result = data.get("result", {})
    model_name = result.get("model_name", "?")
    output = result.get("output", "")
    finish_reason = result.get("finish_reason", "?")
    routed_via_gateway = result.get("routed_via_gateway", False)

    print(f"  Status: {status}")
    print(f"  Model: {model_name}")
    print(f"  Gateway: {routed_via_gateway}")
    print(f"  Finish Reason: {finish_reason}")
    print(f"  Latency: {result.get('latency_ms', '?')}ms (total: {elapsed}ms)")
    print(f"  Output: {output[:200]}")

    has_tool_call = finish_reason == "tool_calls" or "[Tool Call]" in output
    supports_tools_model = "kimi" in model_name.lower()

    if has_tool_call:
        print("  ✅ Model returned tool_calls response")
    elif supports_tools_model:
        print("  ⚠️  Routed to tool-capable model but no tool_calls in response")
    else:
        print("  ⚠️  Not routed to tool-capable model")

    return {
        "test": "python_direct_with_tools",
        "status": status,
        "model": model_name,
        "finish_reason": finish_reason,
        "has_tool_call": has_tool_call,
        "supports_tools_model": supports_tools_model,
    }


async def test_bridge_with_tools(client: httpx.AsyncClient):
    print("\n" + "=" * 60)
    print("TEST 2: Bridge + tools -> tool-capable model")
    print("=" * 60)

    gateway_available = False
    try:
        gw_resp = await client.get("http://localhost:3000/health", timeout=3)
        gateway_available = gw_resp.status_code == 200
    except Exception:
        pass

    if gateway_available:
        print("  Gateway is available, testing Bridge→Gateway path")
    else:
        print("  Gateway not available, testing Bridge direct path (USE_OPENCLAW_GATEWAY=false)")

    payload = {
        "appid": "toolcall-test",
        "type": "tool_call",
        "prompt": "请搜索最新的AI新闻",
        "priority": 3,
        "tools": SAMPLE_TOOLS,
        "tool_choice": "auto",
    }

    start = time.time()
    try:
        resp = await client.post(f"{BRIDGE_BASE}/dispatch", json=payload, timeout=120)
        elapsed = int((time.time() - start) * 1000)
        data = resp.json()

        status = data.get("status", "unknown")
        result = data.get("result", {})
        model_name = result.get("model_name", "?")
        output = result.get("output", "")
        finish_reason = result.get("finish_reason", "?")
        routed_via_gateway = result.get("routed_via_gateway", False)
        strategy = data.get("strategy_name", "?")

        print(f"  Status: {status}")
        print(f"  Model: {model_name}")
        print(f"  Strategy: {strategy}")
        print(f"  Gateway: {routed_via_gateway}")
        print(f"  Finish Reason: {finish_reason}")
        print(f"  Latency: {result.get('latency_ms', '?')}ms (total: {elapsed}ms)")
        print(f"  Output: {output[:200]}")

        has_tool_call = finish_reason == "tool_calls" or "[Tool Call]" in output

        if status == "success" and has_tool_call:
            print("  ✅ Bridge returned tool_calls response")
        elif status == "success":
            print("  ⚠️  Bridge succeeded but no tool_calls in response")
        else:
            print(f"  ⚠️  Bridge failed (status={status}), likely Gateway unavailable")

        return {
            "test": "bridge_with_tools",
            "status": status,
            "model": model_name,
            "finish_reason": finish_reason,
            "has_tool_call": has_tool_call,
            "routed_via_gateway": routed_via_gateway,
            "gateway_available": gateway_available,
        }
    except Exception as e:
        print(f"  ❌ Bridge request failed: {e}")
        return {"test": "bridge_with_tools", "status": "error", "error": str(e), "gateway_available": gateway_available}


async def test_bridge_tool_call_without_tools(client: httpx.AsyncClient):
    print("\n" + "=" * 60)
    print("TEST 3: Bridge + tool_call WITHOUT tools -> degrades to normal chat")
    print("=" * 60)

    payload = {
        "appid": "toolcall-test",
        "type": "tool_call",
        "prompt": "请帮我计算 2+2 等于多少",
        "priority": 3,
    }

    start = time.time()
    try:
        resp = await client.post(f"{BRIDGE_BASE}/dispatch", json=payload, timeout=120)
        elapsed = int((time.time() - start) * 1000)
        data = resp.json()

        status = data.get("status", "unknown")
        result = data.get("result", {})
        model_name = result.get("model_name", "?")
        output = result.get("output", "")
        finish_reason = result.get("finish_reason", "?")

        print(f"  Status: {status}")
        print(f"  Model: {model_name}")
        print(f"  Finish Reason: {finish_reason}")
        print(f"  Latency: {result.get('latency_ms', '?')}ms (total: {elapsed}ms)")
        print(f"  Output: {output[:200]}")

        is_normal_chat = finish_reason == "stop" or (output and "[Tool Call]" not in output)
        if is_normal_chat and status == "success":
            print("  ✅ Bridge correctly degraded tool_call to normal chat (no tools)")
        else:
            print("  ⚠️  Unexpected behavior for Bridge tool_call without tools")

        return {
            "test": "bridge_tool_call_without_tools",
            "status": status,
            "model": model_name,
            "finish_reason": finish_reason,
            "is_normal_chat": is_normal_chat,
        }
    except Exception as e:
        print(f"  ❌ Bridge request failed: {e}")
        return {"test": "bridge_tool_call_without_tools", "status": "error", "error": str(e)}


async def test_python_tool_call_without_tools(client: httpx.AsyncClient):
    print("\n" + "=" * 60)
    print("TEST 4: Python Direct + tool_call WITHOUT tools -> degrades to normal chat")
    print("=" * 60)

    payload = {
        "appid": "toolcall-test",
        "type": "tool_call",
        "prompt": "请帮我计算 3+5 等于多少",
        "priority": 3,
    }

    start = time.time()
    resp = await client.post(f"{API_BASE}/dispatch", json=payload, timeout=60)
    elapsed = int((time.time() - start) * 1000)
    data = resp.json()

    status = data.get("status", "unknown")
    result = data.get("result", {})
    model_name = result.get("model_name", "?")
    output = result.get("output", "")
    finish_reason = result.get("finish_reason", "?")

    print(f"  Status: {status}")
    print(f"  Model: {model_name}")
    print(f"  Finish Reason: {finish_reason}")
    print(f"  Latency: {result.get('latency_ms', '?')}ms (total: {elapsed}ms)")
    print(f"  Output: {output[:200]}")

    is_normal_chat = finish_reason == "stop" or (output and "[Tool Call]" not in output)
    if is_normal_chat:
        print("  ✅ Correctly degraded to normal chat (no tools provided)")
    else:
        print("  ⚠️  Unexpected behavior for tool_call without tools")

    return {
        "test": "python_tool_call_without_tools",
        "status": status,
        "model": model_name,
        "finish_reason": finish_reason,
        "is_normal_chat": is_normal_chat,
    }


async def test_bridge_normal_chat(client: httpx.AsyncClient):
    print("\n" + "=" * 60)
    print("TEST 5: Bridge + normal chat (Bridge path regression test)")
    print("=" * 60)

    payload = {
        "appid": "bridge-chat-test",
        "type": "chat",
        "prompt": "用一句话介绍量子计算",
        "priority": 3,
    }

    start = time.time()
    try:
        resp = await client.post(f"{BRIDGE_BASE}/dispatch", json=payload, timeout=120)
        elapsed = int((time.time() - start) * 1000)
        data = resp.json()

        status = data.get("status", "unknown")
        result = data.get("result", {})
        model_name = result.get("model_name", "?")
        output = result.get("output", "")

        print(f"  Status: {status}")
        print(f"  Model: {model_name}")
        print(f"  Latency: {result.get('latency_ms', '?')}ms (total: {elapsed}ms)")
        print(f"  Output: {output[:200]}")

        if status == "success" and output:
            print("  ✅ Bridge normal chat works correctly")
        else:
            print("  ❌ Bridge normal chat broken!")

        return {
            "test": "bridge_normal_chat",
            "status": status,
            "model": model_name,
            "has_output": bool(output),
        }
    except Exception as e:
        print(f"  ❌ Bridge request failed: {e}")
        return {"test": "bridge_normal_chat", "status": "error", "error": str(e)}


async def test_normal_chat_unchanged(client: httpx.AsyncClient):
    print("\n" + "=" * 60)
    print("TEST 6: Normal chat via Python (regression test)")
    print("=" * 60)

    payload = {
        "appid": "regression-test",
        "type": "chat",
        "prompt": "用一句话介绍量子计算",
        "priority": 3,
    }

    start = time.time()
    resp = await client.post(f"{API_BASE}/dispatch", json=payload, timeout=60)
    elapsed = int((time.time() - start) * 1000)
    data = resp.json()

    status = data.get("status", "unknown")
    result = data.get("result", {})
    model_name = result.get("model_name", "?")
    output = result.get("output", "")

    print(f"  Status: {status}")
    print(f"  Model: {model_name}")
    print(f"  Latency: {result.get('latency_ms', '?')}ms (total: {elapsed}ms)")
    print(f"  Output: {output[:200]}")

    if status == "success" and output:
        print("  ✅ Normal chat still works correctly")
    else:
        print("  ❌ Normal chat broken!")

    return {
        "test": "normal_chat_regression",
        "status": status,
        "model": model_name,
        "has_output": bool(output),
    }


async def main():
    print("🐾 OpenClaw Tool Call E2E Test")
    print(f"   API: {API_BASE}")
    print(f"   Bridge: {BRIDGE_BASE}")
    print(f"   Gateway: http://localhost:3000")

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            health = await client.get(f"{API_BASE}/health", timeout=5)
            if health.status_code != 200:
                print("❌ FastAPI scheduler not available!")
                return
            print("✅ FastAPI scheduler is healthy")
        except Exception as e:
            print(f"❌ Cannot connect to FastAPI: {e}")
            return

        gateway_available = False
        try:
            gw_health = await client.get("http://localhost:3000/health", timeout=5)
            if gw_health.status_code == 200:
                gw_data = gw_health.json()
                print(f"✅ Gateway is healthy (models={gw_data.get('models_registered')}, agents={gw_data.get('agents_registered')})")
                gateway_available = True
            else:
                print("⚠️  Gateway returned non-200")
        except Exception:
            print("⚠️  Gateway not available")

        bridge_available = False
        try:
            bridge_health = await client.get(f"{BRIDGE_BASE}/health", timeout=5)
            if bridge_health.status_code == 200:
                bh_data = bridge_health.json()
                gw_routing = bh_data.get("gateway_routing", False)
                print(f"✅ Bridge is healthy (gateway_routing={gw_routing})")
                bridge_available = True
            else:
                print("⚠️  Bridge returned non-200")
        except Exception:
            print("⚠️  Bridge not available (Bridge tests will be skipped)")

        results = []

        results.append(await test_python_direct_with_tools(client))
        await asyncio.sleep(0.5)

        if bridge_available:
            results.append(await test_bridge_with_tools(client))
            await asyncio.sleep(0.5)

            results.append(await test_bridge_tool_call_without_tools(client))
            await asyncio.sleep(0.5)

            results.append(await test_bridge_normal_chat(client))
            await asyncio.sleep(0.5)
        else:
            results.append({"test": "bridge_with_tools", "status": "skipped"})
            results.append({"test": "bridge_tool_call_without_tools", "status": "skipped"})
            results.append({"test": "bridge_normal_chat", "status": "skipped"})

        results.append(await test_python_tool_call_without_tools(client))
        await asyncio.sleep(0.5)

        results.append(await test_normal_chat_unchanged(client))

        results.append({"test": "gateway_health", "status": "healthy" if gateway_available else "unavailable", "gateway_available": gateway_available})

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in results:
        test_name = r["test"]
        status = r.get("status", "?")
        model = r.get("model", "?")
        extra = ""
        if "has_tool_call" in r:
            extra += f" tool_call={'✅' if r['has_tool_call'] else '❌'}"
        if "is_normal_chat" in r:
            extra += f" normal_chat={'✅' if r['is_normal_chat'] else '❌'}"
        if "has_output" in r:
            extra += f" output={'✅' if r['has_output'] else '❌'}"
        if "gateway_available" in r:
            extra += f" gateway={'✅' if r['gateway_available'] else '❌'}"
        print(f"  {test_name}: status={status} model={model}{extra}")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
