# 下一步工作 (feature_localUsingvLLM / 服务器移植)

> 背景:测试服务器 4×H100 (2 卡 deepseek-r1-distill-qwen-32b tp2 + 2 卡 qwen3-vl-32b-instruct tp2, 两模型常驻), vLLM 经 GPUStack 暴露于 `http://192.168.0.151/v1`。服务器 K8S 尚未 ready, 当前在本机 Kind 经 GPUStack 测试。

## 当前进行中

### 任务 1: H100 容量曲线压测 (本地经 GPUStack) ★
扩展 `scripts/vllm_perf.py`,把 H100 真实容量摸清,作为服务器 K8S 配置(litellm `max_parallel_requests`、vLLM `max_num_seqs`、HPA 目标、超时)的输入。

- **并发阶梯压测 `--ramp`**:固定 prompt,并发 10/20/40/80/160,测成功率、TTFT 退化曲线、聚合吞吐(tokens/s)、排队拐点(延迟陡升)。文本 + 视觉两组。
- **长上下文测试 `--longctx`**:输入 token 1k/4k/16k/32k,测 TTFT vs 输入长度(prefill 成本)、是否正确处理、`prompt_tokens` 上报。
- **产出**:容量曲线表 → 写入本文档"实测结果"段,作为服务器配置依据。

## 待办(按优先级)

### 任务 2: 把 feature_deploytoTestServer 改为"直连 GPUStack"版(方案 A)
vLLM 在 K8S 外(GPUStack),服务器 K8S 只跑 OpenClaw 自身 pod,litellm 直连 `http://192.168.0.151/v1` + key。

- `02-litellm-config-server.yaml`:api_base 改 `http://192.168.0.151/v1`,api_key `os.environ/VLLM_API_KEY`(复用 local-vllm 配置)。
- 删除/改 `13-inference-endpoints.yaml`:不再桥接 in-cluster vLLM;若跨网段用 ExternalName 指向 192.168.0.151,同网段直接 IP。
- 保留占位 ollama Service(nginx 启动兼容)。
- `VLLM_API_KEY` 走 secret(不落 git)。
- 服务器 K8S ready 后直接 `kubectl apply`。

### 任务 3: 其余本地测试(任务 1 后按需)
- reasoning 输出特性:max_tokens 是否严格执行、reasoning_content vs content、推理/答案 token 比 → 定 `STRIP_REASONING_TAGS` 与超时。
- 视觉鲁棒性:多分辨率/多图/OCR/文档图。
- soak 稳定性:5–10min 混合流量,捕捉瞬态 connection refused、测自愈时间。
- 链路延迟拆解:`--kind` vs `--direct` 每跳开销。
- 故障注入:中途杀连接,验证 litellm retry/proxy 错误响应。

### 任务 4: 服务器 K8S 移植准备(集群 ready 后)
- **基础设施**:kubeadm 集群;镜像 registry(5 个 openclaw-* 镜像);网络(pod→192.168.0.151 可达,NodePort 30080 或 Ingress+TLS);MinIO/postgres PVC + StorageClass + 备份。
- **配置调优**:超时统一 300s+(deepseek-r1 长推理);reasoning 策略;并发参数(据任务 1 数据);FunASR CPU 部署。
- **安全**:换默认密钥(sk-litellm-local/minioadmin);VLLM_API_KEY 用 SealedSecret/External Secrets;TLS + NetworkPolicy。
- **可观测**:litellm prometheus → Grafana;日志聚合;HPA(清单已有,设 minReplicas)。
- **流程**:CI/CD 镜像流水线;部署自动化 + 回滚;`scripts/minio_setup.py` 灌数据;服务器上跑 E2E(`DASHBOARD_URL` 指向服务器)+ perf + 压测对照本地基准。
- **运维**:runbook(部署/扩缩/排错/轮换 key/更新模型);DR(PV snapshot、postgres 备份恢复);容量规划 SLO。

## 实测结果 (2026-06-30, 本地 Kind 经 GPUStack 192.168.0.151)

> 环境: 服务器 4×H100, 2 卡 deepseek-r1-distill-qwen-32b (tp2) + 2 卡 qwen3-vl-32b-instruct (tp2), 两模型常驻。本机 Kind 经 nginx→proxy→litellm→GPUStack 全链路。

### 1. 并发阶梯压测 (`vllm_perf.py --ramp`)

**文本 deepseek-r1-distill-qwen-32b** (max_tokens=50, 流式):

