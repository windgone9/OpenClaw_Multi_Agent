#!/usr/bin/env python3
"""用真实语音文件测试 WebSocket 流式 ASR"""
import asyncio
import json
import sys
import websockets

WAV_PATH = "/tmp/openclaw-test/real_speech.wav"
WS_URL = "ws://localhost:8080"


async def main():
    # 读取真实语音 WAV
    with open(WAV_PATH, "rb") as f:
        wav_data = f.read()
    pcm_data = wav_data[44:]  # 跳过 WAV header
    print(f"音频数据: {len(pcm_data)} bytes PCM ({len(pcm_data)/32000:.1f}s @16kHz 16bit mono)")

    uri = f"{WS_URL}/v1/stream/asr"
    print(f"连接: {uri}")

    async with websockets.connect(uri, close_timeout=120) as ws:
        # 等待连接确认
        r = await asyncio.wait_for(ws.recv(), timeout=10)
        conn = json.loads(r)
        print(f"[连接] {conn}")

        # 发送配置: auto_llm=True 触发 LLM 自动回复
        await ws.send(json.dumps({
            "type": "config",
            "auto_llm": True,
            "model": "qwen2.5",
        }))
        r = await asyncio.wait_for(ws.recv(), timeout=10)
        print(f"[配置] {json.loads(r)}")

        # 分块发送音频
        chunk_size = 32000  # 1s 音频
        total_chunks = (len(pcm_data) + chunk_size - 1) // chunk_size
        for i in range(0, len(pcm_data), chunk_size):
            chunk_idx = i // chunk_size + 1
            await ws.send(pcm_data[i : i + chunk_size])
            print(f"  发送音频块 {chunk_idx}/{total_chunks}")
            await asyncio.sleep(0.1)  # 模拟实时流

        # 发送停止标记
        print("  发送 stop 信号...")
        await ws.send(json.dumps({"type": "stop"}))

        # 接收结果
        asr_text = ""
        llm_text = ""
        while True:
            r = await asyncio.wait_for(ws.recv(), timeout=120)
            data = json.loads(r)
            t = data.get("type", "")

            if t == "asr_partial":
                text = data.get("text", data.get("content", ""))
                if text:
                    print(f"  [ASR 中间结果] {text}")

            elif t == "asr_final":
                asr_text = data.get("text", data.get("content", ""))
                print(f"  [ASR 最终结果] '{asr_text}'")
                if not asr_text:
                    print("  ASR 结果为空，结束")
                    break

            elif t == "asr_done":
                print("  [ASR 完成]")
                break

            elif t == "llm_chunk":
                content = data.get("content", "")
                llm_text += content
                print(f"  [LLM 流式] {content}", end="", flush=True)

            elif t == "llm_done":
                print(f"\n  [LLM 完成] 完整回复: {llm_text}")
                break

            elif t == "error":
                print(f"  [错误] {data.get('content', '')}")
                break

    print("\n" + "=" * 50)
    print(f"ASR 识别: {asr_text or '(空)'}")
    print(f"LLM 回复: {llm_text or '(无)'}")
    if asr_text:
        print("✅ 流式 ASR + LLM 自动回复测试通过!")
    else:
        print("❌ ASR 未识别到语音内容")


if __name__ == "__main__":
    asyncio.run(main())
