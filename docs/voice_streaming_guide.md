# 语音推流功能实现与调用指南

> OpenClaw Multi-Agent 语音推流功能包含 WebSocket 双向实时对话、流式语音识别、HTTP 文件上传 ASR 和 SSE 单向推流四种方式，均由 Stream Service (`hermes/stream_service.py` 端口 8084) 实现。

---

## 一、ws_chat — WebSocket 双向实时对话 + 语音输入

### 端点

`WS /v1/stream/ws?model=qwen2.5&asr_provider=funasr`

### 1.1 完整交互时序

```
客户端 (浏览器/SDK)               Stream Service (:8084)              LiteLLM (:4000) → Ollama (:11434)
  │                                   │                                    │
  │ ① WebSocket 握手                  │                                    │
  │──CONNECT /v1/stream/ws──────────→│ accept()                           │
  │  ?model=qwen2.5                   │ 解析: model="qwen2.5"              │
  │  &asr_provider=funasr             │ asr_provider="funasr"              │
  │                                   │ session_id=uuid4[:8]               │
  │                                   │                                    │
  │ ═══ 文本路径 ═══                  │                                    │
  │                                   │                                    │
  │ ② 发送文本                        │                                    │
  │──{"type":"text","content":"你好"}→│ json.loads(raw["text"])            │
  │                                   │ msg_type="text", content="你好"    │
  │                                   │                                    │
  │ ③ 调用 LLM 流式推理               │                                    │
  │                                   │──POST /v1/chat/completions────────→│──推理 qwen2.5──→│
  │                                   │  {stream:true, messages:[...]}     │                    │
  │                                   │  Authorization: Bearer sk-xxx      │                    │
  │                                   │                                    │                    │
  │ ④ 逐 token 返回                   │←──data:{delta:{content:"你"}}────│←──data:{delta}────│
  │←─{"type":"llm_chunk",             │  解析 SSE → 提取 delta.content     │                    │
  │    "content":"你"}────────────────│  full_response += "你"             │                    │
  │←─{"type":"llm_chunk",             │←──data:{delta:{content:"好"}}────│                    │
  │    "content":"好"}────────────────│  full_response += "好"             │                    │
  │←─{"type":"llm_chunk",             │←──data:{delta:{content:"！"}}────│                    │
  │    "content":"！"}────────────────│  full_response += "！"             │                    │
  │←─...                              │←──data:[DONE]─────────────────────│                    │
  │                                   │                                    │                    │
  │ ⑤ LLM 完成信号                    │                                    │                    │
  │←─{"type":"llm_done",              │  发送 llm_done                     │                    │
  │    "content":"你好！"}────────────│                                    │                    │
  │                                   │                                    │                    │
  │ ═══ 音频路径 ═══                  │                                    │                    │
  │                                   │                                    │                    │
  │ ⑥ 发送二进制音频                  │                                    │                    │
  │──[raw bytes: WAV/PCM]───────────→│ raw.get("bytes") = audio_data     │                    │
  │                                   │                                    │                    │
  │ ⑦ ASR 转写                        │                                    │                    │
  │                                   │──transcribe_audio(audio_data)───→ │──FunASR /v1/audio/ │
  │                                   │  级联: FunASR→Doubao→Ollama        │  transcriptions──→ │
  │                                   │  返回 "今天天气怎么样"              │                    │
  │                                   │                                    │                    │
  │ ⑧ ASR 结果                        │                                    │                    │
  │←─{"type":"asr_text",              │                                    │                    │
  │    "content":"今天天气怎么样"}────│                                    │                    │
  │                                   │                                    │                    │
  │ ⑨ 对 ASR 文本做 LLM 回复          │                                    │                    │
  │                                   │──stream_llm_response("今天天气怎么样", "qwen2.5")──→│──推理──→│
  │←─{"type":"llm_chunk","content":"很"}─│←──SSE delta──────────────────│←──delta──│
  │←─{"type":"llm_chunk","content":"抱歉"}─│                              │          │
  │←─{"type":"llm_done","content":"很抱歉，我无法获取实时天气..."}─│        │          │
  │                                   │                                    │          │
  │ ═══ 心跳 ═══                      │                                    │          │
  │──{"type":"ping"}────────────────→│                                    │          │
  │←─{"type":"pong"}─────────────────│                                    │          │
  │                                   │                                    │          │
  │ ═══ 错误 ═══                      │                                    │          │
  │←─{"type":"error","content":"语音识别结果为空"}─│ (ASR 返回空文本)      │          │
  │←─{"type":"error","content":"连接超时"}────────│ (3600s 无消息)       │          │
```

