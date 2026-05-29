import { createServer } from "http";
import { readFileSync, existsSync, statSync, readdirSync } from "fs";
import { join, dirname, extname } from "path";
import { fileURLToPath } from "url";
import { spawn } from "child_process";
import net from "net";

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = join(__dirname, "..");

const OPENCLAW_GATEWAY_URL =
  process.env.OPENCLAW_GATEWAY_URL || "http://localhost:3000";
const OPENCLAW_OFFICIAL_GATEWAY_URL =
  process.env.OPENCLAW_OFFICIAL_GATEWAY_URL || "http://127.0.0.1:3005";
const BRIDGE_PORT = parseInt(process.env.BRIDGE_PORT || "3001", 10);
const USE_OPENCLAW_GATEWAY =
  process.env.USE_OPENCLAW_GATEWAY !== "false";
let DEFAULT_ROUTE_MODE =
  process.env.DEFAULT_ROUTE_MODE || "smart";
const AGENT_EXECUTION_TIMEOUT =
  parseInt(process.env.AGENT_EXECUTION_TIMEOUT || "120000", 10);
let SMART_ROUTER_THRESHOLD =
  parseInt(process.env.SMART_ROUTER_THRESHOLD || "40", 10);

const managedProcesses = {
  gateway: { process: null, port: 3000, status: "unknown", pid: null },
  officialGateway: { process: null, port: 3005, status: "unknown", pid: null },
};

async function checkPortReachable(port, host = "localhost") {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    sock.setTimeout(1500);
    sock.on("connect", () => { sock.destroy(); resolve(true); });
    sock.on("error", () => resolve(false));
    sock.on("timeout", () => { sock.destroy(); resolve(false); });
    sock.connect(port, host);
  });
}

function startManagedService(name, command, args, cwd, port) {
  const proc = managedProcesses[name];
  if (proc.process) {
    return { started: false, message: `${name} already managed (pid=${proc.pid})` };
  }
  const child = spawn(command, args, {
    cwd,
    stdio: "pipe",
    detached: false,
    env: { ...process.env },
  });
  proc.process = child;
  proc.pid = child.pid;
  proc.status = "starting";
  child.on("error", (err) => {
    console.error(`[${name}] spawn error:`, err.message);
    proc.status = "error";
    proc.process = null;
    proc.pid = null;
  });
  child.on("exit", (code) => {
    console.log(`[${name}] exited with code ${code}`);
    proc.status = "stopped";
    proc.process = null;
    proc.pid = null;
  });
  child.stdout?.on("data", (d) => process.stdout.write(`[${name}] ${d}`));
  child.stderr?.on("data", (d) => process.stderr.write(`[${name}] ${d}`));
  return { started: true, pid: child.pid, message: `${name} starting on port ${port}...` };
}

function stopManagedService(name) {
  const proc = managedProcesses[name];
  if (!proc.process) {
    return { stopped: false, message: `${name} not managed` };
  }
  proc.process.kill("SIGTERM");
  proc.status = "stopping";
  return { stopped: true, pid: proc.pid, message: `${name} stopping...` };
}

const MODEL_CATALOG = {
  local: [
    {
      id: "ollama/qwen2.5:3b",
      provider: "ollama",
      modelId: "qwen2.5:3b",
      baseUrl: "http://localhost:11434/v1",
      contextLength: 32768,
      supportsStreaming: true,
      supportsTools: false,
      costPer1kInput: 0,
      costPer1kOutput: 0,
      priority: 1,
      tags: ["chat", "completion", "local"],
    },
  ],
  cloud: [
    {
      id: "moonshot/kimi-k2.6",
      provider: "moonshot",
      modelId: "kimi-k2.6",
      baseUrl: "https://api.moonshot.cn/v1",
      apiKey: process.env.MOONSHOT_API_KEY || "",
      contextLength: 262144,
      supportsStreaming: true,
      supportsTools: true,
      costPer1kInput: 0.76,
      costPer1kOutput: 3.2,
      priority: 2,
      tags: ["chat", "completion", "tool_call", "cloud"],
    },
    {
      id: "deepseek/deepseek-chat",
      provider: "deepseek",
      modelId: "deepseek-chat",
      baseUrl: "https://api.deepseek.com/v1",
      apiKey: process.env.DEEPSEEK_API_KEY || "",
      contextLength: 64000,
      supportsStreaming: true,
      supportsTools: false,
      costPer1kInput: 0.00014,
      costPer1kOutput: 0.00028,
      priority: 3,
      tags: ["chat", "completion", "code", "cloud"],
    },
  ],
};

const bridgeStats = {
  totalRequests: 0,
  successRequests: 0,
  failedRequests: 0,
  totalLatencyMs: 0,
};

const endpointState = {};
for (const model of [...MODEL_CATALOG.local, ...MODEL_CATALOG.cloud]) {
  endpointState[model.id] = {
    currentLoad: 0,
    maxConcurrent: model.id.includes("ollama") ? 5 : 20,
    avgLatencyMs: 0,
    successRate: 1.0,
    totalRequests: 0,
    failedRequests: 0,
  };
}

function loadOpenClawConfig() {
  const configPath = join(PROJECT_ROOT, "openclaw.json");
  if (existsSync(configPath)) {
    try {
      return JSON.parse(readFileSync(configPath, "utf-8"));
    } catch {
      return null;
    }
  }
  return null;
}

let openclawConfig = loadOpenClawConfig();

const AGENT_SOULS = {};

function loadAgentSouls() {
  const agents = openclawConfig?.agents?.list || [];
  const defaults = openclawConfig?.agents?.defaults || {};
  for (const agent of agents) {
    AGENT_SOULS[agent.id] = {
      id: agent.id,
      isDefault: agent.default || false,
      workspace: agent.workspace || defaults.workspace || "./workspace",
      model: agent.model || { primary: defaults.model },
      skills: agent.skills || [],
      tools: agent.tools || defaults.tools || { allow: [], deny: [] },
      thinkingLevel: agent.thinkingLevel || defaults.thinkingLevel || "medium",
      sandbox: agent.sandbox || defaults.sandbox || { mode: "non-main", scope: "session" },
      soul: buildAgentSoul(agent),
    };
  }
}

function buildAgentSoul(agent) {
  const id = agent.id;
  const skills = agent.skills || [];
  const toolAllow = agent.tools?.allow || [];

  const roleMap = {
    "local-dispatcher": {
      name: "Local Dispatcher",
      personality: "高效、精准、低延迟优先",
      description: "本地模型调度专家，专注于低延迟、零成本的本地推理任务",
      capabilities: ["本地模型调度", "低延迟推理", "隐私数据处理", "成本优化"],
    },
    "cloud-dispatcher": {
      name: "Cloud Dispatcher",
      personality: "全面、强大、能力优先",
      description: "云端模型调度专家，处理复杂任务、工具调用和多步骤推理",
      capabilities: ["云端模型调度", "工具调用", "多步推理", "长上下文处理"],
    },
    "code-executor": {
      name: "Code Executor",
      personality: "严谨、精确、代码优先",
      description: "代码执行专家，擅长代码生成、调试和技术问题解决",
      capabilities: ["代码生成", "代码调试", "技术问题分析", "Python/Shell执行"],
    },
    "main": {
      name: "Main Agent",
      personality: "通用、灵活、综合",
      description: "通用调度Agent，根据任务复杂度自动选择最优路径",
      capabilities: ["通用对话", "任务分析", "智能路由", "多Agent协调"],
    },
  };

  const soul = roleMap[id] || {
    name: id,
    personality: "专业、可靠",
    description: `Agent ${id}`,
    capabilities: skills.length > 0 ? skills : ["通用任务处理"],
  };

  soul.id = id;
  soul.skills = skills;
  soul.toolPermissions = toolAllow;
  soul.executionSteps = [
    "analyze_request",
    "select_model",
    "execute_inference",
    "validate_output",
    "report_result",
  ];

  return soul;
}

