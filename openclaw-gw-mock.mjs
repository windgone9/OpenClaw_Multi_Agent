#!/usr/bin/env node
/**
 * Mock OpenClaw Official Gateway (port 3005)
 *
 * Simulates the OpenClaw Official Gateway for testing agent_chain routing.
 * Supports:
 *   - /v1/chat/completions — Agent task execution with Volcano scheduling simulation
 *   - /v1/models — Model catalog
 *   - /health — Health check
 *   - /volcano/status — Volcano scheduler status
 *   - /volcano/queues — Volcano queue management
 */

import http from "node:http";

const PORT = 3005;

// ── Volcano Simulation ─────────────────────────────────────────────
const volcanoState = {
  queues: [
    { name: "default", weight: 1, running: 0, pending: 0, capacity: 10 },
    { name: "high-priority", weight: 10, running: 0, pending: 0, capacity: 5 },
    { name: "batch", weight: 1, running: 0, pending: 0, capacity: 20 },
  ],
  jobs: [],
  jobIdCounter: 1000,
};

function submitVolcanoJob(taskName, queue, resources) {
  const jobId = `volcano-job-${volcanoState.jobIdCounter++}`;
  const job = {
    job_id: jobId,
    task_name: taskName,
    queue: queue || "default",
    status: "running",
    resources: resources || { cpu: "2", memory: "4Gi", gpu: "0" },
    created_at: new Date().toISOString(),
    started_at: new Date().toISOString(),
  };
  volcanoState.jobs.push(job);

  // Update queue stats
  const q = volcanoState.queues.find((q) => q.name === (queue || "default"));
  if (q) q.running++;

  return job;
}

function completeVolcanoJob(jobId) {
  const job = volcanoState.jobs.find((j) => j.job_id === jobId);
  if (job) {
    job.status = "completed";
    job.completed_at = new Date().toISOString();
    const q = volcanoState.queues.find((q) => q.name === job.queue);
    if (q) {
      q.running = Math.max(0, q.running - 1);
    }
  }
  return job;
}

// ── Agent Task Simulation ──────────────────────────────────────────
const AGENT_RESPONSES = {
  "code-execution": {
    role: "assistant",
    content:
      "## 代码执行结果\n\n已完成代码生成和执行任务。以下是执行摘要：\n\n" +
      "### 执行步骤\n1. **环境初始化** — 分配计算资源 (Volcano Job)\n2. **代码编译** — 构建依赖并编译\n3. **单元测试** — 运行 12 个测试用例，全部通过\n4. **部署验证** — 健康检查通过\n\n### Volcano 调度信息\n- Queue: high-priority\n- Resources: 2 CPU, 4Gi Memory\n- Priority: high\n- Status: completed",
  },
  "multi-step-plan": {
    role: "assistant",
    content:
      "## 多步骤执行计划\n\n已通过 Volcano 资源调度完成多步骤任务编排：\n\n" +
      "### 步骤分解\n1. **需求分析** — 解析任务需求，识别依赖关系\n2. **资源申请** — 通过 Volcano 调度器分配 GPU 计算节点\n3. **并行执行** — 启动 3 个并行子任务\n4. **结果聚合** — 收集并合并各子任务输出\n5. **质量验证** — 运行自动化测试验证结果\n\n### 调度详情\n- Volcano Queue: batch\n- Pod Group: plan-{id}\n- Min Available: 3\n- Total Resources: 6 CPU, 16Gi Memory, 2 GPU",
  },
  "data-pipeline": {
    role: "assistant",
    content:
      "## 数据管道执行结果\n\n已通过 Volcano 作业调度完成数据处理管道：\n\n" +
      "### 管道阶段\n1. **数据采集** — 从 3 个数据源拉取原始数据 (Volcano Job: data-ingest)\n2. **数据清洗** — 去重、格式化、异常值处理 (Volcano Job: data-clean)\n3. **特征工程** — 特征提取和变换 (Volcano Job: feature-eng, GPU加速)\n4. **模型推理** — 批量推理 10,000 条记录 (Volcano Job: inference)\n5. **结果输出** — 写入结果存储\n\n### 资源使用\n- 总作业数: 4\n- 峰值并行: 2\n- GPU 使用: 2×A100 (特征工程+推理阶段)",
  },
  default: {
    role: "assistant",
    content:
      "## Agent 任务完成\n\n已通过 OpenClaw + Volcano 调度完成您的请求。\n\n" +
      "### 执行信息\n- 路由: Hermes → Gateway → OpenClaw Official GW → Volcano\n- 调度器: Volcano\n- 状态: 成功\n\n该请求已通过智能路由系统分发到 OpenClaw 官方网关，并使用 Volcano 进行资源调度和任务编排。",
  },
};

function detectTaskType(messages) {
  const lastMsg = messages?.[messages.length - 1]?.content || "";
  const lower = lastMsg.toLowerCase();
  if (/代码|code|实现|编程|函数|编译|执行|部署/.test(lower)) return "code-execution";
  if (/计划|规划|多步|方案|pipeline|管道|编排/.test(lower)) return "multi-step-plan";
  if (/数据|data|etl|处理|训练|推理|inference/.test(lower)) return "data-pipeline";
  return "default";
}