### 1.2 消息类型定义

#### 客户端 → 服务端

| type | 格式 | 说明 |
|------|------|------|
| `text` | `{type:"text", content:"你好"}` | 文本消息，触发 LLM 流式推理 |
| 二进制 | `[raw bytes: WAV/PCM]` | 音频数据，触发 ASR → LLM |
| `ping` | `{type:"ping"}` | 心跳探测 |

#### 服务端 → 客户端

| type | 格式 | 说明 |
|------|------|------|
| `llm_chunk` | `{type:"llm_chunk", content:"你"}` | LLM 流式 token 片段 |
| `llm_done` | `{type:"llm_done", content:"你好！"}` | LLM 完成，content 为完整回复 |
| `asr_text` | `{type:"asr_text", content:"今天天气怎么样"}` | 音频 ASR 转写结果（仅音频路径） |
| `pong` | `{type:"pong"}` | 心跳响应 |
| `error` | `{type:"error", content:"语音识别结果为空"}` | 错误信息 |

### 1.3 ASR 级联 (`transcribe_audio()`)

音频数据按优先级依次尝试三个 ASR 服务，任一成功即返回：

```
audio_bytes (WAV/PCM)
      │
      ├─ FunASR (优先级 0)
      │  POST {FUNASR_URL}/v1/audio/transcriptions
      │  multipart: file=audio.wav, model=sensevoice
      │  → 返回 text, 经过 _clean_funasr_text() 清理 SenseVoice 特殊标签
      │  (如 <|zh|><|EMO_HAPPY|><|SPEAKER1|>实际语音文本 → "实际语音文本")
      │
      ├─ Doubao 豆包 ASR (优先级 1, 需要 DOUBAO_ASR_KEY)
      │  POST {DOUBAO_ASR_URL}?format=wav&rate=16000
      │  Authorization: Bearer {DOUBAO_ASR_KEY}
      │  Content-Type: application/octet-stream
      │  → 返回 result[0].text
      │
      └─ Ollama Whisper (优先级 2, fallback)
         POST {OLLAMA_URL}/api/generate
         model=whisper, images=[base64(audio)]
         → 返回 response (转写文本)
```

### 1.4 LLM 流式调用 (`stream_llm_response()`)

```
text (用户输入或 ASR 转写文本) + model
      │
      ├─ LiteLLM Proxy (优先级 0, 需要 EXTERNAL_LITELLM_URL)
      │  │
      │  ├─ 连通性探测 (30s 缓存)
      │  │  POST {EXTERNAL_LITELLM_URL}/v1/chat/completions
      │  │  {stream:false, max_tokens:1} → 验证服务可达
      │  │
      │  └─ 流式调用
      │     POST {EXTERNAL_LITELLM_URL}/v1/chat/completions
      │     {stream:true, messages:[{role:"user",content:text}], model}
      │     Authorization: Bearer {LITELLM_MASTER_KEY}
      │     → SSE data: {choices:[{delta:{content:"..."}}]} 逐行 yield
      │     → data: [DONE]
      │
      └─ Ollama 直连 (优先级 1, fallback)
         POST {OLLAMA_URL}/v1/chat/completions
         {stream:true, messages:[{role:"user",content:text}], model}
         → SSE data: {choices:[{delta:{content:"..."}}]} 逐行 yield
```

### 1.5 调用示例

#### JavaScript (浏览器)

