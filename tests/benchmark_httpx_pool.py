"""
OpenClaw Benchmark — httpx 连接池复用性能对比测试

对比 main 分支（per-request httpx 客户端）与 feature_synccall 分支（共享持久化客户端）的性能差异。

用法:
  python tests/benchmark_httpx_pool.py --url http://localhost:8090 --output results.json
  python tests/benchmark_httpx_pool.py --url http://localhost:8082 --output results.json

输出 JSON 格式:
{
  "branch": "benchmark_baseline",
  "timestamp": "...",
  "results": {
    "health": { "first_ms": X, "avg_ms": Y, "p50_ms": ..., "p95_ms": ..., "p99_ms": ... },
    "chat_completions": { ... },
    "v1_chat": { ... },
    "ollama_tags": { ... },
    "hermes_stats": { ... }
  },
  "summary": {
    "health_improvement_pct": ...,
    "chat_improvement_pct": ...
  }
}
"""

import argparse
import json
import statistics
import time
import sys
from typing import Dict, List, Optional


def measure_endpoint(base_url: str, method: str, path: str,
                     payload: Optional[dict] = None,
                     headers: Optional[dict] = None,
                     n_requests: int = 20,
                     label: str = "") -> Dict:
    """对单个端点发送 n 次请求，测量延迟分布。"""
    import httpx

    latencies: List[float] = []
    statuses: List[int] = []
    errors: List[str] = []

    # 使用非持久化客户端来模拟真实客户端行为（每个测试脚本自身不复用连接）
    # 但我们测的是服务端是否复用了连接池
    client = httpx.Client(timeout=30.0)

    for i in range(n_requests):
        start = time.time()
        try:
            if method == "GET":
                resp = client.get(f"{base_url}{path}", headers=headers)
            elif method == "POST":
                resp = client.post(f"{base_url}{path}", json=payload, headers=headers)
            else:
                raise ValueError(f"Unknown method: {method}")
            elapsed_ms = (time.time() - start) * 1000
            latencies.append(elapsed_ms)
            statuses.append(resp.status_code)
        except Exception as e:
            elapsed_ms = (time.time() - start) * 1000
            latencies.append(elapsed_ms)
            errors.append(str(e)[:100])
            statuses.append(0)

        # 请求间间隔 0.5s，避免过快导致队列堆积
        time.sleep(0.5)

    client.close()

    if not latencies:
        return {"error": "no requests completed"}

    # 计算延迟分布
    sorted_lat = sorted(latencies)
    first_ms = sorted_lat[0]
    # 排除首次请求（可能包含冷启动/TCP连接建立）
    subsequent = sorted_lat[1:] if len(sorted_lat) > 1 else sorted_lat
    avg_ms = statistics.mean(latencies)
    subsequent_avg = statistics.mean(subsequent) if subsequent else avg_ms

    result = {
        "label": label,
        "n_requests": n_requests,
        "first_ms": round(first_ms, 2),
        "avg_ms": round(avg_ms, 2),
        "subsequent_avg_ms": round(subsequent_avg, 2),
        "min_ms": round(sorted_lat[0], 2),
        "max_ms": round(sorted_lat[-1], 2),
        "p50_ms": round(sorted_lat[len(sorted_lat) // 2], 2),
        "p95_ms": round(sorted_lat[int(len(sorted_lat) * 0.95)], 2) if len(sorted_lat) >= 20 else round(sorted_lat[-1], 2),
        "p99_ms": round(sorted_lat[-1], 2),
        "success_count": sum(1 for s in statuses if 200 <= s < 300),
        "error_count": sum(1 for s in statuses if s == 0 or s >= 400),
        "errors": errors[:5],
    }

    print(f"  [{label}] avg={avg_ms:.1f}ms first={first_ms:.1f}ms subsequent_avg={subsequent_avg:.1f}ms "
          f"p50={result['p50_ms']:.1f}ms p95={result['p95_ms']:.1f}ms "
          f"success={result['success_count']}/{n_requests}")
    return result


def run_benchmark(base_url: str, n_requests: int = 20) -> Dict:
    """运行完整 benchmark，覆盖 5 个关键端点。"""
    print(f"\n=== OpenClaw Benchmark ===")
    print(f"  URL: {base_url}")
    print(f"  Requests per endpoint: {n_requests}")
    print(f"  Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    results = {}

    # 1. Health endpoint — 轻量级，最能体现 TCP 连接复用效果
    print("[1/5] Testing /health (lightweight, TCP reuse benchmark)")
    results["health"] = measure_endpoint(
        base_url, "GET", "/health",
        n_requests=n_requests, label="health"
    )

    # 2. Ollama tags — 中等重量，经过 nginx → hermes → ollama 链路
    print("[2/5] Testing /svc/ollama/api/tags (service proxy chain)")
    # 尝试两个路径：通过 nginx 代理 或直接
    ollama_path = "/svc/ollama/api/tags"
    results["ollama_tags"] = measure_endpoint(
        base_url, "GET", ollama_path,
        n_requests=n_requests, label="ollama_tags"
    )

    # 3. Hermes stats — 经过 nginx → hermes，包含多个内部调用
    print("[3/5] Testing /hermes/stats (hermes routing stats)")
    results["hermes_stats"] = measure_endpoint(
        base_url, "GET", "/hermes/stats",
        n_requests=n_requests, label="hermes_stats"
    )

    # 4. Chat completions — 重请求，经完整 LLM 链路
    print("[4/5] Testing /v1/chat/completions (full LLM pipeline)")
    chat_payload = {
        "model": "qwen2.5:3b",
        "messages": [{"role": "user", "content": "你好"}],
        "stream": False,
        "max_tokens": 50,
    }
    results["chat_completions"] = measure_endpoint(
        base_url, "POST", "/v1/chat/completions",
        payload=chat_payload,
        n_requests=max(5, n_requests // 4),  # LLM 请求较少，避免耗时过长
        label="chat_completions"
    )

    # 5. Unified /v1/chat — Hermes 智能路由入口
    print("[5/5] Testing /v1/chat (Hermes unified routing entry)")
    unified_payload = {
        "type": "chat",
        "model": "qwen2.5:3b",
        "messages": [{"role": "user", "content": "简单测试"}],
        "stream": False,
    }
    results["v1_chat"] = measure_endpoint(
        base_url, "POST", "/v1/chat",
        payload=unified_payload,
        n_requests=max(5, n_requests // 4),
        label="v1_chat"
    )

    print()
    print("=== Summary ===")
    for key, data in results.items():
        if "error" in data:
            print(f"  {key}: ERROR - {data['error']}")
        else:
            print(f"  {key}: avg={data['avg_ms']}ms subsequent={data['subsequent_avg_ms']}ms "
                  f"p50={data['p50_ms']}ms p95={data['p95_ms']}ms")

    return results


def compare_results(baseline_file: str, feature_file: str) -> Dict:
    """对比两组 benchmark 结果。"""
    with open(baseline_file) as f:
        baseline = json.load(f)
    with open(feature_file) as f:
        feature = json.load(f)

    comparison = {}
    for endpoint in ["health", "ollama_tags", "hermes_stats", "chat_completions", "v1_chat"]:
        b = baseline["results"].get(endpoint, {})
        f = feature["results"].get(endpoint, {})

        if "error" in b or "error" in f:
            comparison[endpoint] = {"status": "error", "baseline_error": b.get("error"), "feature_error": f.get("error")}
            continue

        b_avg = b.get("avg_ms", 0)
        f_avg = f.get("avg_ms", 0)
        b_sub = b.get("subsequent_avg_ms", 0)
        f_sub = f.get("subsequent_avg_ms", 0)
        b_p95 = b.get("p95_ms", 0)
        f_p95 = f.get("p95_ms", 0)

        # 计算提升百分比
        avg_improvement = ((b_avg - f_avg) / b_avg * 100) if b_avg > 0 else 0
        sub_improvement = ((b_sub - f_sub) / b_sub * 100) if b_sub > 0 else 0
        p95_improvement = ((b_p95 - f_p95) / b_p95 * 100) if b_p95 > 0 else 0

        comparison[endpoint] = {
            "baseline_avg_ms": b_avg,
            "feature_avg_ms": f_avg,
            "avg_improvement_pct": round(avg_improvement, 2),
            "baseline_subsequent_ms": b_sub,
            "feature_subsequent_ms": f_sub,
            "subsequent_improvement_pct": round(sub_improvement, 2),
            "baseline_p95_ms": b_p95,
            "feature_p95_ms": f_p95,
            "p95_improvement_pct": round(p95_improvement, 2),
        }

    return comparison


def main():
    parser = argparse.ArgumentParser(description="OpenClaw httpx pool benchmark")
    parser.add_argument("--url", default="http://localhost:8090", help="Base URL (nginx port for K8S, or 8082 for local)")
    parser.add_argument("--output", default=None, help="Output JSON file path")
    parser.add_argument("--n", type=int, default=20, help="Number of requests per endpoint")
    parser.add_argument("--compare", nargs=2, metavar=("BASELINE", "FEATURE"),
                        help="Compare two result files and print improvement percentages")
    parser.add_argument("--branch", default=None, help="Branch name to record in results")

    args = parser.parse_args()

    if args.compare:
        comparison = compare_results(args.compare[0], args.compare[1])
        print("\n=== Performance Comparison ===")
        for endpoint, data in comparison.items():
            if data.get("status") == "error":
                print(f"  {endpoint}: ERROR")
                continue
            print(f"  {endpoint}:")
            print(f"    avg:     {data['baseline_avg_ms']}ms → {data['feature_avg_ms']}ms  ({data['avg_improvement_pct']:+.1f}%)")
            print(f"    subsequent: {data['baseline_subsequent_ms']}ms → {data['feature_subsequent_ms']}ms  ({data['subsequent_improvement_pct']:+.1f}%)")
            print(f"    p95:     {data['baseline_p95_ms']}ms → {data['feature_p95_ms']}ms  ({data['p95_improvement_pct']:+.1f}%)")

        if args.output:
            with open(args.output, "w") as f:
                json.dump({"comparison": comparison}, f, indent=2)
            print(f"\nComparison saved to {args.output}")
        return

    # 获取当前分支名
    branch = args.branch
    if not branch:
        import subprocess
        try:
            branch = subprocess.check_output(["git", "branch", "--show-current"]).decode().strip()
        except Exception:
            branch = "unknown"

    results = run_benchmark(args.url, args.n)

    output = {
        "branch": branch,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "base_url": args.url,
        "n_requests": args.n,
        "results": results,
    }

    if args.output:
        with open(args.output, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\nResults saved to {args.output}")
    else:
        # 默认保存到 tests/results/{branch}.json
        import os
        results_dir = os.path.join(os.path.dirname(__file__), "results")
        os.makedirs(results_dir, exist_ok=True)
        output_file = os.path.join(results_dir, f"{branch}.json")
        with open(output_file, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\nResults saved to {output_file}")


if __name__ == "__main__":
    main()
