#!/usr/bin/env python3
"""
OpenClaw Multi-Agent 完整链路回归测试
覆盖: HTTP 统一接口 + WebSocket 流式 + 真实语音 ASR + 多模态 Vision
"""
import asyncio
import base64
import json
import os
import struct
import subprocess
import sys
import time

import httpx
import websockets

BASE_URL = "http://localhost:8080"
WS_URL = "ws://localhost:8080"
TIMEOUT = 300

# ── 测试数据路径 ──────────────────────────────────────────────
TEST_DIR = "/tmp/openclaw-test"
REAL_SPEECH_WAV = f"{TEST_DIR}/real_speech.wav"
SINE_WAV = f"{TEST_DIR}/sine_speech_test.wav"


# ── 工具函数 ──────────────────────────────────────────────────

def ensure_real_speech_audio():
    """用 macOS TTS 生成真实中文语音 WAV (16kHz 16bit mono)"""
    os.makedirs(TEST_DIR, exist_ok=True)
    if os.path.exists(REAL_SPEECH_WAV):
        return
    aiff_path = f"{TEST_DIR}/real_speech.aiff"
    try:
        subprocess.run(
            ["say", "-o", aiff_path, "你好，今天天气怎么样？"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["afconvert", aiff_path, REAL_SPEECH_WAV,
             "-d", "LEI16@16000", "-f", "WAVE", "-c", "1"],
            check=True, capture_output=True,
        )
        os.remove(aiff_path)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  ⚠ TTS 生成失败 ({e})，回退到正弦波音频")
        _generate_sine_wav(REAL_SPEECH_WAV)


def ensure_sine_audio():
    """生成正弦波 WAV (无语音内容，仅验证接口不报错)"""
    os.makedirs(TEST_DIR, exist_ok=True)
    if not os.path.exists(SINE_WAV):
        _generate_sine_wav(SINE_WAV)


def _generate_sine_wav(path: str, sr: int = 16000, dur: float = 2.0):
    import math
    n_samples = int(sr * dur)
    pcm = b''.join(
        struct.pack('<h', int(32767 * 0.3 * math.sin(2 * math.pi * 440 * i / sr)))
        for i in range(n_samples)
    )
    with open(path, 'wb') as f:
        f.write(b'RIFF')
        f.write(struct.pack('<I', 36 + len(pcm)))
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write(struct.pack('<IHHIIHH', 16, 1, 1, sr, sr * 2, 2, 16))
        f.write(b'data')
        f.write(struct.pack('<I', len(pcm)))
        f.write(pcm)


def get_test_image_base64():
    """生成红色方块 PNG 并返回 base64 data URI"""
    import zlib
    width, height = 64, 64
    raw = b''
    for y in range(height):
        raw += b'\x00'
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
    return f"data:image/png;base64,{base64.b64encode(png).decode()}"


def print_header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_pass(label: str):
    print(f"  ✅ {label} PASS")


def print_fail(label: str, err: str):
    print(f"  ❌ {label} FAIL: {err}")


# ── 测试用例 ──────────────────────────────────────────────────

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
                ok = resp.status_code == 200
                print(f"  {name}: {'OK' if ok else f'FAIL({resp.status_code})'}")
                if not ok:
                    all_ok = False
        except Exception as e:
            print(f"  {name}: FAIL ({e})")
            all_ok = False
    if all_ok:
        print_pass("健康检查")
    return all_ok


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
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"  model: {data.get('model')}")
            print(f"  回复: {content[:200]}")
            assert content, "回复内容为空"
            print_pass("type=chat")
            return True
    except Exception as e:
        print_fail("type=chat", str(e))
        return False