```javascript
const ws = new WebSocket('ws://localhost:8090/v1/stream/ws?model=qwen2.5');

ws.onopen = () => {
  // 发送文本消息
  ws.send(JSON.stringify({ type: 'text', content: '你好' }));
};

ws.onmessage = (evt) => {
  const d = JSON.parse(evt.data);
  if (d.type === 'llm_chunk') {
    // 流式 token 片段，逐步拼接
    console.log('chunk:', d.content);
  } else if (d.type === 'llm_done') {
    // LLM 完成，d.content 是完整回复
    console.log('done:', d.content);
    ws.close();
  } else if (d.type === 'asr_text') {
    // 音频被 ASR 转写后的文本（仅二进制音频路径）
    console.log('asr:', d.content);
  } else if (d.type === 'error') {
    console.error('error:', d.content);
  }
};

// 也可发送二进制音频
// ws.send(audioArrayBuffer);
```

#### Python (websocket-client)

```python
import websocket
import json

ws = websocket.create_connection("ws://localhost:8090/v1/stream/ws?model=qwen2.5")

# 发送文本
ws.send(json.dumps({"type": "text", "content": "你好"}))

# 接收流式回复
full_text = ""
while True:
    result = ws.recv()
    d = json.loads(result)
    if d["type"] == "llm_chunk":
        full_text += d["content"]
    elif d["type"] == "llm_done":
        print(f"完成: {full_text}")
        break
    elif d["type"] == "error":
        print(f"错误: {d['content']}")
        break

ws.close()
```

---

## 二、ws_asr — WebSocket 流式语音识别 + 自动 LLM 回复

### 端点

`WS /v1/stream/asr`

这是更复杂的协议——客户端持续推送 PCM 音频块，服务端边接收边识别边回复，实现**实时对话**体验。

### 2.1 音频数据准备

PCM 音频格式要求: **16kHz 采样率、16bit 位深、单声道**

```
原始 WAV 文件 (含 44 字节头)
      │
      │  buf.slice(44)  ← 去掉 WAV 头
      │
      ↓
裸 PCM 数据 (raw bytes, 16kHz 16bit mono)
      │
      │  切分成小块，逐块发送
      │  块大小: 3200 bytes (E2E) 或 32000 bytes (Playground)
      │  间隔: 100ms (E2E) 或 0ms (Playground)
      │
      ↓
WebSocket binary frame → 服务端 audio_buffer.extend(chunk)
```

#### PCM 格式参数计算

| 参数 | 值 | 计算 |
|------|-----|------|
| 采样率 | 16kHz | 16000 samples/s |
| 位深 | 16bit = 2 bytes | 每个采样点 2 bytes |
| 通道数 | 1 (mono) | — |
| 字节率 | 32000 bytes/s | 16000 × 2 × 1 |
| 每秒时长 | 1s = 32000 bytes | — |
| 3200 bytes | ≈ 0.1s 音频 | 3200/32000 |
| 32000 bytes | ≈ 1s 音频 | 32000/32000 |
| 320000 bytes | ≈ 10s 音频 | MAX_BYTES 阈值 |

### 2.2 消息类型定义

#### 客户端 → 服务端

| type | 格式 | 说明 |
|------|------|------|
| `config` | `{type:"config", auto_llm:true, model:"qwen2.5"}` | 会话配置 |
| 二进制 PCM | `[raw bytes]` | PCM 音频数据块 |
| `stop` | `{type:"stop"}` | 结束推流，触发最终 ASR |

#### 服务端 → 客户端

| type | 格式 | 说明 |
|------|------|------|
| `connected` | `{type:"connected", session_id:"abc", asr_provider:"funasr", funasr_url:"..."}` | 连接确认 |
| `config_ack` | `{type:"config_ack", auto_llm:true, model:"qwen2.5"}` | 配置确认 |
| `asr_partial` | `{type:"asr_partial", content:"今天天气", is_final:false, buffer_ms:1000}` | 中间 ASR 结果 |
| `asr_final` | `{type:"asr_final", content:"今天天气怎么样", segments:2}` | 最终 ASR 结果 |
| `llm_chunk` | `{type:"llm_chunk", content:"很"}` | LLM 流式 token |
| `llm_done` | `{type:"llm_done", content:"很抱歉..."}` | LLM 完成 |
| `error` | `{type:"error", content:"..."}` | 错误 |

### 2.3 完整交互时序 (auto_llm=true)