// ── HTTP Server ────────────────────────────────────────────────────
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const method = req.method;

  // CORS
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");
  if (method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }

  // Parse body
  let body = {};
  if (method === "POST") {
    const raw = await new Promise((resolve) => {
      let data = "";
      req.on("data", (chunk) => (data += chunk));
      req.on("end", () => resolve(data));
    });
    try {
      body = JSON.parse(raw);
    } catch {
      body = {};
    }
  }

  // ── Routes ─────────────────────────────────────────────────────
  // Health check
  if (url.pathname === "/health" && method === "GET") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        status: "healthy",
        service: "openclaw-official-gateway",
        version: "1.0.0-mock",
        uptime: process.uptime(),
        volcano: { status: "running", queues: volcanoState.queues.length, active_jobs: volcanoState.jobs.filter((j) => j.status === "running").length },
      })
    );
    return;
  }

  // Models catalog
  if (url.pathname === "/v1/models" && method === "GET") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        object: "list",
        data: [
          { id: "openclaw", object: "model", owned_by: "openclaw", capabilities: ["chat", "agent", "tool_call"] },
          { id: "openclaw/local-dispatcher", object: "model", owned_by: "openclaw", capabilities: ["chat", "routing"] },
          { id: "openclaw/cloud-dispatcher", object: "model", owned_by: "openclaw", capabilities: ["agent", "tool_call", "volcano"] },
          { id: "openclaw/code-executor", object: "model", owned_by: "openclaw", capabilities: ["code", "execution", "volcano"] },
        ],
      })
    );
    return;
  }

  // Chat completions — main agent execution endpoint
  if (url.pathname === "/v1/chat/completions" && method === "POST") {
    const taskType = detectTaskType(body.messages);
    const agentResponse = AGENT_RESPONSES[taskType] || AGENT_RESPONSES.default;
    const model = body.model || "openclaw/cloud-dispatcher";

    // Simulate Volcano job submission
    const queue = taskType === "code-execution" ? "high-priority" : taskType === "data-pipeline" ? "batch" : "default";
    const resources =
      taskType === "data-pipeline"
        ? { cpu: "4", memory: "16Gi", gpu: "2" }
        : taskType === "code-execution"
          ? { cpu: "2", memory: "4Gi", gpu: "0" }
          : { cpu: "2", memory: "4Gi", gpu: "0" };

    const volcanoJob = submitVolcanoJob(`agent-${taskType}`, queue, resources);

    // Simulate processing delay
    const processingTime = 200 + Math.random() * 800;
    await new Promise((resolve) => setTimeout(resolve, processingTime));

    // Complete the Volcano job
    completeVolcanoJob(volcanoJob.job_id);

    const response = {
      id: `chatcmpl-openclaw-${Date.now()}`,
      object: "chat.completion",
      created: Math.floor(Date.now() / 1000),
      model: model,
      choices: [
        {
          index: 0,
          message: {
            role: "assistant",
            content: agentResponse.content,
          },
          finish_reason: "stop",
        },
      ],
      usage: {
        prompt_tokens: JSON.stringify(body.messages || []).length,
        completion_tokens: agentResponse.content.length,
        total_tokens: JSON.stringify(body.messages || []).length + agentResponse.content.length,
      },
      // OpenClaw-specific metadata
      openclaw_metadata: {
        agent_id: model.replace("openclaw/", ""),
        task_type: taskType,
        volcano_job: volcanoJob,
        routing_trace: ["hermes", "gateway", "openclaw-official-gw", "volcano"],
        execution_time_ms: processingTime,
      },
    };

    console.log(
      `[OpenClaw GW] ✓ ${model} | task=${taskType} | volcano=${volcanoJob.job_id} | queue=${queue} | ${Math.round(processingTime)}ms`
    );

    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify(response));
    return;
  }

  // Volcano status
  if (url.pathname === "/volcano/status" && method === "GET") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        status: "running",
        scheduler: "volcano",
        version: "1.8.0-mock",
        queues: volcanoState.queues,
        active_jobs: volcanoState.jobs.filter((j) => j.status === "running").length,
        completed_jobs: volcanoState.jobs.filter((j) => j.status === "completed").length,
        total_jobs: volcanoState.jobs.length,
      })
    );
    return;
  }

  // Volcano queues
  if (url.pathname === "/volcano/queues" && method === "GET") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ queues: volcanoState.queues }));
    return;
  }

  // Volcano jobs
  if (url.pathname === "/volcano/jobs" && method === "GET") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ jobs: volcanoState.jobs }));
    return;
  }

  // 404
  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "Not Found", path: url.pathname }));
});

server.listen(PORT, () => {
  console.log(`[OpenClaw Official GW] Mock server running on http://localhost:${PORT}`);
  console.log(`[OpenClaw Official GW] Endpoints:`);
  console.log(`  POST /v1/chat/completions  — Agent task execution`);
  console.log(`  GET  /v1/models            — Model catalog`);
  console.log(`  GET  /health               — Health check`);
  console.log(`  GET  /volcano/status       — Volcano scheduler status`);
  console.log(`  GET  /volcano/queues       — Queue management`);
  console.log(`  GET  /volcano/jobs         — Job listing`);
});