loadAgentSouls();

class AgentExecutionContext {
  constructor(agentId, request) {
    this.agentId = agentId;
    this.request = request;
    this.soul = AGENT_SOULS[agentId] || null;
    this.trace = [];
    this.startTime = Date.now();
    this.status = "initialized";
    this.steps = [];
    this.currentStep = 0;
  }

  addTrace(agent, message) {
    this.trace.push({
      agent,
      message,
      timestamp: Date.now(),
      elapsed: Date.now() - this.startTime,
    });
  }

  addStep(stepName, detail) {
    this.steps.push({
      step: stepName,
      detail,
      startTime: Date.now(),
      status: "running",
    });
    this.currentStep = this.steps.length - 1;
  }

  completeStep(result) {
    if (this.currentStep < this.steps.length) {
      const step = this.steps[this.currentStep];
      step.status = "completed";
      step.endTime = Date.now();
      step.duration = step.endTime - step.startTime;
      step.result = result;
    }
  }

  failStep(error) {
    if (this.currentStep < this.steps.length) {
      const step = this.steps[this.currentStep];
      step.status = "failed";
      step.endTime = Date.now();
      step.duration = step.endTime - step.startTime;
      step.error = error;
    }
  }

  getExecutionPlan() {
    if (!this.soul) return [];
    return this.soul.soul.executionSteps.map((step, i) => ({
      order: i + 1,
      name: step,
      description: this.getStepDescription(step),
    }));
  }

  getStepDescription(step) {
    const descMap = {
      analyze_request: "分析请求内容，识别任务类型和复杂度",
      select_model: "根据Agent Soul和能力需求选择最优模型",
      execute_inference: "通过Gateway执行模型推理",
      validate_output: "验证输出质量和完整性",
      report_result: "生成执行报告并返回结果",
    };
    return descMap[step] || step;
  }

  getSummary() {
    return {
      agent_id: this.agentId,
      soul_name: this.soul?.soul?.name || this.agentId,
      soul_personality: this.soul?.soul?.personality || "-",
      status: this.status,
      total_duration_ms: Date.now() - this.startTime,
      steps_completed: this.steps.filter((s) => s.status === "completed").length,
      steps_total: this.steps.length,
      steps: this.steps.map((s) => ({
        step: s.step,
        status: s.status,
        duration_ms: s.duration || 0,
      })),
      trace_count: this.trace.length,
    };
  }
}

async function dispatchViaAgentChain(request) {
  const agentId = resolveAgentId(request);
  const ctx = new AgentExecutionContext(agentId, request);

  ctx.addTrace("agent_router", `Agent chain mode: routing to agent=${agentId}`);

  if (!ctx.soul) {
    ctx.addTrace("agent_router", `Agent '${agentId}' not found, falling back to gateway mode`);
    return dispatchViaOpenClaw(request);
  }

  ctx.addTrace("agent_soul", `Loaded soul: ${ctx.soul.soul.name} (${ctx.soul.soul.personality})`);
  ctx.addTrace("agent_soul", `Capabilities: ${ctx.soul.soul.capabilities.join(", ")}`);
  ctx.addTrace("agent_soul", `Execution plan: ${ctx.soul.soul.executionSteps.join(" → ")}`);

  ctx.addStep("analyze_request", "Analyzing request type and complexity");
  const analysis = analyzeRequest(request, ctx.soul);
  ctx.addTrace("agent_analyzer", `Request analysis: type=${analysis.type}, complexity=${analysis.complexity}, requires_tools=${analysis.requires_tools}, recommended_model=${analysis.recommendedModel}`);
  ctx.completeStep(analysis);

  ctx.addStep("select_model", "Selecting optimal model based on agent soul and request analysis");
  const modelSelection = selectModelForAgent(analysis, ctx.soul);
  ctx.addTrace("agent_selector", `Model selected: ${modelSelection.endpoint.id} (reason: ${modelSelection.reason})`);
  if (modelSelection.fallbacks.length > 0) {
    ctx.addTrace("agent_selector", `Fallback chain: ${modelSelection.fallbacks.map((f) => f.id).join(" → ")}`);
  }
  ctx.completeStep(modelSelection);

  ctx.addStep("execute_inference", "Executing model inference via OpenClaw Official Gateway");
  let result;
  try {
    result = await callOpenClawOfficialGateway(request, agentId);
    ctx.addTrace("agent_executor", `OpenClaw Gateway inference completed: model=${result.model_name}, latency=${result.latency_ms}ms, finish=${result.finish_reason}`);
    ctx.completeStep(result);
  } catch (error) {
    ctx.addTrace("agent_executor", `OpenClaw Gateway failed: ${error.message}, falling back to direct model call`);
    ctx.failStep(error.message);

    try {
      result = await callModelApi(modelSelection.endpoint, request);
      ctx.addTrace("agent_executor", `Fallback direct call succeeded: model=${result.model_name}, latency=${result.latency_ms}ms`);
      result.fallback_used = true;
      result.fallback_from = "openclaw_official_gateway";
      ctx.completeStep(result);
    } catch (fbError) {
      ctx.addTrace("agent_executor", `Fallback also failed: ${fbError.message}`);

      if (modelSelection.fallbacks.length > 0) {
        ctx.addTrace("agent_fallback", `Attempting fallback models...`);
        for (const fb of modelSelection.fallbacks.slice(0, 2)) {
          try {
            result = await callModelApi(fb, request);
            ctx.addTrace("agent_fallback", `Fallback succeeded: model=${result.model_name}, latency=${result.latency_ms}ms`);
            result.fallback_used = true;
            result.fallback_from = modelSelection.endpoint.id;
            break;
          } catch (fbErr) {
            ctx.addTrace("agent_fallback", `Fallback ${fb.id} failed: ${fbErr.message}`);
          }
        }
      }

      if (!result) {
        ctx.status = "failed";
        ctx.addTrace("agent_chain", `All models failed for agent ${agentId}`);
        return {
          request_id: request.request_id,
          appid: request.appid,
          status: "failed",
          error: { code: "ALL_MODELS_FAILED", message: `Agent ${agentId}: all models failed`, retryable: true },
          agent_trace: ctx.trace,
          execution_context: ctx.getSummary(),
          strategy_name: "agent_chain_failed",
        };
      }
    }
  }

  ctx.addStep("validate_output", "Validating output quality");
  const validation = validateOutput(result, analysis);
  ctx.addTrace("agent_validator", `Output validation: quality=${validation.quality}, complete=${validation.isComplete}, tokens=${result.usage?.total_tokens || "?"}`);
  ctx.completeStep(validation);

  ctx.addStep("report_result", "Generating execution report");
  ctx.status = "completed";
  const summary = ctx.getSummary();
  ctx.addTrace("agent_report", `Execution complete: ${summary.steps_completed}/${summary.steps_total} steps, total=${summary.total_duration_ms}ms`);
  ctx.completeStep(summary);

  return {
    request_id: request.request_id,
    appid: request.appid,
    status: "success",
    result,
    fallback_results: null,
    agent_trace: ctx.trace,
    execution_context: summary,
    strategy_name: "agent_chain",
    routing_decision: {
      agent_type: result.model_type || "cloud",
      selected_endpoint: result.model_name,
      strategy_name: `agent_chain_openclaw_gateway`,
      reason: `Routed via OpenClaw Official Gateway (agent=${agentId})`,
      openclaw_gateway: OPENCLAW_OFFICIAL_GATEWAY_URL,
    },
  };
}