```
客户端                              Stream Service                      FunASR          LiteLLM→Ollama
  │                                    │                                │                │
  │ ① WebSocket 连接                   │                                │                │
  │──CONNECT /v1/stream/asr──────────→│ accept()                       │                │
  │                                    │ session_id=uuid[:8]            │                │
  │                                    │ audio_buffer=bytearray()       │                │
  │                                    │ all_asr_text=[]                │                │
  │                                    │ auto_llm=False (默认)          │                │
  │                                    │                                │                │
  │ ② 接收欢迎消息                     │                                │                │
  │←─{"type":"connected",              │                                │                │
  │    "session_id":"abc123",           │                                │                │
  │    "asr_provider":"funasr",         │                                │                │
  │    "funasr_url":"http://funasr..."}│                                │                │
  │                                    │                                │                │
  │ ③ 发送配置                         │                                │                │
  │──{"type":"config",                 │                                │                │
  │    "auto_llm":true,                 │ auto_llm = true                │                │
  │    "model":"qwen2.5"}─────────────→│ llm_model = "qwen2.5"         │                │
  │                                    │                                │                │
  │ ④ 接收配置确认                     │                                │                │
  │←─{"type":"config_ack",             │                                │                │
  │    "auto_llm":true,                 │                                │                │
  │    "model":"qwen2.5"}──────────────│                                │                │
  │                                    │                                │                │
  │ ⑤ 持续推送 PCM 音频块               │                                │                │
  │──[3200 bytes PCM]──(100ms)──→│ audio_buffer.extend(chunk)      │                │
  │──[3200 bytes PCM]──(100ms)──→│ len(audio_buffer) = 6400        │                │
  │──[3200 bytes PCM]──(100ms)──→│ len(audio_buffer) = 9600        │                │
  │──...                               │                                │                │
  │──[3200 bytes PCM]──(100ms)──→│ len(audio_buffer) = 32000       │                │
  │                                    │ should_flush 检查:             │                │
  │                                    │ buffer >= 32000 ✓              │                │
  │                                    │ elapsed >= 2.0s ✓              │                │
  │                                    │                                │                │
  │ ⑥ 服务端刷新 ASR                   │                                │                │
  │                                    │ _flush_asr_buffer():           │                │
  │                                    │ PCM → _pcm_to_wav()            │                │
  │                                    │   _make_wav_header(32000):     │                │
  │                                    │     RIFF+WAVE+fmt+data 头     │                │
  │                                    │     44字节头 + 32000字节PCM   │                │
  │                                    │   = 32044字节完整WAV           │                │
  │                                    │──transcribe_audio(wav_data)──→│──POST /v1/audio│
  │                                    │                                │  /transcriptions│
  │                                    │                                │  → "今天天气"──→│
  │                                    │                                │                │
  │ ⑦ 接收中间 ASR 结果               │                                │                │
  │←─{"type":"asr_partial",            │ all_asr_text=["今天天气"]      │                │
  │    "content":"今天天气",            │ len(all_asr_text) = 1          │                │
  │    "is_final":false,               │ (< 2, 不触发 LLM)              │                │
  │    "buffer_ms":1000}───────────────│                                │                │
  │                                    │                                │                │
  │ ⑤ 继续推送                         │                                │                │
  │──[3200 bytes PCM]──(100ms)──→│ audio_buffer 继续累积            │                │
  │──...                               │                                │                │
  │──[3200 bytes PCM]──(100ms)──→│ should_flush 再次触发             │                │
  │                                    │ _flush_asr_buffer():           │                │
  │                                    │──transcribe_audio(wav_data)──→│──"怎么样"────→│
  │                                    │                                │                │
  │ ⑦ 第二次中间 ASR 结果             │                                │                │
  │←─{"type":"asr_partial",            │ all_asr_text=["今天天气","怎么样"]│              │
  │    "content":"怎么样",              │ len(all_asr_text) = 2          │                │
  │    "is_final":false,               │                                │                │
  │    "buffer_ms":1000}───────────────│                                │                │
  │                                    │                                │                │
  │ ═══ auto_llm 中间触发 ═══          │                                │                │
  │                                    │ if auto_llm && len >= 2:       │                │
  │                                    │   combined = "今天天气 怎么样" │                │
  │                                    │──stream_llm_response(combined)──────────────────→│──推理──→│
  │                                    │                                │                │
  │ ⑧ LLM 流式回复                    │                                │                │←─delta──│
  │←─{"type":"llm_chunk","content":"很"}─│←──SSE delta───────────────────────────────────│
  │←─{"type":"llm_chunk","content":"抱歉"}─│                              │                │
  │←─{"type":"llm_done","content":"很抱歉..."}─│                          │                │
  │                                    │ all_asr_text.clear()           │                │
  │                                    │ // 清空，准备下一轮             │                │
  │                                    │                                │                │
  │ ⑤ 继续推送                         │                                │                │
  │──[3200 bytes PCM]──(100ms)──→│ 继续累积...                      │                │
  │──...                               │                                │                │
  │                                    │                                │                │
  │ ⑨ 客户端发送停止                   │                                │                │
  │──{"type":"stop"}──────────────────→│ 处理剩余缓冲:                   │                │
  │                                    │  _flush_asr_buffer() → 最后ASR │                │
  │                                    │  audio_buffer.clear()           │                │
  │                                    │                                │                │
  │ ⑩ 最终 ASR 结果                   │                                │                │
  │←─{"type":"asr_final",              │ final_text = " ".join(all_asr_text)│            │
  │    "content":"我想出去散步",         │ segments = len(all_asr_text)   │                │
  │    "segments":1}────────────────────│                                │                │
  │                                    │                                │                │
  │ ═══ auto_llm 最终触发 ═══          │                                │                │
  │                                    │ if auto_llm && final_text:     │                │
  │                                    │──stream_llm_response(final_text)────────────────→│──推理──→│
  │                                    │                                │                │
  │ ⑪ 最终 LLM 回复                   │                                │                │←─delta──│
  │←─{"type":"llm_chunk","content":"很"}─│                              │                │
  │←─{"type":"llm_done","content":"很遗憾..."}─│                        │                │
  │                                    │ all_asr_text.clear()           │                │
  │                                    │ session_done = true            │                │
  │                                    │ // 连接结束                     │                │
```

