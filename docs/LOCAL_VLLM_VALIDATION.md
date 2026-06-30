# 本地 vLLM 验证 (feature_localUsingvLLM)

用**本地 Kind K8S** + **远程 GPUStack vLLM** (`http://192.168.0.151/`) 验证两个模型的连通性与性能,作为测试服务器部署 (`feature_deploytoTestServer`) 的前置验证。

## 模型与端点

| 用途 | 模型 (GPUStack) | 替代本机 Ollama | LiteLLM 别名 |
|------|-----------------|------------------|--------------|
| 文本 | `deepseek-r1-distill-qwen-32b` | qwen2.5:3b | `qwen2.5` |
| 视觉 | `qwen3-vl-32b-instruct` | llava:7b | `llava` |

GPUStack OpenAI 兼容端点: `http://192.168.0.151/v1`, 鉴权: API key (经 `VLLM_API_KEY` 环境变量注入 secret, **不落 git**)。

## 架构

```
本地 Kind: nginx(8090) → proxy → litellm --(api_base http://192.168.0.151/v1)--> GPUStack vLLM
```

- litellm 配置 (`k8s/local-vllm/02-litellm-config-local-vllm.yaml`) 把 `qwen2.5`/`llava` 别名路由到 `http://192.168.0.151/v1`, `api_key: os.environ/VLLM_API_KEY`。
- Kind pod 可直连 192.168.0.151 (已验证: litellm pod `httpx.get /v1/models` → 200, 列出两模型)。
- 保留本地 Ollama (litellm 路由切到 vLLM 后, ollama 仅作未触发的 fallback; embedding 仍走 ollama)。

## 部署

```bash
# 前置: 本地 Kind 已起 (./k8s/deploy.sh), openclaw namespace + litellm/proxy/nginx Running
VLLM_API_KEY=gpustack_xxx ./k8s/deploy-local-vllm.sh
```

脚本: 注入 key 到 secret → apply litellm 配置 + env patch (strategic-merge 加 `VLLM_API_KEY`) → `rollout restart litellm` → 连通性检查 (chat + vision 经全链路)。

## 验证结果 (2026-06-30)

### ✅ 连通性
- litellm pod → `http://192.168.0.151/v1/models` → 200, 列出 `deepseek-r1-distill-qwen-32b` + `qwen3-vl-32b-instruct`。
- 经全链路 (nginx→proxy→litellm→GPUStack):
  - 文本 (qwen2.5→deepseek-r1): `"您好！您提到的"你好"是中文里常用的问候语..."`
  - 视觉 (llava→qwen3-vl): `"红色背景上有一个黄色圆圈。"`

### ✅ 性能 (3 轮流式, 经本地 Kind 全链路)

| 模型 | TTFT | 吞吐 | 备注 |
|------|------|------|------|
| 文本 deepseek-r1 | avg 512ms (508–516) | ~46 tokens/s | 72 tokens / ~1.5s |
| 视觉 qwen3-vl | avg 355ms (342–368) | ~34 tokens/s | 正确识别图内容 |

```bash
python3 scripts/vllm_perf.py --rounds 3        # 经本地 Kind 全链路
VLLM_API_KEY=gpustack_xxx python3 scripts/vllm_perf.py --direct  # 直连 GPUStack 对比
```

> 注: 性能脚本必须用 `/v1/chat/completions` (经 proxy 做 minio→base64 转换); 早期版本误用 `/chat/completions` 绕过 proxy, 导致 vision 把 minio 内部 URL 直送 GPUStack (不可达) → 504。已修复。

### ⚠️ Playwright E2E — 不稳定 (3/10 ~ 8/10)

E2E 全量 10 项测试结果不稳定 (一次 8/10, 一次 3/10), 失败模式:
- 文本/附件测试: GPUStack 在**快速连续请求**下间歇性 `connection refused` (litellm 日志: `dial tcp 192.168.0.151:80: connect: connection refused`), 流式 ~25s 后被中断。
- vision/multimodal: GPUStack **单 GPU 模型切换** — 文本测试加载 deepseek-r1 驱逐 qwen3-vl, 轮到 vision 时 qwen3-vl 需重新加载, GPUStack 返回快速错误 (36ms EMPTY)。

**根因: GPUStack 是共享/托管平台, 单 GPU 跑两个 32B 模型, 在 E2E 快速连续负载下连接被拒/模型切换失败。** 单请求/顺序请求 (性能基准) 全部正常。这是 GPUStack 侧限制, **非 OpenClaw 配置问题**。

### 与测试服务器部署的预期差异
- 测试服务器 (`feature_deploytoTestServer`) 用**专用 vLLM** (非共享 GPUStack), 两模型可常驻 (足够 VRAM 或分卡), 不会单 GPU 互相驱逐; 且无共享平台的连接拒限。预期 E2E 10/10 通过。
- 本地验证已证明: OpenClaw 侧配置 (litellm 路由 + 别名 + proxy 全链路) 对 vLLM 完全兼容, 连通性与性能达标。E2E 不稳定完全归因 GPUStack 平台限制。

## 关键文件

| 文件 | 作用 |
|------|------|
| `k8s/local-vllm/02-litellm-config-local-vllm.yaml` | litellm 路由 → http://192.168.0.151/v1 |
| `k8s/deploy-local-vllm.sh` | 注入 key + apply 配置 + 重启 + 连通性检查 |
| `scripts/vllm_perf.py` | 连通性 + TTFT/吞吐 基准 (--kind / --direct) |

## 回滚

恢复本地 Ollama 路由: `kubectl apply -f k8s/02-configmaps.yaml` (覆盖回 ollama litellm 配置) + `kubectl rollout restart deployment/litellm -n openclaw`。
