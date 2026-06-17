"""
Stream Service 独立服务 — 实时推流服务

独立运行: python -m hermes.stream_service  (端口 8084)

提供 WebSocket 和 SSE 两种推流方式:
  1. WebSocket (/v1/stream/ws): 双向通信，前端发送音频 → ASR → LLM → 文字回复
  2. SSE (/v1/stream/sse): 单向推流，前端发送请求后接收流式文字

ASR 集成:
  - 豆包（火山引擎）云端 ASR
  - 本地 Ollama Whisper 模型作为 fallback
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Dict, Optional

import httpx
from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────────

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
# 外部 LiteLLM Proxy 地址（Docker 部署时使用）；为空则直接调用 Ollama
EXTERNAL_LITELLM_URL = os.getenv("EXTERNAL_LITELLM_URL", "")
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")

# ASR 配置
DOUBAO_ASR_KEY = os.getenv("DOUBAO_ASR_KEY", "")
DOUBAO_ASR_URL = os.getenv("DOUBAO_ASR_URL", "https://openspeech.bytedance.com/api/v1/auc/recognize")

DEFAULT_CHAT_MODEL = os.getenv("DEFAULT_CHAT_MODEL", "qwen2.5:3b")

# ── ASR 引擎 ─────────────────────────────────────────────────────

async def transcribe_with_doubao(audio_chunk: bytes, format: str = "wav") -> str:
    """调用豆包 ASR 将音频转为文字。"""
    if not DOUBAO_ASR_KEY:
        raise RuntimeError("DOUBAO_ASR_KEY 未配置")

    headers = {
        "Authorization": f"Bearer {DOUBAO_ASR_KEY}",
        "Content-Type": "application/octet-stream",
    }
    params = {"format": format, "rate": "16000"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
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

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("response", "")
        except Exception as e:
            logger.warning("[ASR] Ollama Whisper 失败: %s", e)
            return ""


async def transcribe_audio(audio_chunk: bytes, format: str = "wav") -> str:
    """ASR 转写: 优先豆包，fallback 到 Ollama。"""
    try:
        if DOUBAO_ASR_KEY:
            return await transcribe_with_doubao(audio_chunk, format)
    except Exception as e:
        logger.warning("[ASR] 豆包 ASR 失败, 尝试 Ollama fallback: %s", e)

    try:
        return await transcribe_with_ollama(audio_chunk)
    except Exception as e:
        logger.error("[ASR] 全部 ASR 引擎失败: %s", e)
        return ""


# ── LLM 流式调用 ─────────────────────────────────────────────────

async def stream_llm_response(text: str, model: str = DEFAULT_CHAT_MODEL):
    """调用 LLM 并以 SSE 格式流式返回。

    优先级: 外部 LiteLLM Proxy → Ollama 直连
    """
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
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST",
                    f"{EXTERNAL_LITELLM_URL}/v1/chat/completions",
                    json=payload,
                    headers=headers,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            yield line + "\n\n"
            return
        except Exception as e:
            logger.warning("[StreamLLM] 外部 LiteLLM 流式失败, fallback 到 Ollama: %s", e)

    # 直接调用 Ollama 流式
    ollama_model = model.split("/")[-1] if "/" in model else model
    ollama_payload = {
        "model": ollama_model,
        "messages": [{"role": "user", "content": text}],
        "stream": True,
    }
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream(
            "POST",
            f"{OLLAMA_URL}/v1/chat/completions",
            json=ollama_payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    yield line + "\n\n"


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
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{EXTERNAL_LITELLM_URL}/v1/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning("[StreamLLM] 外部 LiteLLM 同步失败, fallback 到 Ollama: %s", e)

    # 直接调用 Ollama
    ollama_model = model.split("/")[-1] if "/" in model else model
    ollama_payload = {
        "model": ollama_model,
        "messages": [{"role": "user", "content": text}],
        "stream": False,
        "options": {"num_ctx": 4096, "temperature": 0.7},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{OLLAMA_URL}/v1/chat/completions", json=ollama_payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")


# ── API Router ────────────────────────────────────────────────────

stream_router = APIRouter(tags=["Stream Service"])


@stream_router.websocket("/v1/stream/ws")
async def stream_websocket(websocket: WebSocket):
    """WebSocket 实时推流接口。

    协议:
      1. 前端连接时发送 query params: ?model=qwen2.5&asr_provider=doubao
      2. 前端发送 JSON 消息: {"type": "text", "content": "你好"}
      3. 前端发送二进制消息: 音频数据 (自动 ASR 转文字)
      4. 后端返回 JSON: {"type": "asr_text", "content": "识别的文字"}
      5. 后端返回 JSON: {"type": "llm_chunk", "content": "模型回复片段"}
      6. 后端返回 JSON: {"type": "llm_done", "content": "完整回复"}
      7. 后端返回 JSON: {"type": "error", "content": "错误信息"}
    """
    await websocket.accept()

    model = websocket.query_params.get("model", DEFAULT_CHAT_MODEL)
    asr_provider = websocket.query_params.get("asr_provider", "doubao")

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
    except Exception as e:
        logger.error("[StreamWS] [%s] 异常: %s", session_id, e, exc_info=True)
        try:
            await websocket.send_json({"type": "error", "content": str(e)})
        except Exception:
            pass


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


@stream_router.get("/v1/stream/health")
async def stream_health():
    """Stream Service 健康检查。"""
    return {
        "status": "healthy",
        "service": "stream-service",
        "ollama_url": OLLAMA_URL,
        "litellm_url": EXTERNAL_LITELLM_URL or "(direct ollama)",
        "asr_configured": bool(DOUBAO_ASR_KEY),
    }


# ── 独立服务入口 ─────────────────────────────────────────────────

def create_app() -> FastAPI:
    """创建 Stream Service 独立 FastAPI 应用。"""
    app = FastAPI(
        title="Stream Service",
        description="实时推流服务 — WebSocket/SSE + 豆包 ASR",
        version="1.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(stream_router)

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
    logger.info("  ASR configured: %s", bool(DOUBAO_ASR_KEY))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")
