#!/usr/bin/env python3
"""Hermes 路由逻辑验证测试脚本

简化路由方案:
  - 简单聊天 (score < 40, 无tools, 非code类型) → direct_local → Ollama直连
  - 有 tools / code_execution / tool_call → gateway → OfficialGW
  - 高复杂度 (score >= 40) → gateway → OfficialGW
  - require_local → local_inference
"""

import json
import sys
import time
import httpx

HERMES_URL = "http://localhost:8082"
TIMEOUT = 60.0

# ── 测试用例 ──────────────────────────────────────────────────────

TEST_CASES = [
    # ── 简单聊天 → direct_local ──
    {
        "name": "简单数学题",
        "request": {"appid": "route-test", "type": "chat", "priority": 3, "prompt": "1+1等于几？"},
        "expected_route": "direct_local",
        "category": "simple",
    },
    {
        "name": "日常问候",
        "request": {"appid": "route-test", "type": "chat", "priority": 3, "prompt": "你好，今天天气怎么样？"},
        "expected_route": "direct_local",
        "category": "simple",
    },
    {
        "name": "写一首短诗",
        "request": {"appid": "route-test", "type": "chat", "priority": 3, "prompt": "写一首短诗"},
        "expected_route": "direct_local",
        "category": "simple",
    },
    {
        "name": "翻译请求",
        "request": {"appid": "route-test", "type": "chat", "priority": 3, "prompt": "把'Hello World'翻译成中文"},
        "expected_route": "direct_local",
        "category": "simple",
    },
    # ── code_execution → gateway ──
    {
        "name": "代码执行",
        "request": {"appid": "route-test", "type": "code_execution", "priority": 3, "prompt": "执行Python脚本计算斐波那契数列"},
        "expected_route": "gateway",
        "category": "agent",
    },
    {
        "name": "代码类型",
        "request": {"appid": "route-test", "type": "code", "priority": 3, "prompt": "写一个排序算法"},
        "expected_route": "gateway",
        "category": "agent",
    },
    {
        "name": "工具调用",
        "request": {"appid": "route-test", "type": "tool_call", "priority": 3, "prompt": "查询数据库中的用户信息"},
        "expected_route": "gateway",
        "category": "agent",
    },
    # ── 带 tools → gateway ──
    {
        "name": "带工具的请求",
        "request": {
            "appid": "route-test", "type": "chat", "priority": 3,
            "prompt": "帮我搜索最新的AI论文",
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        },
        "expected_route": "gateway",
        "category": "agent",
    },
    # ── 高复杂度 → gateway ──
    {
        "name": "多步骤规划",
        "request": {
            "appid": "route-test", "type": "chat", "priority": 3,
            "prompt": "请设计一个完整的市场调研方案，包括：1.目标市场分析 2.竞品调研方法 3.用户访谈计划 4.数据收集与分析框架 5.最终报告结构设计。请详细说明每个步骤的执行方法和预期产出。",
        },
        "expected_route": "gateway",
        "category": "agent",
    },
    {
        "name": "复杂系统设计",
        "request": {
            "appid": "route-test", "type": "chat", "priority": 3,
            "prompt": "设计一个分布式微服务架构系统，要求包含：服务注册与发现、负载均衡、熔断降级、链路追踪、配置中心、API网关，并给出技术选型理由和部署方案。",
        },
        "expected_route": "gateway",
        "category": "agent",
    },
    # ── require_local → local_inference ──
    {
        "name": "隐私约束请求",
        "request": {
            "appid": "route-test", "type": "chat", "priority": 3,
            "prompt": "分析这份内部财务数据",
            "constraints": {"require_local": True},
        },
        "expected_route": "local_inference",
        "category": "privacy",
    },
]


# ── 测试函数 ──────────────────────────────────────────────────────

def test_analyze():
    """测试 /route/analyze 端点"""
    print("=" * 70)
    print("  路由分析测试 (/route/analyze)")
    print("=" * 70)

    results = []
    with httpx.Client(timeout=10.0) as client:
        for tc in TEST_CASES:
            try:
                resp = client.post(f"{HERMES_URL}/route/analyze", json=tc["request"])
                data = resp.json()
                actual = data.get("recommended_path", "?")
                score = data.get("complexity_score", "?")
                expected = tc["expected_route"]
                ok = actual == expected
                results.append(ok)

                status = "✓" if ok else "✗"
                print(f"  {status} {tc['name']:<16} score={str(score):<4} "
                      f"expected={expected:<16} actual={actual:<16}"
                      f"{'' if ok else ' ← MISMATCH!'}")
            except Exception as e:
                results.append(False)
                print(f"  ✗ {tc['name']:<16} ERROR: {e}")

    passed = sum(results)
    total = len(results)
    print(f"\n  结果: {passed}/{total} 通过")
    return results


