#!/usr/bin/env python3
"""vLLM 连通性 + 性能基准 (feature_localUsingvLLM)。

测量 chat (deepseek-r1-distill-qwen-32b) 与 vision (qwen3-vl-32b-instruct):
  - 连通性 (HTTP 200 + 非空 content)
  - 非流式总延迟
  - 流式 TTFT (time to first token) 与 tokens/s (吞吐)
  - 多轮平均

两种目标:
  --kind   (默认) 经本地 Kind nginx (localhost:8090) 全链路: nginx→proxy→litellm→GPUStack
  --direct 直连 GPUStack (http://192.168.0.151/v1), 对比网络/预处理开销

用法:
  python3 scripts/vllm_perf.py                 # 经本地 Kind 全链路, 3 轮
  python3 scripts/vllm_perf.py --direct        # 直连 GPUStack
  python3 scripts/vllm_perf.py --rounds 5
  python3 scripts/vllm_perf.py --no-stream     # 只测非流式
  VLLM_API_KEY=gpustack_xxx python3 scripts/vllm_perf.py --direct   # 直连需 key

依赖: requests (本机有)。流式用 SSE 解析。
"""
import argparse
import concurrent.futures
import json
import os
import sys
import time
import urllib.request
import urllib.error

KIND_BASE = os.environ.get("KIND_BASE", "http://localhost:8090")
DIRECT_BASE = os.environ.get("DIRECT_BASE", "http://192.168.0.151")
TEXT_MODEL = "deepseek-r1-distill-qwen-32b"
VISION_MODEL = "qwen3-vl-32b-instruct"
# 经 Kind 时用别名 (litellm 路由), 直连时用真实模型名
TEXT_MODEL_KIND = "qwen2.5"
VISION_MODEL_KIND = "llava"  # proxy 含图自动切 llava; 直发 llava 也行


def c(s): return f"\033[36m{s}\033[0m"
def g(s): return f"\033[32m{s}\033[0m"
def y(s): return f"\033[33m{s}\033[0m"
def r(s): return f"\033[31m{s}\033[0m"


def post_json(base, key, body, timeout):
    url = base.rstrip("/") + "/v1/chat/completions"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        **({"Authorization": f"Bearer {key}"} if key else {}),
    })
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    return (time.time() - t0), payload


def stream_sse(base, key, body, timeout):
    """返回 (ttft_s, total_s, token_count, content)."""
    url = base.rstrip("/") + "/v1/chat/completions"
    body = dict(body, stream=True)
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        **({"Authorization": f"Bearer {key}"} if key else {}),
    })
    t0 = time.time()
    ttft = None
    tokens = 0
    content = ""
    resp = urllib.request.urlopen(req, timeout=timeout)
    buf = b""
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data:"):
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    continue
                try:
                    d = json.loads(payload)
                except Exception:
                    continue
                delta = (d.get("choices") or [{}])[0].get("delta", {})
                t = delta.get("content") or ""
                if t:
                    if ttft is None:
                        ttft = time.time() - t0
                    tokens += 1
                    content += t
    total = time.time() - t0
    return (ttft if ttft is not None else total), total, tokens, content


def bench_chat(base, key, model, rounds, do_stream):
    print(c(f"\n[文本] {model} @ {base}"))
    lat, ttfts, tps_list = [], [], []
    for i in range(rounds):
        body = {"model": model, "messages": [{"role": "user", "content": "用一句话介绍你自己。"}], "max_tokens": 80}
        try:
            if do_stream:
                ttft, total, toks, content = stream_sse(base, key, body, timeout=120)
                ttfts.append(ttft)
                if total > 0 and toks > 0:
                    tps_list.append(toks / total)
                print(f"  轮{i+1}: TTFT={ttft*1000:.0f}ms total={total*1000:.0f}ms tokens={toks} tps={toks/total:.1f}")
            else:
                dt, payload = post_json(base, key, body, timeout=120)
                lat.append(dt)
                content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
                print(f"  轮{i+1}: total={dt*1000:.0f}ms content={(content or '')[:60]!r}")
        except Exception as e:
            print(r(f"  轮{i+1}: 失败 {e}"))
    return lat, ttfts, tps_list


