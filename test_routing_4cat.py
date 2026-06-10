#!/usr/bin/env python3
"""Test 4-category routing strategy"""
import urllib.request, json, sys

tests = [
    ("单步问答", "chat", "你好，今天天气怎么样？", None, "direct_local"),
    ("多步批处理", "code", "执行Python脚本计算斐波那契数列", None, "gateway"),
    ("多模态", "chat", "请分析这张图片中的文字内容", None, "multimodal"),
    ("隐私", "chat", "分析这份内部财务数据", {"require_local": True}, "local_inference"),
    ("Volcano", "chat", "使用Volcano调度分布式训练任务", None, "gateway"),
    ("翻译", "chat", "翻译hello world到中文", None, "direct_local"),
    ("OCR", "chat", "识别图片中的文字并提取", None, "multimodal"),
    ("架构设计", "chat", "设计一个微服务架构方案", None, "gateway"),
]

print("=" * 60)
print("4类路由策略测试")
print("=" * 60)
passed = 0
for name, rtype, prompt, constraints, expect in tests:
    body = {"appid": "test", "type": rtype, "priority": 3, "prompt": prompt}
    if constraints:
        body["constraints"] = constraints
    req = urllib.request.Request(
        "http://localhost:8082/route/analyze",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read())
        actual = d.get("recommended_path", "?")
        score = d.get("complexity_score", 0)
        ok = actual == expect
        if ok: passed += 1
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}: expect={expect}, actual={actual}, score={score}")
    except Exception as e:
        print(f"  [FAIL] {name}: error={e}")

print(f"\n结果: {passed}/{len(tests)} 通过")
sys.exit(0 if passed == len(tests) else 1)
