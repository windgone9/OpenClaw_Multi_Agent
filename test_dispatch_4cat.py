#!/usr/bin/env python3
"""Test full dispatch chain for 4-category routing"""
import urllib.request, json, time, sys

def queue_submit(body):
    req = urllib.request.Request(
        "http://localhost:8082/queue/submit",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def get_results(limit=5):
    req = urllib.request.Request(
        f"http://localhost:8082/queue/results?limit={limit}&pop=false",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

tests = [
    ("单步问答", "chat", "你好，今天天气怎么样？", None, "direct_local"),
    ("多模态", "chat", "请分析这张图片中的文字内容", None, "multimodal"),
    ("隐私", "chat", "分析这份内部财务数据", {"require_local": True}, "local_inference"),
]

print("=" * 60)
print("完整 Dispatch 链路测试")
print("=" * 60)

for name, rtype, prompt, constraints, expect in tests:
    body = {"appid": "test", "type": rtype, "priority": 3, "prompt": prompt}
    if constraints:
        body["constraints"] = constraints
    try:
        result = queue_submit(body)
        rid = result.get("request_id", "?")[:8]
        print(f"  提交: {name} → ID={rid}, 状态={result.get('status')}")
    except Exception as e:
        print(f"  [FAIL] {name}: 提交失败 - {e}")

print("\n等待处理 (10s)...")
time.sleep(10)

data = get_results(limit=5)
results = data.get("results", [])
print(f"\n获取到 {len(results)} 个结果:")
for r in results[:5]:
    routing = r.get("routing", {})
    route_path = routing.get("route_path", "?")
    status = r.get("status", "?")
    latency = r.get("total_latency_ms", "?")
    error = r.get("error", {})
    err_msg = error.get("message", "")[:60] if error else ""
    print(f"  路由={route_path}, 状态={status}, 延迟={latency}ms {err_msg}")