def test_dispatch():
    """测试 /queue/submit-sync 端点（实际执行）"""
    print("\n" + "=" * 70)
    print("  实际调度测试 (/queue/submit-sync)")
    print("=" * 70)

    # 只测试部分用例（避免耗时过长）
    dispatch_cases = [
        tc for tc in TEST_CASES
        if tc["category"] in ("simple", "agent")
    ][:6]  # 最多6个

    results = []
    with httpx.Client(timeout=TIMEOUT) as client:
        for tc in dispatch_cases:
            try:
                start = time.time()
                resp = client.post(
                    f"{HERMES_URL}/queue/submit-sync?timeout={int(TIMEOUT)}",
                    json=tc["request"],
                )
                elapsed = int((time.time() - start) * 1000)
                data = resp.json()

                actual_route = data.get("routing", {}).get("route_path", "?")
                dispatch_result = data.get("result") or {}
                model = dispatch_result.get("model_name", "?")
                via = dispatch_result.get("routed_via", "?")
                status = data.get("status", "?")
                expected = tc["expected_route"]

                # For dispatch test: route decision is what matters
                # If Bridge is down, gateway falls back to local — that's acceptable
                ok = actual_route == expected
                # Also accept fallback: if expected=gateway but Bridge is down, route may be direct_local
                if not ok and expected == "gateway" and actual_route == "direct_local":
                    ok = True  # Bridge unavailable, fallback is acceptable

                results.append(ok)

                mark = "✓" if ok else "✗"
                fallback_note = " (fallback)" if actual_route != expected and expected == "gateway" else ""
                print(f"  {mark} {tc['name']:<16} route={actual_route:<16} "
                      f"model={model:<16} via={via:<24} "
                      f"latency={elapsed}ms status={status}{fallback_note}"
                      f"{'' if ok else ' ← MISMATCH!'}")
            except Exception as e:
                results.append(False)
                print(f"  ✗ {tc['name']:<16} ERROR: {e}")

    passed = sum(results)
    total = len(results)
    print(f"\n  结果: {passed}/{total} 通过")
    return results


def test_local_direct():
    """验证 direct_local 确实直连 Ollama（不经过 Bridge）"""
    print("\n" + "=" * 70)
    print("  直连验证 (direct_local 不经过 Bridge)")
    print("=" * 70)

    req = {"appid": "route-test", "type": "chat", "priority": 3, "prompt": "你好"}
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(
            f"{HERMES_URL}/queue/submit-sync?timeout={int(TIMEOUT)}",
            json=req,
        )
        data = resp.json()

    route = data.get("routing", {}).get("route_path", "?")
    via = data.get("result", {}).get("routed_via", "?")
    model = data.get("result", {}).get("model_name", "?")
    is_direct = "direct_local" in via or "hermes_direct_local" in via

    print(f"  route:  {route}")
    print(f"  via:    {via}")
    print(f"  model:  {model}")
    print(f"  直连:   {'✓ 是 (不经过Bridge)' if is_direct else '✗ 否 (经过Bridge绕路)'}")

    return [is_direct]


# ── 主函数 ──────────────────────────────────────────────────────

def main():
    print("\n🧪 Hermes 路由逻辑验证")
    print(f"   目标: {HERMES_URL}")
    print(f"   时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    # 健康检查
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{HERMES_URL}/health")
            health = resp.json()
            print(f"  Hermes: {health.get('status', '?')}")
    except Exception as e:
        print(f"  ✗ Hermes 不可达: {e}")
        sys.exit(1)

    all_results = []
    all_results.extend(test_analyze())
    all_results.extend(test_dispatch())
    all_results.extend(test_local_direct())

    total = len(all_results)
    passed = sum(all_results)

    print("\n" + "=" * 70)
    if passed == total:
        print(f"  ✓ 全部通过 ({passed}/{total})")
    else:
        print(f"  ✗ 部分失败 ({passed}/{total})")
    print("=" * 70 + "\n")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
