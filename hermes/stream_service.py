"""
Stream Service 独立服务 — 实时推流服务

独立运行: python -m hermes.stream_service  (端口 8084)

提供 WebSocket 和 SSE 两种推流方式:
  1. WebSocket (/v1/stream/ws): 双向通信，前端发送音频 → ASR → LLM → 文字回复
  2. SSE (/v1/stream/sse): 单向推流，前端发送请求后接收流式文字
  3. WebSocket (/v1/stream/asr): FunASR 实时语音转文字流式推流

ASR 集成:
  - FunASR 本地部署 (SenseVoice / Paraformer-online) — 最高优先级
  - 豆包（火山引擎）云端 ASR
  - 本地 Ollama Whisper 模型作为 fallback
"""

import asyncio
import json
import logging
import os
import re
import struct
import time
import uuid
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

# ── 持久 HTTP 客户端（连接池复用）──────────────────────────────────────
_funasr_client: httpx.AsyncClient | None = None
_litellm_stream_client: httpx.AsyncClient | None = None
_ollama_stream_client: httpx.AsyncClient | None = None
_download_client: httpx.AsyncClient | None = None
_litellm_sync_client: httpx.AsyncClient | None = None
_ollama_sync_client: httpx.AsyncClient | None = None
_funasr_health_client: httpx.AsyncClient | None = None


async def _init_persistent_clients():
    """初始化持久 HTTP 客户端（在 FastAPI lifespan 中调用）。"""
    global _funasr_client, _litellm_stream_client, _ollama_stream_client, _download_client
    global _litellm_sync_client, _ollama_sync_client, _funasr_health_client
    _funasr_client = httpx.AsyncClient(
        timeout=60.0,
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
    )
    _litellm_stream_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=3),
    )
    _ollama_stream_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
    )
    _download_client = httpx.AsyncClient(
        timeout=30.0,
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
    )
    # LiteLLM 同步客户端（仅当 EXTERNAL_LITELLM_URL 设置时创建）
    if EXTERNAL_LITELLM_URL:
        _litellm_sync_client = httpx.AsyncClient(
            base_url=EXTERNAL_LITELLM_URL,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
        )
    else:
        _litellm_sync_client = None
    # Ollama 同步客户端
    _ollama_sync_client = httpx.AsyncClient(
        base_url=OLLAMA_URL,
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
    )
    # FunASR 健康检查客户端（仅当 FUNASR_URL 设置时创建）
    if FUNASR_URL:
        _funasr_health_client = httpx.AsyncClient(
            base_url=FUNASR_URL,
            timeout=5.0,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )
    else:
        _funasr_health_client = None


async def _close_persistent_clients():
    """关闭持久 HTTP 客户端（在 FastAPI lifespan 退出时调用）。"""
    for name, client in [("funasr", _funasr_client), ("litellm", _litellm_stream_client),
                         ("ollama", _ollama_stream_client), ("download", _download_client),
                         ("litellm_sync", _litellm_sync_client), ("ollama_sync", _ollama_sync_client),
                         ("funasr_health", _funasr_health_client)]:
        if client:
            await client.aclose()

# ── 配置 ──────────────────────────────────────────────────────────

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
# 外部 LiteLLM Proxy 地址（Docker 部署时使用）；为空则直接调用 Ollama
EXTERNAL_LITELLM_URL = os.getenv("EXTERNAL_LITELLM_URL", "")
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")

# ASR 配置
DOUBAO_ASR_KEY = os.getenv("DOUBAO_ASR_KEY", "")
DOUBAO_ASR_URL = os.getenv("DOUBAO_ASR_URL", "https://openspeech.bytedance.com/api/v1/auc/recognize")

# FunASR 配置
FUNASR_URL = os.getenv("FUNASR_URL", "")  # e.g. http://localhost:8199
FUNASR_MODEL = os.getenv("FUNASR_MODEL", "sensevoice")

