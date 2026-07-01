# 星脑AI服务使用指南

> 状态：可用  
> 版本：v1.1（对外版）  
> 更新时间：2026-07-01  
> 维护方：Starbrain AI Platform Team  
> 适用对象：内部研发团队

---

## 1. 概述

星脑AI服务提供大语言模型推理能力，兼容 OpenAI API 格式，支持文本对话、图片理解、语音识别、流式输出及文档附件处理。服务运行在 4 张 H100 GPU 上，已上线两个模型，可满足推理、代码、多模态分析等多种场景。

| 服务 | 模型 | 能力 | 状态 | 适用场景 |
|------|------|------|------|----------|
| 文本推理 | `deepseek-r1-distill-qwen-32b` | 文本 | 可用 | 推理、代码、复杂问答、附件总结 |
| 多模态/视觉 | `qwen3-vl-32b-instruct` | 文本 + 图片 | 可用 | 图片理解、视觉问答、多模态分析 |
| 语音识别 (ASR) | `funasr` (sensevoice) | 音频转文字 | 可用 | 语音转写、语音→LLM 级联回答 |
| 流式对话 | 同文本/视觉 | SSE + WebSocket | 可用 | 实时逐字输出、实时语音流 |

---

## 2. 接入信息

| 项目 | 说明 |
|------|------|
| 服务地址 | `http://192.168.0.151:30080` |
| 对话/补全接口 | `POST /v1/chat/completions`（OpenAI 兼容） |
| ASR 接口 | `POST /v1/audio/transcriptions` |
| 流式 WebSocket | `ws://192.168.0.151:30080/v1/stream/ws`（对话）、`ws://192.168.0.151:30080/v1/stream/asr`（实时语音） |
| 认证方式 | 内网调用**无需认证**；如需鉴权请联系管理员 |
| API 格式 | 兼容 OpenAI API |
| 网络范围 | 内网访问 |
| 支持方式 | 普通请求和流式请求 |

> 所有调用请求使用 OpenAI 兼容格式，`model` 字段填模型名称即可。内网调用无需 API Key。

---

## 3. 文本场景

文本对话使用 `deepseek-r1-distill-qwen-32b`（推理模型，输出可能含推理过程）。

### 3.1 非流式（curl）

```bash
curl http://192.168.0.151:30080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-r1-distill-qwen-32b",
    "messages": [
      {"role": "user", "content": "请用三句话解释 Kubernetes 调度。"}
    ],
    "max_tokens": 512,
    "stream": false
  }'
```

### 3.2 Python（openai SDK）

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://192.168.0.151:30080/v1",
    api_key="not-needed",  # 内网无需 key, SDK 字段必填, 任意值即可
)

resp = client.chat.completions.create(
    model="deepseek-r1-distill-qwen-32b",
    messages=[{"role": "user", "content": "请用三句话解释 Kubernetes 调度。"}],
    max_tokens=512,
)
print(resp.choices[0].message.content)
```

### 3.3 返回示例

```json
{
  "id": "chatcmpl-...",
  "model": "deepseek-r1-distill-qwen-32b",
  "choices": [
    {"finish_reason": "stop", "index": 0,
     "message": {"role": "assistant", "content": "..."}}
  ],
  "usage": {"prompt_tokens": 10, "completion_tokens": 64, "total_tokens": 74}
}
```

> `deepseek-r1-distill-qwen-32b` 是推理模型，输出可能包含思维链推理过程，属正常行为。

---

## 4. 多模态 / 视觉场景

图片理解使用 `qwen3-vl-32b-instruct`。两种方式：① 直接指定视觉模型；② 发文本模型 + 图片，服务自动检测图片切到视觉模型（推荐，调用方无需关心）。

### 4.1 直接指定视觉模型

```bash
curl http://192.168.0.151:30080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-vl-32b-instruct",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "请描述这张图片的主要内容。"},
        {"type": "image_url", "image_url": {"url": "http://<image-url>"}}
      ]
    }],
    "max_tokens": 256
  }'
```

### 4.2 自动切视觉模型（推荐）

发文本模型 `deepseek-r1-distill-qwen-32b` + 图片，服务检测到 `image_url` 自动切到 `qwen3-vl-32b-instruct`。

```bash
curl http://192.168.0.151:30080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-r1-distill-qwen-32b",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "图里有什么?"},
        {"type": "image_url", "image_url": {"url": "http://<image-url>"}}
      ]
    }],
    "max_tokens": 128
  }'
```

> `image_url.url` 可用：① 公网/内网可达的图片 URL；② `data:image/png;base64,<BASE64>` data URI。

---

## 5. 语音场景（ASR）

语音转文字使用 FunASR（`sensevoice` 模型）。支持音频文件/URL 转写，以及"语音→LLM 级联回答"。

### 5.1 音频转文字

```bash
curl http://192.168.0.151:30080/v1/audio/transcriptions \
  -F 'file=http://<audio-url>'