async function callOpenClawOfficialGateway(request, agentId) {
  const startTime = Date.now();
  const messages = [];
  if (request.context) {
    messages.push(...request.context);
  }
  messages.push({ role: "user", content: request.prompt });

  const model = agentId ? `openclaw/${agentId}` : "openclaw";

  const payload = {
    model,
    messages,
    max_tokens: 1024,
  };
  if (request.tools && request.tools.length > 0) {
    payload.tools = request.tools;
    payload.tool_choice = request.tool_choice || "auto";
  }

  const headers = { "Content-Type": "application/json" };
  const gatewayToken = process.env.OPENCLAW_TOKEN || "";
  if (gatewayToken) {
    headers["Authorization"] = `Bearer ${gatewayToken}`;
  }

  const targetUrl = `${OPENCLAW_OFFICIAL_GATEWAY_URL}/v1/chat/completions`;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), AGENT_EXECUTION_TIMEOUT);

  try {
    const resp = await fetch(targetUrl, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    const data = await resp.json();
    clearTimeout(timeout);

    if (!resp.ok) {
      throw new Error(`OpenClaw Gateway error: ${data.error?.message || resp.statusText}`);
    }

    const choice = data.choices?.[0];
    const content = choice?.message?.content || "";
    const toolCalls = choice?.message?.tool_calls || null;
    const finishReason = choice?.finish_reason || "stop";

    const latencyMs = Date.now() - startTime;

    return {
      model_name: data.model || model,
      model_type: "openclaw-agent",
      provider: "openclaw",
      output: content,
      tool_calls: toolCalls,
      usage: data.usage || {},
      latency_ms: latencyMs,
      cost: 0,
      finish_reason: finishReason,
      routed_via_gateway: true,
      actual_model: data.model || model,
      openclaw_official: true,
    };
  } catch (error) {
    clearTimeout(timeout);
    if (error.name === "AbortError") {
      throw new Error(`OpenClaw Gateway timeout after ${AGENT_EXECUTION_TIMEOUT}ms`);
    }
    throw error;
  }
}

function resolveAgentId(request) {
  if (request.agent_id && AGENT_SOULS[request.agent_id]) {
    return request.agent_id;
  }
  const type = request.type || "chat";
  const hasTools = !!(request.tools && request.tools.length > 0);
  if (type === "tool_call" || hasTools) {
    return "cloud-dispatcher";
  }
  if (type === "chat" || type === "completion") {
    return "local-dispatcher";
  }
  const defaultAgent = Object.values(AGENT_SOULS).find((a) => a.isDefault);
  return defaultAgent?.id || "main";
}

function analyzeRequest(request, soul) {
  const type = request.type || "chat";
  const hasTools = !!(request.tools && request.tools.length > 0);
  const prompt = request.prompt || "";
  const promptLen = prompt.length;

  let complexity = "simple";
  if (hasTools || type === "tool_call") complexity = "complex";
  else if (promptLen > 500 || type === "code_execution") complexity = "moderate";
  else if (prompt.includes("分析") || prompt.includes("比较") || prompt.includes("设计")) complexity = "moderate";

  let recommendedModel = soul.model.primary;
  if (hasTools || type === "tool_call") {
    const toolCapable = [...MODEL_CATALOG.local, ...MODEL_CATALOG.cloud].filter((m) => m.supportsTools);
    if (toolCapable.length > 0) recommendedModel = toolCapable[0].id;
  }

  return {
    type,
    complexity,
    requiresTools: hasTools,
    promptLength: promptLen,
    recommendedModel,
    soulCapabilities: soul.soul.capabilities,
  };
}

function selectModelForAgent(analysis, soul) {
  const allModels = [...MODEL_CATALOG.local, ...MODEL_CATALOG.cloud];
  const primary = allModels.find((m) => m.id === soul.model.primary);
  const fallbacks = (soul.model.fallbacks || [])
    .map((id) => allModels.find((m) => m.id === id))
    .filter(Boolean);

  if (analysis.requiresTools) {
    const toolCapable = allModels.filter((m) => m.supportsTools && m.currentLoad < m.maxConcurrent);
    if (toolCapable.length > 0) {
      const selected = toolCapable[0];
      const toolFallbacks = allModels.filter((m) => m.supportsTools && m.id !== selected.id);
      return {
        endpoint: selected,
        fallbacks: [...toolFallbacks, ...fallbacks.filter((f) => !f.supportsTools)],
        reason: "tool_capable_required",
      };
    }
  }

  if (analysis.complexity === "complex" && primary?.tags?.includes("cloud")) {
    return { endpoint: primary, fallbacks, reason: "complex_task_cloud" };
  }

  if (primary) {
    const state = endpointState[primary.id];
    if (state && state.currentLoad < state.maxConcurrent) {
      return { endpoint: primary, fallbacks, reason: "agent_primary_model" };
    }
  }

  const localAvailable = MODEL_CATALOG.local.filter((m) => {
    const s = endpointState[m.id];
    return s && s.currentLoad < s.maxConcurrent;
  });
  if (localAvailable.length > 0) {
    return {
      endpoint: localAvailable[0],
      fallbacks: [...fallbacks, ...MODEL_CATALOG.cloud.slice(0, 2)],
      reason: "agent_local_available",
    };
  }

  const cloudAvailable = MODEL_CATALOG.cloud.filter((m) => {
    const s = endpointState[m.id];
    return s && s.currentLoad < s.maxConcurrent;
  });
  if (cloudAvailable.length > 0) {
    return { endpoint: cloudAvailable[0], fallbacks: [], reason: "agent_cloud_only" };
  }

  return { endpoint: primary, fallbacks, reason: "agent_default" };
}

function validateOutput(result, analysis) {
  const hasOutput = !!(result.output && result.output.trim().length > 0);
  const hasUsage = !!result.usage;
  const quality = hasOutput ? (result.output.length > 10 ? "good" : "minimal") : "empty";
  return {
    quality,
    isComplete: hasOutput && result.finish_reason === "stop",
    hasUsage,
    outputLength: result.output?.length || 0,
  };
}

