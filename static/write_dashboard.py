#!/usr/bin/env python3
"""Write the complete dashboard.html file."""
import os

DASHBOARD = os.path.join(os.path.dirname(__file__), "dashboard.html")

html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenClaw 调度监控中心</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0e1a;--card:#111827;--border:#1e293b;--border2:#334155;--text:#e2e8f0;--text2:#94a3b8;--text3:#64748b;--blue:#38bdf8;--blue2:#2563eb;--green:#4ade80;--green2:#059669;--yellow:#facc15;--red:#f87171;--purple:#a78bfa;--purple2:#7c3aed;--orange:#fb923c;--cyan:#22d3ee}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans SC',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}
.topbar{background:linear-gradient(135deg,#0f172a,#1e1b4b);padding:12px 24px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.topbar h1{font-size:18px;font-weight:700;display:flex;align-items:center;gap:10px}
.topbar h1 .logo{font-size:22px}
.topbar h1 span{background:linear-gradient(135deg,var(--blue),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.topbar .meta{display:flex;align-items:center;gap:14px;font-size:12px;color:var(--text3)}
.topbar .meta .svc-dot{display:flex;align-items:center;gap:4px;padding:2px 8px;border-radius:10px;background:var(--card);border:1px solid var(--border)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot.ok{background:var(--green);box-shadow:0 0 6px var(--green)}
.dot.err{background:var(--red);box-shadow:0 0 6px var(--red)}
.tab-nav{display:flex;background:var(--card);border-bottom:1px solid var(--border);padding:0 16px}
.tab-btn{padding:10px 20px;font-size:13px;font-weight:600;color:var(--text3);border:none;background:none;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}
.tab-btn:hover{color:var(--text2)}
.tab-btn.active{color:var(--blue);border-bottom-color:var(--blue)}
.tab-content{display:none;height:calc(100vh - 90px);overflow:hidden}
.tab-content.active{display:flex}
.pipeline-layout{display:grid;grid-template-columns:280px 1fr 360px;gap:0;width:100%;height:100%}
.pl-left{border-right:1px solid var(--border);overflow-y:auto;padding:14px;background:#0d1117}
.pl-center{display:flex;flex-direction:column;overflow:hidden}
.pl-right{border-left:1px solid var(--border);overflow-y:auto;padding:14px;background:#0d1117}
.pf-stage{width:100%;margin-bottom:2px}
.pf-stage-header{display:flex;align-items:center;gap:10px;padding:10px 14px;border-radius:8px;background:var(--card);border:1px solid var(--border);cursor:pointer;transition:all .15s}
.pf-stage-header:hover{border-color:var(--blue)}
.pf-icon{font-size:18px;width:28px;text-align:center}
.pf-info{flex:1}
.pf-title{font-size:13px;font-weight:700}
.pf-desc{font-size:10px;color:var(--text3);margin-top:2px}
.pf-badge{font-size:10px;padding:2px 8px;border-radius:10px;font-weight:600}
.pf-badge.ok{background:#052e16;color:var(--green)}
.pf-badge.err{background:#450a0a;color:var(--red)}
.pf-badge.idle{background:var(--border);color:var(--text3)}
.pf-metric{font-size:11px;color:var(--text2);font-weight:600}
.pf-arrow{display:flex;justify-content:center;padding:2px 0;color:var(--border2);font-size:18px}
.pf-detail{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:6px;font-size:11px;display:none}
.pf-detail.show{display:block}
.pf-detail-row{display:flex;justify-content:space-between;margin-bottom:4px}
.pf-detail-key{color:var(--text3)}
.pf-detail-val{font-weight:600}
.req-form{display:grid;grid-template-columns:1fr 1fr 1fr 1fr 1fr;gap:8px;margin-bottom:10px;padding:14px;border-bottom:1px solid var(--border)}
.req-form select,.req-form input{background:var(--bg);border:1px solid var(--border2);border-radius:6px;color:var(--text);padding:7px 10px;font-size:12px;width:100%}
.req-form select:focus,.req-form input:focus{outline:none;border-color:var(--blue)}
.req-form textarea{grid-column:1/-1;background:var(--bg);border:1px solid var(--border2);border-radius:6px;color:var(--text);padding:10px;font-size:13px;resize:vertical;min-height:50px;font-family:inherit}
.req-form textarea:focus{outline:none;border-color:var(--blue)}
.btn-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.btn{padding:7px 14px;border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;transition:all .15s;display:flex;align-items:center;gap:4px}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-primary{background:var(--blue2);color:#fff}
.btn-primary:hover:not(:disabled){background:#1d4ed8}
.btn-green{background:var(--green2);color:#fff}
.btn-purple{background:var(--purple2);color:#fff}
.btn-ghost{background:var(--border2);color:var(--text)}
.btn-sm{padding:5px 10px;font-size:11px}
.dispatch-card{background:var(--card);border:1px solid var(--border);border-radius:10px;margin-bottom:14px;overflow:hidden;animation:slideIn .3s ease}
@keyframes slideIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.dc-header{padding:12px 14px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);cursor:pointer}
.dc-left{display:flex;align-items:center;gap:10px}
.dc-id{font-size:11px;color:var(--text3);font-family:monospace}
.dc-strategy{font-size:11px;padding:3px 8px;border-radius:6px;font-weight:600}
.dc-strategy.local{background:#052e16;color:var(--green)}
.dc-strategy.cloud{background:#172554;color:var(--blue)}
.dc-strategy.agent{background:#312e81;color:var(--purple)}
.dc-model{font-size:12px;font-weight:600}
.dc-right{display:flex;align-items:center;gap:10px}
.dc-latency{font-size:12px;font-weight:600;color:var(--blue)}
.dc-status{font-size:11px;padding:3px 8px;border-radius:6px;font-weight:600}
.dc-status.success{background:#052e16;color:var(--green)}
.dc-status.failed{background:#450a0a;color:var(--red)}
.dc-status.running{background:#172554;color:var(--blue)}
.dc-body{padding:14px}
.dc-body.collapsed{display:none}
.di-label{font-size:10px;color:var(--text3);margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px}
.di-text{background:var(--bg);border-radius:6px;padding:8px 10px;font-size:12px;color:var(--text);border:1px solid var(--border)}
.do-label{font-size:10px;color:var(--text3);margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px}
.do-text{background:var(--bg);border-radius:6px;padding:8px 10px;font-size:12px;color:var(--text);border:1px solid var(--border);max-height:120px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
.trace-timeline{position:relative;padding-left:24px}
.trace-timeline::before{content:'';position:absolute;left:8px;top:0;bottom:0;width:2px;background:var(--border)}
.trace-step{position:relative;margin-bottom:6px;padding:6px 10px;border-radius:6px;font-size:11px;display:flex;align-items:center;gap:8px;background:var(--bg)}
.trace-step::before{content:'';position:absolute;left:-20px;top:50%;transform:translateY(-50%);width:10px;height:10px;border-radius:50%;border:2px solid var(--border2);background:var(--card)}
.trace-step.router::before{border-color:var(--blue);background:var(--blue)}
.trace-step.agent::before{border-color:var(--green);background:var(--green)}
.trace-step.fallback::before{border-color:var(--orange);background:var(--orange)}
.trace-step.result-ok::before{border-color:var(--green);background:var(--green)}
.trace-step.result-err::before{border-color:var(--red);background:var(--red)}
.ts-agent{color:var(--text3);min-width:80px;font-weight:600}
.ts-msg{flex:1;color:var(--text2)}
.tag{display:inline-block;font-size:10px;padding:2px 6px;border-radius:4px;margin-right:4px;font-weight:600}
.tag-local{background:#052e16;color:var(--green)}
.tag-cloud{background:#172554;color:var(--blue)}
.svc-layout{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:20px;overflow-y:auto;width:100%}
.svc-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px}
.svc-card-title{font-size:16px;font-weight:700;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.svc-card-title .svc-dot{width:10px;height:10px;border-radius:50%}
.svc-card-title .svc-dot.ok{background:var(--green);box-shadow:0 0 8px var(--green)}
.svc-card-title .svc-dot.err{background:var(--red);box-shadow:0 0 8px var(--red)}
.svc-row{display:flex;justify-content:space-between;padding:6px 0;font-size:13px;border-bottom:1px solid var(--border)}
.svc-row:last-child{border-bottom:none}
.svc-key{color:var(--text3)}
.svc-val{font-weight:600}
.svc-actions{display:flex;gap:8px;margin-top:14px}
.svc-btn{padding:8px 16px;border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;transition:all .15s}
.svc-btn:disabled{opacity:.4;cursor:not-allowed}
.svc-btn-start{background:var(--green2);color:#fff}
.svc-btn-stop{background:#991b1b;color:#fff}
.svc-btn-restart{background:var(--blue2);color:#fff}
.metrics-layout{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:20px;overflow-y:auto;width:100%}
.metrics-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px}
.metrics-card-title{font-size:14px;font-weight:700;margin-bottom:12px;display:flex;align-items:center;gap:6px}
.gauge-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.mini-gauge{background:var(--bg);border-radius:8px;padding:12px;text-align:center}
.mini-gauge .mg-val{font-size:24px;font-weight:700}
.mini-gauge .mg-lbl{font-size:10px;color:var(--text3);margin-top:4px}
.mini-gauge .mg-bar{height:4px;background:var(--border);border-radius:2px;margin-top:6px;overflow:hidden}
.mini-gauge .mg-bar-fill{height:100%;border-radius:2px;transition:width .5s}
.chart-container{background:var(--bg);border-radius:8px;padding:12px;margin-top:10px}
.chart-container canvas{width:100%!important}
.route-bar{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:12px}
.route-bar .rb-label{width:100px;color:var(--text3);font-size:11px}
.route-bar .rb-track{flex:1;height:14px;background:var(--bg);border-radius:6px;overflow:hidden}
.route-bar .rb-fill{height:100%;border-radius:6px;transition:width .5s;display:flex;align-items:center;justify-content:center;font-size:9px;color:#fff;font-weight:600}
.logs-layout{display:flex;flex-direction:column;width:100%;height:100%}
.logs-toolbar{display:flex;gap:8px;padding:12px 16px;border-bottom:1px solid var(--border);align-items:center}
.logs-toolbar select,.logs-toolbar input{background:var(--bg);border:1px solid var(--border2);border-radius:6px;color:var(--text);padding:6px 10px;font-size:12px}
.logs-body{flex:1;overflow-y:auto;padding:12px 16px;font-family:'SF Mono',Monaco,'Cascadia Code',monospace;font-size:11px;line-height:1.7}
.log-entry{padding:2px 0;display:flex;gap:10px;border-bottom:1px solid rgba(30,41,59,.3)}
.log-entry .le-time{color:var(--text3);min-width:85px}
.log-entry .le-svc{min-width:60px;font-weight:600}
.log-entry .le-svc.hermes{color:var(--cyan)}
.log-entry .le-svc.bridge{color:var(--purple)}
.log-entry .le-svc.gateway{color:var(--green)}
.log-entry .le-svc.official{color:var(--orange)}
.log-entry .le-svc.ollama{color:var(--yellow)}
.log-entry .le-level{min-width:40px;font-weight:600}
.log-entry .le-level.info{color:var(--blue)}
.log-entry .le-level.ok{color:var(--green)}
.log-entry .le-level.warn{color:var(--yellow)}
.log-entry .le-level.err{color:var(--red)}
.log-entry .le-msg{color:var(--text2);flex:1;word-break:break-all}
.spinner{display:inline-block;width:12px;height:12px;border:2px solid var(--border2);border-top-color:var(--blue);border-radius:50%;animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.empty-state{text-align:center;padding:40px 20px;color:var(--text3);font-size:13px}
.empty-state .es-icon{font-size:32px;margin-bottom:8px}
.panel-title{font-size:13px;font-weight:700;color:var(--text2);text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;display:flex;align-items:center;gap:6px}
.panel-title .pt-icon{font-size:14px}
.log-line{padding:1px 0;display:flex;gap:8px}
.log-line .ll-time{color:var(--text3);min-width:70px}
.log-line .ll-level{min-width:40px;font-weight:600}
.log-line .ll-level.info{color:var(--blue)}
.log-line .ll-level.ok{color:var(--green)}
.log-line .ll-level.warn{color:var(--yellow)}
.log-line .ll-level.err{color:var(--red)}
.log-line .ll-msg{color:var(--text2);flex:1;word-break:break-all}
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
</style>
</head>
<body>
<div class="topbar">
  <h1><span class="logo">🐾</span> <span>OpenClaw 调度监控中心</span></h1>
  <div class="meta">
    <span class="svc-dot"><span class="dot" id="dot-hermes"></span> Hermes</span>
    <span class="svc-dot"><span class="dot" id="dot-bridge"></span> Bridge</span>
    <span class="svc-dot"><span class="dot" id="dot-gateway"></span> Gateway</span>
    <span class="svc-dot"><span class="dot" id="dot-official"></span> OfficialGW</span>
    <span class="svc-dot"><span class="dot" id="dot-ollama"></span> Ollama</span>
    <span style="color:var(--purple)">v2026.6</span>
    <span id="clock" style="color:var(--text3)"></span>
  </div>
</div>
<div class="tab-nav">
  <button class="tab-btn active" onclick="switchTab('pipeline',this)">🔗 全链路追踪</button>
  <button class="tab-btn" onclick="switchTab('services',this)">🚀 服务管理</button>
  <button class="tab-btn" onclick="switchTab('metrics',this)">📊 监控指标</button>
  <button class="tab-btn" onclick="switchTab('logs',this)">📋 实时日志</button>
</div>
<div class="tab-content active" id="tab-pipeline">
  <div class="pipeline-layout">
    <div class="pl-left">
      <div class="panel-title"><span class="pt-icon">🔗</span> 请求全链路</div>
      <div class="pf-stage"><div class="pf-stage-header" onclick="toggleDetail('pf-q')"><span class="pf-icon">📨</span><div class="pf-info"><div class="pf-title">消息队列</div><div class="pf-desc">Hermes Queue 接收请求</div></div><span class="pf-badge idle" id="pf-q-badge">待机</span><span class="pf-metric" id="pf-q-metric">0</span></div><div class="pf-detail" id="pf-q-detail"><div class="pf-detail-row"><span class="pf-detail-key">后端</span><span class="pf-detail-val" id="pf-q-backend">-</span></div><div class="pf-detail-row"><span class="pf-detail-key">Worker</span><span class="pf-detail-val" id="pf-q-worker">-</span></div><div class="pf-detail-row"><span class="pf-detail-key">请求队列</span><span class="pf-detail-val" id="pf-q-req">0</span></div><div class="pf-detail-row"><span class="pf-detail-key">结果队列</span><span class="pf-detail-val" id="pf-q-res">0</span></div><div class="pf-detail-row"><span class="pf-detail-key">已处理</span><span class="pf-detail-val" id="pf-q-proc">0</span></div></div></div>
      <div class="pf-arrow">▼</div>
      <div class="pf-stage"><div class="pf-stage-header" onclick="toggleDetail('pf-hm')"><span class="pf-icon">🧠</span><div class="pf-info"><div class="pf-title">Hermes 智能路由</div><div class="pf-desc">复杂度评分 + Memory规则</div></div><span class="pf-badge idle" id="pf-hm-badge">待机</span><span class="pf-metric" id="pf-hm-metric">-</span></div><div class="pf-detail" id="pf-hm-detail"><div class="pf-detail-row"><span class="pf-detail-key">路由模式</span><span class="pf-detail-val" id="pf-hm-mode">-</span></div><div class="pf-detail-row"><span class="pf-detail-key">复杂度阈值</span><span class="pf-detail-val" id="pf-hm-threshold">-</span></div><div class="pf-detail-row"><span class="pf-detail-key">探索率</span><span class="pf-detail-val" id="pf-hm-exploration">-</span></div><div class="pf-detail-row"><span class="pf-detail-key">记忆记录</span><span class="pf-detail-val" id="pf-hm-memory">-</span></div><div class="pf-detail-row"><span class="pf-detail-key">已学技能</span><span class="pf-detail-val" id="pf-hm-skills">-</span></div></div></div>
      <div class="pf-arrow">▼</div>
      <div class="pf-stage"><div class="pf-stage-header" onclick="toggleDetail('pf-br')"><span class="pf-icon">🌉</span><div class="pf-info"><div class="pf-title">Bridge 调度</div><div class="pf-desc">路由分发 → Gateway/Agent</div></div><span class="pf-badge idle" id="pf-br-badge">待机</span><span class="pf-metric" id="pf-br-metric">-</span></div><div class="pf-detail" id="pf-br-detail"><div class="pf-detail-row"><span class="pf-detail-key">策略</span><span class="pf-detail-val" id="pf-br-strategy">-</span></div><div class="pf-detail-row"><span class="pf-detail-key">本地模型</span><span class="pf-detail-val" id="pf-br-local">-</span></div><div class="pf-detail-row"><span class="pf-detail-key">云端模型</span><span class="pf-detail-val" id="pf-br-cloud">-</span></div><div class="pf-detail-row"><span class="pf-detail-key">GW路由</span><span class="pf-detail-val" id="pf-br-gwr">-</span></div></div></div>
      <div class="pf-arrow">▼</div>
      <div class="pf-stage"><div class="pf-stage-header" onclick="toggleDetail('pf-ex')"><span class="pf-icon">⚡</span><div class="pf-info"><div class="pf-title">执行引擎</div><div class="pf-desc">direct_local / agent_chain</div></div><span class="pf-badge idle" id="pf-ex-badge">待机</span><span class="pf-metric" id="pf-ex-metric">-</span></div><div class="pf-detail" id="pf-ex-detail"><div class="pf-detail-row"><span class="pf-detail-key">Gateway</span><span class="pf-detail-val" id="pf-ex-gw">-</span></div><div class="pf-detail-row"><span class="pf-detail-key">Official GW</span><span class="pf-detail-val" id="pf-ex-ogw">-</span></div><div class="pf-detail-row"><span class="pf-detail-key">Ollama</span><span class="pf-detail-val" id="pf-ex-ollama">-</span></div></div></div>
      <div class="pf-arrow">▼</div>
      <div class="pf-stage"><div class="pf-stage-header" onclick="toggleDetail('pf-rt')"><span class="pf-icon">📤</span><div class="pf-info"><div class="pf-title">结果返回</div><div class="pf-desc">结果入队 + 反馈学习</div></div><span class="pf-badge idle" id="pf-rt-badge">待机</span><span class="pf-metric" id="pf-rt-metric">-</span></div><div class="pf-detail" id="pf-rt-detail"><div class="pf-detail-row"><span class="pf-detail-key">结果队列</span><span class="pf-detail-val" id="pf-rt-resq">0</span></div><div class="pf-detail-row"><span class="pf-detail-key">反馈记录</span><span class="pf-detail-val" id="pf-rt-fb">0</span></div><div class="pf-detail-row"><span class="pf-detail-key">成功率</span><span class="pf-detail-val" id="pf-rt-srate">-</span></div></div></div>
    </div>
    <div class="pl-center">
      <div class="req-form">
        <select id="req-appid"><option value="test-app">test-app</option><option value="prod-app">prod-app</option><option value="demo">demo</option></select>
        <select id="req-type"><option value="chat">chat</option><option value="completion">completion</option></select>
        <select id="req-priority"><option value="3">P3 普通</option><option value="2">P2 较高</option><option value="1">P1 紧急</option></select>
        <select id="req-mode"><option value="queue">Queue 队列</option><option value="bridge">Bridge 直连</option></select>
        <select id="req-model"><option value="">自动选择</option><option value="ollama/qwen2.5:3b">qwen2.5:3b</option><option value="deepseek-chat">deepseek-chat</option></select>
        <textarea id="req-prompt" rows="2" placeholder="输入请求内容...">你好，请介绍一下你自己</textarea>
        <div class="btn-row">
          <button class="btn btn-primary" onclick="sendRequest()">🚀 发送请求</button>
          <button class="btn btn-green" onclick="sendBatch()">📦 批量测试</button>
          <button class="btn btn-purple" onclick="analyzeComplexity()">🧠 分析复杂度</button>
          <button class="btn btn-ghost" onclick="clearResults()">🗑 清空</button>
        </div>
      </div>
      <div id="results-area" style="flex:1;overflow-y:auto;padding:14px">
        <div class="empty-state" id="empty-hint"><div class="es-icon">🐾</div>发送请求以查看全链路追踪</div>
      </div>
    </div>
    <div class="pl-right">
      <div class="panel-title"><span class="pt-icon">📋</span> 实时日志</div>
      <div id="pipeline-log" style="font-family:'SF Mono',Monaco,monospace;font-size:11px;line-height:1.6;max-height:calc(100vh - 160px);overflow-y:auto"></div>
    </div>
  </div>
</div>
<div class="tab-content" id="tab-services"><div class="svc-layout" id="svc-layout"></div></div>
<div class="tab-content" id="tab-metrics">
  <div class="metrics-layout">
    <div class="metrics-card"><div class="metrics-card-title">🖥️ 系统资源</div><div class="gauge-grid"><div class="mini-gauge"><div class="mg-val" id="m-cpu">-</div><div class="mg-lbl">CPU %</div><div class="mg-bar"><div class="mg-bar-fill" id="m-cpu-bar" style="width:0;background:var(--green)"></div></div></div><div class="mini-gauge"><div class="mg-val" id="m-mem">-</div><div class="mg-lbl">内存 %</div><div class="mg-bar"><div class="mg-bar-fill" id="m-mem-bar" style="width:0;background:var(--blue)"></div></div></div><div class="mini-gauge"><div class="mg-val" id="m-gpu">-</div><div class="mg-lbl">GPU %</div><div class="mg-bar"><div class="mg-bar-fill" id="m-gpu-bar" style="width:0;background:var(--purple)"></div></div></div><div class="mini-gauge"><div class="mg-val" id="m-gpumem">-</div><div class="mg-lbl">GPU显存 %</div><div class="mg-bar"><div class="mg-bar-fill" id="m-gpumem-bar" style="width:0;background:var(--orange)"></div></div></div></div><div style="margin-top:12px;font-size:11px;color:var(--text3)"><div style="display:flex;justify-content:space-between;margin-bottom:4px"><span>内存总量</span><span id="m-mem-total">-</span></div><div style="display:flex;justify-content:space-between;margin-bottom:4px"><span>内存已用</span><span id="m-mem-used">-</span></div><div style="display:flex;justify-content:space-between"><span>GPU显存已用</span><span id="m-gpumem-used">-</span></div></div></div>
    <div class="metrics-card"><div class="metrics-card-title">📊 请求统计</div><div class="gauge-grid"><div class="mini-gauge"><div class="mg-val" id="m-total">0</div><div class="mg-lbl">总请求</div></div><div class="mini-gauge"><div class="mg-val" id="m-success">0</div><div class="mg-lbl">成功</div></div><div class="mini-gauge"><div class="mg-val" id="m-failed">0</div><div class="mg-lbl">失败</div></div><div class="mini-gauge"><div class="mg-val" id="m-srate">-</div><div class="mg-lbl">成功率</div></div></div><div style="margin-top:12px"><div style="font-size:12px;color:var(--text3);margin-bottom:6px">路由分布</div><div id="route-dist"></div></div></div>
    <div class="metrics-card"><div class="metrics-card-title">⏱️ 延迟趋势 (ms)</div><div class="chart-container"><canvas id="chart-latency"></canvas></div></div>
    <div class="metrics-card"><div class="metrics-card-title">✅ 成功率趋势 (%)</div><div class="chart-container"><canvas id="chart-success"></canvas></div></div>
  </div>
</div>
<div class="tab-content" id="tab-logs">
  <div class="logs-layout">
    <div class="logs-toolbar">
      <select id="log-filter-svc"><option value="">全部服务</option><option value="hermes">Hermes</option><option value="bridge">Bridge</option><option value="gateway">Gateway</option><option value="official">OfficialGW</option><option value="ollama">Ollama</option></select>
      <select id="log-filter-level"><option value="">全部级别</option><option value="info">INFO</option><option value="ok">OK</option><option value="warn">WARN</option><option value="err">ERROR</option></select>
      <input type="text" id="log-search" placeholder="搜索日志..." style="flex:1">
      <button class="btn btn-ghost btn-sm" onclick="clearLogs()">清空</button>
      <button class="btn btn-ghost btn-sm" onclick="exportLogs()">导出</button>
      <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:var(--text3)"><input type="checkbox" id="log-autoscroll" checked>自动滚动</label>
    </div>
    <div class="logs-body" id="logs-body"></div>
  </div>
</div>
"""

js = """<script>
const H='http://localhost:8082',B='http://localhost:3001',GW='http://localhost:3000',OGW='http://127.0.0.1:3005',OLL='http://localhost:11434';
let dc=0;const AR=[],RC={},LH=[],SH=[],LOGS=[];const MAX_LOGS=2000;
let svcStatus={hermes:false,bridge:false,gateway:false,official:false,ollama:false};

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function ts(){return new Date().toLocaleTimeString('zh-CN',{hour12:false})}
function log(level,msg,svc){const e={time:ts(),level,svc:svc||'system',msg};LOGS.push(e);if(LOGS.length>MAX_LOGS)LOGS.shift();renderPipelineLog(e);renderLogEntry(e)}

function renderPipelineLog(e){
  const c=document.getElementById('pipeline-log');if(!c)return;
  const d=document.createElement('div');d.className='log-line';
  d.innerHTML='<span class="ll-time">'+e.time+'</span><span class="ll-level '+e.level+'">'+e.level.toUpperCase()+'</span><span class="ll-msg">['+e.svc+'] '+esc(e.msg)+'</span>';
  c.appendChild(d);if(c.children.length>500)c.removeChild(c.firstChild);c.scrollTop=c.scrollHeight;
}

function renderLogEntry(e){
  const c=document.getElementById('logs-body');if(!c)return;
  const fS=document.getElementById('log-filter-svc').value,fL=document.getElementById('log-filter-level').value,fR=document.getElementById('log-search').value.toLowerCase();
  if(fS&&e.svc!==fS)return;if(fL&&e.level!==fL)return;if(fR&&!e.msg.toLowerCase().includes(fR))return;
  const d=document.createElement('div');d.className='log-entry';
  d.innerHTML='<span class="le-time">'+e.time+'</span><span class="le-svc '+e.svc+'">'+e.svc+'</span><span class="le-level '+e.level+'">'+e.level.toUpperCase()+'</span><span class="le-msg">'+esc(e.msg)+'</span>';
  c.appendChild(d);if(c.children.length>2000)c.removeChild(c.firstChild);
  if(document.getElementById('log-autoscroll').checked)c.scrollTop=c.scrollHeight;
}

function clearLogs(){document.getElementById('logs-body').innerHTML='';LOGS.length=0;log('info','日志已清空')}
function exportLogs(){
  const t=LOGS.map(e=>'['+e.time+']['+e.svc+']['+e.level.toUpperCase()+'] '+e.msg).join('\\n');
  const b=new Blob([t],{type:'text/plain'});const a=document.createElement('a');
  a.href=URL.createObjectURL(b);a.download='openclaw-logs-'+Date.now()+'.txt';a.click();
}
function toggleDetail(id){const d=document.getElementById(id+'-detail');if(d)d.classList.toggle('show')}
function switchTab(name,btn){
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');btn.classList.add('active');
  if(name==='services')refreshServices();if(name==='metrics')refreshMetrics();
}

function extractStrat(trace){
  if(!trace||!trace.length)return{name:'unknown'};
  for(const t of trace){
    if(t.agent&&t.agent.includes('router')){const m=t.message&&t.message.match(/strategy[=:]\\s*(\\w+)/i);if(m)return{name:m[1]}}
    if(t.agent&&t.agent.includes('local'))return{name:'local_first'};
    if(t.agent&&t.agent.includes('cloud'))return{name:'cloud_first'};
    if(t.agent&&t.agent.includes('agent'))return{name:'agent_chain'};
  }
  return{name:trace[0]?trace[0].agent:'unknown'};
}
function getStratCls(n){if(n.includes('local'))return'local';if(n.includes('cloud'))return'cloud';if(n.includes('agent'))return'agent';if(n.includes('latency'))return'latency';return''}

function renderCard(data){
  const strat=extractStrat(data.agent_trace),sc=getStratCls(strat.name);
  const isOk=data.status==='success',result=data.result||{};
  const mt=result.model_type||'unknown',mTag=mt==='local'?'tag-local':'tag-cloud';
  let h='<div class="dispatch-card"><div class="dc-header" onclick="toggleCard(\\''+data.request_id+'\\')"><div class="dc-left">';
  h+='<span class="dc-id">#'+(data.request_id||'?').slice(0,8)+'</span>';
  h+='<span class="dc-strategy '+sc+'">'+strat.name.replace('adaptive_','')+'</span>';
  h+='<span class="tag '+mTag+'">'+mt+'</span>';
  if(data.is_hermes&&data.hermes_routing){
    const hr=data.hermes_routing;
    const pi=hr.route_path==='agent_chain'?'\\u{1F916}':hr.route_path==='gateway'?'\\u{1F309}':hr.route_path==='direct_local'?'\\u{1F3E0}':'\\u2601';
    h+='<span class="tag" style="background:#0c2d48;color:var(--cyan)">\\u{1F9E0} '+(hr.complexity_score||'?')+'/'+(hr.threshold||40)+' \\u2192 '+pi+' '+hr.route_path+'</span>';
  }
  if(result.routed_via_gateway)h+='<span class="tag" style="background:#312e81;color:var(--purple)">\\u{1F309} GW</span>';
  if(data.is_agent_chain)h+='<span class="tag" style="background:#4c1d95;color:var(--purple)">\\u{1F916} Agent</span>';
  h+='<span class="dc-model">'+(result.model_name||'-')+'</span></div><div class="dc-right">';
  if(data.total_latency_ms)h+='<span class="dc-latency">'+data.total_latency_ms+'ms</span>';
  h+='<span class="dc-status '+(isOk?'success':'failed')+'">'+(isOk?'\\u2713 成功':'\\u2717 失败')+'</span></div></div>';
  h+='<div class="dc-body" id="dc-body-'+data.request_id+'">';
  h+='<div class="dc-input"><div class="di-label">\\u{1F4E5} 输入</div><div class="di-text"><span class="tag '+mTag+'">'+data.appid+'</span> <span class="tag" style="background:var(--border2);color:var(--text2)">'+(data.type||'chat')+'</span> <span class="tag" style="background:var(--border2);color:var(--text2)">P'+(data.priority||3)+'</span><br>'+esc(data.prompt||'')+'</div></div>';
  if(isOk&&result.output)h+='<div class="dc-output"><div class="do-label">\\u{1F4E4} 输出 <span style="color:var(--text3);font-weight:400">('+(result.usage&&result.usage.total_tokens||'?')+' tokens, '+(result.latency_ms||'?')+'ms)</span></div><div class="do-text">'+esc(result.output)+'</div></div>';
  if(data.is_hermes&&data.hermes_routing){
    const hr=data.hermes_routing;
    const cc=(hr.complexity_score||0)>=(hr.threshold||40)?'var(--red)':'var(--green)';
    const pn={gateway:'\\u{1F309} Gateway',agent_chain:'\\u{1F916} Agent链路',direct_local:'\\u{1F3E0} 本地直连',local_inference:'\\u{1F512} 本地推理'};
    h+='<div style="margin-top:8px"><div class="di-label">\\u{1F9E0} Hermes 路由决策</div><div style="background:var(--bg);border-radius:6px;padding:8px;font-size:11px;border:1px solid #0e7490"><div style="margin-bottom:4px"><b>复杂度:</b> <span style="color:'+cc+';font-weight:700">'+(hr.complexity_score||'?')+'</span> / '+(hr.threshold||40)+' \\u2192 <b>'+(pn[hr.route_path]||hr.route_path)+'</b></div><div style="margin-bottom:4px"><b>模型:</b> '+(hr.selected_model||'-')+'</div><div><b>原因:</b> '+esc(hr.reason||'')+'</div></div></div>';
  }
  if(data.agent_trace&&data.agent_trace.length>0){
    h+='<div style="margin-top:8px"><div class="di-label">\\u{1F517} 调度链路</div><div class="trace-timeline">';
    data.agent_trace.forEach(t=>{
      let c='';if(t.agent.includes('router'))c='router';else if(t.agent.includes('agent')||t.agent.includes('local')||t.agent.includes('openclaw'))c='agent';else if(t.agent.includes('fallback'))c='fallback';else if(t.message&&t.message.includes('完成'))c='result-ok';else if(t.message&&t.message.includes('失败'))c='result-err';
      h+='<div class="trace-step '+c+'"><span class="ts-agent">'+t.agent+'</span><span class="ts-msg">'+esc((t.message||'').slice(0,120))+'</span></div>';
    });
    h+='</div></div>';
  }
  h+='</div></div>';return h;
}
function toggleCard(id){const b=document.getElementById('dc-body-'+id);if(b)b.classList.toggle('collapsed')}

async function sendQueueRequest(body,label){
  dc++;const rid='q-'+dc;
  log('info','['+rid+'] \\u2192 Queue: type='+body.type+', prompt="'+body.prompt.slice(0,50)+'..."','hermes');
  const eh=document.getElementById('empty-hint');if(eh)eh.remove();
  const area=document.getElementById('results-area');
  const ph=document.createElement('div');ph.className='dispatch-card';ph.id=rid;
  ph.innerHTML='<div class="dc-header"><div class="dc-left"><span class="dc-id">#'+rid+'</span><span class="dc-strategy agent">\\u{1F4E8} '+label+'</span><span class="dc-model" style="color:var(--cyan)"><span class="spinner"></span> 队列调度中...</span></div><div class="dc-right"><span class="dc-status running">\\u23F3 运行中</span></div></div>';
  area.insertBefore(ph,area.firstChild);
  const st=Date.now();
  try{
    const res=await fetch(H+'/queue/submit-sync?timeout=120',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await res.json();const elapsed=Date.now()-st;
    const isOk=data.status==='success',result=data.result||{},routing=data.routing||{};
    log(isOk?'ok':'err','['+rid+'] '+(isOk?'\\u2713':'\\u2717')+' Queue: path='+(routing.route_path||'-')+', model='+(routing.selected_model||'-')+', latency='+(data.total_latency_ms||elapsed)+'ms','hermes');
    if(routing.route_path)RC[routing.route_path]=(RC[routing.route_path]||0)+1;
    LH.push({value:elapsed,label:'#'+dc});SH.push({rate:isOk?100:0});
    const cardData={
      request_id:data.request_id||rid,appid:body.appid||'q-test',status:isOk?'success':'failed',
      result:{model_name:result.model_name||'-',model_type:result.model_type||'unknown',output:result.output||'',latency_ms:result.latency_ms||elapsed,cost:result.cost,usage:result.usage,routed_via_gateway:result.routed_via_gateway||false,actual_model:result.actual_model||null,finish_reason:result.finish_reason||'stop'},
      agent_trace:data.agent_trace||[],prompt:body.prompt,type:body.type,priority:body.priority,
      total_latency_ms:elapsed,strategy_name:routing.route_path||'-',is_bridge:true,
      is_agent_chain:routing.route_path==='agent_chain',is_hermes:true,
      hermes_routing:{route_path:routing.route_path,complexity_score:routing.complexity_score,threshold:40,selected_model:routing.selected_model,reason:routing.reason}
    };
    AR.push(cardData);ph.outerHTML=renderCard(cardData);
  }catch(e){
    log('err','['+rid+'] \\u2717 Queue失败: '+e.message,'hermes');
    ph.innerHTML='<div class="dc-header"><div class="dc-left"><span class="dc-id">#'+rid+'</span><span class="dc-model" style="color:var(--red)">失败: '+e.message+'</span></div><div class="dc-right"><span class="dc-status failed">\\u2717 失败</span></div></div>';
  }
  refreshPipeline();updateCharts();
}

async function sendBridgeRequest(body,label){
  dc++;const rid='br-'+dc;
  log('info','['+rid+'] \\u2192 Bridge: type='+body.type+', prompt="'+body.prompt.slice(0,50)+'..."','bridge');
  const eh=document.getElementById('empty-hint');if(eh)eh.remove();
  const area=document.getElementById('results-area');
  const ph=document.createElement('div');ph.className='dispatch-card';ph.id=rid;
  ph.innerHTML='<div class="dc-header"><div class="dc-left"><span class="dc-id">#'+rid+'</span><span class="dc-strategy cloud">\\u{1F309} '+label+'</span><span class="dc-model" style="color:var(--purple)"><span class="spinner"></span> Bridge调度中...</span></div><div class="dc-right"><span class="dc-status running">\\u23F3 运行中</span></div></div>';
  area.insertBefore(ph,area.firstChild);
  const st=Date.now();
  try{
    const res=await fetch(B+'/dispatch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await res.json();const elapsed=Date.now()-st;
    const isOk=data.status==='success',result=data.result||{},routing=data.routing||{};
    log(isOk?'ok':'err','['+rid+'] '+(isOk?'\\u2713':'\\u2717')+' Bridge: path='+(routing.route_path||'-')+', model='+(routing.selected_model||'-')+', latency='+(data.total_latency_ms||elapsed)+'ms','bridge');
    if(routing.route_path)RC[routing.route_path]=(RC[routing.route_path]||0)+1;
    LH.push({value:elapsed,label:'#'+dc});SH.push({rate:isOk?100:0});
    const cardData={
      request_id:data.request_id||rid,appid:body.appid||'br-test',status:isOk?'success':'failed',
      result:{model_name:result.model_name||'-',model_type:result.model_type||'unknown',output:result.output||'',latency_ms:result.latency_ms||elapsed,cost:result.cost,usage:result.usage,routed_via_gateway:result.routed_via_gateway||false,actual_model:result.actual_model||null,finish_reason:result.finish_reason||'stop'},
      agent_trace:data.agent_trace||[],prompt:body.prompt,type:body.type,priority:body.priority,
      total_latency_ms:elapsed,strategy_name:routing.route_path||'-',is_bridge:true,
      is_agent_chain:routing.route_path==='agent_chain',is_hermes:!!data.hermes_routing,
      hermes_routing:data.hermes_routing?{route_path:data.hermes_routing.route_path,complexity_score:data.hermes_routing.complexity_score,threshold:40,selected_model:data.hermes_routing.selected_model,reason:data.hermes_routing.reason}:null
    };
    AR.push(cardData);ph.outerHTML=renderCard(cardData);
  }catch(e){
    log('err','['+rid+'] \\u2717 Bridge失败: '+e.message,'bridge');
    ph.innerHTML='<div class="dc-header"><div class="dc-left"><span class="dc-id">#'+rid+'</span><span class="dc-model" style="color:var(--red)">失败: '+e.message+'</span></div><div class="dc-right"><span class="dc-status failed">\\u2717 失败</span></div></div>';
  }
  refreshPipeline();updateCharts();
}

function sendRequest(){
  const body={appid:document.getElementById('req-appid').value,type:document.getElementById('req-type').value,priority:parseInt(document.getElementById('req-priority').value),prompt:document.getElementById('req-prompt').value,model:document.getElementById('req-model').value||undefined};
  const mode=document.getElementById('req-mode').value;
  if(mode==='queue')sendQueueRequest(body,'Queue调度');else sendBridgeRequest(body,'Bridge调度');
}

function sendBatch(){
  const prompts=['你好','1+1等于几？','写一首短诗','什么是AI？','翻译：hello world'];
  prompts.forEach((p,i)=>{setTimeout(()=>{sendQueueRequest({appid:'batch-test',type:'chat',priority:3,prompt:p},'批量#'+(i+1))},i*1500)});
}

async function analyzeComplexity(){
  const prompt=document.getElementById('req-prompt').value;
  if(!prompt){log('warn','请输入请求内容','system');return}
  log('info','分析复杂度...','hermes');
  try{
    const res=await fetch(H+'/route/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({appid:'test',type:'chat',priority:3,prompt})});
    const data=await res.json();
    log('ok','复杂度: '+data.complexity_score+'/'+data.threshold+', 推荐: '+data.recommended_path+', 级别: '+data.complexity_level,'hermes');
    if(data.agent_routed)log('info','Agent决策: '+data.agent_decision+', 置信度: '+data.agent_confidence+', 技能: '+(data.skill_matched||'无'),'hermes');
  }catch(e){log('err','复杂度分析失败: '+e.message,'hermes')}
}

function clearResults(){
  document.getElementById('results-area').innerHTML='<div class="empty-state" id="empty-hint"><div class="es-icon">\\u{1F43E}</div>发送请求以查看全链路追踪</div>';
  AR.length=0;
}

async function refreshPipeline(){
  try{const r=await fetch(H+'/queue/status');const d=await r.json();const q=d.queue_backend||{},w=d.worker||{};setBadge('pf-q','ok');setMetric('pf-q',q.size||0);document.getElementById('pf-q-backend').textContent=q.backend||'-';document.getElementById('pf-q-worker').textContent=w.running?'运行中':'停止';document.getElementById('pf-q-req').textContent=q.requests_size||q.size||0;document.getElementById('pf-q-res').textContent=q.results_size||0;document.getElementById('pf-q-proc').textContent=w.total_processed||0}catch{setBadge('pf-q','err');setMetric('pf-q','-')}
  try{const r=await fetch(H+'/health');const d=await r.json();setBadge('pf-hm','ok');setMetric('pf-hm',d.skills_count||0);document.getElementById('pf-hm-mode').textContent=d.hermes_agent_enabled?'Agent模式':'规则模式';document.getElementById('pf-hm-threshold').textContent=d.complexity_threshold||'-';document.getElementById('pf-hm-exploration').textContent=d.exploration_rate?d.exploration_rate.toFixed(3):'-';document.getElementById('pf-hm-memory').textContent=d.memory_records||0;document.getElementById('pf-hm-skills').textContent=d.skills_count||0}catch{setBadge('pf-hm','err');setMetric('pf-hm','-')}
  try{const r=await fetch(B+'/health');const d=await r.json();setBadge('pf-br','ok');setMetric('pf-br',d.stats&&d.stats.total||0);document.getElementById('pf-br-strategy').textContent=d.route_mode||'-';document.getElementById('pf-br-local').textContent=d.local_models||'-';document.getElementById('pf-br-cloud').textContent=d.cloud_models||'-';document.getElementById('pf-br-gwr').textContent=d.use_openclaw_gateway?'启用':'禁用'}catch{setBadge('pf-br','err');setMetric('pf-br','-')}
  let gwOk=false,ogwOk=false,ollOk=false;
  try{const r=await fetch(GW+'/health');if(r.ok)gwOk=true}catch{}
  try{const r=await fetch(OGW+'/health');if(r.ok)ogwOk=true}catch{}
  try{const r=await fetch(OLL+'/api/tags');if(r.ok)ollOk=true}catch{}
  setBadge('pf-ex',gwOk||ogwOk||ollOk?'ok':'err');
  setMetric('pf-ex',[gwOk?'GW':'',ogwOk?'OGW':'',ollOk?'Oll':''].filter(Boolean).join('/')||'-');
  document.getElementById('pf-ex-gw').textContent=gwOk?'\\u2713 运行':'\\u2717 停止';
  document.getElementById('pf-ex-ogw').textContent=ogwOk?'\\u2713 运行':'\\u2717 停止';
  document.getElementById('pf-ex-ollama').textContent=ollOk?'\\u2713 运行':'\\u2717 停止';
  try{const r=await fetch(H+'/stats');const d=await r.json();const req=d.requests||{};setBadge('pf-rt',req.total>0?'ok':'idle');setMetric('pf-rt',req.success_rate?Math.round(req.success_rate*100)+'%':'-');document.getElementById('pf-rt-srate').textContent=req.success_rate?Math.round(req.success_rate*10000)/100+'%':'-'}catch{setBadge('pf-rt','err');setMetric('pf-rt','-')}
  try{const r=await fetch(H+'/queue/feedback?limit=1');const d=await r.json();document.getElementById('pf-rt-fb').textContent=d.count||0}catch{}
  try{const r=await fetch(H+'/queue/results?limit=1&pop=false');const d=await r.json();document.getElementById('pf-rt-resq').textContent=d.remaining||0}catch{}
}

function setBadge(prefix,state){const b=document.getElementById(prefix+'-badge');if(b){b.className='pf-badge '+state;b.textContent={ok:'运行',err:'异常',idle:'待机',running:'运行中'}[state]||state}}
function setMetric(prefix,val){const m=document.getElementById(prefix+'-metric');if(m)m.textContent=val}

const SVC_DEFS=[
  {id:'hermes',name:'Hermes 路由器',icon:'\\u{1F9E0}',url:H+'/health',port:8082,startSvc:null,stopSvc:null},
  {id:'bridge',name:'Bridge 调度器',icon:'\\u{1F309}',url:B+'/health',port:3001,startSvc:null,stopSvc:null},
  {id:'gateway',name:'OpenClaw Gateway',icon:'\\u26A1',url:GW+'/health',port:3000,startSvc:'gateway',stopSvc:'gateway'},
  {id:'official',name:'Official Gateway',icon:'\\u{1F3E2}',url:OGW+'/health',port:3005,startSvc:'officialGateway',stopSvc:'officialGateway'},
  {id:'ollama',name:'Ollama 本地推理',icon:'\\u{1F999}',url:OLL+'/api/tags',port:11434,startSvc:null,stopSvc:null},
];

async function refreshServices(){
  const layout=document.getElementById('svc-layout');let html='';
  for(const svc of SVC_DEFS){
    let healthy=false,info={};
    try{const r=await fetch(svc.url,{signal:AbortSignal.timeout(3000)});info=await r.json();healthy=r.ok}catch{}
    svcStatus[svc.id]=healthy;
    const dotCls=healthy?'ok':'err';const statusText=healthy?'\\u2713 运行中':'\\u2717 已停止';
    const canStart=!!svc.startSvc;const canStop=!!svc.stopSvc;
    html+='<div class="svc-card"><div class="svc-card-title"><span class="svc-dot '+dotCls+'"></span>'+svc.icon+' '+svc.name+'</div>';
    html+='<div class="svc-row"><span class="svc-key">状态</span><span class="svc-val" style="color:'+(healthy?'var(--green)':'var(--red)')+'">'+statusText+'</span></div>';
    html+='<div class="svc-row"><span class="svc-key">端口</span><span class="svc-val">'+svc.port+'</span></div>';
    html+='<div class="svc-row"><span class="svc-key">URL</span><span class="svc-val" style="font-size:11px">'+svc.url+'</span></div>';
    if(svc.id==='hermes'&&healthy){
      html+='<div class="svc-row"><span class="svc-key">路由模式</span><span class="svc-val">'+(info.hermes_agent_enabled?'Agent':'规则')+'</span></div>';
      html+='<div class="svc-row"><span class="svc-key">复杂度阈值</span><span class="svc-val">'+(info.complexity_threshold||'-')+'</span></div>';
      html+='<div class="svc-row"><span class="svc-key">记忆记录</span><span class="svc-val">'+(info.memory_records||0)+'</span></div>';
      html+='<div class="svc-row"><span class="svc-key">技能数</span><span class="svc-val">'+(info.skills_count||0)+'</span></div>';
    }
    if(svc.id==='bridge'&&healthy){
      html+='<div class="svc-row"><span class="svc-key">路由模式</span><span class="svc-val">'+(info.route_mode||'-')+'</span></div>';
      html+='<div class="svc-row"><span class="svc-key">总请求</span><span class="svc-val">'+(info.stats&&info.stats.total||0)+'</span></div>';
    }
    if(svc.id==='gateway'&&healthy){
      html+='<div class="svc-row"><span class="svc-key">模型数</span><span class="svc-val">'+(info.models_registered||0)+'</span></div>';
      html+='<div class="svc-row"><span class="svc-key">Agent数</span><span class="svc-val">'+(info.agents_registered||0)+'</span></div>';
      html+='<div class="svc-row"><span class="svc-key">总请求</span><span class="svc-val">'+(info.stats&&info.stats.total||0)+'</span></div>';
    }
    if(svc.id==='ollama'&&healthy){
      const models=info.models||[];
      html+='<div class="svc-row"><span class="svc-key">模型数</span><span class="svc-val">'+models.length+'</span></div>';
      if(models.length>0)html+='<div class="svc-row"><span class="svc-key">模型列表</span><span class="svc-val" style="font-size:10px">'+models.slice(0,3).map(m=>m.name).join(', ')+(models.length>3?'...':'')+'</span></div>';
    }
    html+='<div class="svc-actions">';
    if(canStart)html+='<button class="svc-btn svc-btn-start" onclick="startService(\\''+svc.startSvc+'\\')" '+(healthy?'disabled':'')+' >\\u25B6 启动</button>';
    if(canStop)html+='<button class="svc-btn svc-btn-stop" onclick="stopService(\\''+svc.stopSvc+'\\')" '+(!healthy?'disabled':'')+'>\\u25A0 停止</button>';
    html+='<button class="svc-btn svc-btn-restart" onclick="restartService(\\''+svc.id+'\\')\\u21BB 刷新</button>';
    html+='</div></div>';
  }
  layout.innerHTML=html;updateTopbarDots();
}

async function startService(name){if(!name)return;log('info','启动服务: '+name,'system');try{const r=await fetch(B+'/services/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({service:name})});const d=await r.json();log(d.started?'ok':'warn',d.message||JSON.stringify(d),'system');setTimeout(refreshServices,2000)}catch(e){log('err','启动失败: '+e.message,'system')}}
async function stopService(name){if(!name)return;log('info','停止服务: '+name,'system');try{const r=await fetch(B+'/services/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({service:name})});const d=await r.json();log(d.stopped?'ok':'warn',d.message||JSON.stringify(d),'system');setTimeout(refreshServices,2000)}catch(e){log('err','停止失败: '+e.message,'system')}}
function restartService(id){const svc=SVC_DEFS.find(s=>s.id===id);if(svc&&svc.stopSvc){stopService(svc.stopSvc).then(()=>setTimeout(()=>startService(svc.startSvc),1500))}else{refreshServices()}}

async function refreshMetrics(){
  try{const r=await fetch(H+'/stats');const d=await r.json();const sys=d.system||{},req=d.requests||{};document.getElementById('m-cpu').textContent=sys.cpu_percent?sys.cpu_percent.toFixed(1):'-';document.getElementById('m-mem').textContent=sys.memory_percent?sys.memory_percent.toFixed(1):'-';document.getElementById('m-mem-total').textContent=sys.memory_total_gb?sys.memory_total_gb+'GB':'-';document.getElementById('m-mem-used').textContent=sys.memory_used_gb?sys.memory_used_gb+'GB':'-';const cpuPct=sys.cpu_percent||0;const cpuBar=document.getElementById('m-cpu-bar');cpuBar.style.width=cpuPct+'%';cpuBar.style.background=cpuPct>80?'var(--red)':cpuPct>50?'var(--yellow)':'var(--green)';const memPct=sys.memory_percent||0;const memBar=document.getElementById('m-mem-bar');memBar.style.width=memPct+'%';memBar.style.background=memPct>80?'var(--red)':memPct>50?'var(--yellow)':'var(--blue)';document.getElementById('m-total').textContent=req.total||0;document.getElementById('m-success').textContent=req.success||0;document.getElementById('m-failed').textContent=req.failed||0;document.getElementById('m-srate').textContent=req.success_rate?Math.round(req.success_rate*100)+'%':'-'}catch{}
  try{const r=await fetch(OLL+'/api/ps');const d=await r.json();const models=d.models||[];document.getElementById('m-gpu').textContent=models.length>0?'活跃':'-';document.getElementById('m-gpumem').textContent=models.length>0?'占用':'-';document.getElementById('m-gpu-bar').style.width=models.length>0?'60%':'0%';document.getElementById('m-gpumem-bar').style.width=models.length>0?'40%':'0%';if(models.length>0){const vram=models.reduce((s,m)=>s+(m.size||0),0);document.getElementById('m-gpumem-used').textContent=(vram/1e9).toFixed(1)+'GB'}else{document.getElementById('m-gpumem-used').textContent='-'}}catch{document.getElementById('m-gpu').textContent='-';document.getElementById('m-gpumem').textContent='-';document.getElementById('m-gpumem-used').textContent='-'}
  renderRouteDist();updateCharts();
}

function renderRouteDist(){
  const total=Object.values(RC).reduce((s,v)=>s+v,0);const el=document.getElementById('route-dist');
  if(!el||total===0)return;
  const colors={gateway:'var(--blue)',agent_chain:'var(--purple)',direct_local:'var(--green)',local_inference:'var(--cyan)'};
  const labels={gateway:'\\u{1F309} Gateway',agent_chain:'\\u{1F916} Agent链路',direct_local:'\\u{1F3E0} 本地直连',local_inference:'\\u{1F512} 本地推理'};
  let html='';
  for(const[k,v]of Object.entries(RC)){const pct=Math.round(v/total*100);html+='<div class="route-bar"><span class="rb-label">'+(labels[k]||k)+'</span><div class="rb-track"><div class="rb-fill" style="width:'+pct+'%;background:'+(colors[k]||'var(--border2)')+'">'+pct+'%</div></div><span style="font-size:11px;color:var(--text3)">'+v+'</span></div>'}
  el.innerHTML=html;
}

function drawChart(cid,pts,color,unit){
  const canvas=document.getElementById(cid);if(!canvas)return;
  const ctx=canvas.getContext('2d');const w=canvas.width=canvas.parentElement.clientWidth-24;const h=canvas.height=180;
  ctx.clearRect(0,0,w,h);
  if(pts.length<2){ctx.fillStyle='#64748b';ctx.font='12px sans-serif';ctx.fillText('等待数据...',w/2-30,h/2);return}
  const data=pts.slice(-30);const maxV=Math.max(...data.map(d=>d.value||d.rate||0),1);
  const pad={top:20,right:20,bottom:30,left:50};const cW=w-pad.left-pad.right,cH=h-pad.top-pad.bottom;
  ctx.strokeStyle='#1e293b';ctx.lineWidth=1;
  for(let i=0;i<=4;i++){const y=pad.top+(cH/4)*i;ctx.beginPath();ctx.moveTo(pad.left,y);ctx.lineTo(w-pad.right,y);ctx.stroke();ctx.fillStyle='#64748b';ctx.font='10px sans-serif';ctx.textAlign='right';const val=maxV-(maxV/4)*i;ctx.fillText(unit==='%'?val.toFixed(0)+'%':Math.round(val),pad.left-8,y+4)}
  const vals=data.map(d=>d.value||d.rate||0);
  ctx.beginPath();ctx.strokeStyle=color;ctx.lineWidth=2;
  vals.forEach((v,i)=>{const x=pad.left+(cW/(vals.length-1))*i;const y=pad.top+cH-(v/maxV)*cH;i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)});
  ctx.stroke();
  ctx.fillStyle=color+'30';ctx.beginPath();ctx.moveTo(pad.left,pad.top+cH);
  vals.forEach((v,i)=>{const x=pad.left+(cW/(vals.length-1))*i;const y=pad.top+cH-(v/maxV)*cH;ctx.lineTo(x,y)});
  ctx.lineTo(pad.left+(cW/(vals.length-1))*(vals.length-1),pad.top+cH);ctx.closePath();ctx.fill();
  vals.forEach((v,i)=>{const x=pad.left+(cW/(vals.length-1))*i;const y=pad.top+cH-(v/maxV)*cH;ctx.beginPath();ctx.arc(x,y,3,0,Math.PI*2);ctx.fillStyle=color;ctx.fill()});
}

function updateCharts(){drawChart('chart-latency',LH,'#38bdf8','ms');drawChart('chart-success',SH,'#4ade80','%')}

function updateTopbarDots(){for(const[id,ok]of Object.entries(svcStatus)){const d=document.getElementById('dot-'+id);if(d)d.className='dot '+(ok?'ok':'err')}}
function updateClock(){document.getElementById('clock').textContent=ts()}

document.getElementById('log-filter-svc').addEventListener('change',refilterLogs);
document.getElementById('log-filter-level').addEventListener('change',refilterLogs);
document.getElementById('log-search').addEventListener('input',refilterLogs);

function refilterLogs(){
  const c=document.getElementById('logs-body');c.innerHTML='';
  const fS=document.getElementById('log-filter-svc').value,fL=document.getElementById('log-filter-level').value,fR=document.getElementById('log-search').value.toLowerCase();
  LOGS.forEach(e=>{
    if(fS&&e.svc!==fS)return;if(fL&&e.level!==fL)return;if(fR&&!e.msg.toLowerCase().includes(fR))return;
    const d=document.createElement('div');d.className='log-entry';
    d.innerHTML='<span class="le-time">'+e.time+'</span><span class="le-svc '+e.svc+'">'+e.svc+'</span><span class="le-level '+e.level+'">'+e.level.toUpperCase()+'</span><span class="le-msg">'+esc(e.msg)+'</span>';
    c.appendChild(d);
  });
  if(document.getElementById('log-autoscroll').checked)c.scrollTop=c.scrollHeight;
}

async function init(){
  updateClock();setInterval(updateClock,1000);
  log('info','OpenClaw 调度监控中心已启动','system');
  async function checkSvc(id,url){try{const r=await fetch(url,{signal:AbortSignal.timeout(3000)});svcStatus[id]=r.ok}catch{svcStatus[id]=false}}
  await Promise.all([checkSvc('hermes',H+'/health'),checkSvc('bridge',B+'/health'),checkSvc('gateway',GW+'/health'),checkSvc('official',OGW+'/health'),checkSvc('ollama',OLL+'/api/tags')]);
  updateTopbarDots();refreshPipeline();
  setInterval(async()=>{await Promise.all([checkSvc('hermes',H+'/health'),checkSvc('bridge',B+'/health'),checkSvc('gateway',GW+'/health'),checkSvc('official',OGW+'/health'),checkSvc('ollama',OLL+'/api/tags')]);updateTopbarDots()},10000);
  setInterval(refreshPipeline,5000);
}
init();
</script>
</body>
</html>"""

with open(DASHBOARD, "w", encoding="utf-8") as f:
    f.write(html + js)

print(f"Dashboard written: {os.path.getsize(DASHBOARD)} bytes")