```

```json
{"text": "你好这是一个语音识别的端到端测试请确认你能听懂我说的话"}
```

### 5.2 语音→LLM 级联（音频附件）

用 `/v1/chat/completions` 的 `attachments` 字段传音频 URL，服务先 ASR 转写，再把转写文本拼入 prompt 送 LLM 回答。

```bash
curl http://192.168.0.151:30080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-r1-distill-qwen-32b",
    "messages": [{"role": "user", "content": "请根据以下语音内容回答问题"}],
    "attachments": [{"type": "audio", "url": "http://<audio-url>"}],
    "max_tokens": 256
  }'
```

---

## 6. 流式场景

### 6.1 SSE 流式对话（HTTP）

`/v1/chat/completions` 设 `"stream": true`，返回 `text/event-stream`，逐 token 输出。

```bash
curl -N http://192.168.0.151:30080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-r1-distill-qwen-32b",
    "messages": [{"role": "user", "content": "讲个短故事"}],
    "stream": true,
    "max_tokens": 256
  }'
```

每个 chunk：`data: {"choices":[{"delta":{"content":"..."}}]}`，结束：`data: [DONE]`

### 6.2 WebSocket 流式对话（`/v1/stream/ws`）

双向 WebSocket，适合交互式对话。连接后发 JSON 文本消息，后端流式回 LLM 文字。

```
ws://192.168.0.151:30080/v1/stream/ws?model=deepseek-r1-distill-qwen-32b
```

```json
// 客户端 → 服务端
{"type": "text", "content": "你好,请简短回复"}

// 服务端 → 客户端 (流式)
{"type": "llm_chunk", "content": "你"}
{"type": "llm_chunk", "content": "好"}
{"type": "llm_done",  "content": "你好!..."}
```

### 6.3 WebSocket 实时语音流（`/v1/stream/asr`）

实时语音流式 ASR + LLM。前端推 16kHz/16bit/mono PCM 裸数据，后端实时转写并可选自动 LLM 回答。

```
ws://192.168.0.151:30080/v1/stream/asr
```

```json
// 1. 客户端连上后发配置
{"type": "config", "auto_llm": true, "model": "deepseek-r1-distill-qwen-32b"}
// 2. 收到 {"type":"config_ack"} 后, 持续发送二进制 PCM (3200 字节/块 ≈ 0.1s)
// 3. 服务端流式返回:
{"type": "asr_partial", "content": "部分识别"}
{"type": "asr_final",   "content": "最终识别"}
{"type": "llm_chunk",   "content": "回复片段"}
{"type": "llm_done",    "content": "完整回复"}
// 4. 客户端发 {"type":"stop"} 结束当前会话
```

> PCM 推流：WAV 文件需去掉 44 字节头发裸 PCM；麦克风实时输入用 Web Audio API 采 16kHz/16bit/mono。

---

## 7. 附件场景（PDF / Word / 音频）

`/v1/chat/completions` 扩展 `attachments` 字段，服务自动预处理：PDF/Word 提取文本、音频转写，拼入 prompt 送 LLM。

| attachment.type | 处理 |
|-----------------|------|
| `pdf` | 提取文本 |
| `word` | 提取文本 |
| `audio` | 语音转写→文本 |
| `image` | 转视觉模型处理 |

```bash
curl http://192.168.0.151:30080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-r1-distill-qwen-32b",
    "messages": [{"role": "user", "content": "请简要总结以下PDF文档的内容"}],
    "attachments": [{"type": "pdf", "url": "http://<pdf-url>"}],
    "stream": true,
    "max_tokens": 256
  }'
```

---

## 8. 通用接口说明

服务采用 OpenAI-compatible API，统一使用 `/v1/chat/completions`。

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | 是 | 模型名称 |
| `messages` | array | 是 | 对话消息 |
| `temperature` | number | 否 | 采样温度 |
| `max_tokens` | integer | 否 | 最大生成长度 |
| `stream` | boolean | 否 | 是否流式返回 |
| `attachments` | array | 否 | 附件（PDF/Word/audio/image） |

常见 `messages` 格式：

```json
[
  {"role": "system", "content": "你是一个有帮助的助手。"},
  {"role": "user", "content": "请解释 GPU 调度。"}
]
```

---

## 9. 模型使用说明

### 9.1 `qwen3-vl-32b-instruct`

| 项目 | 说明 |
|------|------|
| 能力 | 文本 + 图片理解 |
| 适用场景 | 图片问答、截图分析、视觉内容总结、多模态推理 |
| 限制 | 图片不宜过小（建议 ≥64×64）；图片大小/并发受服务约束 |

### 9.2 `deepseek-r1-distill-qwen-32b`

| 项目 | 说明 |
|------|------|
| 能力 | 文本推理（推理模型，含思维链） |
| 适用场景 | 代码分析、复杂问题拆解、长文本问答、方案推理、附件内容总结 |
| 限制 | 上下文长度上限 32k tokens；输入超限报 400 |

---

## 10. 支持与反馈

使用中遇到问题，或有新模型、新能力、配额提升、压测支持等需求，可联系星脑AI平台团队反馈和提需求。

反馈问题时请尽量提供以下信息：

- 模型名称与调用场景（文本/视觉/ASR/流式/附件）
- 请求时间
- 请求 ID（如果有）
- 输入规模，例如 token 数、图片大小、音频时长
- 错误信息或异常现象
- 是否使用流式返回
- 复现方式或最小请求示例