const SMART_ROUTER_RULES_DEFAULTS = {
  typeWeights: {
    chat: 5, completion: 5, tool_call: 35, code_execution: 40,
    image: 25, audio: 25, analysis: 20, translation: 10,
  },
  toolCallBase: 30,
  multiToolBonus: 10,
  promptLengthThresholds: [
    { max: 50, score: 0 }, { max: 200, score: 3 },
    { max: 500, score: 8 }, { max: 1000, score: 12 },
    { max: Infinity, score: 18 },
  ],
  complexityKeywords: {
    moderate: ["分析", "比较", "设计", "评估", "优化", "推荐", "规划", "analyze", "compare", "design", "evaluate", "optimize"],
    high: ["多步骤", "链式", "编排", "协同", "自主", "执行计划", "multi-step", "chain", "orchestrate", "autonomous", "agent"],
  },
  contextWeight: 5,
  contextMaxScore: 15,
  priorityBoost: { 1: 10, 2: 5, 3: 0, 4: -3, 5: -5 },
};

function buildSmartRouterRules() {
  const cfg = openclawConfig?.models?.smartRouter;
  if (!cfg || !cfg.enabled) return SMART_ROUTER_RULES_DEFAULTS;

  if (cfg.threshold) SMART_ROUTER_THRESHOLD = cfg.threshold;
  if (cfg.defaultMode) DEFAULT_ROUTE_MODE = cfg.defaultMode;

  const r = cfg.rules || {};
  const rules = {
    typeWeights: { ...SMART_ROUTER_RULES_DEFAULTS.typeWeights, ...(r.typeWeights || {}) },
    toolCallBase: r.toolCallBase ?? SMART_ROUTER_RULES_DEFAULTS.toolCallBase,
    multiToolBonus: r.multiToolBonus ?? SMART_ROUTER_RULES_DEFAULTS.multiToolBonus,
    promptLengthThresholds: r.promptLengthThresholds
      ? r.promptLengthThresholds.map(t => ({ max: t.max, score: t.score }))
      : SMART_ROUTER_RULES_DEFAULTS.promptLengthThresholds,
    complexityKeywords: {
      moderate: r.complexityKeywords?.moderate || SMART_ROUTER_RULES_DEFAULTS.complexityKeywords.moderate,
      high: r.complexityKeywords?.high || SMART_ROUTER_RULES_DEFAULTS.complexityKeywords.high,
    },
    contextWeight: r.contextWeight ?? SMART_ROUTER_RULES_DEFAULTS.contextWeight,
    contextMaxScore: r.contextMaxScore ?? SMART_ROUTER_RULES_DEFAULTS.contextMaxScore,
    priorityBoost: { ...SMART_ROUTER_RULES_DEFAULTS.priorityBoost, ...(r.priorityBoost || {}) },
  };
  return rules;
}

let SMART_ROUTER_RULES = buildSmartRouterRules();

function smartRouterScore(request) {
  const type = request.type || "chat";
  const prompt = request.prompt || "";
  const tools = request.tools || [];
  const context = request.context || [];
  const priority = request.priority || 3;

  const scores = {};
  let total = 0;

  const typeWeight = SMART_ROUTER_RULES.typeWeights[type] ?? 5;
  scores.type = { value: typeWeight, detail: `type=${type} → +${typeWeight}` };
  total += typeWeight;

  const hasTools = tools.length > 0;
  let toolScore = 0;
  if (hasTools) {
    toolScore = SMART_ROUTER_RULES.toolCallBase;
    if (tools.length > 1) {
      toolScore += SMART_ROUTER_RULES.multiToolBonus;
    }
  }
  scores.tools = { value: toolScore, detail: hasTools ? `${tools.length} tool(s) → +${toolScore}` : "no tools → +0" };
  total += toolScore;

  const promptLen = prompt.length;
  let promptScore = 0;
  for (const t of SMART_ROUTER_RULES.promptLengthThresholds) {
    if (promptLen <= t.max) {
      promptScore = t.score;
      break;
    }
  }
  scores.promptLength = { value: promptScore, detail: `len=${promptLen} → +${promptScore}` };
  total += promptScore;

  let keywordScore = 0;
  let keywordDetail = [];
  for (const kw of SMART_ROUTER_RULES.complexityKeywords.moderate) {
    if (prompt.includes(kw)) {
      keywordScore += 5;
      keywordDetail.push(kw);
    }
  }
  for (const kw of SMART_ROUTER_RULES.complexityKeywords.high) {
    if (prompt.includes(kw)) {
      keywordScore += 10;
      keywordDetail.push(kw);
    }
  }
  scores.keywords = { value: keywordScore, detail: keywordDetail.length > 0 ? `${keywordDetail.join(",")} → +${keywordScore}` : "no keywords → +0" };
  total += keywordScore;

  const ctxScore = Math.min(context.length * SMART_ROUTER_RULES.contextWeight, SMART_ROUTER_RULES.contextMaxScore);
  scores.context = { value: ctxScore, detail: `${context.length} messages → +${ctxScore}` };
  total += ctxScore;

  const prioBoost = SMART_ROUTER_RULES.priorityBoost[priority] ?? 0;
  scores.priority = { value: prioBoost, detail: `priority=${priority} → ${prioBoost >= 0 ? "+" : ""}${prioBoost}` };
  total += prioBoost;

  total = Math.max(0, Math.min(100, total));

  return { total, scores, breakdown: Object.entries(scores).map(([k, v]) => v.detail).join(" | ") };
}

function smartRouteDecision(request) {
  const { total, scores, breakdown } = smartRouterScore(request);
  const threshold = SMART_ROUTER_THRESHOLD;
  const decision = total >= threshold ? "agent" : "gateway";

  const reasons = [];
  if (scores.tools?.value > 0) reasons.push("requires tool execution");
  if (scores.type?.value >= 25) reasons.push(`complex type (${request.type})`);
  if (scores.keywords?.value > 0) reasons.push("complexity keywords detected");
  if (scores.promptLength?.value >= 12) reasons.push("long prompt");
  if (scores.context?.value >= 10) reasons.push("rich context");

  const gatewayReason = total < 10
    ? "simple request, gateway sufficient"
    : "below agent threshold, gateway for low latency";
  const agentReason = reasons.length > 0
    ? reasons.join(", ")
    : "score exceeds threshold";

  return {
    score: total,
    threshold,
    decision,
    scores,
    breakdown,
    reason: decision === "agent" ? agentReason : gatewayReason,
    estimatedLatency: decision === "gateway" ? "300-800ms" : "3-60s",
  };
}

function getRoutingConfig() {
  if (openclawConfig?.models?.routing) {
    return openclawConfig.models.routing;
  }
  return {
    defaultStrategy: "adaptive",
    strategies: {
      adaptive: {
        localFirst: true,
        latencyThresholdMs: 3000,
        costThreshold: 0.01,
        fallbackOnTimeout: true,
        fallbackOnError: true,
        preferLocalForPrivacy: true,
        preferCloudForComplex: true,
        complexityIndicators: ["tool_call", "code_execution", "multi_step"],
      },
    },
  };
}

