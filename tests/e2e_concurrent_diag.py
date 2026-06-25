"""Playwright 并发 SSE 诊断 — 2个并发 chat 请求的详细解析过程"""
import json
import time
from playwright.sync_api import sync_playwright

DASHBOARD_URL = "http://localhost:8090/new_dashboard.html"

def diagnose_concurrent():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(DASHBOARD_URL, timeout=15000)
        print("✓ Page loaded")

        # ── Run 2 concurrent chat SSE requests with detailed parsing ──
        result = page.evaluate("""
            async () => {
                var t0 = performance.now();
                var results = [];

                async function testChat(id) {
                    var input = { type: 'chat', messages: [{ role: 'user', content: 'say hi briefly' }], model: 'qwen2.5', stream: true, max_tokens: 10 };
                    var r = await fetch(API + '/v1/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) });
                    var httpStatus = r.status;
                    var contentType = r.headers.get('content-type');

                    // SSE incremental parsing (same as e2eChat)
                    var reader = r.body.getReader();
                    var decoder = new TextDecoder();
                    var fullText = '';
                    var firstChunkMs = null;
                    var buffer = '';
                    var chunkDetails = [];
                    var rawChunksReceived = 0;

                    while (true) {
                        var {done, value} = await reader.read();
                        if (done) break;
                        rawChunksReceived++;
                        var rawChunk = decoder.decode(value, {stream: true});
                        buffer += rawChunk;
                        var parts = buffer.split('\\n\\n');
                        buffer = parts.pop();
                        for (var i = 0; i < parts.length; i++) {
                            var line = parts[i].trim();
                            if (line.startsWith('data: ') && line !== 'data: [DONE]') {
                                try {
                                    var data = JSON.parse(line.slice(6));
                                    var delta = data.choices && data.choices[0] && data.choices[0].delta;
                                    var content = delta && delta.content;
                                    chunkDetails.push({
                                        hasContent: !!content,
                                        contentLen: content ? content.length : 0,
                                        content: content || '',
                                        hasDelta: !!delta,
                                        deltaKeys: delta ? Object.keys(delta) : [],
                                        rawLine: line.substring(0, 120)
                                    });
                                    if (content) {
                                        fullText += content;
                                        if (!firstChunkMs) firstChunkMs = +(performance.now() - t0).toFixed(0);
                                    }
                                } catch(e) {
                                    chunkDetails.push({parseError: e.message, rawLine: line.substring(0, 80)});
                                }
                            }
                        }
                    }
                    var totalMs = +(performance.now() - t0).toFixed(0);
                    return {
                        id: id,
                        httpStatus: httpStatus,
                        contentType: contentType,
                        rawChunksReceived: rawChunksReceived,
                        totalMs: totalMs,
                        fullText: fullText,
                        firstChunkMs: firstChunkMs,
                        chunkDetailsCount: chunkDetails.length,
                        chunkDetailsPreview: chunkDetails.slice(0, 5),
                        bufferRemaining: buffer.substring(0, 100)
                    };
                }

                // Run 2 concurrent chat tests
                var p1 = testChat('chat-1');
                var p2 = testChat('chat-2');
                var r1 = await p1;
                var r2 = await p2;
                return JSON.stringify([r1, r2]);
            }
        """)

        results = json.loads(result)
        for r in results:
            print(f"\n── {r['id']} ──")
            print(f"  HTTP Status: {r['httpStatus']}")
            print(f"  Content-Type: {r['contentType']}")
            print(f"  Raw chunks received: {r['rawChunksReceived']}")
            print(f"  Total time: {r['totalMs']}ms")
            print(f"  FullText: {r['fullText'] or 'EMPTY'}")
            print(f"  First chunk ms: {r['firstChunkMs']}")
            print(f"  Parsed chunk details: {r['chunkDetailsCount']}")
            print(f"  Buffer remaining: {r.get('bufferRemaining', '')[:80]}")
            for cd in r.get('chunkDetailsPreview', []):
                print(f"    Chunk: hasContent={cd.get('hasContent')}, len={cd.get('contentLen')}, deltaKeys={cd.get('deltaKeys')}, content='{cd.get('content', '')[:20]}'")
                if cd.get('parseError'):
                    print(f"    Parse error: {cd['parseError']}")

        browser.close()

if __name__ == "__main__":
    diagnose_concurrent()