def bench_vision(base, key, model, rounds, do_stream, img_url):
    print(c(f"\n[视觉] {model} @ {base}"))
    lat, ttfts, tps_list = [], [], []
    for i in range(rounds):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "图里有什么?中文简短"},
                {"type": "image_url", "image_url": {"url": img_url}},
            ]}],
            "max_tokens": 60,
        }
        try:
            if do_stream:
                ttft, total, toks, content = stream_sse(base, key, body, timeout=120)
                ttfts.append(ttft)
                if total > 0 and toks > 0:
                    tps_list.append(toks / total)
                print(f"  轮{i+1}: TTFT={ttft*1000:.0f}ms total={total*1000:.0f}ms tokens={toks} tps={toks/total:.1f} content={content[:50]!r}")
            else:
                dt, payload = post_json(base, key, body, timeout=120)
                lat.append(dt)
                content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
                print(f"  轮{i+1}: total={dt*1000:.0f}ms content={(content or '')[:60]!r}")
        except Exception as e:
            print(r(f"  轮{i+1}: 失败 {e}"))
    return lat, ttfts, tps_list


def summary(name, lat, ttfts, tps):
    def avg(x): return sum(x) / len(x) if x else 0
    def mn(x): return min(x) if x else 0
    def mx(x): return max(x) if x else 0
    print(y(f"  ── {name} 汇总 ──"))
    if lat:
        print(f"    非流式 latency: avg={avg(lat)*1000:.0f}ms min={mn(lat)*1000:.0f}ms max={mx(lat)*1000:.0f}ms (n={len(lat)})")
    if ttfts:
        print(f"    流式 TTFT:      avg={avg(ttfts)*1000:.0f}ms min={mn(ttfts)*1000:.0f}ms max={mx(ttfts)*1000:.0f}ms (n={len(ttfts)})")
    if tps:
        print(f"    流式 吞吐:      avg={avg(tps):.1f} tokens/s min={mn(tps):.1f} max={mx(tps):.1f}")


def ramp_test(base, key, model, levels, max_tokens, image_url=None, vision=False):
    """并发阶梯压测: 固定 prompt, 逐级加并发, 找排队拐点与容量上限。"""
    label = f"{model} (视觉)" if vision else model
    print(c(f"\n[并发阶梯] {label} @ {base}  max_tokens={max_tokens}  stream=True"))
    print(f"  {'并发':>4} | {'成功率':>6} | {'avgTTFT':>9} | {'p95TTFT':>9} | {'聚合吞吐':>8} | {'wall':>6} | 备注")
    print("  " + "-" * 72)
    for n in levels:
        if vision:
            body_base = {"model": model, "stream": True, "max_tokens": max_tokens,
                         "messages": [{"role": "user", "content": [
                             {"type": "text", "text": "描述图片"},
                             {"type": "image_url", "image_url": {"url": image_url}}]}]}
        else:
            body_base = {"model": model, "stream": True, "max_tokens": max_tokens,
                         "messages": [{"role": "user", "content": "用一句话介绍你自己。"}]}

        def one(_):
            return stream_sse(base, key, body_base, timeout=180)

        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(n) as ex:
            results = list(ex.map(one, range(n)))
        wall = time.time() - t0
        ok = [r for r in results if r[2] > 0]   # tokens>0 视为成功
        fails = [r for r in results if r[2] == 0]
        ttfts = [r[0] for r in ok]
        toks = sum(r[2] for r in ok)

        def pct(p):
            if not ttfts:
                return 0
            s = sorted(ttfts)
            return s[min(len(s) - 1, int(len(s) * p))]

        agg = toks / wall if wall > 0 else 0
        succ = len(ok) / n
        note = ""
        if fails:
            # 取首个失败原因样例
            sample = next((r for r in fails if len(r) > 3), None)
            note += f"{len(fails)}失败 "
            if sample and len(sample) > 3:
                note += f"({str(sample)[:30]}) "
        if ttfts and pct(0.95) >= 2.0:
            note += "TTFT升高→疑似排队"
        avg_ttft = (sum(ttfts) / len(ttfts) * 1000) if ttfts else 0
        print(f"  {n:>4} | {succ*100:>5.0f}% | {avg_ttft:>8.0f}ms | {pct(0.95)*1000:>8.0f}ms | {agg:>7.1f}t/s | {wall:>5.1f}s | {note}")
        time.sleep(3)   # 级间冷却, 让 vLLM 队列排空


