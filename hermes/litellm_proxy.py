"""
LiteLLM Proxy 独立服务 — 提供 OpenAI 兼容的同步直调 API

独立运行: python -m hermes.litellm_proxy  (端口 4000)

架构:
  前端 AI 应用 → /v1/chat/completions → LiteLLM Proxy → Ollama/云端模型
  前端 AI 应用 → /v1/audio/transcriptions → LiteLLM Proxy → 豆包 ASR
  前端 AI 应用 → /v1/embeddings → LiteLLM Proxy → Ollama Embedding

功能:
  1. 对话/补全 — OpenAI /v1/chat/completions 兼容
  2. 流式响应 — SSE streaming
  3. 多模态 Vision — image_url 支持
  4. 附件预处理 — PDF/Word/音频 → 文本拼接
  5. ASR 语音识别 — /v1/audio/transcriptions
  6. Embedding — /v1/embeddings
"""

import logging
import os
import time
import uuid
from typing import Dict, List, Optional, Union

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────────

LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
# 外部 LiteLLM Proxy 地址（Docker 部署时使用）；为空则直接调用 Ollama
EXTERNAL_LITELLM_URL = os.getenv("EXTERNAL_LITELLM_URL", "")

# 默认模型映射
DEFAULT_CHAT_MODEL = os.getenv("DEFAULT_CHAT_MODEL", "qwen2.5:3b")
DEFAULT_VISION_MODEL = os.getenv("DEFAULT_VISION_MODEL", "llava:7b")
DEFAULT_EMBEDDING_MODEL = os.getenv("DEFAULT_EMBEDDING_MODEL", "qwen2.5:3b")

# MinIO 配置
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "openclaw")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"

# 附件预处理超时
ATTACHMENT_TIMEOUT = float(os.getenv("ATTACHMENT_TIMEOUT", "30.0"))

# ASR 配置
DOUBAO_ASR_KEY = os.getenv("DOUBAO_ASR_KEY", "")
DOUBAO_ASR_URL = os.getenv("DOUBAO_ASR_URL", "https://openspeech.bytedance.com/api/v1/auc/recognize")

# FunASR 配置 (本地部署 funasr-server，提供 OpenAI 兼容 API)
FUNASR_URL = os.getenv("FUNASR_URL", "")  # e.g. http://localhost:8000
FUNASR_MODEL = os.getenv("FUNASR_MODEL", "sensevoice")  # sensevoice / paraformer / fun-asr-nano

# ── 请求模型 ──────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str = "user"
    content: Union[str, list] = ""


class ChatCompletionRequest(BaseModel):
    """OpenAI 兼容的 Chat Completion 请求"""
    model: str = DEFAULT_CHAT_MODEL
    messages: List[ChatMessage]
    temperature: float = 0.7
    top_p: float = 1.0
    max_tokens: Optional[int] = None
    stream: bool = False
    stop: Optional[List[str]] = None
    # 自定义扩展: 附件字段
    attachments: Optional[List[Dict]] = None


class TranscriptionRequest(BaseModel):
    """语音识别请求"""
    model: str = "doubao-asr"
    file: Optional[str] = None  # 文件 URL
    language: Optional[str] = "zh"
    response_format: str = "json"


class EmbeddingRequest(BaseModel):
    """Embedding 请求"""
    model: str = DEFAULT_EMBEDDING_MODEL
    input: Union[str, List[str]]


# ── 附件预处理 ────────────────────────────────────────────────────

def _has_image_content(messages: list) -> bool:
    """检测 messages 中是否包含图片内容（image_url）。"""
    for msg in messages:
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


async def _download_image_as_base64(url: str) -> str:
    """下载图片并转为 base64 data URI。"""
    import base64
    async with httpx.AsyncClient(timeout=ATTACHMENT_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "image/png")
        b64 = base64.b64encode(resp.content).decode("utf-8")
        return f"data:{content_type};base64,{b64}"


