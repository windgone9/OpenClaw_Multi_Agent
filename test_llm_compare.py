import json, urllib.request, time

HERMES = "http://localhost:8082"

def post(url, body, timeout=90):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def set_llm(enabled):
    post(f"{HERMES}/llm-enhancer/config", {"enabled": enabled})

test_cases = [
    {"label": "简单问候", "body": {"appid":"cmp","type":"chat","prompt":"你好，今天天气怎么样","priority":3}},
    {"label": "代码调试(规则盲区)", "body": {"appid":"cmp","type":"chat","prompt":"帮我调试这段代码的性能瓶颈，分析内存泄漏的原因","priority":2}},
    {"label": "多步骤规划", "body": {"appid":"cmp","type":"chat","prompt":"设计一个多步骤的自动化方案来优化系统性能","priority":2}},
    {"label": "隐私数据处理", "body": {"appid":"cmp","type":"chat","prompt":"处理敏感数据：用户信息加密","priority":3,"constraints":{"require_local":True}}},
    {"label": "工具调用", "body": {"appid":"cmp","type":"tool_call","prompt":"搜索最新的AI新闻","priority":3,"tools":[{"type":"function","function":{"name":"search","parameters":{}}}]}},
    {"label": "创意写作(规则误判)", "body": {"appid":"cmp","type":"chat","prompt":"写一篇关于人工智能未来发展的创意短文","priority":3}},
]

print("=" * 80)
print("  LLM 增强对比测试：规则路由 vs LLM 增强路由")
print("=" * 80)

set_llm(False)
print("\n--- Phase 1: 纯规则路由 (LLM enhancer OFF) ---")
rule_results = []
for tc in test_cases:
    r = post(f"{HERMES}/route/analyze", tc["body"])
    rule_results.append({
        "label": tc["label"],
        "score": r["complexity_score"],
        "level": r["complexity_level"],
        "path": r["recommended_path"],
        "skill": r.get("skill_matched"),
        "breakdown": r.get("breakdown", {}),
    })
    print(f"  {tc['label']:20s} -> score={r['complexity_score']:3.0f}, level={r['complexity_level']:10s}, path={r['recommended_path']:15s}, skill={r.get('skill_matched')}")

set_llm(True)
print("\n--- Phase 2: LLM 增强路由 (LLM enhancer ON) ---")
llm_results = []
for tc in test_cases:
    r = post(f"{HERMES}/route/analyze", tc["body"])
    llm_results.append({
        "label": tc["label"],
        "score": r["complexity_score"],
        "level": r["complexity_level"],
        "path": r["recommended_path"],
        "skill": r.get("skill_matched"),
        "llm_enhanced": r.get("llm_enhanced", False),
        "llm_intent": r.get("llm_intent"),
        "llm_confidence": r.get("llm_confidence"),
        "llm_suggested_path": r.get("llm_suggested_path"),
    })
    enh = f"llm={r.get('llm_intent','?')}({r.get('llm_confidence','?')})" if r.get("llm_enhanced") else "no-llm"
    print(f"  {tc['label']:20s} -> score={r['complexity_score']:3.0f}, level={r['complexity_level']:10s}, path={r['recommended_path']:15s}, {enh}")

print("\n" + "=" * 80)
print("  对比结果")
print("=" * 80)
print(f"\n  {'请求':20s} | {'规则评分':>8s} | {'增强评分':>8s} | {'规则路径':>15s} | {'增强路径':>15s} | {'LLM意图':>12s} | 差异")
print("  " + "-" * 105)
diffs = 0
for rule, llm in zip(rule_results, llm_results):
    score_diff = llm["score"] - rule["score"]
    path_diff = "same" if rule["path"] == llm["path"] else "PATH CHANGED"
    if rule["path"] != llm["path"]:
        diffs += 1
    intent_str = llm.get("llm_intent") or "-"
    print(f"  {rule['label']:20s} | {rule['score']:8.0f} | {llm['score']:8.0f} | {rule['path']:>15s} | {llm['path']:>15s} | {intent_str:>12s} | {path_diff}")

print(f"\n  路径变更: {diffs}/{len(test_cases)} 个请求")

stats = json.loads(urllib.request.urlopen(f"{HERMES}/llm-enhancer").read().decode("utf-8"))
print(f"\n  LLM Enhancer 统计:")
print(f"    调用次数: {stats['call_count']}, 成功率: {stats['success_rate']:.0%}, 平均延迟: {stats['avg_latency_ms']}ms")
