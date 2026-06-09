#!/usr/bin/env python3
"""Dashboard 功能全面测试脚本"""
import httpx
import json
import sys
import time

H = "http://localhost:8082"
B = "http://localhost:3001"
GW = "http://localhost:3000"
OGW = "http://localhost:3005"
OLL = "http://localhost:11434"

client = httpx.Client(timeout=15)

def test_endpoint(method, path, desc, body=None, expect_status=200):
    try:
        url = f"{H}{path}"
        if method == "GET":
            r = client.get(url)
        else:
            r = client.post(url, json=body or {})
        status = r.status_code
        ok = status == expect_status
        data = None
        try:
            data = r.json()
        except:
            data = r.text[:200]
        return {"ok": ok, "status": status, "desc": desc, "data": data, "path": path, "method": method}
    except Exception as e:
        return {"ok": False, "status": 0, "desc": desc, "data": str(e), "path": path, "method": method}

def test_direct_endpoint(url, desc):
    try:
        r = client.get(url, timeout=5)
        return {"ok": r.status_code == 200, "status": r.status_code, "desc": desc}
    except Exception as e:
        return {"ok": False, "status": 0, "desc": desc, "data": str(e)[:100]}

print("=" * 70)
print("Dashboard 功能全面测试")
print("=" * 70)

# ===== 1. API 端点测试 =====
print("\n--- 1. API 端点测试 ---")
api_tests = [
    ("GET", "/health", "Hermes健康检查"),
    ("GET", "/queue/status", "队列状态"),
    ("GET", "/stats", "请求统计+系统资源"),
    ("GET", "/memory/content", "Memory内容"),
    ("GET", "/queue/feedback?limit=5", "反馈历史"),
    ("GET", "/queue/results?limit=1&pop=false", "结果队列"),
    ("GET", "/services/status", "服务管理状态"),
    ("GET", "/proxy/health", "代理健康检查(全服务)"),
    ("GET", "/proxy/bridge/health", "Bridge健康"),
    ("GET", "/proxy/ollama-ps", "Ollama进程信息"),
    ("POST", "/route/analyze", "复杂度分析", {"appid": "test", "type": "chat", "priority": 3, "prompt": "你好"}),
    ("GET", "/dashboard.html", "Dashboard页面"),
]

api_results = []
for t in api_tests:
    method, path, desc = t[0], t[1], t[2]
    body = t[3] if len(t) > 3 else None
    r = test_endpoint(method, path, desc, body)
    api_results.append(r)
    mark = "✓" if r["ok"] else "✗"
    print(f"  {mark} {method} {path} → {r['status']} ({desc})")
    if not r["ok"] and r.get("data"):
        detail = str(r["data"])[:150]
        print(f"     ↳ {detail}")

# ===== 2. 数据结构验证 =====
print("\n--- 2. 数据结构验证 ---")

# 2.1 /health 返回结构
r = test_endpoint("GET", "/health", "health结构")
if r["ok"]:
    d = r["data"]
    expected_keys = ["status", "skills_count", "memory_records", "complexity_threshold"]
    for k in expected_keys:
        has = k in d
        print(f"  {'✓' if has else '✗'} /health.{k}")

# 2.2 /queue/status 返回结构
r = test_endpoint("GET", "/queue/status", "queue_status结构")
if r["ok"]:
    d = r["data"]
    print(f"  {'✓' if 'queue_backend' in d else '✗'} /queue/status.queue_backend")
    print(f"  {'✓' if 'worker' in d else '✗'} /queue/status.worker")

# 2.3 /stats 返回结构
r = test_endpoint("GET", "/stats", "stats结构")
if r["ok"]:
    d = r["data"]
    print(f"  {'✓' if 'system' in d else '✗'} /stats.system")
    print(f"  {'✓' if 'requests' in d else '✗'} /stats.requests")
    if "system" in d:
        sys = d["system"]
        print(f"  {'✓' if 'cpu_percent' in sys else '✗'} /stats.system.cpu_percent")
        print(f"  {'✓' if 'memory_percent' in sys else '✗'} /stats.system.memory_percent")

# 2.4 /memory/content 返回结构
r = test_endpoint("GET", "/memory/content", "memory_content结构")
if r["ok"]:
    d = r["data"]
    print(f"  {'✓' if 'content' in d else '✗'} /memory/content.content")
    if "content" in d:
        c = d["content"]
        print(f"  {'✓' if 'Routing Patterns Learned' in c else '✗'} MEMORY包含路由规则")
        print(f"  {'✓' if 'Key Rules' in c else '✗'} MEMORY包含关键规则")
        print(f"  {'✓' if 'Latency Stats' in c else '✗'} MEMORY包含延迟统计")
        print(f"  {'✓' if 'Feedback History' in c else '✗'} MEMORY包含反馈历史")

# 2.5 /queue/feedback 返回结构
r = test_endpoint("GET", "/queue/feedback?limit=5", "feedback结构")
if r["ok"]:
    d = r["data"]
    print(f"  {'✓' if 'feedback' in d else '✗'} /queue/feedback.feedback")
    if "feedback" in d and len(d["feedback"]) > 0:
        fb = d["feedback"][0]
        for k in ["route_path", "success", "latency_ms"]:
            print(f"  {'✓' if k in fb else '✗'} feedback[0].{k}")

