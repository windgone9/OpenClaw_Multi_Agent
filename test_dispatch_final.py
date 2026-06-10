#!/usr/bin/env python3
"""Test full Bridge dispatch for 4-category routing with OfficialGW running"""
import urllib.request, json, sys

def bridge_dispatch(body):
    req = urllib.request.Request(
        "http://localhost:3001/dispatch",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())

tests = [
    ("单步问答", "chat", "你好，今天天气怎么样？", None, "direct_local"),
    ("多步批处理", "code", "执行Python脚本计算斐波那契数列", None, "gateway"),
    ("多模态", "chat", "请分析这张图片中的文字内容", None, "multimodal"),
    ("隐私", "chat", "分析这份内部财务数据", {"require_local": True}, "local_inference"),
    ("Volcano调度", "chat", "使用Volcano调度分布式训练任务", None, "gateway"),
    ("架构设计", "chat", "设计一个微服务架构方案", None, "gateway"),
]

print("=" * 60)
print("Bridge Dispatch 4类路由测试 (OfficialGW已启动)")
print("=" * 60)
passed = 0

for name, rtype, prompt, constraints, expect in tests:
    body = {"appid": "test", "type": rtype, "priority": 3, "prompt": prompt}
    if constraints:
        body["constraints"] = constraints
    body["hermes_routing"] = {"route_path": expect, "complexity_score": 15}

    try:
        d = bridge_dispatch(body)
        status = d.get("status", "?")
        hr = d.get("hermes_routing", {})
        actual_route = hr.get("route_path", "?")
        result = d.get("result") or {}
        model = result.get("model_name", "?")
        note = d.get("routing_note", "")
        strategy = d.get("strategy_name", "?")
        latency = d.get("total_latency_ms", "?")
        ok = actual_route == expect
        if ok:
            passed += 1
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}: route={actual_route}, status={status}, model={model}, latency={latency}ms")
        if note:
            print(f"         note: {note[:80]}")
    except Exception as e:
        print(f"  [FAIL] {name}: error={e}")

print(f"\n结果: {passed}/{len(tests)} 通过")
sys.exit(0 if passed == len(tests) else 1)
