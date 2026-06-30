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

### ✅ Playwright E2E — 10/10 PASS (GPUStack 稳定时)

GPUStack 稳定时, 全量 10 项 E2E 100% 通过 (88s): chat / ws_chat / pdf / word / audio 附件 / asr / ws_asr / minio_presign / vision / multimodal 全 ok (vision 正确描述旗帜, qwen3-vl)。

#### 早期出现的间歇性 `connection refused` (已澄清)

首次 E2E 跑出现不稳定 (一次 8/10、一次 3/10、一次 300s 超时), litellm 日志: `dial tcp 192.168.0.151:80: connect: connection refused`。**这不是并发上限**, 而是瞬态可用性问题 (见下节"并发上限澄清")。GPUStack 恢复后, 同一套 E2E 稳定 10/10。

#### 并发上限澄清 (实测, 非推断)

为定位 `connection refused`, 跑了多组探针, **均无法复现失败**:

| 探针 | 结果 |
|------|------|
| 16 并发短请求 (max_tokens=15) | 全 ok (<1s) |
| 6 并发长流式 (max_tokens=200, 推理 prompt) | 全 ok (~7s) |
| 文本↔视觉交替 (deepseek-r1 ↔ qwen3-vl 模型切换) | 全 ok (~500–800ms, 无切换失败) |
| 12 个顺序混合请求 (max_tokens 64–128, 模拟 E2E 负载) | 全 ok (25s, 0 失败) |
| 完整 Playwright E2E (GPUStack 稳定) | 10/10 (88s) |

**结论: 未发现硬性并发上限。** `connection refused` 不是撞并发上限的表现 (撞上限应是 HTTP 429 / 排队 / 变慢, 而非 TCP RST)。它是 GPUStack 瞬态不可用: 端口 80 的网关/上游 vLLM 进程那一刻没有接受连接 (进程重启 / 共享平台其它租户突增 / vLLM worker 崩溃后重启)。一次 E2E 的 25s 流式中断 + 后续 36ms 快速拒绝, 符合"vLLM worker 崩溃→重启中"的形态; 恢复后全绿。

### 与测试服务器部署的预期差异
- 测试服务器 (`feature_deploytoTestServer`) 用**专用 vLLM** (非共享 GPUStack), 无共享租户突增、无瞬态进程重启, 可用性更高; 预期 E2E 稳定 10/10。
- 本地验证已证明: OpenClaw 侧配置 (litellm 路由 + 别名 + proxy 全链路) 对 vLLM 完全兼容, 连通性/性能/E2E 全达标。早期 E2E 间歇失败归因 GPUStack 瞬态可用性, 非 OpenClaw 配置问题。

## 关键文件

| 文件 | 作用 |
|------|------|
| `k8s/local-vllm/02-litellm-config-local-vllm.yaml` | litellm 路由 → http://192.168.0.151/v1 |
| `k8s/deploy-local-vllm.sh` | 注入 key + apply 配置 + 重启 + 连通性检查 |
| `scripts/vllm_perf.py` | 连通性 + TTFT/吞吐 基准 (--kind / --direct) |

## 回滚

恢复本地 Ollama 路由: `kubectl apply -f k8s/02-configmaps.yaml` (覆盖回 ollama litellm 配置) + `kubectl rollout restart deployment/litellm -n openclaw`。