DEFAULT_CHAT_MODEL = os.getenv("DEFAULT_CHAT_MODEL", "qwen2.5:3b")

# 流式 ASR 缓冲配置
STREAM_ASR_MIN_BYTES = int(os.getenv("STREAM_ASR_MIN_BYTES", "32000"))  # ~1s @16kHz 16bit
STREAM_ASR_INTERVAL = float(os.getenv("STREAM_ASR_INTERVAL", "2.0"))  # 秒
STREAM_ASR_MAX_BYTES = int(os.getenv("STREAM_ASR_MAX_BYTES", "320000"))  # ~10s @16kHz 16bit

# ── LiteLLM 连通性探针缓存（避免每次流式请求都发探针）──────────
_litellm_last_success_time: float = 0.0
_LITELLM_PROBE_INTERVAL: float = 30.0  # 30s 内有成功记录则跳过探针


# ── FunASR 标签清理 ──────────────────────────────────────────────

def _clean_funasr_text(text: str) -> str:
    """清理 FunASR SenseVoice 输出中的特殊标签。"""
    if not text:
        return text
    # 移除 <|zh|><|en|> 等语言标签
    text = re.sub(r'<\|[^|]+\|>', '', text)
    # 移除 <|EMO_XXX|> 情感标签
    text = re.sub(r'<\|EMO_\w+\|>', '', text)
    # 移除 <|BGM|><|SPEAKERXX|> 等事件标签
    text = re.sub(r'<\|BGM\|>', '', text)
    text = re.sub(r'<\|SPEAKER\d+\|>', '', text)
    # 移除剩余的尖括号标签
    text = re.sub(r'<\|[^>]+\|>', '', text)
    return text.strip()


# ── ASR 引擎 ─────────────────────────────────────────────────────

async def transcribe_with_funasr(audio_chunk: bytes) -> str:
    """调用 FunASR HTTP API 将音频转为文字。"""
    if not FUNASR_URL:
        raise RuntimeError("FUNASR_URL 未配置")

    files = {"file": ("audio.wav", audio_chunk, "audio/wav")}
    data = {"model": FUNASR_MODEL, "response_format": "json"}
    resp = await _funasr_client.post(
        f"{FUNASR_URL}/v1/audio/transcriptions",
        files=files, data=data,
    )
    resp.raise_for_status()
    result = resp.json()
    text = result.get("text", "")
    if text:
        return _clean_funasr_text(text)
    return ""


async def transcribe_with_doubao(audio_chunk: bytes, format: str = "wav") -> str:
    """调用豆包 ASR 将音频转为文字。"""
    if not DOUBAO_ASR_KEY:
        raise RuntimeError("DOUBAO_ASR_KEY 未配置")

    headers = {
        "Authorization": f"Bearer {DOUBAO_ASR_KEY}",
        "Content-Type": "application/octet-stream",
    }
    params = {"format": format, "rate": "16000"}

    resp = await _download_client.post(
        DOUBAO_ASR_URL,
        content=audio_chunk,
        headers=headers,
        params=params,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", {}).get("text", "")


async def transcribe_with_ollama(audio_chunk: bytes) -> str:
    """使用 Ollama Whisper 模型作为 ASR fallback。"""
    import base64
    audio_b64 = base64.b64encode(audio_chunk).decode()

    payload = {
        "model": "whisper",
        "prompt": "请将以下音频转写为文字",
        "audio": audio_b64,
    }

    try:
        resp = await _ollama_stream_client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", "")
    except Exception as e:
        logger.warning("[ASR] Ollama Whisper 失败: %s", e)
        return ""


async def transcribe_audio(audio_chunk: bytes, format: str = "wav") -> str:
    """ASR 转写: 优先 FunASR → 豆包 → Ollama。"""
    # 0. FunASR 本地部署
    if FUNASR_URL:
        try:
            text = await transcribe_with_funasr(audio_chunk)
            if text:
                logger.info("[ASR] FunASR 转写成功: %s", text[:80])
                return text
        except Exception as e:
            logger.warning("[ASR] FunASR 失败, 尝试降级: %s", e)

    # 1. 豆包 ASR
    try:
        if DOUBAO_ASR_KEY:
            return await transcribe_with_doubao(audio_chunk, format)
    except Exception as e:
        logger.warning("[ASR] 豆包 ASR 失败, 尝试 Ollama fallback: %s", e)

    # 2. Ollama Whisper
    try:
        return await transcribe_with_ollama(audio_chunk)
    except Exception as e:
        logger.error("[ASR] 全部 ASR 引擎失败: %s", e)
        return ""


# ── WAV 头构造 ───────────────────────────────────────────────────

def _make_wav_header(data_len: int, sample_rate: int = 16000, bits: int = 16, channels: int = 1) -> bytes:
    """构造 WAV 文件头。"""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        36 + data_len,
        b'WAVE',
        b'fmt ',
        16,  # PCM
        1,   # PCM format
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b'data',
        data_len,
    )
    return header


