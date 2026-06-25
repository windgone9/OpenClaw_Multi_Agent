"""Playwright 严格顺序 E2E — 每个测试依次执行，不并发"""
import json
import time
from playwright.sync_api import sync_playwright

DASHBOARD_URL = "http://localhost:8090/new_dashboard.html"

def run_sequential():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_viewport_size({"width": 1280, "height": 900})

        page.goto(DASHBOARD_URL, timeout=15000)
        print("✓ Page loaded")

        total_start = time.time()

        # Run each test type sequentially
        tests = ['chat', 'vision', 'asr', 'multimodal', 'ws_chat', 'ws_asr']
        results = {}

        for test_name in tests:
            start = time.time()
            print(f"\n── Running {test_name} ──")

            js_code = ""
            if test_name == 'chat':
                js_code = "async () => { try { return JSON.stringify(await e2eChat('qwen2.5')); } catch(e) { return JSON.stringify({error: e.message}); } }"
            elif test_name == 'vision':
                js_code = "async () => { try { return JSON.stringify(await e2eVision('qwen2.5')); } catch(e) { return JSON.stringify({error: e.message}); } }"
            elif test_name == 'asr':
                js_code = "async () => { try { return JSON.stringify(await e2eASR()); } catch(e) { return JSON.stringify({error: e.message}); } }"
            elif test_name == 'multimodal':
                js_code = "async () => { try { return JSON.stringify(await e2eMultimodal('qwen2.5')); } catch(e) { return JSON.stringify({error: e.message}); } }"
            elif test_name == 'ws_chat':
                js_code = "async () => { try { return JSON.stringify(await e2eWSChat('qwen2.5')); } catch(e) { return JSON.stringify({error: e.message}); } }"
            elif test_name == 'ws_asr':
                js_code = "async () => { try { return JSON.stringify(await e2eWSASR()); } catch(e) { return JSON.stringify({error: e.message}); } }"

            r_json = page.evaluate(js_code)
            r = json.loads(r_json)
            results[test_name] = r
            elapsed = time.time() - start

            status = r.get('status', '?')
            lat = r.get('latency', '?')
            output = r.get('output', {})
            content = ''
            fcm = None

            if test_name == 'chat' or test_name == 'vision' or test_name == 'multimodal' or test_name == 'ws_chat':
                content = (output.get('content') or 'EMPTY')[:80]
                fcm = output.get('first_chunk_ms')
                if test_name == 'ws_chat':
                    fcm = (output.get('timing') or {}).get('first_chunk_ms')
            elif test_name == 'asr':
                content = (output.get('text') or 'EMPTY')[:60]
            elif test_name == 'ws_asr':
                content = (output.get('asr_text') or 'EMPTY')[:30] + ' → ' + (output.get('llm_text') or 'EMPTY')[:40]
                fcm = (output.get('timing') or {}).get('llm_first_chunk_ms')

            print(f"  status={status} latency={lat}ms wall={elapsed:.1f}s fcm={fcm}")
            print(f"  content: {content}")

            # Brief pause between tests to let GPU cooldown
            page.wait_for_timeout(2000)

        total_elapsed = time.time() - total_start
        print(f"\n── Summary ──")
        passed = sum(1 for r in results.values() if r.get('status') == 'ok')
        print(f"  Passed: {passed}/{len(results)}")
        print(f"  Total wall time: {total_elapsed:.1f}s")
        for name, r in results.items():
            st = r.get('status', '?')
            lat = r.get('latency', '?')
            print(f"  {name}: {st} {lat}ms")

        browser.close()
        return results

if __name__ == "__main__":
    results = run_sequential()
