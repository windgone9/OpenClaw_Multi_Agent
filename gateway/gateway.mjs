import { createServer } from "http";
import { readFileSync, existsSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = join(__dirname, "..");

const GATEWAY_PORT = parseInt(process.env.GATEWAY_PORT || "3000", 10);
const GATEWAY_HOST = process.env.GATEWAY_HOST || "0.0.0.0";
const MAX_REQUEST_LOG = 200;

const OPENCLAW_CONFIG_PATH = join(PROJECT_ROOT, "openclaw.json");

const requestLog = [];
let totalRequests = 0;
let successCount = 0;
let failCount = 0;
let fallbackCount = 0;

function loadConfig() {
  if (existsSync(OPENCLAW_CONFIG_PATH)) {
    try {
      return JSON.parse(readFileSync(OPENCLAW_CONFIG_PATH, "utf-8"));
    } catch (e) {
      console.error("Failed to load openclaw.json:", e.message);
    }
  }
  return null;
}

const config = loadConfig();

const MODEL_CATALOG = {};
const AGENT_MAP = {};

if (config?.models?.catalog) {
  for (const [type, models] of Object.entries(config.models.catalog)) {
    if (Array.isArray(models)) {
      for (const m of models) {
        MODEL_CATALOG[m.id] = m;
      }
    }
  }
}

if (config?.agents?.list) {
  for (const agent of config.agents.list) {
    AGENT_MAP[agent.id] = agent;
  }
}

const API_KEYS = {
  moonshot: process.env.MOONSHOT_API_KEY || "",
  deepseek: process.env.DEEPSEEK_API_KEY || "",
  ollama: "",
};

function resolveModel(modelName) {
  if (modelName.startsWith("openclaw/")) {
    const agentId = modelName.replace("openclaw/", "");
    const agent = AGENT_MAP[agentId];
    if (agent?.model?.primary) {
      return MODEL_CATALOG[agent.model.primary] || null;
    }
    if (agent?.model) {
      return MODEL_CATALOG[agent.model] || null;
    }
  }
  if (MODEL_CATALOG[modelName]) {
    return MODEL_CATALOG[modelName];
  }
  for (const [id, m] of Object.entries(MODEL_CATALOG)) {
    if (m.modelId === modelName || id === modelName) {
      return m;
    }
  }
  return null;
}

function getApiKey(provider) {
  return API_KEYS[provider] || "";
}

function logRequest(entry) {
  totalRequests++;
  if (entry.status === "success") successCount++;
  else failCount++;
  if (entry.fallback_used) fallbackCount++;

  requestLog.unshift(entry);
  if (requestLog.length > MAX_REQUEST_LOG) {
    requestLog.pop();
  }

  const statusIcon = entry.status === "success" ? "✓" : "✗";
  const fbTag = entry.fallback_used ? " [FALLBACK]" : "";
  const toolTag = entry.has_tools ? " [TOOLS]" : "";
  const modeTag = entry.route_mode === "agent" ? " [AGENT]" : "";
  const agentTag = entry.agent_id ? `[${entry.agent_id}]` : "";
  console.log(
    `[Gateway] ${statusIcon} #${entry.id} ${entry.requested_model} → ${entry.resolved_model}${fbTag}${toolTag}${modeTag}${agentTag} ${entry.latency_ms}ms`
  );
}

async function callProviderApi(endpoint, payload, timeoutMs = 60000) {
  const baseUrl = endpoint.baseUrl;
  const url = `${baseUrl}/chat/completions`;
  const apiKey = getApiKey(endpoint.provider);

  const headers = { "Content-Type": "application/json" };
  if (apiKey) {
    headers["Authorization"] = `Bearer ${apiKey}`;
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    if (!response.ok) {
      const text = await response.text();
      return { status: "error", httpStatus: response.status, error: text };
    }

    const data = await response.json();
    return { status: "ok", data };
  } catch (e) {
    clearTimeout(timeoutId);
    return { status: "error", error: e.message };
  }
}

function parseBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

function sendJson(res, statusCode, data) {
  res.writeHead(statusCode, {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
  });
  res.end(JSON.stringify(data));
}

function sendHtml(res, html) {
  res.writeHead(200, {
    "Content-Type": "text/html; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
  });
  res.end(html);
}

const CONSOLE_HTML = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenClaw Gateway Console</title>
<style>
:root {
  --bg: #0f172a; --surface: #1e293b; --surface2: #334155;
  --border: #475569; --text: #e2e8f0; --text2: #94a3b8;
  --green: #22c55e; --red: #ef4444; --yellow: #eab308;
  --blue: #3b82f6; --purple: #a855f7; --orange: #f97316;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }
.header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
.header h1 { font-size: 20px; font-weight: 600; }
.header h1 span { color: var(--purple); }
.header-right { display: flex; gap: 12px; align-items: center; }
.header-right a { color: var(--blue); text-decoration: none; font-size: 14px; }
.header-right a:hover { text-decoration: underline; }
.stats-bar { display: flex; gap: 16px; padding: 16px 24px; background: var(--surface); border-bottom: 1px solid var(--border); }
.stat-card { background: var(--surface2); border-radius: 8px; padding: 12px 20px; flex: 1; }
.stat-card .label { font-size: 12px; color: var(--text2); margin-bottom: 4px; }
.stat-card .value { font-size: 24px; font-weight: 700; }
.stat-card .value.green { color: var(--green); }
.stat-card .value.red { color: var(--red); }
.stat-card .value.yellow { color: var(--yellow); }
.stat-card .value.blue { color: var(--blue); }
.stat-card .value.purple { color: var(--purple); }
.toolbar { display: flex; gap: 8px; padding: 12px 24px; align-items: center; }
.toolbar button { background: var(--surface2); border: 1px solid var(--border); color: var(--text); padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.toolbar button:hover { background: var(--border); }
.toolbar button.active { background: var(--blue); border-color: var(--blue); }
.toolbar .spacer { flex: 1; }
.toolbar .auto-refresh { font-size: 12px; color: var(--text2); }
.request-list { padding: 0 24px 24px; }
.req-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 8px; overflow: hidden; transition: border-color 0.2s; }
.req-card:hover { border-color: var(--blue); }
.req-header { display: flex; align-items: center; padding: 10px 16px; gap: 10px; cursor: pointer; }
.req-id { font-size: 12px; color: var(--text2); min-width: 50px; }
.req-status { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 4px; }
.req-status.success { background: #052e16; color: var(--green); }
.req-status.error { background: #450a0a; color: var(--red); }
.req-model { font-size: 13px; color: var(--blue); }
.req-arrow { color: var(--text2); font-size: 12px; }
.req-resolved { font-size: 13px; color: var(--purple); }
.req-tags { display: flex; gap: 4px; }
.req-tag { font-size: 10px; padding: 1px 6px; border-radius: 3px; background: var(--surface2); color: var(--text2); }
.req-tag.fallback { background: #422006; color: var(--orange); }
.req-tag.tools { background: #1e1b4b; color: var(--purple); }
.req-latency { font-size: 12px; color: var(--text2); margin-left: auto; }
.req-time { font-size: 11px; color: var(--text2); }
.req-detail { display: none; border-top: 1px solid var(--border); padding: 12px 16px; background: var(--surface2); }
.req-card.expanded .req-detail { display: block; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.detail-item { }
.detail-item .dk { font-size: 11px; color: var(--text2); margin-bottom: 2px; }
.detail-item .dv { font-size: 13px; word-break: break-all; }
.detail-full { grid-column: 1 / -1; margin-top: 8px; }
.detail-full .dk { font-size: 11px; color: var(--text2); margin-bottom: 4px; }
.detail-full pre { background: var(--bg); border: 1px solid var(--border); border-radius: 4px; padding: 8px; font-size: 11px; overflow-x: auto; max-height: 200px; color: var(--text2); }
.empty-state { text-align: center; padding: 60px 24px; color: var(--text2); }
.empty-state .icon { font-size: 48px; margin-bottom: 16px; }
.empty-state p { font-size: 14px; }
</style>
</head>
<body>
<div class="header">
  <h1>🚪 <span>OpenClaw</span> Gateway Console</h1>
  <div class="header-right">
    <a href="/health" target="_blank">Health API</a>
    <a href="/v1/models" target="_blank">Models API</a>
    <a href="http://localhost:8000" target="_blank">Scheduler Dashboard</a>
  </div>
</div>
<div class="stats-bar" id="stats-bar">
  <div class="stat-card"><div class="label">总请求数</div><div class="value blue" id="s-total">0</div></div>
  <div class="stat-card"><div class="label">成功</div><div class="value green" id="s-success">0</div></div>
  <div class="stat-card"><div class="label">失败</div><div class="value red" id="s-fail">0</div></div>
  <div class="stat-card"><div class="label">Fallback</div><div class="value yellow" id="s-fallback">0</div></div>
  <div class="stat-card"><div class="label">成功率</div><div class="value purple" id="s-rate">-</div></div>
</div>
<div class="toolbar">
  <button onclick="setFilter('all')" id="f-all" class="active">全部</button>
  <button onclick="setFilter('success')" id="f-success">成功</button>
  <button onclick="setFilter('error')" id="f-error">失败</button>
  <button onclick="setFilter('fallback')" id="f-fallback">Fallback</button>
  <button onclick="setFilter('tools')" id="f-tools">Tool Call</button>
  <div class="spacer"></div>
  <button onclick="clearLog()">清空日志</button>
  <span class="auto-refresh">自动刷新: <span id="refresh-status">ON</span></span>
</div>
<div class="request-list" id="request-list">
  <div class="empty-state"><div class="icon">📭</div><p>暂无请求记录，发送请求后将在此显示</p></div>
</div>
<script>
let currentFilter = 'all';
let autoRefresh = true;

function setFilter(f) {
  currentFilter = f;
  document.querySelectorAll('.toolbar button[id^="f-"]').forEach(b => b.classList.remove('active'));
  document.getElementById('f-' + f)?.classList.add('active');
  renderRequests();
}

async function fetchRequests() {
  try {
    const res = await fetch('/api/requests');
    const data = await res.json();
    updateStats(data.stats);
    renderRequests(data.requests);
  } catch(e) {
    console.error('Failed to fetch requests:', e);
  }
}

function updateStats(stats) {
  document.getElementById('s-total').textContent = stats.total;
  document.getElementById('s-success').textContent = stats.success;
  document.getElementById('s-fail').textContent = stats.fail;
  document.getElementById('s-fallback').textContent = stats.fallback;
  const rate = stats.total > 0 ? ((stats.success / stats.total) * 100).toFixed(1) + '%' : '-';
  document.getElementById('s-rate').textContent = rate;
}

let allRequests = [];

function renderRequests(requests) {
  if (requests) allRequests = requests;
  const filtered = currentFilter === 'all' ? allRequests : allRequests.filter(r => {
    if (currentFilter === 'success') return r.status === 'success';
    if (currentFilter === 'error') return r.status === 'error';
    if (currentFilter === 'fallback') return r.fallback_used;
    if (currentFilter === 'tools') return r.has_tools;
    return true;
  });

  const container = document.getElementById('request-list');
  if (filtered.length === 0) {
    container.innerHTML = '<div class="empty-state"><div class="icon">📭</div><p>暂无匹配的请求记录</p></div>';
    return;
  }

  container.innerHTML = filtered.map(r => {
    const statusClass = r.status === 'success' ? 'success' : 'error';
    const statusText = r.status === 'success' ? '✓ 成功' : '✗ 失败';
    const tags = [];
    if (r.fallback_used) tags.push('<span class="req-tag fallback">FALLBACK</span>');
    if (r.has_tools) tags.push('<span class="req-tag tools">TOOLS</span>');
    if (r.has_tool_calls) tags.push('<span class="req-tag tools">TOOL_CALLS</span>');

    const messagesPreview = (r.messages || []).map(m => {
      const role = m.role;
      const content = (typeof m.content === 'string' ? m.content : JSON.stringify(m.content)).slice(0, 80);
      return { role, content };
    });

    const responsePreview = r.response_output ? r.response_output.slice(0, 300) : '-';

    return '<div class="req-card" id="card-' + r.id + '" onclick="toggleCard(' + r.id + ')">' +
      '<div class="req-header">' +
        '<span class="req-id">#' + r.id + '</span>' +
        '<span class="req-status ' + statusClass + '">' + statusText + '</span>' +
        '<span class="req-model">' + esc(r.requested_model) + '</span>' +
        '<span class="req-arrow">→</span>' +
        '<span class="req-resolved">' + esc(r.resolved_model || '?') + '</span>' +
        '<span class="req-tags">' + tags.join('') + '</span>' +
        '<span class="req-latency">' + r.latency_ms + 'ms</span>' +
        '<span class="req-time">' + formatTime(r.timestamp) + '</span>' +
      '</div>' +
      '<div class="req-detail">' +
        '<div class="detail-grid">' +
          '<div class="detail-item"><div class="dk">请求模型</div><div class="dv">' + esc(r.requested_model) + '</div></div>' +
          '<div class="detail-item"><div class="dk">实际模型</div><div class="dv">' + esc(r.resolved_model || '-') + '</div></div>' +
          '<div class="detail-item"><div class="dk">Provider</div><div class="dv">' + esc(r.provider || '-') + '</div></div>' +
          '<div class="detail-item"><div class="dk">延迟</div><div class="dv">' + r.latency_ms + 'ms</div></div>' +
          '<div class="detail-item"><div class="dk">Fallback</div><div class="dv">' + (r.fallback_used ? '是 → ' + esc(r.fallback_model || '?') : '否') + '</div></div>' +
          '<div class="detail-item"><div class="dk">Tools</div><div class="dv">' + (r.has_tools ? r.tools_count + ' 个' : '无') + '</div></div>' +
          '<div class="detail-item"><div class="dk">Finish Reason</div><div class="dv">' + esc(r.finish_reason || '-') + '</div></div>' +
          '<div class="detail-item"><div class="dk">Token Usage</div><div class="dv">' + (r.usage ? (r.usage.prompt_tokens || '?') + '/' + (r.usage.completion_tokens || '?') : '-') + '</div></div>' +
        '</div>' +
        '<div class="detail-full"><div class="dk">Messages</div><pre>' + esc(JSON.stringify(messagesPreview, null, 2)) + '</pre></div>' +
        '<div class="detail-full"><div class="dk">Response</div><pre>' + esc(responsePreview) + '</pre></div>' +
        (r.error ? '<div class="detail-full"><div class="dk">Error</div><pre style="color:var(--red)">' + esc(r.error) + '</pre></div>' : '') +
      '</div>' +
    '</div>';
  }).join('');
}

function toggleCard(id) {
  const card = document.getElementById('card-' + id);
  if (card) card.classList.toggle('expanded');
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function formatTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}

async function clearLog() {
  try {
    await fetch('/api/requests/clear', { method: 'POST' });
    allRequests = [];
    renderRequests();
    fetchRequests();
  } catch(e) {}
}

setInterval(() => { if (autoRefresh) fetchRequests(); }, 2000);
fetchRequests();
</script>
</body>
</html>`;

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);

  if (req.method === "OPTIONS") {
    sendJson(res, 200, {});
    return;
  }

  try {
    if (req.method === "GET" && url.pathname === "/health") {
      sendJson(res, 200, {
        status: "healthy",
        service: "openclaw-gateway",
        version: "2026.4.27",
        models_registered: Object.keys(MODEL_CATALOG).length,
        agents_registered: Object.keys(AGENT_MAP).length,
        stats: { total: totalRequests, success: successCount, fail: failCount, fallback: fallbackCount },
      });
      return;
    }

    if (req.method === "GET" && url.pathname === "/v1/models") {
      const models = Object.values(MODEL_CATALOG).map((m) => ({
        id: m.id,
        object: "model",
        created: Math.floor(Date.now() / 1000),
        owned_by: m.provider,
        permissions: [],
      }));
      sendJson(res, 200, { object: "list", data: models });
      return;
    }

    if (req.method === "GET" && url.pathname === "/console") {
      sendHtml(res, CONSOLE_HTML);
      return;
    }

    if (req.method === "GET" && url.pathname === "/api/requests") {
      sendJson(res, 200, {
        stats: { total: totalRequests, success: successCount, fail: failCount, fallback: fallbackCount },
        requests: requestLog,
      });
      return;
    }

    if (req.method === "POST" && url.pathname === "/api/requests/clear") {
      requestLog.length = 0;
      totalRequests = 0;
      successCount = 0;
      failCount = 0;
      fallbackCount = 0;
      sendJson(res, 200, { cleared: true });
      return;
    }

    if (req.method === "POST" && url.pathname === "/v1/chat/completions") {
      const body = await parseBody(req);
      const requestedModel = body.model || "openclaw/local-dispatcher";
      const endpoint = resolveModel(requestedModel);
      const requestId = totalRequests + 1;
      const startTime = Date.now();

      const logEntry = {
        id: requestId,
        timestamp: new Date().toISOString(),
        requested_model: requestedModel,
        resolved_model: endpoint ? endpoint.id : null,
        provider: endpoint ? endpoint.provider : null,
        has_tools: !!(body.tools && body.tools.length > 0),
        tools_count: body.tools ? body.tools.length : 0,
        messages_count: body.messages ? body.messages.length : 0,
        messages: (body.messages || []).map(m => ({ role: m.role, content: typeof m.content === 'string' ? m.content.slice(0, 200) : JSON.stringify(m.content).slice(0, 200) })),
        route_mode: body.route_mode || "gateway",
        agent_id: body.agent_id || null,
        status: "pending",
        latency_ms: 0,
        fallback_used: false,
        fallback_model: null,
        finish_reason: null,
        usage: null,
        response_output: null,
        error: null,
      };

      if (!endpoint) {
        logEntry.status = "error";
        logEntry.error = `Model '${requestedModel}' not found in gateway catalog`;
        logEntry.latency_ms = Date.now() - startTime;
        logRequest(logEntry);
        sendJson(res, 400, {
          error: {
            message: `Model '${requestedModel}' not found in gateway catalog`,
            type: "invalid_request_error",
            code: "model_not_found",
          },
        });
        return;
      }

      const providerPayload = {
        model: endpoint.modelId,
        messages: body.messages || [],
        max_tokens: body.max_tokens || 1024,
      };

      if (body.tools && endpoint.supportsTools) {
        providerPayload.tools = body.tools;
        providerPayload.tool_choice = body.tool_choice || "auto";
      }

      if (body.temperature !== undefined) providerPayload.temperature = body.temperature;
      if (body.top_p !== undefined) providerPayload.top_p = body.top_p;
      if (body.stream !== undefined) providerPayload.stream = body.stream;

      const timeoutMs = body.timeout_ms || 60000;
      const result = await callProviderApi(endpoint, providerPayload, timeoutMs);

      if (result.status === "error") {
        const agent = Object.entries(AGENT_MAP).find(
          ([_, a]) => a.model?.primary === endpoint.id || a.model === endpoint.id
        );

        if (agent) {
          const agentConfig = agent[1];
          const fallbacks = agentConfig.model?.fallbacks || [];
          for (const fbId of fallbacks) {
            const fbEndpoint = MODEL_CATALOG[fbId];
            if (fbEndpoint) {
              const fbPayload = { ...providerPayload, model: fbEndpoint.modelId };
              if (body.tools && !fbEndpoint.supportsTools) {
                delete fbPayload.tools;
                delete fbPayload.tool_choice;
              }
              const fbResult = await callProviderApi(fbEndpoint, fbPayload, timeoutMs);
              if (fbResult.status === "ok") {
                logEntry.status = "success";
                logEntry.latency_ms = Date.now() - startTime;
                logEntry.fallback_used = true;
                logEntry.fallback_model = fbId;
                const fbData = fbResult.data;
                const fbMessage = fbData.choices?.[0]?.message;
                logEntry.finish_reason = fbData.choices?.[0]?.finish_reason || null;
                logEntry.usage = fbData.usage || null;
                logEntry.response_output = fbMessage?.content || (fbMessage?.tool_calls?.map(tc => `[Tool Call] ${tc.function?.name}(${tc.function?.arguments?.slice(0,100)})`).join('\n')) || null;
                logEntry.has_tool_calls = !!(fbMessage?.tool_calls && fbMessage.tool_calls.length > 0);
                logRequest(logEntry);
                sendJson(res, 200, fbData);
                return;
              }
            }
          }
        }

        logEntry.status = "error";
        logEntry.latency_ms = Date.now() - startTime;
        logEntry.error = `All endpoints failed: ${result.error}`;
        logRequest(logEntry);
        sendJson(res, 502, {
          error: {
            message: `All endpoints failed for model '${requestedModel}': ${result.error}`,
            type: "upstream_error",
            code: "all_endpoints_failed",
          },
        });
        return;
      }

      const data = result.data;
      const message = data.choices?.[0]?.message;
      logEntry.status = "success";
      logEntry.latency_ms = Date.now() - startTime;
      logEntry.finish_reason = data.choices?.[0]?.finish_reason || null;
      logEntry.usage = data.usage || null;
      logEntry.response_output = message?.content || (message?.tool_calls?.map(tc => `[Tool Call] ${tc.function?.name}(${tc.function?.arguments?.slice(0,100)})`).join('\n')) || null;
      logEntry.has_tool_calls = !!(message?.tool_calls && message.tool_calls.length > 0);
      logRequest(logEntry);
      sendJson(res, 200, data);
      return;
    }

    if (req.method === "GET" && url.pathname === "/v1/agents") {
      const agents = Object.entries(AGENT_MAP).map(([id, a]) => ({
        id,
        model: a.model?.primary || a.model,
        fallbacks: a.model?.fallbacks || [],
      }));
      sendJson(res, 200, { agents });
      return;
    }

    if (req.method === "GET" && url.pathname === "/") {
      sendHtml(res, `<html><body><h2>OpenClaw Gateway</h2><p><a href="/console">Open Console</a></p><ul><li><a href="/health">Health</a></li><li><a href="/v1/models">Models</a></li><li><a href="/v1/agents">Agents</a></li><li><a href="/api/requests">Request Log API</a></li></ul></body></html>`);
      return;
    }

    sendJson(res, 404, { error: "Not found" });
  } catch (error) {
    console.error("Gateway error:", error);
    sendJson(res, 500, { error: error.message });
  }
});

server.listen(GATEWAY_PORT, GATEWAY_HOST, () => {
  console.log(`OpenClaw Gateway running on ${GATEWAY_HOST}:${GATEWAY_PORT}`);
  console.log(`Console: http://localhost:${GATEWAY_PORT}/console`);
  console.log(`Models: ${Object.keys(MODEL_CATALOG).length} registered`);
  console.log(`Agents: ${Object.keys(AGENT_MAP).length} registered`);
  console.log(`Config: ${OPENCLAW_CONFIG_PATH}`);
});
