#!/usr/bin/env python3
"""Queue 模式 vs Bridge 直连模式 对比测试

对比两种模式在相同请求下的:
  - 路由决策 (route_path, reason)
  - 端到端延迟
  - 执行路径 (via)
  - 反馈闭环差异
"""

import json
import sys
import time
import httpx

HERMES_URL = "http://localhost:8082"
TIMEOUT = 180.0  # Official GW may take >60s for code execution

# ── 测试用例 (覆盖简单/复杂/隐私) ────────────────────────────────

TEST_CASES = [
    {
        "name": "简单问候",
        "request": {"appid": "cmp-test", "type": "chat", "priority": 3, "prompt": "你好，今天天气怎么样？"},
        "expected_route": "direct_local",
    },
    {
        "name": "数学计算",
        "request": {"appid": "cmp-test", "type": "chat", "priority": 3, "prompt": "1+1等于几？"},
        "expected_route": "direct_local",
    },
    {
        "name": "代码执行",
        "request": {"appid": "cmp-test", "type": "code_execution", "priority": 3, "prompt": "执行Python脚本计算斐波那契数列"},
        "expected_route": "gateway",
    },
    {
        "name": "多步骤规划",
        "request": {"appid": "cmp-test", "type": "chat", "priority": 3,
                    "prompt": "请设计一个完整的市场调研方案，包括：1.目标市场分析 2.竞品调研方法 3.用户访谈计划 4.数据收集与分析框架"},
        "expected_route": "gateway",
    },
    {
        "name": "架构设计",
        "request": {"appid": "cmp-test", "type": "chat", "priority": 3,
                    "prompt": "设计一个分布式微服务架构系统，要求包含：服务注册与发现、负载均衡、熔断降级、链路追踪"},
        "expected_route": "gateway",
    },
    {
        "name": "隐私约束",
        "request": {"appid": "cmp-test", "type": "chat", "priority": 3,
                    "prompt": "分析这份内部财务数据",
                    "constraints": {"require_local": True}},
        "expected_route": "local_inference",
    },
]


def test_queue_mode(client: httpx.Client, tc: dict) -> dict:
    """Queue 模式: /queue/submit-sync"""
    start = time.time()
    resp = client.post(f"{HERMES_URL}/queue/submit-sync?timeout=180", json=tc["request"], timeout=TIMEOUT)
    elapsed = int((time.time() - start) * 1000)
    data = resp.json()

    routing = data.get("routing", {})
    result = data.get("result") or {}
    return {
        "mode": "Queue",
        "name": tc["name"],
        "route_path": routing.get("route_path", "?"),
        "reason": routing.get("reason", "")[:100],
        "agent_decision": routing.get("agent_decision", ""),
        "post_validated": routing.get("post_validated", False),
        "model": result.get("model_name", "?"),
        "via": result.get("routed_via", "?"),
        "latency_ms": elapsed,
        "status": data.get("status", "?"),
        "has_feedback": True,  # Queue 模式总是记录反馈
    }


def test_bridge_mode(client: httpx.Client, tc: dict) -> dict:
    """Bridge 直连模式: /route"""
    start = time.time()
    resp = client.post(f"{HERMES_URL}/route", json=tc["request"], timeout=TIMEOUT)
    elapsed = int((time.time() - start) * 1000)
    data = resp.json()

    routing = data.get("routing", {})
    result = data.get("result") or {}
    trace = data.get("hermes_trace", [])
    return {
        "mode": "Bridge",
        "name": tc["name"],
        "route_path": routing.get("route_path", "?"),
        "reason": routing.get("reason", "")[:100],
        "agent_decision": routing.get("agent_decision", ""),
        "post_validated": False,  # Bridge 模式用简化路由覆盖
        "model": result.get("model_name", "?"),
        "via": "bridge_direct" if result.get("model_name") else "N/A",
        "latency_ms": elapsed,
        "status": data.get("status", "?"),
        "has_feedback": False,  # Bridge 模式只写 SQLite
        "trace_steps": len(trace),
    }