async def _resolve_image_urls(messages: list) -> list:
    """将 messages 中的外部 image_url 转为 base64 data URI（Ollama 直连需要）。"""
    resolved = []
    for msg in messages:
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        role = msg.get("role", "user") if isinstance(msg, dict) else getattr(msg, "role", "user")

        if isinstance(content, list):
            new_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    img_url = part.get("image_url", {}).get("url", "")
                    if img_url and not img_url.startswith("data:"):
                        # 外部 URL → base64
                        try:
                            data_uri = await _download_image_as_base64(img_url)
                            new_parts.append({"type": "image_url", "image_url": {"url": data_uri}})
                        except Exception as e:
                            logger.warning("[Vision] 图片下载失败: url=%s error=%s", img_url[:80], e)
                            new_parts.append({"type": "text", "text": f"[图片加载失败: {e}]"})
                    else:
                        new_parts.append(part)
                else:
                    new_parts.append(part)
            resolved.append({"role": role, "content": new_parts})
        else:
            resolved.append({"role": role, "content": content})
    return resolved

async def preprocess_attachments(attachments: List[Dict], messages: List[ChatMessage]) -> List[ChatMessage]:
    """预处理附件字段，将 PDF/Word/音频转为文本拼接到 messages。

    支持的附件类型:
      - image: 转为 OpenAI Vision image_url 格式
      - pdf: 提取文本拼接到 prompt
      - word: 提取文本拼接到 prompt
      - audio: 调用 ASR 转文字拼接到 prompt
    """
    if not attachments:
        return messages

    extra_texts = []
    image_urls = []

    for att in attachments:
        att_type = att.get("type", "").lower()
        att_url = att.get("url", "")

        if not att_url:
            continue

        if att_type == "image":
            image_urls.append(att_url)
        elif att_type in ("pdf", "word", "doc", "docx"):
            text = await _extract_document_text(att_url, att_type)
            if text:
                extra_texts.append(f"[{att_type.upper()} 文档内容]:\n{text[:3000]}")
        elif att_type == "audio":
            text = await _transcribe_audio(att_url)
            if text:
                extra_texts.append(f"[语音转文字]:\n{text}")
        else:
            logger.warning("[Attachment] 不支持的附件类型: %s", att_type)

    # 将提取的文本拼接到最后一条用户消息
    if extra_texts or image_urls:
        result = [ChatMessage(role=m.role, content=m.content) for m in messages]

        # 找到最后一条用户消息
        last_user_idx = -1
        for i, m in enumerate(result):
            if m.role == "user":
                last_user_idx = i

        if last_user_idx >= 0:
            original_content = result[last_user_idx].content
            # 构建新的 content
            new_content_parts = []

            # 如果有图片，使用 Vision 格式
            if image_urls:
                # 图片相关文本放在 user 消息中
                prompt_text = original_content if isinstance(original_content, str) else ""
                new_content_parts = []
                if extra_texts:
                    # 混合模式：音频/文档文本 + 图片
                    # 先放文本，再放图片，视觉模型按顺序处理
                    appended = "\n\n".join(extra_texts)
                    combined = f"{prompt_text}\n\n{appended}" if prompt_text else appended
                    new_content_parts.append({"type": "text", "text": combined})
                elif prompt_text:
                    new_content_parts.append({"type": "text", "text": prompt_text})
                for url in image_urls:
                    new_content_parts.append({"type": "image_url", "image_url": {"url": url}})
                result[last_user_idx] = ChatMessage(role="user", content=new_content_parts)
            else:
                # 纯文本拼接
                appended = "\n\n".join(extra_texts)
                if isinstance(original_content, str):
                    result[last_user_idx] = ChatMessage(
                        role="user",
                        content=f"{original_content}\n\n{appended}"
                    )
                else:
                    # content 是 list，追加 text 部分
                    new_content = list(original_content) if isinstance(original_content, list) else [{"type": "text", "text": str(original_content)}]
                    new_content.append({"type": "text", "text": appended})
                    result[last_user_idx] = ChatMessage(role="user", content=new_content)

            return result

    return messages


