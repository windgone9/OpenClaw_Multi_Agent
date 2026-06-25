"""Playwright E2E 诊断脚本 — 自动运行 Dashboard 测试并捕获详细结果"""
import json
import sys
import time
from playwright.sync_api import sync_playwright

DASHBOARD_URL = "http://localhost:8090/new_dashboard.html"
TIMEOUT = 120  # seconds per test

def run_e2e_tests():
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Capture console logs
        console_logs = []
        page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))

        page.goto(DASHBOARD_URL, timeout=15000)
        print(f"✓ Page loaded: {page.title()}")

        # ── Test 1: Single chat test ──
        print("\n── Test: Chat (stream=true) ──")
        chat_result = page.evaluate("""
            async () => {
                try {
                    var r = await e2eChat('qwen2.5');
                    return JSON.stringify(r);
                } catch(e) {
                    return JSON.stringify({error: e.message});
                }
            }
        """)
        r = json.loads(chat_result)
        results['chat'] = r
        print(f"  status: {r.get('status')}, latency: {r.get('latency')}ms")
        output = r.get('output', {})
        print(f"  content: {output.get('content', '')[:80]}")
        print(f"  first_chunk_ms: {output.get('first_chunk_ms')}")

        # ── Test 2: Single vision test ──
        print("\n── Test: Vision (stream=true) ──")
        vision_result = page.evaluate("""
            async () => {
                try {
                    var r = await e2eVision('qwen2.5');
                    return JSON.stringify(r);
                } catch(e) {
                    return JSON.stringify({error: e.message});
                }
            }
        """)
        r = json.loads(vision_result)
        results['vision'] = r
        print(f"  status: {r.get('status')}, latency: {r.get('latency')}ms")
        output = r.get('output', {})
        print(f"  content: {output.get('content', '')[:80] if output.get('content') else 'EMPTY'}")
        print(f"  first_chunk_ms: {output.get('first_chunk_ms')}")

        # ── Test 3: Single ASR test ──
        print("\n── Test: ASR ──")
        asr_result = page.evaluate("""
            async () => {
                try {
                    var r = await e2eASR();
                    return JSON.stringify(r);
                } catch(e) {
                    return JSON.stringify({error: e.message});
                }
            }
        """)
        r = json.loads(asr_result)
        results['asr'] = r
        print(f"  status: {r.get('status')}, latency: {r.get('latency')}ms")
        output = r.get('output', {})
        print(f"  text: {output.get('text', '')[:60]}")

        # ── Test 4: WS Chat test ──
        print("\n── Test: WS Chat ──")
        ws_result = page.evaluate("""
            async () => {
                try {
                    var r = await e2eWSChat('qwen2.5');
                    return JSON.stringify(r);
                } catch(e) {
                    return JSON.stringify({error: e.message});
                }
            }
        """)
        r = json.loads(ws_result)
        results['ws_chat'] = r
        print(f"  status: {r.get('status')}, latency: {r.get('latency')}ms")
        output = r.get('output', {})
        print(f"  content: {(output.get('content') or '')[:80]}")
        timing = output.get('timing', {})
        print(f"  first_chunk_ms: {timing.get('first_chunk_ms')}")

        # ── Test 5: WS ASR test ──
        print("\n── Test: WS ASR ──")
        ws_asr_result = page.evaluate("""
            async () => {
                try {
                    var r = await e2eWSASR();
                    return JSON.stringify(r);
                } catch(e) {
                    return JSON.stringify({error: e.message});
                }
            }
        """)
        r = json.loads(ws_asr_result)
        results['ws_asr'] = r
        print(f"  status: {r.get('status')}, latency: {r.get('latency')}ms")
        output = r.get('output', {})
        print(f"  asr_text: {(output.get('asr_text') or '')[:60]}")
        print(f"  llm_text: {(output.get('llm_text') or '')[:80]}")
        timing = output.get('timing', {})
        print(f"  llm_first_chunk_ms: {timing.get('llm_first_chunk_ms')}")

        # ── Print relevant console logs ──
        print("\n── Console Logs (WS/Vision related) ──")
        for log in console_logs:
            if any(kw in log for kw in ['WS', 'vision', 'Vision', 'chunk', 'delta', 'content', 'data:', 'first_chunk']):
                print(f"  {log[:200]}")

        browser.close()

    # ── Summary ──
    print("\n── Summary ──")
    passed = sum(1 for r in results.values() if r.get('status') == 'ok')
    total = len(results)
    print(f"  Passed: {passed}/{total}")
    for name, r in results.items():
        st = r.get('status', '?')
        lat = r.get('latency', '?')
        content_preview = ''
        out = r.get('output', {})
        if name == 'chat':
            content_preview = (out.get('content') or '')[:40]
        elif name in ('vision', 'multimodal'):
            content_preview = (out.get('content') or 'EMPTY')[:40]
        elif name == 'asr':
            content_preview = (out.get('text') or '')[:40]
        elif name == 'ws_chat':
            content_preview = (out.get('content') or '')[:40]
        elif name == 'ws_asr':
            content_preview = (out.get('llm_text') or '')[:40]
        print(f"  {name}: {st} {lat}ms | {content_preview}")

    return results

if __name__ == "__main__":
    results = run_e2e_tests()
    # Save to file for later analysis
    with open("/tmp/e2e_playwright_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to /tmp/e2e_playwright_results.json")