def long_context_test(base, key, model, token_levels, max_tokens=20):
    """长上下文测试: 输入 token 阶梯, 测 prefill 成本与上下文上限。"""
    print(c(f"\n[长上下文] {model} @ {base}  输出 max_tokens={max_tokens}  stream=False"))
    # 中文填充段 (~60 字), 重复到目标 token 量 (近似 1 token ≈ 1.5 字)
    para = "量子计算利用叠加与纠缠原理进行计算,理论上可在特定问题上实现指数级加速,与传统二进制计算有本质区别。"
    print(f"  {'目标tokens':>10} | {'实际prompt_tk':>13} | {'总延迟':>8} | {'状态':>6} | 备注")
    print("  " + "-" * 60)
    for t in token_levels:
        repeat = max(1, int(t * 1.5 / len(para)) + 1)
        prompt = "请用一句话总结以下内容的核心: " + para * repeat
        body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "stream": False}
        try:
            dt, payload = post_json(base, key, body, timeout=180)
            usage = payload.get("usage", {})
            pt = usage.get("prompt_tokens", 0)
            content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
            status = "ok" if content else "EMPTY"
            note = f"prefill~{dt*1000:.0f}ms" + (f" (约{pt/t:.1f}x目标)" if t and pt else "")
            print(f"  {t:>10} | {pt:>13} | {dt*1000:>7.0f}ms | {status:>6} | {note}")
        except urllib.error.HTTPError as e:
            print(f"  {t:>10} | {'-':>13} | {'-':>8} | {f'HTTP{e.code}':>6} | {e.read()[:40]!r}")
        except Exception as e:
            print(f"  {t:>10} | {'-':>13} | {'-':>8} | {'ERR':>6} | {str(e)[:40]}")
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--direct", action="store_true", help="直连 GPUStack (不经 Kind)")
    ap.add_argument("--kind", action="store_true", help="经本地 Kind nginx 全链路 (默认)")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--no-stream", action="store_true")
    ap.add_argument("--img-url", default=None, help="视觉测试图片 URL (默认: 直连用 data URI 占位, Kind 用 minio)")
    # 并发阶梯压测
    ap.add_argument("--ramp", action="store_true", help="并发阶梯压测 (找排队拐点/容量上限)")
    ap.add_argument("--ramp-vision", action="store_true", help="ramp 时额外测视觉模型并发")
    ap.add_argument("--levels", default="10,20,40,80,160", help="ramp 并发级别 (逗号分隔)")
    ap.add_argument("--ramp-mt", type=int, default=50, help="ramp 单请求 max_tokens")
    # 长上下文测试
    ap.add_argument("--longctx", action="store_true", help="长上下文测试 (prefill 成本)")
    ap.add_argument("--longctx-levels", default="1000,4000,16000,32000", help="长上下文输入 token 阶梯")
    ap.add_argument("--longctx-mt", type=int, default=20, help="longctx 输出 max_tokens")
    args = ap.parse_args()

    if args.direct:
        base, key = DIRECT_BASE, os.environ.get("VLLM_API_KEY", "")
        if not key:
            print(r("直连需 VLLM_API_KEY 环境变量")); sys.exit(2)
        text_model, vision_model = TEXT_MODEL, VISION_MODEL
        # 直连视觉: 用一个公网/本机图片 URL; GPUStack 需能取图。用 minio 经 Kind 暴露的代理路径。
        img_url = args.img_url or "http://localhost:8090/v1/minio/openclaw-test/test_image.png"
    else:
        base, key = KIND_BASE, ""  # 经 Kind nginx, proxy 注入 master key
        text_model, vision_model = TEXT_MODEL_KIND, VISION_MODEL_KIND
        img_url = args.img_url or "http://minio:9000/openclaw-test/test_image.png"

    # 连通性
    try:
        dt, payload = post_json(base, key, {"model": text_model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}, timeout=60)
        print(g(f"连通性 OK (文本, {dt*1000:.0f}ms) — 目标 {base}"))
    except Exception as e:
        print(r(f"连通性 FAIL: {e}")); sys.exit(1)

    # ---- 并发阶梯压测 ----
    if args.ramp:
        levels = [int(x) for x in args.levels.split(",")]
        ramp_test(base, key, text_model, levels, args.ramp_mt, image_url=img_url, vision=False)
        if args.ramp_vision:
            ramp_test(base, key, vision_model, levels, args.ramp_mt, image_url=img_url, vision=True)
        print()
        return

    # ---- 长上下文测试 ----
    if args.longctx:
        levels = [int(x) for x in args.longctx_levels.split(",")]
        long_context_test(base, key, text_model, levels, max_tokens=args.longctx_mt)
        print()
        return

    # ---- 默认: 文本 + 视觉基准 ----
    do_stream = not args.no_stream
    print(g(f"vLLM 性能基准 — 模型: text={text_model} vision={vision_model}  rounds={args.rounds} stream={do_stream}"))
    tl, tt, tp = bench_chat(base, key, text_model, args.rounds, do_stream)
    summary("文本", tl, tt, tp)
    vl, vt, vp = bench_vision(base, key, vision_model, args.rounds, do_stream, img_url)
    summary("视觉", vl, vt, vp)
    print()


if __name__ == "__main__":
    main()