### 2.4 音频缓冲刷新逻辑

每次收到 PCM chunk 后检查是否需要触发 ASR：

```python
# 刷新条件
should_flush = (
    len(audio_buffer) >= STREAM_ASR_MAX_BYTES   # 320000 bytes ≈ 10s, 强制刷新
    OR
    (len(audio_buffer) >= STREAM_ASR_MIN_BYTES   # 32000 bytes ≈ 1s, 最小阈值
     AND
     now - last_asr_time >= STREAM_ASR_INTERVAL) # 距上次 ASR ≥ 2.0s
)
```

刷新时执行步骤：

1. `audio_buffer` → `_pcm_to_wav()` (44字节WAV头 + PCM数据 = 完整WAV)
2. 完整WAV → `transcribe_audio()` → FunASR/Doubao/Ollama 级联
3. 结果加入 `all_asr_text[]`
4. 如果 `auto_llm=true` 且 `len(all_asr_text) >= 2` → 触发 LLM 中间回复
5. `audio_buffer.clear()`, `last_asr_time = now`

#### 关键常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `STREAM_ASR_MIN_BYTES` | 32000 | 最小刷新阈值 (~1s 音频) |
| `STREAM_ASR_MAX_BYTES` | 320000 | 强制刷新阈值 (~10s 音频) |
| `STREAM_ASR_INTERVAL` | 2.0 | 最小刷新间隔 (秒) |

### 2.5 PCM → WAV 包装 (`_pcm_to_wav()`)

```python
def _make_wav_header(data_len, sample_rate=16000, bits=16, channels=1):
    byte_rate = sample_rate * channels * bits // 8  # 32000
    block_align = channels * bits // 8              # 2
    # struct.pack 构建 44 字节标准 WAV 头:
    # RIFF(4) + chunk_size(4) + WAVE(4)
    # fmt (16 bytes: format=1 PCM, channels, sample_rate, byte_rate, block_align, bits)
    # data(4) + data_len(4)
    return struct.pack('<4sI4s4sIHHIIHH4sI', ...)  # 44 bytes total

def _pcm_to_wav(pcm_data, sample_rate=16000, bits=16, channels=1):
    header = _make_wav_header(len(pcm_data), sample_rate, bits, channels)
    return header + pcm_data  # 完整 WAV = 44字节头 + 原始PCM
```