def _pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000, bits: int = 16, channels: int = 1) -> bytes:
    """将 PCM 原始数据包装为 WAV 格式。"""
    header = _make_wav_header(len(pcm_data), sample_rate, bits, channels)
    return header + pcm_data


# ── LLM 流式调用 ─────────────────────────────────────────────────

async def stream_llm_response(text: str, model: str = DEFAULT_CHAT_MODEL):
    """调用 LLM 并以 SSE 格式流式返回。

    优先级: 外部 LiteLLM Proxy → Ollama 直连
    """
    global _litellm_last_success_time
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "stream": True,
        "temperature": 0.7,
    }

    if EXTERNAL_LITELLM_URL:
        headers = {"Content-Type": "application/json"}
        if LITELLM_MASTER_KEY:
            headers["Authorization"] = f"Bearer {LITELLM_MASTER_KEY}"

        # 条件探针：仅在最近 30s 内无成功记录时才检查连通性
        current_time = time.time()
        need_probe = (current_time - _litellm_last_success_time) > _LITELLM_PROBE_INTERVAL

        try:
            if need_probe:
                # 非流式探针检查连通性，避免流式挂起
                probe_resp = await _litellm_sync_client.post(
                    f"{EXTERNAL_LITELLM_URL}/v1/chat/completions",
                    json={**payload, "stream": False, "max_tokens": 1},
                    headers=headers,
                )
                if probe_resp.status_code == 401:
                    logger.warning("[StreamLLM] LiteLLM 返回 401 Unauthorized, 检查 LITELLM_MASTER_KEY 配置")
                    raise httpx.HTTPStatusError(
                        "LiteLLM 401 Unauthorized",
                        request=probe_resp.request,
                        response=probe_resp,
                    )
                probe_resp.raise_for_status()
                _litellm_last_success_time = current_time
                logger.info("[StreamLLM] LiteLLM 连通性检查通过, 开始流式请求")
            else:
                logger.info("[StreamLLM] LiteLLM 连通性缓存有效(%.0fs内已验证), 跳过探针",
                            current_time - _litellm_last_success_time)

            async with _litellm_stream_client.stream(
                "POST",
                f"{EXTERNAL_LITELLM_URL}/v1/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                logger.info("[StreamLLM] LiteLLM 流式响应开始, status=%d", resp.status_code)
                line_count = 0
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        line_count += 1
                        yield line + "\n\n"
                logger.info("[StreamLLM] LiteLLM 流式响应结束, lines=%d", line_count)
            # 流式请求成功完成，更新连通性缓存时间戳
            _litellm_last_success_time = time.time()
            return
        except Exception as e:
            logger.warning("[StreamLLM] 外部 LiteLLM 失败, fallback 到 Ollama: %s", e)

    # 直接调用 Ollama 流式
    # 模型名需要包含 tag（如 qwen2.5:3b），否则用 DEFAULT_CHAT_MODEL
    ollama_model = model if ":" in model else DEFAULT_CHAT_MODEL
    ollama_payload = {
        "model": ollama_model,
        "messages": [{"role": "user", "content": text}],
        "stream": True,
    }
    try:
        async with _ollama_stream_client.stream(
            "POST",
            f"{OLLAMA_URL}/v1/chat/completions",
            json=ollama_payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    yield line + "\n\n"
    except Exception as e:
        logger.error("[StreamLLM] Ollama 直连也失败: %s", e)
        yield f"data: {{\"error\": \"LLM 调用失败: {e}\"}}\n\n"


async def call_llm_sync(text: str, model: str = DEFAULT_CHAT_MODEL) -> str:
    """同步调用 LLM 获取完整回复。

    优先级: 外部 LiteLLM Proxy → Ollama 直连
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "stream": False,
        "temperature": 0.7,
    }

    if EXTERNAL_LITELLM_URL:
        headers = {"Content-Type": "application/json"}
        if LITELLM_MASTER_KEY:
            headers["Authorization"] = f"Bearer {LITELLM_MASTER_KEY}"
        try:
            if _litellm_sync_client:
                resp = await _litellm_sync_client.post(
                    "/v1/chat/completions",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code == 401:
                    logger.warning("[StreamLLM] LiteLLM 同步请求返回 401, 检查 LITELLM_MASTER_KEY")
                resp.raise_for_status()
                data = resp.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "")
            else:
                logger.warning("[StreamLLM] LiteLLM 同步客户端未初始化 (EXTERNAL_LITELLM_URL 未设置), fallback 到 Ollama")
        except Exception as e:
            logger.warning("[StreamLLM] 外部 LiteLLM 同步失败, fallback 到 Ollama: %s", e)

    # 直接调用 Ollama
    # 模型名需要包含 tag（如 qwen2.5:3b），否则用 DEFAULT_CHAT_MODEL
    ollama_model = model if ":" in model else DEFAULT_CHAT_MODEL
    ollama_payload = {
        "model": ollama_model,
        "messages": [{"role": "user", "content": text}],
        "stream": False,
        "options": {"num_ctx": 4096, "temperature": 0.7},
    }
    try:
        resp = await _ollama_sync_client.post("/v1/chat/completions", json=ollama_payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        logger.error("[StreamLLM] Ollama 同步请求也失败: %s", e)
        return ""


# ── API Router ────────────────────────────────────────────────────

stream_router = APIRouter(tags=["Stream Service"])


@stream_router.websocket("/v1/stream/ws")
async def stream_websocket(websocket: WebSocket):
    """WebSocket 实时推流接口。

    协议:
      1. 前端连接时发送 query params: ?model=qwen2.5&asr_provider=funasr
      2. 前端发送 JSON 消息: {"type": "text", "content": "你好"}
      3. 前端发送二进制消息: 音频数据 (自动 ASR 转文字)
      4. 后端返回 JSON: {"type": "asr_text", "content": "识别的文字"}
      5. 后端返回 JSON: {"type": "llm_chunk", "content": "模型回复片段"}
      6. 后端返回 JSON: {"type": "llm_done", "content": "完整回复"}
      7. 后端返回 JSON: {"type": "error", "content": "错误信息"}
    """
    await websocket.accept()

    model = websocket.query_params.get("model", DEFAULT_CHAT_MODEL)
    asr_provider = websocket.query_params.get("asr_provider", "funasr" if FUNASR_URL else "doubao")

    session_id = str(uuid.uuid4())[:8]
    logger.info("[StreamWS] [%s] 新连接: model=%s asr=%s", session_id, model, asr_provider)

    try:
        while True:
            # 接收消息
            try:
                raw = await asyncio.wait_for(websocket.receive(), timeout=3600)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "error", "content": "连接超时"})
                break

            if raw.get("text"):
                # JSON 文本消息
                try:
                    msg = json.loads(raw["text"])
                except json.JSONDecodeError:
                    msg = {"type": "text", "content": raw["text"]}

                msg_type = msg.get("type", "text")
                content = msg.get("content", "")

                if msg_type == "text" and content:
                    # 文本消息 → 直接调用 LLM
                    logger.info("[StreamWS] [%s] 文本消息: %.50s", session_id, content[:50])
                    full_response = ""
                    chunk_count = 0

                    async for sse_line in stream_llm_response(content, model):
                        # 解析 SSE 数据
                        if sse_line.startswith("data: "):
                            data_str = sse_line[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                delta = data.get("choices", [{}])[0].get("delta", {})
                                chunk_text = delta.get("content", "")
                                if chunk_text:
                                    chunk_count += 1
                                    full_response += chunk_text
                                    await websocket.send_json({
                                        "type": "llm_chunk",
                                        "content": chunk_text,
                                    })
                            except json.JSONDecodeError:
                                pass

                    logger.info("[StreamWS] [%s] LLM 完成, chunks=%d len=%d", session_id, chunk_count, len(full_response))

                    await websocket.send_json({
                        "type": "llm_done",
                        "content": full_response,
                    })

                elif msg_type == "ping":
                    await websocket.send_json({"type": "pong"})

            elif raw.get("bytes"):
                # 二进制音频数据 → ASR → LLM
                audio_data = raw["bytes"]
                logger.info("[StreamWS] [%s] 音频数据: %d bytes", session_id, len(audio_data))

                # ASR 转写
                try:
                    asr_text = await transcribe_audio(audio_data)
                    if not asr_text:
                        await websocket.send_json({"type": "error", "content": "语音识别结果为空"})
                        continue

                    await websocket.send_json({"type": "asr_text", "content": asr_text})
                    logger.info("[StreamWS] [%s] ASR 结果: %.50s", session_id, asr_text[:50])
                except Exception as e:
                    await websocket.send_json({"type": "error", "content": f"语音识别失败: {e}"})
                    continue

                # LLM 回复
                full_response = ""
                async for sse_line in stream_llm_response(asr_text, model):
                    if sse_line.startswith("data: "):
                        data_str = sse_line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            chunk_text = delta.get("content", "")
                            if chunk_text:
                                full_response += chunk_text
                                await websocket.send_json({
                                    "type": "llm_chunk",
                                    "content": chunk_text,
                                })
                        except json.JSONDecodeError:
                            pass

                await websocket.send_json({
                    "type": "llm_done",
                    "content": full_response,
                })

    except WebSocketDisconnect:
        logger.info("[StreamWS] [%s] 客户端断开", session_id)
    except RuntimeError as e:
        if "disconnect" in str(e).lower():
            logger.info("[StreamWS] [%s] 客户端断开 (RuntimeError)", session_id)
        else:
            logger.error("[StreamWS] [%s] 异常: %s", session_id, e, exc_info=True)
    except Exception as e:
        logger.error("[StreamWS] [%s] 异常: %s", session_id, e, exc_info=True)
        try:
            await websocket.send_json({"type": "error", "content": str(e)})
        except Exception:
            pass


@stream_router.websocket("/v1/stream/asr")
async def stream_asr_websocket(websocket: WebSocket):
    """FunASR 实时语音转文字流式推流接口。

    协议:
      1. 前端连接 WebSocket
      2. 前端发送 JSON 初始化: {"type":"config","auto_llm":true,"model":"qwen2.5:3b"}
      3. 前端持续发送二进制 PCM 音频数据 (16kHz 16bit mono)
      4. 后端缓冲音频，定期调用 FunASR 进行转写
      5. 后端返回 JSON: {"type":"asr_partial","content":"部分识别结果","is_final":false}
      6. 后端返回 JSON: {"type":"asr_final","content":"最终识别结果"}
      7. 如果 auto_llm=true，自动调用 LLM 生成回复
      8. 后端返回 JSON: {"type":"llm_chunk","content":"回复片段"}
      9. 后端返回 JSON: {"type":"llm_done","content":"完整回复"}
      10. 前端发送 JSON: {"type":"stop"} 结束当前会话
    """
    await websocket.accept()

    session_id = str(uuid.uuid4())[:8]
    auto_llm = False
    llm_model = DEFAULT_CHAT_MODEL

    # 音频缓冲
    audio_buffer = bytearray()
    last_asr_time = time.time()
    all_asr_text = []  # 累积的所有 ASR 结果
    current_partial = ""  # 当前部分结果

    logger.info("[StreamASR] [%s] 新连接", session_id)

    # 发送欢迎消息
    await websocket.send_json({
        "type": "connected",
        "session_id": session_id,
        "asr_provider": "funasr" if FUNASR_URL else "fallback",
        "funasr_url": FUNASR_URL or "(not configured)",
    })

    # 会话是否已结束（收到 stop 或客户端断开）
    session_done = False

    try:
        while not session_done:
            try:
                raw = await asyncio.wait_for(websocket.receive(), timeout=30)
            except asyncio.TimeoutError:
                # 超时，检查缓冲区是否有数据需要处理
                if len(audio_buffer) > 0:
                    await _flush_asr_buffer(
                        websocket, session_id, audio_buffer,
                        all_asr_text, auto_llm, llm_model,
                    )
                    audio_buffer.clear()
                continue

            # 检测客户端断开（receive() 返回 disconnect 类型）
            if raw.get("type") == "websocket.disconnect":
                logger.info("[StreamASR] [%s] 客户端断开 (disconnect message)", session_id)
                session_done = True
                # 处理残留缓冲
                if len(audio_buffer) > 0:
                    await _flush_asr_buffer(
                        websocket, session_id, audio_buffer,
                        all_asr_text, auto_llm, llm_model,
                    )
                    audio_buffer.clear()
                break

            if raw.get("text"):
                # JSON 控制消息
                try:
                    msg = json.loads(raw["text"])
                except json.JSONDecodeError:
                    msg = {"type": "text", "content": raw["text"]}

                msg_type = msg.get("type", "")

                if msg_type == "config":
                    # 配置消息
                    auto_llm = msg.get("auto_llm", False)
                    llm_model = msg.get("model", DEFAULT_CHAT_MODEL)
                    logger.info("[StreamASR] [%s] 配置: auto_llm=%s model=%s",
                                session_id, auto_llm, llm_model)
                    await websocket.send_json({
                        "type": "config_ack",
                        "auto_llm": auto_llm,
                        "model": llm_model,
                    })

                elif msg_type == "stop":
                    # 停止当前会话，处理剩余缓冲
                    logger.info("[StreamASR] [%s] 收到停止信号", session_id)
                    if len(audio_buffer) > 0:
                        await _flush_asr_buffer(
                            websocket, session_id, audio_buffer,
                            all_asr_text, auto_llm, llm_model,
                        )
                        audio_buffer.clear()

                    # 发送最终结果
                    final_text = " ".join(all_asr_text)
                    await websocket.send_json({
                        "type": "asr_final",
                        "content": final_text,
                        "segments": len(all_asr_text),
                    })

                    # 如果有 LLM 且有文本，自动生成回复
                    if auto_llm and final_text:
                        await _stream_llm_for_asr(websocket, final_text, llm_model)

                    all_asr_text.clear()
                    current_partial = ""

                    # stop 处理完毕，结束会话（不再循环 receive）
                    session_done = True

                elif msg_type == "ping":
                    await websocket.send_json({"type": "pong"})

            elif raw.get("bytes"):
                # 二进制 PCM 音频数据
                audio_chunk = raw["bytes"]
                audio_buffer.extend(audio_chunk)
                logger.debug("[StreamASR] [%s] 收到音频: %d bytes, 缓冲: %d bytes",
                             session_id, len(audio_chunk), len(audio_buffer))

                # 检查是否需要触发 ASR
                now = time.time()
                should_flush = (
                    len(audio_buffer) >= STREAM_ASR_MAX_BYTES or
                    (len(audio_buffer) >= STREAM_ASR_MIN_BYTES and
                     now - last_asr_time >= STREAM_ASR_INTERVAL)
                )

                if should_flush:
                    await _flush_asr_buffer(
                        websocket, session_id, audio_buffer,
                        all_asr_text, auto_llm, llm_model,
                    )
                    audio_buffer.clear()
                    last_asr_time = now

    except WebSocketDisconnect:
        logger.info("[StreamASR] [%s] 客户端断开", session_id)
    except Exception as e:
        logger.error("[StreamASR] [%s] 异常: %s", session_id, e, exc_info=True)
        try:
            await websocket.send_json({"type": "error", "content": str(e)})
        except Exception:
            pass


async def _flush_asr_buffer(
    websocket: WebSocket,
    session_id: str,
    audio_buffer: bytearray,
    all_asr_text: List[str],
    auto_llm: bool,
    llm_model: str,
):
    """将缓冲的 PCM 数据发送给 FunASR 进行转写。"""
    if not audio_buffer:
        return

    # 将 PCM 包装为 WAV
    wav_data = _pcm_to_wav(bytes(audio_buffer))
    logger.info("[StreamASR] [%s] 调用 ASR: %d bytes PCM → %d bytes WAV",
                session_id, len(audio_buffer), len(wav_data))

    try:
        asr_text = await transcribe_audio(wav_data)
        if asr_text:
            all_asr_text.append(asr_text)
            await websocket.send_json({
                "type": "asr_partial",
                "content": asr_text,
                "is_final": False,
                "buffer_ms": len(audio_buffer) // 32,  # 16kHz*2bytes=32bytes/ms
            })
            logger.info("[StreamASR] [%s] ASR 结果: %s", session_id, asr_text[:80])

            # 如果 auto_llm 且累积了足够文本，触发 LLM
            if auto_llm and len(all_asr_text) >= 2:
                combined = " ".join(all_asr_text)
                await _stream_llm_for_asr(websocket, combined, llm_model)
                all_asr_text.clear()
        else:
            await websocket.send_json({
                "type": "asr_partial",
                "content": "",
                "is_final": False,
                "buffer_ms": len(audio_buffer) // 32,
            })
    except Exception as e:
        logger.warning("[StreamASR] [%s] ASR 失败: %s", session_id, e)
        await websocket.send_json({
            "type": "error",
            "content": f"ASR 失败: {e}",
        })


async def _stream_llm_for_asr(websocket: WebSocket, text: str, model: str):
    """为 ASR 结果流式调用 LLM。"""
    full_response = ""
    async for sse_line in stream_llm_response(text, model):
        if sse_line.startswith("data: "):
            data_str = sse_line[6:].strip()
            if data_str == "[DONE]":
                break
            try:
                data = json.loads(data_str)
                delta = data.get("choices", [{}])[0].get("delta", {})
                chunk_text = delta.get("content", "")
                if chunk_text:
                    full_response += chunk_text
                    await websocket.send_json({
                        "type": "llm_chunk",
                        "content": chunk_text,
                    })
            except json.JSONDecodeError:
                pass

    await websocket.send_json({
        "type": "llm_done",
        "content": full_response,
    })


@stream_router.get("/v1/stream/sse")
async def stream_sse(request: Request):
    """SSE 单向推流接口。

    Query params:
      - prompt: 要发送的文本
      - model: 模型名称 (默认 qwen2.5)
    """
    prompt = request.query_params.get("prompt", "")
    model = request.query_params.get("model", DEFAULT_CHAT_MODEL)

    if not prompt:
        return StreamingResponse(
            iter(["data: {\"error\": \"缺少 prompt 参数\"}\n\n"]),
            media_type="text/event-stream",
        )

    logger.info("[StreamSSE] 新请求: model=%s prompt=%.50s", model, prompt[:50])

    async def event_generator():
        try:
            async for sse_line in stream_llm_response(prompt, model):
                yield sse_line
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {{\"error\": \"{e}\"}}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@stream_router.post("/v1/stream/asr/file")
async def stream_asr_file(request: Request):
    """上传音频文件进行 ASR 转写（非流式，用于测试）。

    Body: multipart/form-data
      - file: 音频文件 (wav/mp3)
      - model: (可选) FunASR 模型名
    """
    form = await request.form()
    audio_file = form.get("file")
    if not audio_file:
        return {"error": "缺少 file 字段"}

    audio_bytes = await audio_file.read()
    filename = audio_file.filename or "audio.wav"
    logger.info("[StreamASR] 文件转写: %s (%d bytes)", filename, len(audio_bytes))

    asr_text = await transcribe_audio(audio_bytes)
    return {
        "filename": filename,
        "text": asr_text,
        "provider": "funasr" if FUNASR_URL else "fallback",
        "size": len(audio_bytes),
    }


@stream_router.get("/v1/stream/health")
async def stream_health():
    """Stream Service 健康检查。"""
    # 检查 FunASR 连通性
    funasr_ok = False
    if FUNASR_URL and _funasr_health_client:
        try:
            resp = await _funasr_health_client.get("/health")
            funasr_ok = resp.status_code == 200
        except Exception:
            pass

    return {
        "status": "healthy",
        "service": "stream-service",
        "ollama_url": OLLAMA_URL,
        "litellm_url": EXTERNAL_LITELLM_URL or "(direct ollama)",
        "asr_providers": {
            "funasr": {
                "configured": bool(FUNASR_URL),
                "url": FUNASR_URL or "(not configured)",
                "model": FUNASR_MODEL,
                "healthy": funasr_ok,
            },
            "doubao": {
                "configured": bool(DOUBAO_ASR_KEY),
            },
            "ollama_whisper": {
                "available": True,
            },
        },
        "stream_asr": {
            "min_bytes": STREAM_ASR_MIN_BYTES,
            "interval": STREAM_ASR_INTERVAL,
            "max_bytes": STREAM_ASR_MAX_BYTES,
        },
        "endpoints": {
            "ws_chat": "/v1/stream/ws",
            "ws_asr": "/v1/stream/asr",
            "sse": "/v1/stream/sse",
            "asr_file": "/v1/stream/asr/file",
        },
    }


# ── 独立服务入口 ─────────────────────────────────────────────────

def create_app() -> FastAPI:
    """创建 Stream Service 独立 FastAPI 应用。"""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        await _init_persistent_clients()
        yield
        await _close_persistent_clients()

    app = FastAPI(
        title="Stream Service",
        description="实时推流服务 — WebSocket/SSE + FunASR/豆包 ASR",
        version="2.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(stream_router)

    # 挂载 litellm_proxy 路由，支持 /v1/chat/completions 等多模态端点
    try:
        from hermes.litellm_proxy import litellm_router
        app.include_router(litellm_router)
        logger.info("litellm_router 已挂载到 stream-service")
    except Exception as e:
        logger.warning(f"litellm_router 挂载失败: {e}")

    @app.get("/health")
    async def health():
        return {"status": "healthy", "service": "stream-service", "port": 8084}

    return app


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    port = int(os.getenv("STREAM_PORT", "8084"))
    logger.info("Stream Service starting on :%d", port)
    logger.info("  Ollama URL: %s", OLLAMA_URL)
    logger.info("  External LiteLLM URL: %s", EXTERNAL_LITELLM_URL or "(direct ollama)")
    logger.info("  FunASR URL: %s", FUNASR_URL or "(not configured)")
    logger.info("  ASR configured: funasr=%s doubao=%s", bool(FUNASR_URL), bool(DOUBAO_ASR_KEY))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")