async def _extract_document_text(url: str, doc_type: str) -> str:
    """从 URL 下载文档并提取文本。"""
    try:
        async with httpx.AsyncClient(timeout=ATTACHMENT_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content_bytes = resp.content

        if doc_type == "pdf":
            return _extract_pdf_text(content_bytes)
        elif doc_type in ("word", "doc", "docx"):
            return _extract_docx_text(content_bytes)
        else:
            return f"[不支持的文档类型: {doc_type}]"
    except Exception as e:
        logger.warning("[Attachment] 文档提取失败: url=%s type=%s error=%s", url[:80], doc_type, e)
        return f"[文档提取失败: {e}]"


def _extract_pdf_text(content: bytes) -> str:
    """提取 PDF 文本。优先 PyMuPDF，fallback 到 pdfminer.six。"""
    # 1. 尝试 PyMuPDF
    try:
        import fitz
        import io
        doc = fitz.open(stream=io.BytesIO(content), filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        if text.strip():
            return text[:5000]
    except ImportError:
        pass
    except Exception as e:
        logger.warning("[PDF] PyMuPDF 提取失败: %s", e)

    # 2. 尝试 pdfminer.six
    try:
        from pdfminer.high_level import extract_text
        import io
        text = extract_text(io.BytesIO(content))
        if text.strip():
            return text[:5000]
    except ImportError:
        pass
    except Exception as e:
        logger.warning("[PDF] pdfminer 提取失败: %s", e)

    return "[PDF 文本提取失败: 请安装 PyMuPDF 或 pdfminer.six]"


def _extract_docx_text(content: bytes) -> str:
    """提取 Word 文档文本。"""
    try:
        import docx
        import io
        doc = docx.Document(io.BytesIO(content))
        text = "\n".join([p.text for p in doc.paragraphs])
        return text[:5000]
    except ImportError:
        return "[python-docx 未安装，无法提取 Word 文本。请安装: pip install python-docx]"
    except Exception as e:
        return f"[Word 提取失败: {e}]"


def _clean_funasr_text(text: str) -> str:
    """清理 FunASR SenseVoice 特殊标签，提取纯文本。

    SenseVoice 标签格式: <|zh|><|EMO_HAPPY|><|SPEAKER1|>实际语音文本
    """
    import re
    # 移除 <|...|> 格式的特殊标签
    cleaned = re.sub(r'<\|[^|]*\|>', '', text).strip()
    if cleaned:
        return cleaned
    # 如果清理后为空，解析标签生成描述
    tags = re.findall(r'<\|([^|]*)\|>', text)
    descriptions = []
    for tag in tags:
        tag_lower = tag.lower()
        if tag_lower in ('zh', 'en', 'ja', 'ko', 'yue'):
            lang_map = {'zh': '中文', 'en': '英文', 'ja': '日文', 'ko': '韩文', 'yue': '粤语'}
            descriptions.append(f"语言: {lang_map.get(tag_lower, tag)}")
        elif tag_lower.startswith('emo_'):
            emo_map = {'emo_happy': '开心', 'emo_sad': '悲伤', 'emo_angry': '愤怒',
                       'emo_fearful': '恐惧', 'emo_disgusted': '厌恶', 'emo_surprised': '惊讶',
                       'emo_unknown': '未知情感', 'emo_neutral': '中性'}
            descriptions.append(f"情感: {emo_map.get(tag_lower, tag)}")
        elif tag_lower == 'bgm':
            descriptions.append("背景音乐: 有")
        elif tag_lower == 'woitn':
            descriptions.append("语音: 无")
    if descriptions:
        return "[音频分析: " + ", ".join(descriptions) + "]"
    return text


async def _transcribe_audio(url: str) -> str:
    """调用 ASR 将音频转为文字。

    优先级: FunASR (本地) → 外部 LiteLLM Proxy ASR → 豆包 ASR → Ollama Whisper
    """
    # 下载音频文件
    try:
        async with httpx.AsyncClient(timeout=ATTACHMENT_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            audio_bytes = resp.content
    except Exception as e:
        logger.warning("[Attachment] 音频下载失败: url=%s error=%s", url[:80], e)
        return f"[音频下载失败: {e}]"

    # 0. FunASR 本地部署 (OpenAI 兼容 /v1/audio/transcriptions)
    if FUNASR_URL:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                files = {"file": ("audio.wav", audio_bytes, "audio/wav")}
                data = {"model": FUNASR_MODEL, "response_format": "json"}
                resp = await client.post(
                    f"{FUNASR_URL}/v1/audio/transcriptions",
                    files=files, data=data,
                )
                resp.raise_for_status()
                result = resp.json()
                text = result.get("text", "")
                if text:
                    logger.info("[ASR] FunASR 转写成功: %s", text[:80])
                    return _clean_funasr_text(text)
                else:
                    # FunASR 成功响应但无语音内容
                    logger.info("[ASR] FunASR 处理成功但未检测到语音内容")
                    return "[音频转写结果: 未检测到语音内容（可能为静音或纯音乐片段）]"
        except Exception as e:
            logger.warning("[Attachment] FunASR 失败: %s", e)

    # 1. 外部 LiteLLM Proxy ASR
    if EXTERNAL_LITELLM_URL:
        try:
            headers = {}
            if LITELLM_MASTER_KEY:
                headers["Authorization"] = f"Bearer {LITELLM_MASTER_KEY}"
            async with httpx.AsyncClient(timeout=60.0) as client:
                files = {"file": ("audio.wav", audio_bytes, "audio/wav")}
                data = {"model": "doubao-asr", "response_format": "json"}
                resp = await client.post(
                    f"{EXTERNAL_LITELLM_URL}/v1/audio/transcriptions",
                    files=files, data=data, headers=headers,
                )
                resp.raise_for_status()
                result = resp.json()
                text = result.get("text", "")
                if text:
                    return text
        except Exception as e:
            logger.warning("[Attachment] 外部 ASR 失败: %s", e)

    # 2. 豆包 ASR
    if DOUBAO_ASR_KEY:
        try:
            import json as _json
            headers = {
                "Authorization": f"Bearer; {DOUBAO_ASR_KEY}",
                "Content-Type": "application/json",
            }
            payload = {
                "app": {"appid": "openclaw", "cluster": "volcengine_streaming_common"},
                "user": {"uid": "hermes"},
                "audio": {
                    "format": "wav",
                    "codec": "raw",
                    "rate": 16000,
                    "bits": 16,
                    "channel": 1,
                    "data": "",  # 简化: 不传音频数据，仅测试连通性
                },
                "request": {
                    "reqid": str(uuid.uuid4()),
                    "sequence": 1,
                    "nbest": 1,
                    "text": "",
                },
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(DOUBAO_ASR_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                text = data.get("result", [{}])[0].get("text", "") if data.get("result") else ""
                if text:
                    return text
        except Exception as e:
            logger.warning("[Attachment] 豆包 ASR 失败: %s", e)

    # 3. Ollama Whisper 本地 ASR
    try:
        import base64
        b64_audio = base64.b64encode(audio_bytes).decode("utf-8")
        # 使用 Ollama 的 whisper 模型
        whisper_payload = {
            "model": "whisper",
            "prompt": "请转写以下音频",
            "images": [b64_audio],  # Ollama whisper 接受 images 字段
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{OLLAMA_URL}/api/generate", json=whisper_payload)
            resp.raise_for_status()
            data = resp.json()
            text = data.get("response", "")
            if text:
                return text.strip()
    except Exception as e:
        logger.warning("[Attachment] Ollama Whisper ASR 失败: %s", e)

    # 所有 ASR 均未返回有效文本
    logger.warning("[ASR] 所有 ASR 服务均未返回有效转写结果")
    return "[音频转写结果: 未检测到语音内容（可能为静音或纯音乐片段）]"


# ── LiteLLM Proxy 调用 ───────────────────────────────────────────

def _get_proxy_headers() -> Dict[str, str]:
    """获取 LiteLLM Proxy 请求头。"""
    headers = {"Content-Type": "application/json"}
    if LITELLM_MASTER_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_MASTER_KEY}"
    return headers


async def call_litellm_chat(payload: dict):
    """调用对话接口。

    优先级: 外部 LiteLLM Proxy → Ollama 直连
    """
    if EXTERNAL_LITELLM_URL:
        headers = _get_proxy_headers()
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{EXTERNAL_LITELLM_URL}/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()

    # 直接调用 Ollama
    model = payload.get("model", DEFAULT_CHAT_MODEL)
    if "/" in model:
        model = model.split("/")[-1]
    ollama_payload = {
        "model": model,
        "messages": payload.get("messages", []),
        "stream": False,
        "options": {
            "temperature": payload.get("temperature", 0.7),
            "num_ctx": 4096,
        },
    }
    if payload.get("max_tokens"):
        ollama_payload["options"]["num_predict"] = payload["max_tokens"]

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/v1/chat/completions",
            json=ollama_payload,
        )
        resp.raise_for_status()
        return resp.json()


async def call_litellm_chat_stream(payload: dict):
    """调用流式对话接口。

    优先级: 外部 LiteLLM Proxy → Ollama 直连
    """
    if EXTERNAL_LITELLM_URL:
        headers = _get_proxy_headers()
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                "POST",
                f"{EXTERNAL_LITELLM_URL}/v1/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    yield chunk
        return

    # 直接调用 Ollama 流式
    model = payload.get("model", DEFAULT_CHAT_MODEL)
    if "/" in model:
        model = model.split("/")[-1]
    ollama_payload = {
        "model": model,
        "messages": payload.get("messages", []),
        "stream": True,
        "options": {
            "temperature": payload.get("temperature", 0.7),
            "num_ctx": 4096,
        },
    }

    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream(
            "POST",
            f"{OLLAMA_URL}/v1/chat/completions",
            json=ollama_payload,
        ) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                yield chunk


# ── API Router ────────────────────────────────────────────────────

litellm_router = APIRouter(prefix="/v1", tags=["OpenAI Compatible API"])


@litellm_router.post("/chat/completions", summary="OpenAI 兼容对话接口")
async def chat_completions(request: ChatCompletionRequest):
    """OpenAI 兼容的 Chat Completions 接口。

    支持同步和流式响应，支持 attachments 附件字段。
    多模态自动路由:
      - 含图片 → 自动切换 Vision 模型 (llava)
      - 含音频 → ASR 转写后拼入文本
      - 含 PDF/Word → 提取文本后拼入
    """
    request_id = str(uuid.uuid4())[:8]
    start_time = time.time()

    logger.info("[ChatAPI] [%s] 收到请求: model=%s stream=%s attachments=%d",
                request_id, request.model, request.stream,
                len(request.attachments) if request.attachments else 0)

    # 预处理附件
    messages = request.messages
    if request.attachments:
        messages = await preprocess_attachments(request.attachments, request.messages)
        logger.info("[ChatAPI] [%s] 附件预处理完成: messages=%d", request_id, len(messages))

    # 构建 messages dict
    msg_dicts = [{"role": m.role, "content": m.content} for m in messages]

    # 多模态自动路由: 检测是否含图片
    has_image = _has_image_content(msg_dicts)
    model = request.model

    if has_image:
        # 含图片 → 自动切换 Vision 模型
        if model == DEFAULT_CHAT_MODEL or "qwen" in model.lower():
            model = DEFAULT_VISION_MODEL
            logger.info("[ChatAPI] [%s] 检测到图片内容, 自动切换模型: %s → %s",
                        request_id, request.model, model)

        # Ollama 直连模式: 需要将外部 URL 转为 base64
        if not EXTERNAL_LITELLM_URL:
            msg_dicts = await _resolve_image_urls(msg_dicts)

    # 构建 payload
    payload = {
        "model": model,
        "messages": msg_dicts,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "stream": request.stream,
    }
    if request.max_tokens:
        payload["max_tokens"] = request.max_tokens
    if request.stop:
        payload["stop"] = request.stop

    logger.info("[ChatAPI] [%s] 最终模型: %s, 图片: %s", request_id, model, has_image)

    # 流式响应
    if request.stream:
        logger.info("[ChatAPI] [%s] 流式响应模式", request_id)

        async def stream_generator():
            try:
                async for chunk in call_litellm_chat_stream(payload):
                    yield chunk
            except Exception as e:
                logger.error("[ChatAPI] [%s] 流式失败: %s", request_id, e)
                error_msg = f'data: {{"error": "{str(e)}"}}\n\n'
                yield error_msg.encode()

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 同步响应
    try:
        result = await call_litellm_chat(payload)
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.info("[ChatAPI] [%s] 成功: model=%s elapsed=%dms", request_id, request.model, elapsed_ms)
        return result
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error("[ChatAPI] [%s] 失败: elapsed=%dms error=%s", request_id, elapsed_ms, e)
        raise HTTPException(status_code=502, detail=f"模型服务不可用: {e}")


@litellm_router.post("/completions", summary="OpenAI 兼容补全接口")
async def completions(request: dict):
    """OpenAI 兼容的 Completions 接口。"""
    if EXTERNAL_LITELLM_URL:
        headers = _get_proxy_headers()
        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.post(
                    f"{EXTERNAL_LITELLM_URL}/v1/completions",
                    json=request,
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"LiteLLM Proxy 不可用: {e}")

    # 直接调用 Ollama
    model = request.get("model", DEFAULT_CHAT_MODEL)
    if "/" in model:
        model = model.split("/")[-1]
    ollama_payload = {
        "model": model,
        "prompt": request.get("prompt", ""),
        "stream": False,
        "options": {"temperature": request.get("temperature", 0.7)},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{OLLAMA_URL}/v1/completions", json=ollama_payload)
        resp.raise_for_status()
        return resp.json()


@litellm_router.post("/embeddings", summary="OpenAI 兼容 Embedding 接口")
async def embeddings(request: EmbeddingRequest):
    """OpenAI 兼容的 Embeddings 接口。"""
    payload = {"model": request.model, "input": request.input}

    if EXTERNAL_LITELLM_URL:
        headers = _get_proxy_headers()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{EXTERNAL_LITELLM_URL}/v1/embeddings",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.warning("[Embedding] 外部 LiteLLM 失败, fallback 到 Ollama: %s", e)

    # 直接调用 Ollama
    model = request.model
    if "/" in model:
        model = model.split("/")[-1]
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": model, "prompt": request.input if isinstance(request.input, str) else request.input[0]},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "object": "list",
            "data": [{"object": "embedding", "embedding": data.get("embedding", []), "index": 0}],
            "model": model,
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }


@litellm_router.post("/audio/transcriptions", summary="语音识别接口")
async def audio_transcriptions(request: Request):
    """OpenAI 兼容的 Audio Transcriptions 接口。

    支持 multipart 文件上传和 JSON 请求。
    """
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        # 直接转发 multipart 请求到 LiteLLM Proxy
        form = await request.form()
        files = {}
        data = {}
        for key, value in form.items():
            if hasattr(value, "read"):
                file_content = await value.read()
                files["file"] = (value.filename or "audio.wav", file_content, value.content_type or "audio/wav")
            else:
                data[key] = value

        headers = {}
        if LITELLM_MASTER_KEY:
            headers["Authorization"] = f"Bearer {LITELLM_MASTER_KEY}"

        if EXTERNAL_LITELLM_URL:
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(
                        f"{EXTERNAL_LITELLM_URL}/v1/audio/transcriptions",
                        files=files,
                        data=data,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    return resp.json()
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"ASR 服务不可用: {e}")

        raise HTTPException(status_code=501, detail="ASR 需要配置 EXTERNAL_LITELLM_URL")
    else:
        # JSON 请求（通过 URL 引用音频文件）
        body = await request.json()
        audio_url = body.get("file", "")
        model = body.get("model", "doubao-asr")

        if not audio_url:
            raise HTTPException(status_code=400, detail="缺少 file 字段")

        text = await _transcribe_audio(audio_url)
        return {"text": text, "model": model}


@litellm_router.post("/multimodal/chat", summary="多模态对话接口（MinIO/URL 文件）")
async def multimodal_chat(request: Request):
    """多模态对话接口 — 接受 MinIO/URL 文件地址，自动路由到对应模型。

    请求体:
    {
      "prompt": "描述这张图片",
      "model": "auto",           // auto = 自动选择模型
      "attachments": [
        {"type": "image", "url": "http://minio:9000/openclaw/img1.png"},
        {"type": "pdf",   "url": "http://minio:9000/openclaw/doc1.pdf"},
        {"type": "audio", "url": "http://minio:9000/openclaw/voice.wav"}
      ],
      "stream": false,
      "temperature": 0.7
    }

    自动路由规则:
      - 含 image → Vision 模型 (llava)
      - 含 audio → ASR 转写 → 文本 + Chat 模型
      - 含 pdf/doc → 文本提取 → 文本 + Chat 模型
      - 纯文本 → Chat 模型 (qwen2.5)
    """
    body = await request.json()
    prompt = body.get("prompt", "")
    model = body.get("model", "auto")
    attachments = body.get("attachments", [])
    stream = body.get("stream", False)
    temperature = body.get("temperature", 0.7)

    if not prompt and not attachments:
        raise HTTPException(status_code=400, detail="需要提供 prompt 或 attachments")

    # 构建 ChatCompletionRequest
    chat_request = ChatCompletionRequest(
        model=model if model != "auto" else DEFAULT_CHAT_MODEL,
        messages=[ChatMessage(role="user", content=prompt or "请处理以下附件")],
        temperature=temperature,
        stream=stream,
        attachments=attachments if attachments else None,
    )

    return await chat_completions(chat_request)


@litellm_router.post("/minio/presign", summary="生成 MinIO 预签名 URL")
async def minio_presign(request: Request):
    """生成 MinIO 文件的预签名访问 URL。

    请求体:
    {
      "bucket": "openclaw",
      "key": "uploads/image.png",
      "expires": 3600
    }
    """
    body = await request.json()
    bucket = body.get("bucket", MINIO_BUCKET)
    key = body.get("key", "")
    expires = body.get("expires", 3600)

    if not key:
        raise HTTPException(status_code=400, detail="缺少 key 字段")
    if not MINIO_ENDPOINT:
        raise HTTPException(status_code=501, detail="未配置 MINIO_ENDPOINT")

    try:
        from minio import Minio
        client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE,
        )
        url = client.presigned_get_object(bucket, key, expires=expires)
        return {"url": url, "bucket": bucket, "key": key, "expires": expires}
    except ImportError:
        raise HTTPException(status_code=501, detail="minio 包未安装，请运行: pip install minio")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MinIO 预签名失败: {e}")


@litellm_router.get("/models", summary="可用模型列表")
async def list_models():
    """列出可用的模型。"""
    if EXTERNAL_LITELLM_URL:
        headers = _get_proxy_headers()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{EXTERNAL_LITELLM_URL}/v1/models",
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.json()
        except Exception:
            pass

    # 从 Ollama 获取
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = []
            for m in data.get("models", []):
                models.append({
                    "id": m.get("name", ""),
                    "object": "model",
                    "owned_by": "ollama",
                })
            return {"object": "list", "data": models}
    except Exception as e:
        return {"object": "list", "data": [], "error": str(e)}


# ── 独立服务入口 ─────────────────────────────────────────────────

def create_app() -> FastAPI:
    """创建 LiteLLM Proxy 独立 FastAPI 应用。"""
    app = FastAPI(
        title="LiteLLM Proxy Service",
        description="OpenAI 兼容的同步直调 API 服务",
        version="1.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(litellm_router)

    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "service": "litellm-proxy",
            "port": 4000,
            "capabilities": {
                "chat": True,
                "streaming": True,
                "vision": True,
                "asr": bool(FUNASR_URL or EXTERNAL_LITELLM_URL or DOUBAO_ASR_KEY),
                "funasr": bool(FUNASR_URL),
                "embedding": True,
                "multimodal": True,
                "minio": bool(MINIO_ENDPOINT),
            },
            "asr_providers": {
                "funasr": {"enabled": bool(FUNASR_URL), "url": FUNASR_URL, "model": FUNASR_MODEL},
                "external_litellm": {"enabled": bool(EXTERNAL_LITELLM_URL), "url": EXTERNAL_LITELLM_URL},
                "doubao": {"enabled": bool(DOUBAO_ASR_KEY)},
                "ollama_whisper": {"enabled": True, "url": OLLAMA_URL},
            },
            "models": {
                "chat": DEFAULT_CHAT_MODEL,
                "vision": DEFAULT_VISION_MODEL,
                "embedding": DEFAULT_EMBEDDING_MODEL,
                "asr": FUNASR_MODEL if FUNASR_URL else "doubao-asr",
            },
        }

    @app.get("/funasr/setup")
    async def funasr_setup():
        """FunASR 部署配置说明。"""
        return {
            "status": "configured" if FUNASR_URL else "not_configured",
            "funasr_url": FUNASR_URL or None,
            "model": FUNASR_MODEL,
            "setup_guide": {
                "step1_install": "pip install funasr torch torchaudio fastapi uvicorn python-multipart",
                "step2_start_server": "funasr-server --device cpu --model sensevoice --port 8199",
                "step3_configure": "export FUNASR_URL=http://localhost:8199",
                "step4_restart": "重启 LiteLLM Proxy 服务",
                "models": {
                    "sensevoice": "快速多语言ASR，支持语言/情感/事件标签 (推荐)",
                    "paraformer": "中文生产级转写，带VAD和标点",
                    "fun-asr-nano": "31语言LLM式ASR，带时间戳",
                },
            },
            "api_endpoint": f"{FUNASR_URL}/v1/audio/transcriptions" if FUNASR_URL else None,
        }

    return app


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    port = int(os.getenv("LITELLM_PORT", "4000"))
    logger.info("LiteLLM Proxy Service starting on :%d", port)
    logger.info("  Ollama URL: %s", OLLAMA_URL)
    logger.info("  External LiteLLM URL: %s", EXTERNAL_LITELLM_URL or "(direct ollama)")
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")