WAV 头结构 (44 字节):

```
Offset  Size  Field           Value
0       4     ChunkID         "RIFF"
4       4     ChunkSize       data_len + 36
8       4     Format          "WAVE"
12      4     Subchunk1ID     "fmt "
16      4     Subchunk1Size   16
20      2     AudioFormat     1 (PCM)
22      2     NumChannels     1
24      4     SampleRate      16000
28      4     ByteRate        32000
32      2     BlockAlign      2
34      2     BitsPerSample   16
36      4     Subchunk2ID     "data"
40      4     Subchunk2Size   data_len
44      ...   Data            PCM raw bytes
```

### 2.6 auto_llm 触发机制

auto_llm 是 ws_asr 的核心特性——边听边答，在语音推流过程中实时生成 LLM 回复：

- **中间触发**: 当 `auto_llm=true` 且 `all_asr_text` 积累 ≥2 条 ASR 结果时，将所有结果合并为一条文本发送给 LLM
- **最终触发**: 客户端发送 `stop` 后，如果有最终 ASR 结果，再次触发 LLM 回复
- **清空重置**: 每次 LLM 中间触发后，`all_asr_text.clear()`，为下一轮对话做准备
- **关闭 auto_llm**: `auto_llm=false` 时，ws_asr 只做 ASR 识别，不触发 LLM

### 2.7 调用示例

#### JavaScript (浏览器, 模拟实时麦克风推流)

```javascript
const ws = new WebSocket('ws://localhost:8090/v1/stream/asr');

ws.onmessage = (evt) => {
  const d = JSON.parse(evt.data);

  if (d.type === 'connected') {
    // 配置会话
    ws.send(JSON.stringify({ type: 'config', auto_llm: true, model: 'qwen2.5' }));
  } else if (d.type === 'config_ack') {
    // 开始推流音频 (PCM 16kHz 16bit mono)
    startStreamingAudio(ws);
  } else if (d.type === 'asr_partial') {
    console.log('中间识别:', d.content, `(${d.buffer_ms}ms)`);
  } else if (d.type === 'asr_final') {
    console.log('最终识别:', d.content, `(${d.segments}段)`);
  } else if (d.type === 'llm_chunk') {
    console.log('LLM片段:', d.content);
  } else if (d.type === 'llm_done') {
    console.log('LLM完成:', d.content);
    ws.close();
  }
};

function startStreamingAudio(ws) {
  // 方式1: 从文件读取 PCM
  fetch('/v1/minio/openclaw-test/speech_test.wav')
    .then(r => r.arrayBuffer())
    .then(buf => {
      const pcm = buf.slice(44);  // 去掉 WAV 头
      const CHUNK = 3200;         // 每块 ≈ 0.1s
      let offset = 0;
      function send() {
        if (offset >= pcm.byteLength) {
          ws.send(JSON.stringify({ type: 'stop' }));
          return;
        }
        ws.send(pcm.slice(offset, offset + CHUNK));
        offset += CHUNK;
        setTimeout(send, 100);  // 100ms 间隔模拟实时
      }
      send();
    });

  // 方式2: 从麦克风实时采集 (Web Audio API)
  // navigator.mediaDevices.getUserMedia({audio: {sampleRate:16000, channelCount:1}})
  //   .then(stream => { ... AudioWorkletNode 每帧发送 PCM ... })
}

ws.onerror = () => console.error('WS连接失败');
```

#### Python (websocket-client, 推流 WAV 文件)

```python
import websocket
import json
import time

ws = websocket.create_connection("ws://localhost:8090/v1/stream/asr")

# 等待 connected
result = json.loads(ws.recv())
print(f"已连接: {result}")

# 发送配置
ws.send(json.dumps({"type": "config", "auto_llm": True, "model": "qwen2.5"}))

# 等待 config_ack
result = json.loads(ws.recv())
print(f"配置确认: {result}")

# 读取 WAV 文件，去掉头，发送 PCM
with open("speech_test.wav", "rb") as f:
    wav_data = f.read()
pcm_data = wav_data[44:]  # 去掉 44 字节 WAV 头

chunk_size = 3200  # ≈ 0.1s
offset = 0

while offset < len(pcm_data):
    chunk = pcm_data[offset:offset + chunk_size]
    ws.send(chunk)
    offset += chunk_size
    time.sleep(0.1)  # 100ms 间隔

# 发送停止信号
ws.send(json.dumps({"type": "stop"}))

# 接收最终结果
while True:
    result = json.loads(ws.recv())
    if result["type"] == "asr_partial":
        print(f"中间: {result['content']}")
    elif result["type"] == "asr_final":
        print(f"最终: {result['content']} ({result['segments']}段)")
    elif result["type"] == "llm_chunk":
        print(f"LLM: {result['content']}")
    elif result["type"] == "llm_done":
        print(f"完成: {result['content']}")
        break
    elif result["type"] == "error":
        print(f"错误: {result['content']}")
        break

ws.close()
```