function adaptiveRoute(request) {
  const routing = getRoutingConfig();
  const strategy = routing.strategies.adaptive;
  const allModels = [...MODEL_CATALOG.local, ...MODEL_CATALOG.cloud];
  const state = { ...endpointState };

  const constraints = request.constraints || {};
  const priority = request.priority || 3;
  const type = request.type || "chat";

  if (constraints.require_local) {
    const local = selectBestModel(MODEL_CATALOG.local, state, request);
    return {
      agent_type: "local",
      selected_endpoint: local,
      fallback_endpoints: getFallbacks(local, MODEL_CATALOG.cloud, state),
      strategy_name: "adaptive_local_required",
      reason: "require_local constraint enforced",
    };
  }

  if (constraints.preferred_providers && constraints.preferred_providers.length > 0) {
    const preferred = constraints.preferred_providers;
    const matching = allModels.filter((m) => preferred.includes(m.provider));
    if (matching.length > 0) {
      const selected = selectBestModel(matching, state, request);
      const fallbacks = allModels.filter(
        (m) => m.id !== selected.id && !preferred.includes(m.provider)
      );
      return {
        agent_type: selected.tags.includes("local") ? "local" : "cloud",
        selected_endpoint: selected,
        fallback_endpoints: fallbacks.slice(0, 2),
        strategy_name: "adaptive_preferred_provider",
        reason: `Preferred provider match: ${selected.provider}`,
      };
    }
  }

  if (priority <= 2 && strategy.localFirst) {
    const local = selectBestModel(MODEL_CATALOG.local, state, request);
    if (local) {
      return {
        agent_type: "local",
        selected_endpoint: local,
        fallback_endpoints: getFallbacks(local, MODEL_CATALOG.cloud, state),
        strategy_name: "adaptive_priority_local_first",
        reason: `High priority (${priority}), local first for low latency`,
      };
    }
  }

  const isComplex =
    type === "tool_call" ||
    type === "image" ||
    type === "audio" ||
    (strategy.complexityIndicators || []).includes(type);
  if (isComplex && strategy.preferCloudForComplex) {
    let cloud = null;
    if (type === "tool_call" && request.tools) {
      const toolCapable = MODEL_CATALOG.cloud.filter(m => m.supportsTools);
      cloud = selectBestModel(toolCapable, state, request);
    }
    if (!cloud) {
      cloud = selectBestModel(MODEL_CATALOG.cloud, state, request);
    }
    if (cloud) {
      const toolCapable = MODEL_CATALOG.cloud.filter(
        (m) => m.supportsTools && m.id !== cloud.id
      );
      return {
        agent_type: "cloud",
        selected_endpoint: cloud,
        fallback_endpoints: [
          ...toolCapable.slice(0, 1),
          ...MODEL_CATALOG.local.slice(0, 1),
        ],
        strategy_name: "adaptive_complex_cloud_first",
        reason: `Complex type (${type}), cloud model preferred for capability`,
      };
    }
  }

  if (strategy.localFirst) {
    const local = selectBestModel(MODEL_CATALOG.local, state, request);
    if (local) {
      const localState = state[local.id];
      if (
        localState.avgLatencyMs > 0 &&
        localState.avgLatencyMs <= (strategy.latencyThresholdMs || 3000)
      ) {
        return {
          agent_type: "local",
          selected_endpoint: local,
          fallback_endpoints: getFallbacks(local, MODEL_CATALOG.cloud, state),
          strategy_name: "adaptive_local_first",
          reason: "Default: local model selected (low latency, zero cost)",
        };
      }
      return {
        agent_type: "local",
        selected_endpoint: local,
        fallback_endpoints: getFallbacks(local, MODEL_CATALOG.cloud, state),
        strategy_name: "adaptive_local_first",
        reason: "Default: local model selected",
      };
    }
  }

  const cloud = selectBestModel(MODEL_CATALOG.cloud, state, request);
  if (cloud) {
    return {
      agent_type: "cloud",
      selected_endpoint: cloud,
      fallback_endpoints: MODEL_CATALOG.cloud
        .filter((m) => m.id !== cloud.id)
        .slice(0, 2),
      strategy_name: "adaptive_cloud_only",
      reason: "No local available, cloud model selected",
    };
  }

  return {
    agent_type: "local",
    selected_endpoint: null,
    fallback_endpoints: [],
    strategy_name: "adaptive_no_endpoint",
    reason: "No available model endpoints",
  };
}

function selectBestModel(candidates, state, request) {
  if (!candidates || candidates.length === 0) return null;

  const available = candidates.filter((m) => {
    const s = state[m.id];
    return s && s.currentLoad < s.maxConcurrent;
  });

  if (available.length === 0) return null;

  if (request.model_hint) {
    const hint = request.model_hint.toLowerCase();
    const match = available.find(
      (m) =>
        m.id.toLowerCase().includes(hint) ||
        m.modelId.toLowerCase().includes(hint)
    );
    if (match) return match;
  }

  const scored = available.map((m) => {
    const s = state[m.id];
    let score = 100.0;
    score -= (m.priority - 1) * 15.0;
    const loadFactor = s.currentLoad / s.maxConcurrent;
    score -= loadFactor * 20.0;
    score += s.successRate * 10.0;
    if (s.avgLatencyMs > 0) {
      if (
        request.constraints?.max_latency_ms &&
        s.avgLatencyMs > request.constraints.max_latency_ms
      ) {
        score -= 50.0;
      }
    }
    return { model: m, score };
  });

  scored.sort((a, b) => b.score - a.score);
  return scored[0].model;
}

function getFallbacks(primary, fallbackPool, state) {
  if (!primary) return fallbackPool.slice(0, 2);
  return fallbackPool
    .filter((m) => m.id !== primary.id)
    .sort((a, b) => {
      const sa = state[a.id] || { currentLoad: 0, maxConcurrent: 1 };
      const sb = state[b.id] || { currentLoad: 0, maxConcurrent: 1 };
      return sa.currentLoad / sa.maxConcurrent - sb.currentLoad / sb.maxConcurrent;
    })
    .slice(0, 2);
}

function updateEndpointState(modelId, latencyMs, success) {
  const s = endpointState[modelId];
  if (!s) return;
  s.totalRequests++;
  if (!success) s.failedRequests++;
  if (s.avgLatencyMs === 0) {
    s.avgLatencyMs = latencyMs;
  } else {
    s.avgLatencyMs = Math.round(0.7 * s.avgLatencyMs + 0.3 * latencyMs);
  }
  s.successRate = success
    ? 0.95 * s.successRate + 0.05 * 1.0
    : 0.95 * s.successRate + 0.05 * 0.0;
}

const ENDPOINT_TO_AGENT = {
  "ollama/qwen2.5:3b": "openclaw/local-dispatcher",
  "moonshot/kimi-k2.6": "openclaw/cloud-dispatcher",
  "deepseek/deepseek-chat": "openclaw/code-executor",
};

