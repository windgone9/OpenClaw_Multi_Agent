#!/usr/bin/env python3
"""
统一接口 POST /v1/chat 测试脚本
模拟前端通过 type 字段路由到不同后端服务的完整测试
"""
import asyncio
import base64
import json
import os
import struct
import math
import sys
import time

import httpx
import websockets

BASE_URL = "http://localhost:8080"
WS_URL = "ws://localhost:8080"
TIMEOUT = 300


# ── 工具函数 ──────────────────────────────────────────────────

WAV_PATH = "/tmp/openclaw-test/speech_test.wav"


def ensure_test_audio():
    """确保测试音频文件存在，不存在则生成 TTS 模拟音频"""
    os.makedirs(os.path.dirname(WAV_PATH), exist_ok=True)
    if os.path.exists(WAV_PATH):
        return
    # 生成 16kHz 16bit mono 正弦波音频（模拟语音，FunASR 可识别为静音）
    sr, dur = 16000, 3
    n_samples = sr * dur
    samples = []
    for i in range(n_samples):
        v = int(32767 * 0.3 * math.sin(2 * math.pi * 440 * i / sr))
        samples.append(struct.pack('<h', v))
    pcm = b''.join(samples)
    # 写 WAV 文件
    with open(WAV_PATH, 'wb') as f:
        f.write(b'RIFF')
        f.write(struct.pack('<I', 36 + len(pcm)))
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write(struct.pack('<IHHIIHH', 16, 1, 1, sr, sr * 2, 2, 16))
        f.write(b'data')
        f.write(struct.pack('<I', len(pcm)))
        f.write(pcm)
    print(f"  已生成测试音频: {WAV_PATH} ({os.path.getsize(WAV_PATH)} bytes)")


def get_test_image_base64():
    """生成一个简单的红色方块 PNG 图片并返回 base64 data URI"""
    # 生成 4x4 红色 PNG (最小有效 PNG)
    import zlib
    def create_png():
        width, height = 64, 64
        raw = b''
        for y in range(height):
            raw += b'\x00'  # filter byte
            for x in range(width):
                raw += b'\xff\x00\x00\xff'  # RGBA red
        compressed = zlib.compress(raw)
        def chunk(ctype, data):
            c = ctype + data
            return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
        ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
        png = b'\x89PNG\r\n\x1a\n'
        png += chunk(b'IHDR', ihdr)
        png += chunk(b'IDAT', compressed)
        png += chunk(b'IEND', b'')
        return png
    png_data = create_png()
    b64 = base64.b64encode(png_data).decode('utf-8')
    return f"data:image/png;base64,{b64}"
def print_header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_result(label: str, data: dict):
    print(f"\n  [{label}]")
    for k, v in data.items():
        val = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
        if isinstance(val, str) and len(val) > 200:
            val = val[:200] + "..."
        print(f"    {k}: {val}")


def print_pass(label: str):
    print(f"  ✅ {label} PASS")


def print_fail(label: str, err: str):
    print(f"  ❌ {label} FAIL: {err}")


# ── 测试用例 ──────────────────────────────────────────────────