def print_comparison(results_queue: list, results_bridge: list):
    """打印对比表格"""
    print("\n" + "=" * 110)
    print("  Queue 模式 vs Bridge 直连模式 — 路由决策对比")
    print("=" * 110)

    # 路由决策对比
    print(f"\n  {'用例':<12} {'预期路由':<16} {'Queue路由':<16} {'Bridge路由':<16} {'决策一致':>6}")
    print("  " + "-" * 70)

    for q, b in zip(results_queue, results_bridge):
        expected = next(tc["expected_route"] for tc in TEST_CASES if tc["name"] == q["name"])
        q_ok = "✓" if q["route_path"] == expected else "✗"
        b_ok = "✓" if b["route_path"] == expected else "✗"
        match = "✓" if q["route_path"] == b["route_path"] else "✗"
        print(f"  {q['name']:<12} {expected:<16} {q['route_path']:<16} {b['route_path']:<16} {match:>6}")

    # 延迟对比
    print(f"\n  {'用例':<12} {'Queue延迟':>10} {'Bridge延迟':>10} {'差异':>10} {'Queue路由':<16} {'Bridge路由':<16}")
    print("  " + "-" * 80)

    for q, b in zip(results_queue, results_bridge):
        diff = q["latency_ms"] - b["latency_ms"]
        diff_str = f"+{diff}ms" if diff > 0 else f"{diff}ms"
        print(f"  {q['name']:<12} {q['latency_ms']:>8}ms {b['latency_ms']:>8}ms {diff_str:>10} {q['route_path']:<16} {b['route_path']:<16}")

    # 详细路由决策日志
    print(f"\n  {'='*100}")
    print("  详细路由决策日志")
    print("  " + "=" * 100)

    for q, b in zip(results_queue, results_bridge):
        print(f"\n  ┌─ {q['name']}")
        print(f"  │ Queue:  route={q['route_path']:<16} via={q['via']:<24} model={q['model']}")
        print(f"  │         reason={q['reason']}")
        print(f"  │         agent_decision={q['agent_decision']}  post_validated={q['post_validated']}  feedback={q['has_feedback']}")
        print(f"  │ Bridge: route={b['route_path']:<16} model={b['model']}")
        print(f"  │         reason={b['reason']}")
        print(f"  │         agent_decision={b['agent_decision']}  trace_steps={b.get('trace_steps', '?')}")
        print(f"  └─ latency: Queue={q['latency_ms']}ms  Bridge={b['latency_ms']}ms")

    # 汇总统计
    q_total = sum(r["latency_ms"] for r in results_queue)
    b_total = sum(r["latency_ms"] for r in results_bridge)
    q_avg = q_total // len(results_queue) if results_queue else 0
    b_avg = b_total // len(results_bridge) if results_bridge else 0

    q_route_match = sum(1 for r in results_queue if r["route_path"] == next(tc["expected_route"] for tc in TEST_CASES if tc["name"] == r["name"]))
    b_route_match = sum(1 for r in results_bridge if r["route_path"] == next(tc["expected_route"] for tc in TEST_CASES if tc["name"] == r["name"]))

    print(f"\n  {'='*100}")
    print(f"  汇总统计")
    print(f"  {'='*100}")
    print(f"  {'指标':<24} {'Queue模式':>12} {'Bridge模式':>12}")
    print(f"  {'-'*50}")
    print(f"  {'总延迟':<24} {q_total:>10}ms {b_total:>10}ms")
    print(f"  {'平均延迟':<24} {q_avg:>10}ms {b_avg:>10}ms")
    print(f"  {'路由正确率':<24} {q_route_match}/{len(results_queue):>8} {b_route_match}/{len(results_bridge):>8}")
    print(f"  {'Memory闭环反馈':<24} {'✓ 支持':>12} {'✗ 仅SQLite':>12}")
    print(f"  {'Bridge不可用fallback':<24} {'✓ 自动本地':>12} {'✗ 报错':>12}")