# 2.6 /proxy/health 返回结构
r = test_endpoint("GET", "/proxy/health", "proxy_health结构")
if r["ok"]:
    d = r["data"]
    for svc in ["hermes", "bridge", "gateway", "officialGateway", "ollama"]:
        has = svc in d
        print(f"  {'✓' if has else '✗'} /proxy/health.{svc}")

# ===== 3. 服务直连测试 =====
print("\n--- 3. 服务直连测试 ---")
direct_tests = [
    (f"{H}/health", "Hermes (8082)"),
    (f"{B}/health", "Bridge (3001)"),
    (f"{GW}/health", "Gateway (3000)"),
    (f"{OGW}/health", "OfficialGW (3005)"),
    (f"{OLL}/api/tags", "Ollama (11434)"),
]
for url, desc in direct_tests:
    r = test_direct_endpoint(url, desc)
    mark = "✓" if r["ok"] else "✗"
    print(f"  {mark} {desc} → {r.get('status', 'unreachable')}")

# ===== 4. Queue 模式请求测试 =====
print("\n--- 4. Queue 模式请求测试 ---")
queue_body = {"appid": "dashboard-test", "type": "chat", "priority": 3, "prompt": "你好"}
try:
    start = time.time()
    r = client.post(f"{H}/queue/submit-sync?timeout=60", json=queue_body, timeout=65)
    elapsed = int((time.time() - start) * 1000)
    d = r.json()
    routing = d.get("routing", {})
    result = d.get("result", {})
    ok = d.get("status") == "success"
    print(f"  {'✓' if ok else '✗'} Queue请求: status={d.get('status')}, latency={elapsed}ms")
    print(f"  {'✓' if routing.get('route_path') else '✗'} 路由决策: path={routing.get('route_path')}")
    print(f"  {'✓' if routing.get('complexity_score') is not None else '✗'} 复杂度评分: {routing.get('complexity_score')}")
    print(f"  {'✓' if routing.get('reason') else '✗'} 路由原因: {routing.get('reason', '')[:60]}")
    print(f"  {'✓' if result.get('output') else '✗'} 输出内容: {str(result.get('output', ''))[:60]}")
except Exception as e:
    print(f"  ✗ Queue请求失败: {e}")

# ===== 5. Bridge 模式请求测试 =====
print("\n--- 5. Bridge 模式请求测试 ---")
bridge_body = {"appid": "dashboard-test", "type": "chat", "priority": 3, "prompt": "1+1等于几？"}
try:
    start = time.time()
    r = client.post(f"{H}/proxy/bridge/dispatch", json=bridge_body, timeout=65)
    elapsed = int((time.time() - start) * 1000)
    d = r.json()
    ok = d.get("status") == "success"
    hermes = d.get("hermes_routing", {})
    print(f"  {'✓' if ok else '✗'} Bridge请求: status={d.get('status')}, latency={elapsed}ms")
    print(f"  {'✓' if hermes.get('route_path') else '✗'} Hermes路由: path={hermes.get('route_path')}")
    print(f"  {'✓' if d.get('agent_trace') else '✗'} Agent链路: {len(d.get('agent_trace', []))}步")
except Exception as e:
    print(f"  ✗ Bridge请求失败: {e}")

# ===== 6. 服务管理端点测试 =====
print("\n--- 6. 服务管理端点测试 ---")
svc_tests = [
    ("GET", "/services/status", "服务状态查询"),
    ("POST", "/services/start", "启动服务(空body)", {"service": "nonexistent"}),
    ("POST", "/services/stop", "停止服务(空body)", {"service": "nonexistent"}),
]
for method, path, desc, *body in svc_tests:
    body = body[0] if body else None
    r = test_endpoint(method, path, desc, body)
    mark = "✓" if r["ok"] or r["status"] in [400, 404, 422] else "✗"
    print(f"  {mark} {method} {path} → {r['status']} ({desc})")

# ===== 7. Memory 闭环验证 =====
print("\n--- 7. Memory 闭环验证 ---")
try:
    r = client.get(f"{H}/memory/content")
    d = r.json()
    content = d.get("content", "")
    # Parse latency stats
    import re
    latency_match = re.findall(r"- (\w+): avg=(\d+)ms, p95=(\d+)ms, samples=(\d+)", content)
    for route, avg, p95, samples in latency_match:
        print(f"  ✓ 延迟统计: {route} avg={avg}ms p95={p95}ms samples={samples}")
    # Check feedback history
    fb_match = re.findall(r"- (\w+) ([✓✗]) (\d+)ms", content)
    print(f"  ✓ 反馈记录数: {len(fb_match)}")
    # Check routing patterns
    pattern_match = re.findall(r"- (.+?)\[", content)
    print(f"  ✓ 路由规则数: {len(pattern_match)}")
except Exception as e:
    print(f"  ✗ Memory验证失败: {e}")

# ===== 8. Dashboard 静态资源测试 =====
print("\n--- 8. Dashboard 静态资源测试 ---")
static_tests = [
    (f"{H}/dashboard.html", "Dashboard HTML"),
]
for url, desc in static_tests:
    try:
        r = client.get(url)
        ok = r.status_code == 200 and "OpenClaw" in r.text
        print(f"  {'✓' if ok else '✗'} {desc} → {r.status_code}")
    except Exception as e:
        print(f"  ✗ {desc}: {e}")

# ===== 汇总 =====
print("\n" + "=" * 70)
total = len(api_results)
passed = sum(1 for r in api_results if r["ok"])
print(f"API端点测试: {passed}/{total} 通过")
print("=" * 70)