---

## 三、asr/file — HTTP 文件上传 ASR

### 端点

`POST /v1/stream/asr/file` (multipart/form-data)

### 3.1 交互时序

```
客户端                              Stream Service
  │                                    │
  │──POST /v1/stream/asr/file───────→│  解析 multipart form:
  │  Content-Type: multipart/form-data │    file = uploaded audio file
  │  Body:                             │    model = "sensevoice" (可选)
  │    file: audio.wav (binary)        │
  │    model: sensevoice               │
  │                                    │
  │                                    │  audio_bytes = file.read()
  │                                    │  text = transcribe_audio(audio_bytes)
  │                                    │    // 注意: 直接传 WAV bytes
  │                                    │    // 不需要 PCM→WAV 包装
  │                                    │    // FunASR 直接接受 WAV 格式
  │                                    │
  │←─200 OK───────────────────────────│
  │  {                                 │
  │    "filename": "audio.wav",        │
  │    "text": "今天天气怎么样",         │
  │    "provider": "funasr",           │  // 或 "fallback"
  │    "size": 12345                   │  // 文件大小
  │  }                                 │
```

### 3.2 调用示例

#### JavaScript

```javascript
const formData = new FormData();
formData.append('file', audioFile);  // File 对象
formData.append('model', 'sensevoice');

const resp = await fetch('/v1/stream/asr/file', {
  method: 'POST',
  body: formData
});
const result = await resp.json();
console.log(result.text);  // "今天天气怎么样"
```

#### Python

```python
import requests

with open("audio.wav", "rb") as f:
    resp = requests.post(
        "http://localhost:8090/v1/stream/asr/file",
        files={"file": f},
        data={"model": "sensevoice"}
    )
result = resp.json()
print(result["text"])  # "今天天气怎么样"
```

---

## 四、SSE 单向推流 (`/v1/stream/sse`)

### 端点

`GET /v1/stream/sse?prompt=你好&model=qwen2.5`

### 4.1 交互时序

```
客户端                              Stream Service
  │                                    │
  │──GET /v1/stream/sse─────────────→│  event_generator():
  │  ?prompt=你好                      │    async for line in stream_llm_response("你好", "qwen2.5"):
  │  &model=qwen2.5                    │      yield line
  │                                    │    yield "data: [DONE]\n\n"
  │                                    │
  │←─data: {"choices":[...]}\n\n─────│  SSE 流式输出
  │←─data: {"choices":[...]}\n\n─────│
  │←─data: [DONE]\n\n────────────────│
```

### 4.2 调用示例 (浏览器 EventSource)

```javascript
const es = new EventSource('/v1/stream/sse?prompt=你好&model=qwen2.5');
es.onmessage = (evt) => {
  if (evt.data === '[DONE]') { es.close(); return; }
  const d = JSON.parse(evt.data);
  const text = d.choices?.[0]?.delta?.content || '';
  console.log(text);
};
```

---

## 五、四种推流方式对比