function resolveGatewayModel(endpoint) {
  return ENDPOINT_TO_AGENT[endpoint.id] || "openclaw";
}

async function callModelApi(endpoint, request) {
  const s = endpointState[endpoint.id];
  if (s) s.currentLoad++;

  const startTime = Date.now();
  try {
    const messages = [];
    if (request.context) {
      messages.push(...request.context);
    }
    messages.push({ role: "user", content: request.prompt });

    const payload = {
      model: USE_OPENCLAW_GATEWAY
        ? resolveGatewayModel(endpoint)
        : endpoint.modelId,
      messages,
      max_tokens: 1024,
    };
    if (request.parameters) {
      Object.assign(payload, request.parameters);
    }
    if (request.tools && endpoint.supportsTools) {
      payload.tools = request.tools;
      payload.tool_choice = request.tool_choice || "auto";
    }

    const headers = { "Content-Type": "application/json" };
    if (USE_OPENCLAW_GATEWAY) {
      if (request.api_key) {
        headers["Authorization"] = `Bearer ${request.api_key}`;
      }
    } else {
      if (request.api_key || endpoint.apiKey) {
        headers["Authorization"] = `Bearer ${request.api_key || endpoint.apiKey}`;
      }
    }

    const targetUrl = USE_OPENCLAW_GATEWAY
      ? `${OPENCLAW_GATEWAY_URL}/v1/chat/completions`
      : `${endpoint.baseUrl}/chat/completions`;

    const defaultTimeout = USE_OPENCLAW_GATEWAY ? 120000 : 30000;
    const timeout = (request.timeout_ms || defaultTimeout) / 1000;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeout * 1000);

    const response = await fetch(targetUrl, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    if (!response.ok) {
      const text = await response.text();
      throw new Error(`Model ${endpoint.id} returned HTTP ${response.status}: ${text}`);
    }

    const data = await response.json();
    const latencyMs = Date.now() - startTime;

    updateEndpointState(endpoint.id, latencyMs, true);

    const choices = data.choices || [];
    let output = "";
    let finishReason = null;
    if (choices.length > 0) {
      const message = choices[0].message || {};
      output = message.content || "";
      finishReason = choices[0].finish_reason;
      const toolCalls = message.tool_calls;
      if (!output && toolCalls && toolCalls.length > 0) {
        output = toolCalls.map(tc => {
          const fn = tc.function || {};
          return `[Tool Call] ${fn.name || '?'}(${fn.arguments || ''})`;
        }).join('\n');
      }
    }

    const usage = data.usage || null;
    let cost = null;
    if (usage) {
      const inputCost =
        ((usage.prompt_tokens || 0) / 1000) * endpoint.costPer1kInput;
      const outputCost =
        ((usage.completion_tokens || 0) / 1000) * endpoint.costPer1kOutput;
      cost = Math.round((inputCost + outputCost) * 1000000) / 1000000;
    }

    const actualModel = data.model || endpoint.modelId;

    return {
      model_name: endpoint.id,
      model_type: endpoint.tags.includes("local") ? "local" : "cloud",
      provider: endpoint.provider,
      output,
      usage,
      latency_ms: latencyMs,
      cost,
      finish_reason: finishReason,
      routed_via_gateway: USE_OPENCLAW_GATEWAY,
      actual_model: actualModel,
    };
  } catch (error) {
    const latencyMs = Date.now() - startTime;
    updateEndpointState(endpoint.id, latencyMs, false);
    throw error;
  } finally {
    if (s) s.currentLoad--;
  }
}

async function dispatchViaOpenClaw(request) {
  const routing = adaptiveRoute(request);
  const selected = routing.selected_endpoint;

  if (!selected) {
    return {
      request_id: request.request_id,
      appid: request.appid,
      status: "failed",
      error: {
        code: "NO_ENDPOINT",
        message: routing.reason,
        retryable: true,
      },
      agent_trace: [{ agent: "openclaw_router", message: routing.reason }],
      strategy_name: routing.strategy_name,
    };
  }

  const trace = [
    {
      agent: "openclaw_router",
      message: `Routed to ${selected.id} via ${routing.strategy_name}: ${routing.reason}`,
    },
    {
      agent: "openclaw_gateway",
      message: USE_OPENCLAW_GATEWAY
        ? `Forwarding to OpenClaw Gateway at ${OPENCLAW_GATEWAY_URL}/v1/chat/completions`
        : `Direct call to ${selected.baseUrl}/chat/completions (gateway bypass)`,
    },
  ];

  try {
    const result = await callModelApi(selected, request);
    trace.push({
      agent: routing.agent_type,
      message: `Primary result from ${selected.id}, latency=${result.latency_ms}ms`,
    });

    return {
      request_id: request.request_id,
      appid: request.appid,
      status: "success",
      result,
      fallback_results: null,
      agent_trace: trace,
      strategy_name: routing.strategy_name,
      routing_decision: routing,
    };
  } catch (error) {
    trace.push({
      agent: routing.agent_type,
      message: `Primary endpoint ${selected.id} failed: ${error.message}`,
    });

    if (routing.fallback_endpoints && routing.fallback_endpoints.length > 0) {
      for (const fb of routing.fallback_endpoints.slice(0, 2)) {
        try {
          const fbResult = await callModelApi(fb, request);
          trace.push({
            agent: "fallback",
            message: `Fallback succeeded with ${fb.id}, latency=${fbResult.latency_ms}ms`,
          });
          return {
            request_id: request.request_id,
            appid: request.appid,
            status: "success",
            result: fbResult,
            fallback_results: null,
            agent_trace: trace,
            strategy_name: `${routing.strategy_name}_fallback`,
            routing_decision: routing,
          };
        } catch (fbError) {
          trace.push({
            agent: "fallback",
            message: `Fallback ${fb.id} also failed: ${fbError.message}`,
          });
        }
      }
    }

    return {
      request_id: request.request_id,
      appid: request.appid,
      status: "failed",
      error: {
        code: "ALL_ENDPOINTS_FAILED",
        message: error.message,
        retryable: true,
      },
      agent_trace: trace,
      strategy_name: routing.strategy_name,
      routing_decision: routing,
    };
  }
}