async def test_chat():
    """type=chat: 纯文本对话"""
    print_header("TEST 1: type=chat (纯文本对话)")
    payload = {
        "type": "chat",
        "messages": [{"role": "user", "content": "你好，请用一句话介绍你自己"}],
        "model": "qwen2.5",
        "temperature": 0.7,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(f"{BASE_URL}/v1/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            print_result("响应", {"model": data.get("model"), "content": content})
            assert content, "回复内容为空"
            print_pass("type=chat")
            return True
    except Exception as e:
        print_fail("type=chat", str(e))
        return False


async def test_vision():
    """type=vision: 图片理解 (自动切换 llava 模型, base64 图片)"""
    print_header("TEST 2: type=vision (图片理解)")
    img_data_uri = get_test_image_base64()
    payload = {
        "type": "vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请描述这张图片的颜色"},
                    {"type": "image_url", "image_url": {"url": img_data_uri}},
                ],
            }
        ],
        "model": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(f"{BASE_URL}/v1/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            model = data.get("model", "")
            print_result("响应", {"model": model, "content": content})
            assert content, "回复内容为空"
            print_pass("type=vision")
            return True
    except Exception as e:
        print_fail("type=vision", str(e))
        return False


async def test_asr():
    """type=asr: 语音识别 (通过 URL)"""
    print_header("TEST 3: type=asr (语音识别)")
    payload = {
        "type": "asr",
        "file": "http://minio:9000/openclaw-test/speech_test.wav",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(f"{BASE_URL}/v1/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            text = data.get("text", "")
            print_result("响应", {"type": data.get("type"), "text": text, "model": data.get("model")})
            # ASR 可能返回空文本（如果音频文件不可达），但不应报错
            print_pass("type=asr")
            return True
    except Exception as e:
        print_fail("type=asr", str(e))
        return False


async def test_asr_local_file():
    """type=asr: 语音识别 (通过本地文件上传到 stream-service)"""
    print_header("TEST 3b: type=asr (本地文件上传)")
    ensure_test_audio()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            with open(WAV_PATH, "rb") as f:
                resp = await client.post(
                    f"{BASE_URL}/v1/stream/asr/file",
                    files={"file": ("speech_test.wav", f, "audio/wav")},
                )
            resp.raise_for_status()
            data = resp.json()
            text = data.get("text", "")
            print_result("响应", {"text": text, "provider": data.get("provider")})
            # 正弦波音频可能无语音内容，只验证接口不报错
            print_pass("type=asr (文件上传)")
            return True
    except FileNotFoundError:
        print_fail("type=asr (文件上传)", f"测试音频文件不存在: {WAV_PATH}")
        return False
    except Exception as e:
        print_fail("type=asr (文件上传)", str(e))
        return False


async def test_multimodal():
    """type=multimodal: 多模态混合 (图片 + 文本, base64)"""
    print_header("TEST 4: type=multimodal (多模态混合)")
    img_data_uri = get_test_image_base64()
    payload = {
        "type": "multimodal",
        "prompt": "请描述这张图片的颜色",
        "model": "auto",
        "attachments": [
            {
                "type": "image",
                "url": img_data_uri,
            }
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(f"{BASE_URL}/v1/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            print_result("响应", {"model": data.get("model"), "content": content})
            assert content, "回复内容为空"
            print_pass("type=multimodal")
            return True
    except Exception as e:
        print_fail("type=multimodal", str(e))
        return False


async def test_stream_asr():
    """type=stream_asr: 流式语音转写 (返回 WebSocket 地址)"""
    print_header("TEST 5: type=stream_asr (流式语音转写地址)")
    payload = {
        "type": "stream_asr",
        "auto_llm": True,
        "model": "qwen2.5",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{BASE_URL}/v1/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            print_result("响应", data)
            assert data.get("protocol") == "websocket", "协议类型不正确"
            assert data.get("url") == "/v1/stream/asr", "WebSocket 地址不正确"
            print_pass("type=stream_asr")
            return True
    except Exception as e:
        print_fail("type=stream_asr", str(e))
        return False


async def test_stream_asr_websocket():
    """WebSocket 流式 ASR 完整流程测试"""
    print_header("TEST 5b: WebSocket 流式 ASR (完整流程)")
    ensure_test_audio()
    try:
        with open(WAV_PATH, "rb") as f:
            wav_data = f.read()
        pcm_data = wav_data[44:]  # 跳过 WAV header
    except FileNotFoundError:
        print_fail("WS ASR", f"测试音频文件不存在: {WAV_PATH}")
        return False

    uri = f"{WS_URL}/v1/stream/asr"
    try:
        async with websockets.connect(uri, close_timeout=120) as ws:
            # 等待连接确认
            r = await asyncio.wait_for(ws.recv(), timeout=10)
            conn_data = json.loads(r)
            print_result("连接确认", conn_data)

            # 发送配置
            await ws.send(json.dumps({
                "type": "config",
                "auto_llm": True,
                "model": "qwen2.5",
            }))
            r = await asyncio.wait_for(ws.recv(), timeout=10)
            print_result("配置确认", json.loads(r))

            # 分块发送音频
            chunk_size = 32000
            for i in range(0, len(pcm_data), chunk_size):
                await ws.send(pcm_data[i : i + chunk_size])
                await asyncio.sleep(0.05)

            # 发送停止标记
            await ws.send(json.dumps({"type": "stop"}))

            # 接收结果
            asr_text = ""
            llm_text = ""
            got_result = False
            while True:
                r = await asyncio.wait_for(ws.recv(), timeout=60)
                data = json.loads(r)
                t = data.get("type", "")
                if t == "asr_partial":
                    asr_text = data.get("text", data.get("content", ""))
                elif t == "asr_final":
                    asr_text = data.get("text", data.get("content", ""))
                    if asr_text:
                        print(f"  ASR 最终结果: '{asr_text}'")
                    else:
                        print(f"  ASR 最终结果: (空，正弦波无语音内容)")
                    got_result = True
                    # 如果没有 LLM 回复预期，直接结束
                    if not asr_text:
                        break
                elif t == "asr_done":
                    got_result = True
                    break
                elif t == "llm_chunk":
                    llm_text += data.get("content", "")
                elif t == "llm_done":
                    if llm_text:
                        print(f"  LLM 回复: {llm_text[:200]}")
                    break
                elif t == "error":
                    print_fail("WS ASR", data.get("content", ""))
                    return False

            if got_result or llm_text:
                print_pass("WebSocket 流式 ASR")
                return True
            else:
                print_fail("WS ASR", "未收到任何结果")
                return False
    except asyncio.TimeoutError:
        print_fail("WS ASR", "超时")
        return False
    except Exception as e:
        print_fail("WS ASR", str(e))
        return False


async def test_auto_infer():
    """type 缺失: 根据 attachments 自动推断类型"""
    print_header("TEST 6: type 缺失 (自动推断)")

    # 测试 6a: 有 image attachments → 自动推断为 vision
    img_data_uri = get_test_image_base64()
    payload_a = {
        "messages": [{"role": "user", "content": "描述图片"}],
        "attachments": [{"type": "image", "url": img_data_uri}],
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(f"{BASE_URL}/v1/chat", json=payload_a)
            resp.raise_for_status()
            data = resp.json()
            model = data.get("model", "")
            print_result("6a 自动推断 (image→vision)", {"model": model})
            print_pass("type 缺失 → vision")
    except Exception as e:
        print_fail("type 缺失 → vision", str(e))

    # 测试 6b: 无 attachments → 自动推断为 chat
    payload_b = {
        "messages": [{"role": "user", "content": "1+1=?"}],
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(f"{BASE_URL}/v1/chat", json=payload_b)
            resp.raise_for_status()
            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            print_result("6b 自动推断 (无附件→chat)", {"content": content})
            assert content, "回复内容为空"
            print_pass("type 缺失 → chat")
            return True
    except Exception as e:
        print_fail("type 缺失 → chat", str(e))
        return False


async def test_stream_ws_chat():
    """WebSocket 流式聊天"""
    print_header("TEST 7: WebSocket 流式聊天")
    uri = f"{WS_URL}/v1/stream/ws?model=qwen2.5"
    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "text",
                "content": "1+1等于几？只回答数字",
            }))
            full_text = ""
            while True:
                r = await asyncio.wait_for(ws.recv(), timeout=60)
                data = json.loads(r)
                t = data.get("type", "")
                if t == "llm_chunk":
                    full_text += data.get("content", "")
                elif t == "llm_done":
                    print(f"  流式回复: '{full_text}'")
                    break
                elif t == "error":
                    print_fail("WS Chat", data.get("content", ""))
                    return False
            assert full_text, "回复内容为空"
            print_pass("WebSocket 流式聊天")
            return True
    except Exception as e:
        print_fail("WS Chat", str(e))
        return False


async def test_health():
    """健康检查"""
    print_header("TEST 0: 健康检查")
    endpoints = [
        ("Nginx", f"{BASE_URL}/health"),
        ("Hermes", f"{BASE_URL}/hermes/health"),
    ]
    all_ok = True
    for name, url in endpoints:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                status = "OK" if resp.status_code == 200 else f"FAIL({resp.status_code})"
                print(f"  {name}: {status}")
                if resp.status_code != 200:
                    all_ok = False
        except Exception as e:
            print(f"  {name}: FAIL ({e})")
            all_ok = False
    if all_ok:
        print_pass("健康检查")
    return all_ok


# ── 主函数 ────────────────────────────────────────────────────

async def main():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   OpenClaw Multi-Agent — 统一接口 /v1/chat 测试脚本     ║")
    print("╚══════════════════════════════════════════════════════════╝")

    results = {}

    # 健康检查
    results["health"] = await test_health()

    # 统一接口 HTTP 测试
    results["chat"] = await test_chat()
    results["vision"] = await test_vision()
    results["asr"] = await test_asr()
    results["asr_file"] = await test_asr_local_file()
    results["multimodal"] = await test_multimodal()
    results["stream_asr_info"] = await test_stream_asr()
    results["auto_infer"] = await test_auto_infer()

    # WebSocket 测试
    results["ws_chat"] = await test_stream_ws_chat()
    results["ws_asr"] = await test_stream_asr_websocket()

    # 汇总
    print_header("测试结果汇总")
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for name, ok in results.items():
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {name:20s} {status}")
    print(f"\n  总计: {passed}/{total} 通过")

    if passed < total:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
