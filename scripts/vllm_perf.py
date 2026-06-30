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
import json
import os
import sys
import time
import urllib.request
import urllib.error

KIND_BASE = "http://localhost:8090"
DIRECT_BASE = "http://192.168.0.151/v1"
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--direct", action="store_true", help="直连 GPUStack (不经 Kind)")
    ap.add_argument("--kind", action="store_true", help="经本地 Kind nginx 全链路 (默认)")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--no-stream", action="store_true")
    ap.add_argument("--img-url", default=None, help="视觉测试图片 URL (默认: 直连用 data URI 占位, Kind 用 minio)")
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

    do_stream = not args.no_stream
    print(g(f"vLLM 性能基准 — 目标: {base}  模型: text={text_model} vision={vision_model}  rounds={args.rounds} stream={do_stream}"))

    # 连通性
    try:
        dt, payload = post_json(base, key, {"model": text_model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}, timeout=60)
        print(g(f"连通性 OK (文本, {dt*1000:.0f}ms)"))
    except Exception as e:
        print(r(f"连通性 FAIL: {e}")); sys.exit(1)

    tl, tt, tp = bench_chat(base, key, text_model, args.rounds, do_stream)
    summary("文本", tl, tt, tp)
    vl, vt, vp = bench_vision(base, key, vision_model, args.rounds, do_stream, img_url)
    summary("视觉", vl, vt, vp)
    print()


if __name__ == "__main__":
    main()