async function handleOpenClawAgentMessage(request) {
  const agentId = request.agent_id || "main";
  const agents = openclawConfig?.agents?.list || [];
  const agent = agents.find((a) => a.id === agentId);

  if (!agent) {
    return {
      status: "error",
      error: `Agent '${agentId}' not found`,
    };
  }

  const model = agent.model?.primary || agent.model;
  if (!model) {
    return {
      status: "error",
      error: `No model configured for agent '${agentId}'`,
    };
  }

  const allModels = [...MODEL_CATALOG.local, ...MODEL_CATALOG.cloud];
  const endpoint = allModels.find((m) => m.id === model);

  if (!endpoint) {
    return {
      status: "error",
      error: `Model '${model}' not found in catalog`,
    };
  }

  try {
    const result = await callModelApi(endpoint, {
      prompt: request.prompt,
      context: request.context,
      parameters: request.parameters,
      timeout_ms: request.timeout_ms,
      api_key: request.api_key,
    });

    if (agent.model?.fallbacks && result.finish_reason === "error") {
      for (const fallbackId of agent.model.fallbacks) {
        const fbEndpoint = allModels.find((m) => m.id === fallbackId);
        if (fbEndpoint) {
          try {
            const fbResult = await callModelApi(fbEndpoint, {
              prompt: request.prompt,
              context: request.context,
              parameters: request.parameters,
              timeout_ms: request.timeout_ms,
              api_key: request.api_key,
            });
            return {
              status: "success",
              agent_id: agentId,
              result: fbResult,
              used_model: fallbackId,
              fallback: true,
            };
          } catch {}
        }
      }
    }

    return {
      status: "success",
      agent_id: agentId,
      result,
      used_model: model,
      fallback: false,
    };
  } catch (error) {
    if (agent.model?.fallbacks) {
      for (const fallbackId of agent.model.fallbacks) {
        const fbEndpoint = allModels.find((m) => m.id === fallbackId);
        if (fbEndpoint) {
          try {
            const fbResult = await callModelApi(fbEndpoint, {
              prompt: request.prompt,
              context: request.context,
              parameters: request.parameters,
              timeout_ms: request.timeout_ms,
              api_key: request.api_key,
            });
            return {
              status: "success",
              agent_id: agentId,
              result: fbResult,
              used_model: fallbackId,
              fallback: true,
            };
          } catch {}
        }
      }
    }

    return {
      status: "error",
      agent_id: agentId,
      error: error.message,
    };
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

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);

  if (req.method === "OPTIONS") {
    sendJson(res, 200, {});
    return;
  }

  try {
    if (req.method === "POST" && url.pathname === "/dispatch") {
      const body = await parseBody(req);
      const routeMode = body.route_mode || DEFAULT_ROUTE_MODE;
      const dispatchStart = Date.now();
      let result;
      let smartDecision = null;

      if (routeMode === "smart") {
        smartDecision = smartRouteDecision(body);
        const actualMode = smartDecision.decision;
        if (actualMode === "agent") {
          result = await dispatchViaAgentChain(body);
        } else {
          result = await dispatchViaOpenClaw(body);
        }
        result.smart_routing = {
          mode: "smart",
          decision: smartDecision.decision,
          score: smartDecision.score,
          threshold: smartDecision.threshold,
          breakdown: smartDecision.breakdown,
          reason: smartDecision.reason,
          estimated_latency: smartDecision.estimatedLatency,
        };
      } else if (routeMode === "agent") {
        result = await dispatchViaAgentChain(body);
      } else {
        result = await dispatchViaOpenClaw(body);
      }

      bridgeStats.totalRequests++;
      bridgeStats.totalLatencyMs += Date.now() - dispatchStart;
      if (result.status === "success") {
        bridgeStats.successRequests++;
      } else {
        bridgeStats.failedRequests++;
      }
      sendJson(res, result.status === "failed" ? 400 : 200, result);
      return;
    }

    if (req.method === "POST" && url.pathname === "/agent/message") {
      const body = await parseBody(req);
      const result = await handleOpenClawAgentMessage(body);
      sendJson(res, result.status === "error" ? 400 : 200, result);
      return;
    }

    if (req.method === "GET" && url.pathname === "/models") {
      const modelType = url.searchParams.get("type");
      let models = [...MODEL_CATALOG.local, ...MODEL_CATALOG.cloud];
      if (modelType === "local") models = MODEL_CATALOG.local;
      if (modelType === "cloud") models = MODEL_CATALOG.cloud;

      sendJson(res, 200, {
        models: models.map((m) => ({
          ...m,
          state: endpointState[m.id],
        })),
      });
      return;
    }

    if (req.method === "GET" && url.pathname === "/routing/config") {
      sendJson(res, 200, getRoutingConfig());
      return;
    }

    if (req.method === "GET" && url.pathname === "/agents") {
      const agents = openclawConfig?.agents?.list || [];
      sendJson(res, 200, { agents });
      return;
    }

    if (req.method === "GET" && url.pathname === "/health") {
      const localAvailable = MODEL_CATALOG.local.filter((m) => {
        const s = endpointState[m.id];
        return s && s.currentLoad < s.maxConcurrent;
      }).length;
      const cloudAvailable = MODEL_CATALOG.cloud.filter((m) => {
        const s = endpointState[m.id];
        return s && s.currentLoad < s.maxConcurrent;
      }).length;

      let officialGatewayReachable = false;
      try {
        const ogRes = await fetch(`${OPENCLAW_OFFICIAL_GATEWAY_URL}/v1/models`, {
          signal: AbortSignal.timeout(3000),
        });
        officialGatewayReachable = ogRes.ok;
      } catch {}

      sendJson(res, 200, {
        status: "healthy",
        openclaw_version: "2026.4.27",
        bridge_port: BRIDGE_PORT,
        gateway_url: OPENCLAW_GATEWAY_URL,
        gateway_routing: USE_OPENCLAW_GATEWAY,
        official_gateway_url: OPENCLAW_OFFICIAL_GATEWAY_URL,
        official_gateway_reachable: officialGatewayReachable,
        default_route_mode: DEFAULT_ROUTE_MODE,
        local_models: localAvailable,
        cloud_models: cloudAvailable,
        total_endpoints: MODEL_CATALOG.local.length + MODEL_CATALOG.cloud.length,
        agent_souls: Object.keys(AGENT_SOULS).length,
        strategy: getRoutingConfig().defaultStrategy,
      });
      return;
    }

    if (req.method === "GET" && url.pathname === "/agents/souls") {
      const souls = Object.values(AGENT_SOULS).map((a) => ({
        id: a.id,
        is_default: a.isDefault,
        soul_name: a.soul.name,
        soul_personality: a.soul.personality,
        capabilities: a.soul.capabilities,
        primary_model: a.model.primary,
        fallback_models: a.model.fallbacks || [],
        skills: a.skills,
        tool_permissions: a.tools?.allow || [],
        execution_steps: a.soul.executionSteps,
      }));
      sendJson(res, 200, { agents: souls, default_route_mode: DEFAULT_ROUTE_MODE });
      return;
    }

    if (req.method === "POST" && url.pathname === "/route/mode") {
      const body = await parseBody(req);
      const mode = body.mode;
      if (mode === "gateway" || mode === "agent" || mode === "smart") {
        DEFAULT_ROUTE_MODE = mode;
        sendJson(res, 200, { mode: DEFAULT_ROUTE_MODE, message: `Route mode set to '${mode}'` });
      } else {
        sendJson(res, 400, { error: `Invalid mode '${mode}', must be 'gateway', 'agent', or 'smart'` });
      }
      return;
    }

    if (req.method === "POST" && url.pathname === "/route/reload") {
      const newConfig = loadOpenClawConfig();
      if (newConfig) {
        Object.assign(openclawConfig, newConfig);
        SMART_ROUTER_RULES = buildSmartRouterRules();
        const cfg = newConfig.models?.smartRouter;
        sendJson(res, 200, {
          reloaded: true,
          threshold: SMART_ROUTER_THRESHOLD,
          default_mode: DEFAULT_ROUTE_MODE,
          config_source: cfg ? "openclaw.json" : "hardcoded_defaults",
          config_enabled: cfg?.enabled ?? false,
        });
      } else {
        sendJson(res, 500, { error: "Failed to reload openclaw.json" });
      }
      return;
    }

    if (req.method === "POST" && url.pathname === "/route/score") {
      const body = await parseBody(req);
      const decision = smartRouteDecision(body);
      sendJson(res, 200, decision);
      return;
    }

    if (req.method === "GET" && url.pathname === "/route/rules") {
      const cfg = openclawConfig?.models?.smartRouter;
      sendJson(res, 200, {
        threshold: SMART_ROUTER_THRESHOLD,
        rules: SMART_ROUTER_RULES,
        default_mode: DEFAULT_ROUTE_MODE,
        config_source: cfg ? "openclaw.json" : "hardcoded_defaults",
        config_enabled: cfg?.enabled ?? false,
      });
      return;
    }

    if (req.method === "GET" && url.pathname === "/stats") {
      const avgLatency = bridgeStats.totalRequests > 0
        ? Math.round(bridgeStats.totalLatencyMs / bridgeStats.totalRequests)
        : 0;
      sendJson(res, 200, {
        total_requests: bridgeStats.totalRequests,
        success_requests: bridgeStats.successRequests,
        total_failed: bridgeStats.failedRequests,
        success_rate:
          bridgeStats.totalRequests > 0
            ? Math.round((bridgeStats.successRequests / bridgeStats.totalRequests) * 10000) / 10000
            : 0,
        avg_latency_ms: avgLatency,
        endpoints: Object.fromEntries(
          Object.entries(endpointState).map(([id, s]) => [
            id,
            {
              ...s,
              load_factor:
                s.maxConcurrent > 0
                  ? Math.round((s.currentLoad / s.maxConcurrent) * 10000) / 10000
                  : 1,
            },
          ])
        ),
      });
      return;
    }

    if (req.method === "GET" && url.pathname === "/services/status") {
      const gwReachable = await checkPortReachable(3000);
      const officialReachable = await checkPortReachable(3005, "127.0.0.1");
      sendJson(res, 200, {
        gateway: {
          port: 3000,
          reachable: gwReachable,
          managed: !!managedProcesses.gateway.process,
          pid: managedProcesses.gateway.pid,
          status: managedProcesses.gateway.process ? managedProcesses.gateway.status : (gwReachable ? "running-external" : "stopped"),
        },
        officialGateway: {
          port: 3005,
          reachable: officialReachable,
          managed: !!managedProcesses.officialGateway.process,
          pid: managedProcesses.officialGateway.pid,
          status: managedProcesses.officialGateway.process ? managedProcesses.officialGateway.status : (officialReachable ? "running-external" : "stopped"),
        },
      });
      return;
    }

    if (req.method === "POST" && url.pathname === "/services/start") {
      const body = await parseBody(req);
      const service = body.service;
      if (service === "gateway") {
        const result = startManagedService(
          "gateway", "node", [join(PROJECT_ROOT, "gateway", "gateway.mjs")], PROJECT_ROOT, 3000
        );
        sendJson(res, 200, result);
      } else if (service === "officialGateway") {
        const openclawPath = join(PROJECT_ROOT, "..", "OpenClaw", "openclaw", "openclaw.mjs");
        const altPath = join(process.env.HOME || "/root", ".openclaw", "openclaw.mjs");
        const cmdPath = existsSync(openclawPath) ? openclawPath : altPath;
        if (!existsSync(cmdPath)) {
          sendJson(res, 404, { started: false, message: "OpenClaw not found. Install: npm i -g openclaw" });
          return;
        }
        const result = startManagedService(
          "officialGateway", "node", [cmdPath, "gateway", "run", "--port", "3005", "--auth", "none", "--force"],
          dirname(cmdPath), 3005
        );
        sendJson(res, 200, result);
      } else {
        sendJson(res, 400, { error: "Unknown service. Use 'gateway' or 'officialGateway'" });
      }
      return;
    }

    if (req.method === "POST" && url.pathname === "/services/stop") {
      const body = await parseBody(req);
      const service = body.service;
      if (service === "gateway" || service === "officialGateway") {
        const result = stopManagedService(service);
        sendJson(res, 200, result);
      } else {
        sendJson(res, 400, { error: "Unknown service. Use 'gateway' or 'officialGateway'" });
      }
      return;
    }

    const STATIC_DIR = join(PROJECT_ROOT, "static");
    const MIME_TYPES = {
      ".html": "text/html; charset=utf-8",
      ".css": "text/css; charset=utf-8",
      ".js": "application/javascript; charset=utf-8",
      ".json": "application/json; charset=utf-8",
      ".png": "image/png",
      ".jpg": "image/jpeg",
      ".svg": "image/svg+xml",
      ".ico": "image/x-icon",
    };

    if (req.method === "GET" && url.pathname === "/static") {
      res.writeHead(302, { Location: "/static/dashboard.html" });
      res.end();
      return;
    }

    if (req.method === "GET" && url.pathname.startsWith("/static/")) {
      const relPath = url.pathname.slice("/static/".length);
      if (!relPath || relPath.includes("..")) {
        sendJson(res, 403, { error: "Forbidden" });
        return;
      }
      const filePath = join(STATIC_DIR, relPath);
      if (!filePath.startsWith(STATIC_DIR)) {
        sendJson(res, 403, { error: "Forbidden" });
        return;
      }
      if (existsSync(filePath) && statSync(filePath).isFile()) {
        const ext = extname(filePath);
        const contentType = MIME_TYPES[ext] || "application/octet-stream";
        const content = readFileSync(filePath);
        res.writeHead(200, {
          "Content-Type": contentType,
          "Cache-Control": "no-cache",
        });
        res.end(content);
        return;
      }
      sendJson(res, 404, { error: "File not found" });
      return;
    }

    if (req.method === "GET" && (url.pathname === "/" || url.pathname === "/dashboard")) {
      const dashboardPath = join(STATIC_DIR, "dashboard.html");
      if (existsSync(dashboardPath)) {
        const content = readFileSync(dashboardPath, "utf-8");
        res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        res.end(content);
        return;
      }
    }

    sendJson(res, 404, { error: "Not found" });
  } catch (error) {
    console.error("Bridge error:", error);
    sendJson(res, 500, { error: error.message });
  }
});

server.listen(BRIDGE_PORT, () => {
  console.log(`OpenClaw Bridge running on port ${BRIDGE_PORT}`);
  console.log(`Gateway URL: ${OPENCLAW_GATEWAY_URL}`);
  console.log(`Gateway routing: ${USE_OPENCLAW_GATEWAY ? "ENABLED (via OpenClaw Gateway)" : "DISABLED (direct to provider)"}`);
  console.log(`Strategy: ${getRoutingConfig().defaultStrategy}`);
  console.log(
    `Models: ${MODEL_CATALOG.local.length} local, ${MODEL_CATALOG.cloud.length} cloud`
  );
});
