#!/usr/bin/env python3
"""向现有 LiteLLM 注册 OpenClaw 所需模型 (复用模式)。

复用现有 litellm 时, 它不知道 qwen2.5/llava → GPUStack 的路由 (那是我们 configmap 里的)。
本脚本经 litellm 的 /model/info (查已注册) + /model/new (新增, 幂等) 把模型注册到现有 litellm 的 DB。

用法 (deploy-server.sh 复用模式会自动调用; 也可手动):
  # port-forward 现有 litellm (default ns)
  kubectl -n default port-forward svc/litellm 4000:4000
  LITELLM_MASTER_KEY=sk-xxx VLLM_API_KEY=gpustack_xxx \
    python3 scripts/litellm_configure.py --base http://localhost:4000

模型映射 (与 02-litellm-config-server.yaml 一致):
  qwen2.5 / deepseek-r1-distill-qwen-32b → 文本 (http://192.168.0.151/v1)
  llava   / qwen3-vl-32b-instruct        → 视觉 (http://192.168.0.151/v1)
  funasr                                 → 本地 FunASR (http://funasr:8199/v1, 仅同集群可解析)

退出码: 0 全部就绪; 非0 致命错误 (个别模型注册失败不致命, 仅 warn)。
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error

DEFAULT_VLLM_BASE = "http://192.168.0.151/v1"
DEFAULT_FUNASR_BASE = "http://funasr:8199/v1"   # 集群内 DNS (openclaw ns)

# (model_name, litellm_model, api_base, timeout)
MODELS = [
    ("deepseek-r1-distill-qwen-32b", "openai/deepseek-r1-distill-qwen-32b", DEFAULT_VLLM_BASE, 300),
    ("qwen2.5",                      "openai/deepseek-r1-distill-qwen-32b", DEFAULT_VLLM_BASE, 300),
    ("qwen3-vl-32b-instruct",        "openai/qwen3-vl-32b-instruct",        DEFAULT_VLLM_BASE, 300),
    ("llava",                        "openai/qwen3-vl-32b-instruct",        DEFAULT_VLLM_BASE, 300),
    ("funasr",                       "openai/whisper-1",                    DEFAULT_FUNASR_BASE, 60),
]


def c(s): return f"\033[36m{s}\033[0m"
def g(s): return f"\033[32m{s}\033[0m"
def y(s): return f"\033[33m{s}\033[0m"
def r(s): return f"\033[31m{s}\033[0m"


def http(method, base, path, key, body=None, timeout=30):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json",
        **({"Authorization": f"Bearer {key}"} if key else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or "{}")
        except Exception:
            payload = {}
        return e.code, payload
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def list_registered(base, key):
    """返回已注册的 model_name 集合 (litellm /model/info)。"""
    status, payload = http("GET", base, "/model/info", key, timeout=30)
    if status != 200:
        return None, payload
    names = set()
    for entry in payload.get("data", []) or []:
        n = entry.get("model_name")
        if n:
            names.add(n)
    return names, None


def add_model(base, key, model_name, litellm_model, api_base, timeout, vllm_key):
    """POST /model/new 注册模型; 幂等 (已存在则视为成功)。"""
    body = {
        "model_name": model_name,
        "litellm_params": {
            "model": litellm_model,
            "api_base": api_base,
            "timeout": timeout,
        },
    }
    # funasr 无需 vllm key; vllm 模型带 api_key
    if "192.168.0.151" in api_base:
        body["litellm_params"]["api_key"] = vllm_key or "EMPTY"
    status, payload = http("POST", base, "/model/new", key, body, timeout=30)
    if status in (200, 201):
        return True, None
    # 幂等: 已存在视为成功 (litellm 通常返回 400/409 + "already exists" 类似信息)
    msg = json.dumps(payload, ensure_ascii=False)[:200]
    if status in (400, 409) and any(k in msg.lower() for k in ("exist", "already", "duplicate")):
        return True, "already-exists"
    return False, f"HTTP {status}: {msg}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("LITELLM_BASE", "http://localhost:4000"),
                    help="litellm base URL (默认 http://localhost:4000, 经 port-forward)")
    ap.add_argument("--master-key", default=os.environ.get("LITELLM_MASTER_KEY", ""),
                    help="litellm master key (LITELLM_MASTER_KEY env)")
    ap.add_argument("--vllm-key", default=os.environ.get("VLLM_API_KEY", ""),
                    help="GPUStack vLLM api key (VLLM_API_KEY env)")
    args = ap.parse_args()

    if not args.master_key:
        print(r("缺少 LITELLM_MASTER_KEY (--master-key 或 env)")); sys.exit(2)

    print(g(f"配置现有 LiteLLM @ {args.base} (注册 {len(MODELS)} 个模型)"))

    # 连通性 + 列已注册
    registered, err = list_registered(args.base, args.master_key)
    if registered is None:
        print(r(f"无法查询 /model/info (master key 错? litellm 不在线?): {err}"))
        sys.exit(1)
    print(y(f"已注册模型: {sorted(registered) or '(无)'}"))

    if not args.vllm_key:
        print(y("⚠ 未提供 VLLM_API_KEY, vLLM 模型将用 EMPTY key (GPUStack 需鉴权时会 401)"))

    n_ok = n_exist = n_fail = 0
    for name, litellm_model, api_base, timeout in MODELS:
        if name in registered:
            print(f"  {g('skip')} {name:32s} 已注册")
            n_exist += 1
            continue
        ok, info = add_model(args.base, args.master_key, name, litellm_model, api_base, timeout, args.vllm_key)
        if ok:
            tag = "added" if info is None else "added(existed)"
            print(f"  {g('ok')}   {name:32s} {tag}  → {litellm_model} @ {api_base}")
            n_ok += 1
        else:
            print(f"  {r('fail')} {name:32s} {info}")
            n_fail += 1

    # 复查
    registered2, _ = list_registered(args.base, args.master_key)
    missing = [n for n, *_ in MODELS if n not in (registered2 or set())]
    print(g(f"\n完成: 新增 {n_ok}, 已存在 {n_exist}, 失败 {n_fail}"))
    if missing:
        print(r(f"⚠ 仍未注册: {missing} (可能需在 litellm UI 手动加, 或现有 litellm 未开 STORE_MODEL_IN_DB)"))
    else:
        print(g("全部模型就绪 ✓"))
    print(c(f"LiteLLM UI: {args.base}/ui/   (或经 nginx: http://<节点IP>:30080/ui/)"))
    sys.exit(0 if not missing else 1)


if __name__ == "__main__":
    main()
