"""Playwright 全量 E2E 测试 — 先切换到 E2E tab 再运行"""
import json
import os
import time
from playwright.sync_api import sync_playwright

# 可用环境变量覆盖, 便于指向服务器: DASHBOARD_URL=http://<server>:30080/new_dashboard.html ...
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:8090/new_dashboard.html")

def run_full_e2e():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_viewport_size({"width": 1280, "height": 900})

        console_logs = []
        page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text[:300]}"))

        page.goto(DASHBOARD_URL, timeout=15000)
        print(f"✓ Page loaded")

        # Click the E2E tab to make it visible
        page.click("text=🧪 E2E 测试", timeout=5000)
        print(f"✓ Switched to E2E tab")

        # Small wait for tab content to render
        page.wait_for_timeout(500)

        # Check all test types and set model
        page.evaluate("""
            () => {
                var checks = document.querySelectorAll('.e2e-check');
                checks.forEach(function(cb) { cb.checked = true; });
                $('e2e-model').value = 'qwen2.5';
            }
        """)
        print(f"✓ Selected all test types, model=qwen2.5")

        # Click the run button
        page.click("#e2e-go", timeout=10000)
        print(f"✓ Started E2E tests")

        # Wait for tests to complete (button re-enables)
        page.wait_for_function(
            "() => document.querySelector('#e2e-go') && !document.querySelector('#e2e-go').disabled",
            timeout=300000
        )
        elapsed = time.time() - start if 'start' in dir() else 0
        print(f"✓ Tests completed")

        # ── Capture e2eResults ──
        results_json = page.evaluate("() => JSON.stringify(e2eResults)")
        results = json.loads(results_json)

        print(f"\n── Results ({len(results)} tests) ──")
        for r in results:
            name = r.get('type', '?')
            status = r.get('status', '?')
            lat = r.get('latency', '?')
            output = r.get('output', {})
            content = ''
            fcm = None
            if name == 'chat':
                content = (output.get('content') or 'EMPTY')[:60]
                fcm = output.get('first_chunk_ms')
            elif name in ('vision', 'multimodal'):
                content = (output.get('content') or 'EMPTY')[:60]
                fcm = output.get('first_chunk_ms')
            elif name == 'asr':
                content = (output.get('text') or 'EMPTY')[:60]
            elif name == 'ws_chat':
                content = (output.get('content') or 'EMPTY')[:60]
                timing = output.get('timing', {})
                fcm = timing.get('first_chunk_ms')
            elif name == 'ws_asr':
                content = (output.get('llm_text') or 'EMPTY')[:60]
                timing = output.get('timing', {})
                fcm = timing.get('llm_first_chunk_ms')
            elif name == 'ws_asr':
                content = (output.get('asr_text') or 'EMPTY')[:40] + ' → ' + (output.get('llm_text') or 'EMPTY')[:40]
                timing = output.get('timing', {})
                fcm = timing.get('llm_first_chunk_ms')
            print(f"  {name}: {status} {lat}ms | fcm={fcm} | {content}")

        # ── Capture statistics ──
        stats = page.evaluate("""
            () => ({
                total: $('e2e-total').textContent,
                pass: $('e2e-pass').textContent,
                fail: $('e2e-fail').textContent,
                rate: $('e2e-rate').textContent,
                avgms: $('e2e-avgms').textContent,
                totalms: $('e2e-totalms').textContent,
            })
        """)
        print(f"\n── Dashboard Statistics ──")
        print(f"  Total: {stats['total']}")
        print(f"  Pass: {stats['pass']}")
        print(f"  Fail: {stats['fail']}")
        print(f"  Rate: {stats['rate']}")
        print(f"  Avg Latency: {stats['avgms']}")
        print(f"  Total Time: {stats['totalms']}")

        browser.close()
        return results, stats

if __name__ == "__main__":
    start = time.time()
    results, stats = run_full_e2e()
    elapsed = time.time() - start
    print(f"\nWall time: {elapsed:.1f}s")
    with open("/tmp/e2e_full_results.json", "w") as f:
        json.dump({"results": results, "stats": stats, "wall_time": elapsed}, f, indent=2)
    print(f"Saved to /tmp/e2e_full_results.json")