| 并发 | 成功率 | avgTTFT | p95TTFT | 聚合吞吐 | wall |
|---:|:---:|---:|---:|---:|---:|
| 10 | 100% | 609ms | 644ms | 404 t/s | 1.2s |
| 20 | 100% | 865ms | 892ms | 623 t/s | 1.6s |
| 40 | 100% | 1297ms | 2004ms | **759 t/s** ←峰值 | 2.6s |
| 80 | 100% | 2555ms | 4854ms | 728 t/s | 5.5s |
| 160 | 100% | 5640ms | 9459ms | 181 t/s ←过载崩 | 44.3s |

**视觉 qwen3-vl-32b-instruct** (max_tokens=40, 流式):

| 并发 | 成功率 | avgTTFT | p95TTFT | 聚合吞吐 | wall |
|---:|:---:|---:|---:|---:|---:|
| 10 | 100% | 766ms | 849ms | 331 t/s | 1.2s |
| 20 | 100% | 861ms | 1024ms | 568 t/s | 1.4s |
| 40 | 100% | 1419ms | 2181ms | 622 t/s | 2.6s |
| 80 | 100% | 2560ms | 4354ms | **663 t/s** ←峰值 | 4.8s |

**结论**:
- **全程 100% 成功, 无 connection refused** — 再次确认之前 E2E 间歇失败是瞬态, 非并发上限。
- **低延迟甜点 (TTFT<1s): 并发 ≤20** (两模型均 p95<1.1s)。
- **排队拐点: ~40 并发** (TTFT 开始陡升, 但吞吐仍爬升)。
- **聚合吞吐峰值: 文本 ~760 t/s @40并发, 视觉 ~660 t/s @80并发** (continuous batching 上限)。
- **过载点: 文本 160 并发** (吞吐崩至 181 t/s, p95 TTFT 9.5s — KV cache 抖动/排队风暴)。
- 单流吞吐 (无并发): 文本 ~46 t/s, 视觉 ~34 t/s; 高并发下每流吞吐下降换聚合吞吐 (符合 vLLM continuous batching 预期)。

### 2. 长上下文测试 (`vllm_perf.py --longctx`)

**文本 deepseek-r1** (输出 max_tokens=20, 非流式):

| 目标 tokens | 实际 prompt_tokens | 总延迟 (prefill) | 状态 |
|---:|---:|---:|:---:|
| 1000 | 944 | 581ms | ok |
| 4000 | 3644 | 797ms | ok |
| 6000 | 5444 | 803ms | ok |
| 8000 | 7244 | 969ms | ok |
| 10000 | — | — | **400 (超 max_model_len)** |
| 16000 | — | — | 400 |
| 32000 | — | — | 400 |

**结论**:
- **GPUStack 的 vLLM `max_model_len` ≈ 8192** (8k 输入 ok, 10k → 400 Bad Request)。
- deepseek-r1-distill-qwen-32b 原生支持 128k 上下文, 但 GPUStack 启动时把 `max_model_len` 限制到 ~8k (省 KV cache 显存)。**这是当前 GPUStack 配置的硬约束**。
- prefill 成本: 8k 输入仅 ~970ms (H100 算力充裕), 长上下文延迟不是瓶颈, 瓶颈是 `max_model_len` 配置。
- **服务器部署提示**: 若业务需长上下文 (大 PDF 总结、长对话历史), 服务器侧 vLLM 需调高 `max_model_len` (2×H100 80GB 跑 32B 可支持 32k+); 在 GPUStack 上则受限于其 ~8k 配置。

### 3. 服务器配置输入 (据上述数据)

- **litellm `max_parallel_requests`**: 文本/视觉各设 **40** (吞吐峰值点); 延迟敏感场景设 **20**。
- **vLLM `max_num_seqs`** (若可调 GPUStack): ≥40 以利用 continuous batching; 过高 (>80) 收益递减且 TTFT 退化。
- **超时**: deepseek-r1 高并发下 p95 TTFT 可达 5-9s, 单请求总时延 (含推理) 可数十秒 → nginx/litellm/proxy 超时 **≥300s**; E2E `wait_for_function` ≥600s 或按服务器负载调。
- **HPA**: litellm/proxy minReplicas 按预期 QPS 设; 单模型 ~750 t/s 文本 / ~660 t/s 视觉为单实例容量, 超此需多 replica (但 vLLM 在 K8S 外, litellm 多 replica 共用同一 GPUStack, 受 vLLM 上限约束)。
- **max_model_len**: 评估业务最长上下文, 服务器 vLLM 调到匹配值 (≥业务需求, 平衡 KV cache 显存)。