def main():
    print("\n🧪 Queue 模式 vs Bridge 直连模式 对比测试")
    print(f"   目标: {HERMES_URL}")
    print(f"   时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   用例数: {len(TEST_CASES)}")

    # 健康检查
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{HERMES_URL}/health")
            health = resp.json()
            print(f"   Hermes: {health.get('status', '?')}")
    except Exception as e:
        print(f"   ✗ Hermes 不可达: {e}")
        sys.exit(1)

    results_queue = []
    results_bridge = []

    with httpx.Client(timeout=TIMEOUT) as client:
        for i, tc in enumerate(TEST_CASES):
            print(f"\n  [{i+1}/{len(TEST_CASES)}] 测试: {tc['name']}")

            # Queue 模式
            print(f"    → Queue 模式...", end=" ", flush=True)
            try:
                r = test_queue_mode(client, tc)
                results_queue.append(r)
                print(f"route={r['route_path']} latency={r['latency_ms']}ms")
            except Exception as e:
                print(f"ERROR: {e}")
                results_queue.append({"mode": "Queue", "name": tc["name"], "route_path": "error",
                                       "reason": str(e)[:100], "agent_decision": "", "post_validated": False,
                                       "model": "?", "via": "?", "latency_ms": 0, "status": "error", "has_feedback": False})

            # Bridge 直连模式
            print(f"    → Bridge 模式...", end=" ", flush=True)
            try:
                r = test_bridge_mode(client, tc)
                results_bridge.append(r)
                print(f"route={r['route_path']} latency={r['latency_ms']}ms")
            except Exception as e:
                print(f"ERROR: {e}")
                results_bridge.append({"mode": "Bridge", "name": tc["name"], "route_path": "error",
                                       "reason": str(e)[:100], "agent_decision": "", "post_validated": False,
                                       "model": "?", "via": "?", "latency_ms": 0, "status": "error",
                                       "has_feedback": False, "trace_steps": 0})

    # 打印对比结果
    print_comparison(results_queue, results_bridge)

    # 检查 MEMORY.md 反馈
    print(f"\n  {'='*100}")
    print("  Memory 闭环验证")
    print(f"  {'='*100}")

    import os
    memory_file = os.path.expanduser("~/.hermes/memories/MEMORY.md")
    feedback_file = os.path.expanduser("~/.hermes/routing_feedback/feedback.jsonl")

    if os.path.exists(memory_file):
        with open(memory_file, "r") as f:
            content = f.read()
        lines = [l for l in content.split("\n") if l.strip().startswith("-")]
        print(f"  MEMORY.md: ✓ 存在 ({len(lines)} 条规则/反馈)")
        # 显示最近5条反馈
        feedback_lines = [l for l in lines if "✓" in l or "✗" in l]
        if feedback_lines:
            print(f"  最近反馈 ({min(5, len(feedback_lines))} 条):")
            for line in feedback_lines[-5:]:
                print(f"    {line.strip()}")
    else:
        print(f"  MEMORY.md: ✗ 不存在")

    if os.path.exists(feedback_file):
        with open(feedback_file, "r") as f:
            fb_lines = f.readlines()
        print(f"  feedback.jsonl: ✓ 存在 ({len(fb_lines)} 条记录)")
        # 显示最近3条
        if fb_lines:
            print(f"  最近记录:")
            for line in fb_lines[-3:]:
                try:
                    entry = json.loads(line)
                    print(f"    {entry.get('route_path','?')} {'✓' if entry.get('success') else '✗'} "
                          f"{entry.get('latency_ms',0)}ms '{entry.get('request_summary','')[:30]}'")
                except:
                    pass
    else:
        print(f"  feedback.jsonl: ✗ 不存在")

    print()


if __name__ == "__main__":
    main()
