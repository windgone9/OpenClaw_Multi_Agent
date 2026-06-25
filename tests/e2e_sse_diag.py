"""Playwright SSE 解析诊断 — 捕获原始 SSE 数据和 JS 解析结果"""
import json
import sys
from playwright.sync_api import sync_playwright

DASHBOARD_URL = "http://localhost:8090/new_dashboard.html"

def diagnose_sse():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        console_logs = []
        page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))

        page.goto(DASHBOARD_URL, timeout=15000)

        # ── Test: Raw SSE stream for chat ──
        print("\n── Chat SSE Raw Data Diagnosis ──")
        raw_sse = page.evaluate("""
            async () => {
                var input = { type: 'chat', messages: [{ role: 'user', content: 'say hi' }], model: 'qwen2.5', stream: true, max_tokens: 10 };
                var r = await fetch(API + '/v1/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) });
                var contentType = r.headers.get('content-type');
                var status = r.status;
                var reader = r.body.getReader();
                var decoder = new TextDecoder();
                var chunks = [];
                var totalRaw = '';
                while (true) {
                    var {done, value} = await reader.read();
                    if (done) break;
                    var raw = decoder.decode(value, {stream: true});
                    totalRaw += raw;
                    chunks.push(raw);
                }
                // Parse SSE data
                var fullText = '';
                var firstChunkMs = null;
                var parsedLines = [];
                var buffer = totalRaw;
                var parts = buffer.split('\\n\\n');
                for (var i = 0; i < parts.length; i++) {
                    var line = parts[i].trim();
                    if (line.startsWith('data: ') && line !== 'data: [DONE]') {
                        try {
                            var data = JSON.parse(line.slice(6));
                            parsedLines.push(data);
                            if (data.choices && data.choices[0].delta && data.choices[0].delta.content) {
                                fullText += data.choices[0].delta.content;
                            }
                        } catch(e) {
                            parsedLines.push({parseError: e.message, raw: line.slice(0, 100)});
                        }
                    }
                }
                return JSON.stringify({
                    httpStatus: status,
                    contentType: contentType,
                    totalRawLength: totalRaw.length,
                    totalRawPreview: totalRaw.substring(0, 500),
                    chunkCount: chunks.length,
                    firstChunkPreview: chunks[0] ? chunks[0].substring(0, 200) : 'none',
                    partsCount: parts.length,
                    parsedLinesCount: parsedLines.length,
                    fullText: fullText,
                    firstParsedPreview: parsedLines[0] ? JSON.stringify(parsedLines[0]).substring(0, 200) : 'none',
                });
            }
        """)
        r = json.loads(raw_sse)
        print(f"  HTTP Status: {r.get('httpStatus')}")
        print(f"  Content-Type: {r.get('contentType')}")
        print(f"  Total Raw Length: {r.get('totalRawLength')}")
        print(f"  Total Raw Preview: {r.get('totalRawPreview', '')[:300]}")
        print(f"  Chunk Count: {r.get('chunkCount')}")
        print(f"  First Chunk Preview: {r.get('firstChunkPreview', '')[:200]}")
        print(f"  Parts Count (split by \\n\\n): {r.get('partsCount')}")
        print(f"  Parsed Lines Count: {r.get('parsedLinesCount')}")
        print(f"  FullText: {r.get('fullText', 'EMPTY')}")
        print(f"  First Parsed Preview: {r.get('firstParsedPreview', '')[:200]}")

        # ── Also test e2eChat directly ──
        print("\n── e2eChat Direct Call ──")
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
        r2 = json.loads(chat_result)
        print(f"  status: {r2.get('status')}")
        print(f"  latency: {r2.get('latency')}ms")
        print(f"  output.content: {(r2.get('output', {}).get('content') or 'EMPTY')[:100]}")
        print(f"  output.first_chunk_ms: {r2.get('output', {}).get('first_chunk_ms')}")

        browser.close()

    # Print relevant console logs
    print("\n── Console Logs ──")
    for log in console_logs:
        print(f"  {log[:200]}")

if __name__ == "__main__":
    diagnose_sse()