| 特性 | ws_chat | ws_asr | SSE | HTTP ASR file |
|------|---------|--------|-----|---------------|
| **协议** | WebSocket 双向 | WebSocket 双向 | HTTP SSE 单向 | HTTP POST |
| **输入方式** | 文本 or 音频 | PCM 流式音频块 | 文本 (query param) | 完整音频文件 |
| **实时性** | ✅ 实时双向 | ✅ 实时流式 | ⚠️ 单向推流 | ❌ 一次性 |
| **ASR** | 音频→整段转写 | PCM→分段转写+中间结果 | 无 ASR | 整段转写 |
| **LLM** | ✅ 流式 | ✅ 流式 (auto_llm) | ✅ 流式 | ❌ 无 LLM |
| **auto_llm** | ❌ 不支持 | ✅ 边听边答 | ❌ | ❌ |
| **连接时长** | 3600s | 30s receive 超时 | 直到 LLM 完成 | 单次请求 |
| **OpenAI兼容** | ❌ 自定义协议 | ❌ 自定义协议 | ⚠️ 类似但非标准 | ❌ 自定义端点 |
| **适用场景** | 实时对话助手 | 实时语音交互 | 简单文本流式 | 快速ASR转写 |

---

## 六、关键配置参数

### Stream Service 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `APP_PORT` | 8084 | 服务端口 |
| `EXTERNAL_LITELLM_URL` | — | LiteLLM Proxy URL |
| `LITELLM_MASTER_KEY` | — | LiteLLM 认证密钥 |
| `OLLAMA_URL` | http://localhost:11434 | Ollama 直连 URL |
| `FUNASR_URL` | — | FunASR 服务 URL |
| `FUNASR_MODEL` | sensevoice | FunASR 模型名 |
| `DOUBAO_ASR_KEY` | — | 豆包 ASR API Key (可选) |
| `STREAM_ASR_MIN_BYTES` | 32000 | ASR 最小刷新阈值 |
| `STREAM_ASR_MAX_BYTES` | 320000 | ASR 强制刷新阈值 |
| `STREAM_ASR_INTERVAL` | 2.0 | ASR 最小刷新间隔 (秒) |

### K8S 部署配置 (k8s/07-stream.yaml)

```yaml
env:
  - name: APP_PORT
    value: "8084"
  - name: EXTERNAL_LITELLM_URL
    value: "http://litellm:4000"
  - name: OLLAMA_URL
    value: "http://ollama:11434"
  - name: FUNASR_URL
    value: "http://funasr:8199"
  - name: FUNASR_MODEL
    value: "sensevoice"
  - name: LITELLM_MASTER_KEY
    valueFrom:
      secretKeyRef:
        name: openclaw-secrets
        key: litellm-master-key
```

---

## 七、已知问题

### pgWSASR Bug: `config_ok` vs `config_ack` 消息类型不匹配

**问题描述**: Dashboard Playground 版本 `pgWSASR()` 监听 `config_ok` 消息类型，但 Stream Service 发送的是 `config_ack`。导致音频数据发送逻辑永远不会触发，pgWSASR 功能在 Playground 中不可用。

**影响范围**: 仅影响 Dashboard Playground 的 ws_asr 交互功能。E2E 测试版本 `e2eWSASR()` 正确监听 `config_ack`，不受影响。

**修复方案**: 将 `pgWSASR()` 中的 `d.type === 'config_ok'` 改为 `d.type === 'config_ack'`。

---

## 八、Nginx 路由配置

语音推流相关的 WebSocket 端点需要在 Nginx 中配置 WebSocket 代理：

```nginx
# WebSocket — Stream Service
location /v1/stream/ws {
    proxy_pass http://stream_backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
}

location /v1/stream/asr {
    proxy_pass http://stream_backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
}

# SSE — Stream Service
location /v1/stream/sse {
    proxy_pass http://stream_backend;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    chunked_transfer_encoding on;
}

# HTTP ASR file upload — Stream Service
location /v1/stream/asr/file {
    proxy_pass http://stream_backend;
    client_max_body_size 50m;
}
```

---

## 九、参考文件

| 文件 | 说明 |
|------|------|
| `hermes/stream_service.py` | Stream Service 主文件，所有推流端点实现 |
| `hermes/litellm_proxy.py` | Proxy Pod，提供 `/v1/audio/transcriptions` HTTP ASR |
| `static/new_dashboard.html` | Dashboard，包含 pgWSChat/pgWSASR/e2eWSChat/e2eWSASR |
| `docker/nginx.conf` | Nginx 路由配置 |
| `k8s/02-configmaps.yaml` | K8S Nginx ConfigMap |
| `k8s/07-stream.yaml` | Stream Service K8S 部署配置 |