async def test_vision():
    """type=vision: 图片理解 (base64 图片 → llava)"""
    print_header("TEST 2: type=vision (图片理解)")
    img_data_uri = get_test_image_base64()
    payload = {
        "type": "vision",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "请描述这张图片的颜色"},
                {"type": "image_url", "image_url": {"url": img_data_uri}},
            ],
        }],
        "model": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(f"{BASE_URL}/v1/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"  model: {data.get('model')}")
            print(f"  回复: {content[:200]}")
            assert content, "回复内容为空"
            print_pass("type=vision")
            return True
    except Exception as e:
        print_fail("type=vision", str(e))
        return False


async def test_asr_file_upload():
    """type=asr: 语音识别 (文件上传，真实语音)"""
    print_header("TEST 3: type=asr (文件上传 - 真实语音)")
    ensure_real_speech_audio()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            with open(REAL_SPEECH_WAV, "rb") as f:
                resp = await client.post(
                    f"{BASE_URL}/v1/stream/asr/file",
                    files={"file": ("speech.wav", f, "audio/wav")},
                )
            resp.raise_for_status()
            data = resp.json()
            text = data.get("text", "")
            print(f"  ASR 识别: '{text}'")
            print_pass("type=asr (文件上传)")
            return True
    except Exception as e:
        print_fail("type=asr (文件上传)", str(e))
        return False


async def test_multimodal():
    """type=multimodal: 多模态混合 (图片 + 文本)"""
    print_header("TEST 4: type=multimodal (多模态混合)")
    img_data_uri = get_test_image_base64()
    payload = {
        "type": "multimodal",
        "prompt": "请描述这张图片的颜色",
        "model": "auto",
        "attachments": [{"type": "image", "url": img_data_uri}],
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(f"{BASE_URL}/v1/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"  model: {data.get('model')}")
            print(f"  回复: {content[:200]}")
            assert content, "回复内容为空"
            print_pass("type=multimodal")
            return True
    except Exception as e:
        print_fail("type=multimodal", str(e))
        return False


async def test_stream_asr_info():
    """type=stream_asr: 获取 WebSocket 地址"""
    print_header("TEST 5: type=stream_asr (获取 WS 地址)")
    payload = {"type": "stream_asr", "auto_llm": True, "model": "qwen2.5"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{BASE_URL}/v1/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            assert data.get("protocol") == "websocket", "协议类型不正确"
            assert data.get("url") == "/v1/stream/asr", "WebSocket 地址不正确"
            print_pass("type=stream_asr")
            return True
    except Exception as e:
        print_fail("type=stream_asr", str(e))
        return False


async def test_stream_asr_websocket_real():
    """WebSocket 流式 ASR (真实语音 + LLM 自动回复)"""
    print_header("TEST 5b: WebSocket 流式 ASR (真实语音)")
    ensure_real_speech_audio()
    try:
        with open(REAL_SPEECH_WAV, "rb") as f:
            wav_data = f.read()
        pcm_data = wav_data[44:]
    except FileNotFoundError:
        print_fail("WS ASR", f"音频文件不存在: {REAL_SPEECH_WAV}")
        return False

    print(f"  音频: {len(pcm_data)} bytes ({len(pcm_data)/32000:.1f}s)")
    uri = f"{WS_URL}/v1/stream/asr"

    try:
        async with websockets.connect(uri, close_timeout=120) as ws:
            # 连接确认
            r = await asyncio.wait_for(ws.recv(), timeout=10)
            conn = json.loads(r)
            print(f"  连接: session={conn.get('session_id')}")

            # 配置: auto_llm=True 触发 LLM 自动回复
            await ws.send(json.dumps({
                "type": "config", "auto_llm": True, "model": "qwen2.5",
            }))
            r = await asyncio.wait_for(ws.recv(), timeout=10)
            print(f"  配置: {json.loads(r)}")

            # 分块发送音频 (模拟实时流)
            chunk_size = 32000  # 1s
            for i in range(0, len(pcm_data), chunk_size):
                await ws.send(pcm_data[i:i + chunk_size])
                await asyncio.sleep(0.1)

            # 停止
            await ws.send(json.dumps({"type": "stop"}))

            # 接收结果
            asr_text = ""
            llm_text = ""
            got_result = False
            while True:
                r = await asyncio.wait_for(ws.recv(), timeout=120)
                data = json.loads(r)
                t = data.get("type", "")

                if t == "asr_partial":
                    text = data.get("text", data.get("content", ""))
                    if text:
                        print(f"  ASR 中间: {text}")

                elif t == "asr_final":
                    asr_text = data.get("text", data.get("content", ""))
                    print(f"  ASR 最终: '{asr_text}'")
                    got_result = True
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
                print_pass("WebSocket 流式 ASR (真实语音)")
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
    """type 缺失: 根据 attachments 自动推断"""
    print_header("TEST 6: type 缺失 (自动推断)")
    img_data_uri = get_test_image_base64()

    # 6a: 有 image → vision
    payload_a = {
        "messages": [{"role": "user", "content": "描述图片"}],
        "attachments": [{"type": "image", "url": img_data_uri}],
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(f"{BASE_URL}/v1/chat", json=payload_a)
            resp.raise_for_status()
            data = resp.json()
            print(f"  6a image→vision: model={data.get('model')}")
            print_pass("type 缺失 → vision")
    except Exception as e:
        print_fail("type 缺失 → vision", str(e))

    # 6b: 无附件 → chat
    payload_b = {"messages": [{"role": "user", "content": "1+1=?"}]}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(f"{BASE_URL}/v1/chat", json=payload_b)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            assert content, "回复内容为空"
            print(f"  6b 无附件→chat: {content[:100]}")
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
                "type": "text", "content": "1+1等于几？只回答数字",
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


# ── 主函数 ────────────────────────────────────────────────────

async def main():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  OpenClaw Multi-Agent — 完整链路回归测试                ║")
    print("╚══════════════════════════════════════════════════════════╝")

    # 准备测试数据
    print("\n── 准备测试数据 ──")
    ensure_real_speech_audio()
    ensure_sine_audio()
    print(f"  真实语音: {REAL_SPEECH_WAV} ({os.path.getsize(REAL_SPEECH_WAV)} bytes)")
    print(f"  正弦波:   {SINE_WAV} ({os.path.getsize(SINE_WAV)} bytes)")

    results = {}

    # 健康检查
    results["health"] = await test_health()

    # HTTP 统一接口
    results["chat"] = await test_chat()
    results["vision"] = await test_vision()
    results["asr_file"] = await test_asr_file_upload()
    results["multimodal"] = await test_multimodal()
    results["stream_asr_info"] = await test_stream_asr_info()
    results["auto_infer"] = await test_auto_infer()

    # WebSocket
    results["ws_chat"] = await test_stream_ws_chat()
    results["ws_asr_real"] = await test_stream_asr_websocket_real()

    # 汇总
    print_header("回归测试结果汇总")
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
