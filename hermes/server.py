from dotenv import load_dotenv
load_dotenv()

import logging
import os
import subprocess
import threading
import time
import asyncio
import uuid
import psutil
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional as Opt

from hermes.router import HermesRouter, RoutePath, RoutingScore
from hermes.llm_enhancer import LLMEnhancer
from hermes.agent import HermesAgent
from hermes.official_agent_adapter import OfficialHermesAdapter
from hermes.message_queue import (
    create_queue,
    QUEUE_REQUESTS,
    QUEUE_RESULTS,
    QUEUE_FEEDBACK,
)
from hermes.dispatch_worker import DispatchWorker

logger = logging.getLogger(__name__)

# Note: Actual logging configuration is set in __main__ via uvicorn log_config.
# This early basicConfig ensures logs are visible during module import phase.
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
for _n in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
    logging.getLogger(_n).setLevel(logging.WARNING)

HERMES_PORT = int(os.getenv("HERMES_PORT", "8082"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OFFICIAL_GW_URL = os.getenv("OPENCLAW_OFFICIAL_GATEWAY_URL", "http://127.0.0.1:3005")

llm_enhancer = LLMEnhancer()
hermes_agent = HermesAgent()
hermes_agent.bootstrap_from_rules()

OFFICIAL_AGENT_URL = os.getenv("OFFICIAL_AGENT_URL", "http://127.0.0.1:8642")
OFFICIAL_AGENT_KEY = os.getenv("OFFICIAL_AGENT_KEY", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
USE_DIRECT_OLLAMA = os.getenv("USE_DIRECT_OLLAMA", "true").lower() == "true"

# Downstream service timeouts
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "60.0"))
OFFICIAL_GW_TIMEOUT = float(os.getenv("OFFICIAL_GW_TIMEOUT", "120.0"))

official_agent = OfficialHermesAdapter(
    api_url=OFFICIAL_AGENT_URL,
    api_key=OFFICIAL_AGENT_KEY,
    hermes_router_url=f"http://localhost:{HERMES_PORT}",
    ollama_url=OLLAMA_URL,
    ollama_model=OLLAMA_MODEL,
    use_direct_ollama=USE_DIRECT_OLLAMA,
)

hermes = HermesRouter(
    llm_enhancer=llm_enhancer,
    hermes_agent=hermes_agent,
    official_agent=official_agent,
)

# Message queue and dispatch worker
QUEUE_BACKEND = os.getenv("QUEUE_BACKEND", "auto")  # auto | redis | inprocess
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
QUEUE_DATA_DIR = os.getenv("QUEUE_DATA_DIR", "")
DISPATCH_WORKERS = int(os.getenv("DISPATCH_WORKERS", "8"))

msg_queue = create_queue(backend=QUEUE_BACKEND, redis_url=REDIS_URL, data_dir=QUEUE_DATA_DIR)
dispatch_worker = DispatchWorker(
    queue=msg_queue,
    adapter=official_agent,
    max_workers=DISPATCH_WORKERS,
)

_stats: Dict = {
    "total_requests": 0,
    "success_requests": 0,
    "failed_requests": 0,
    "total_latency_ms": 0,
    "dispatch_history": [],
}
MAX_HISTORY = 50

# Pending sync results: request_id → {"event": threading.Event, "holder": dict}
# Uses threading.Event for cross-thread notification (dispatch workers are threads).
_pending_sync: Dict[str, Dict] = {}
_pending_sync_lock = threading.Lock()


class HermesDispatchRequest(BaseModel):
    appid: str = "default"
    type: str = "chat"
    prompt: str
    priority: int = 3
    model_hint: Opt[str] = None
    parameters: Opt[dict] = None
    context: Opt[list] = None
    tools: Opt[list] = None
    tool_choice: Opt[str] = None
    constraints: Opt[dict] = None
    timeout_ms: Opt[int] = None
    route_mode: Opt[str] = None
    agent_id: Opt[str] = None
    attachments: Opt[list] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _watchdog_running, _watchdog_task
    logger.info("Hermes Intelligent Router starting...")
    logger.info(f"  Ollama URL: {OLLAMA_URL}")
    logger.info(f"  OfficialGW URL: {OFFICIAL_GW_URL}")
    logger.info(f"  Port: {HERMES_PORT}")
    logger.info(f"  Queue backend: {QUEUE_BACKEND}")
    _register_known_models()
    dispatch_worker.start()
    logger.info(f"  Dispatch worker started ({DISPATCH_WORKERS} threads)")
    # Auto-start watchdog (load config from file first)
    global _watchdog_config
    _watchdog_config = _load_watchdog_config()
    _watchdog_running = True
    _watchdog_task = asyncio.create_task(_watchdog_loop())
    logger.info("  Service watchdog started (interval=%ds, grace=%ds, shutdown_timeout=%ds)",
                _watchdog_config["interval_seconds"],
                _watchdog_config.get("startup_grace_seconds", 45),
                _watchdog_config.get("shutdown_timeout_seconds", 5))
    yield
    _watchdog_running = False
    dispatch_worker.stop()
    logger.info("Hermes Intelligent Router shutdown")


def _register_known_models():
    for name in [
        "ollama/qwen2.5:3b", "moonshot/kimi-k2.6", "deepseek/deepseek-chat",
    ]:
        hermes.register_model(name)


app = FastAPI(
    title="Hermes Intelligent Router",
    description="Self-learning routing layer for OpenClaw Multi-Agent System",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static dashboard files
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import pathlib
STATIC_DIR = pathlib.Path(__file__).parent.parent / "static"
if STATIC_DIR.exists():
    @app.get("/dashboard.html")
    async def dashboard():
        return FileResponse(STATIC_DIR / "dashboard.html", media_type="text/html")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.post("/route", summary="Hermes intelligent routing decision")
async def route_request(request: HermesDispatchRequest):
    start_time = time.time()
    trace = []

    trace.append({"agent": "hermes_receiver", "message": f"Received request: type={request.type}, priority={request.priority}, tools={len(request.tools) if request.tools else 0}"})

    routing = hermes.route(request.dict())
    agent_tag = f", agent={routing.get('agent_decision', '-')}" if routing.get("agent_routed") else ""
    trace.append({
        "agent": "hermes_router",
        "message": f"Routing decision: path={routing['route_path']}, score={routing['complexity_score']}, level={routing['complexity_level']}, model={routing.get('selected_model', '-')}, skill={routing.get('skill_matched', '-')}, memory={routing.get('memory_match', False)}, reason={routing['reason']}{agent_tag}",
    })

    downstream_result = None
    fallback_results = None
    status = "success"
    error = None

    try:
        downstream_result, fallback_results, ds_trace = await _execute_routing(request, routing)
        trace.extend(ds_trace)
    except Exception as e:
        status = "failed"
        error = {"code": "DOWNSTREAM_ERROR", "message": str(e), "retryable": True}
        trace.append({"agent": "hermes_executor", "message": f"Downstream execution failed: {e}"})

    latency_ms = int((time.time() - start_time) * 1000)

    has_tool_call = bool(request.tools)
    model_name = downstream_result.get("model_name", routing.get("selected_model", "unknown")) if downstream_result else routing.get("selected_model", "unknown")

    hermes.record_result(
        model_name=model_name,
        path=routing["route_path"],
        success=status == "success",
        latency_ms=latency_ms,
        cost=downstream_result.get("cost", 0) if downstream_result else 0,
        has_tool_call=has_tool_call,
        request=request.dict(),
        routing=routing,
    )

    _stats["total_requests"] += 1
    if status == "success":
        _stats["success_requests"] += 1
    else:
        _stats["failed_requests"] += 1
    _stats["total_latency_ms"] += latency_ms

    trace.append({"agent": "hermes_recorder", "message": f"Recorded result: status={status}, latency={latency_ms}ms, model={model_name}, skill={routing.get('skill_matched', '-')}, memory={routing.get('memory_match', False)}"})

    return {
        "appid": request.appid,
        "status": status,
        "routing": routing,
        "result": downstream_result,
        "fallback_results": fallback_results,
        "error": error,
        "hermes_trace": trace,
        "total_latency_ms": latency_ms,
    }


async def _execute_routing(request: HermesDispatchRequest, routing: dict):
    """Execute routing by dispatching directly to downstream service.

    Direct connection (no Bridge/Gateway middle layer):
      - direct_local / local_inference → Ollama(11434)
      - gateway → OfficialGW(3005)
      - multimodal → Ollama(11434) multimodal model
    """
    import httpx

    trace = []
    path = routing["route_path"]
    LOCAL_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:3b")
    MULTIMODAL_MODEL = os.getenv("MULTIMODAL_MODEL", "llava:7b")

    if path in ("direct_local", "local_inference"):
        # Direct to local Ollama/vLLM
        payload = {
            "model": LOCAL_MODEL,
            "messages": [{"role": "user", "content": request.prompt}],
            "stream": False,
            "options": {"num_ctx": 4096, "temperature": 0.7},
        }
        trace.append({"agent": "hermes_dispatch", "message": f"Direct to Ollama: path={path}"})

        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(f"{OLLAMA_URL}/v1/chat/completions", json=payload)
            resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        result = {
            "model_name": LOCAL_MODEL,
            "model_type": "local",
            "output": content,
            "latency_ms": 0,
            "usage": data.get("usage", {}),
            "finish_reason": data.get("choices", [{}])[0].get("finish_reason", "stop"),
            "routed_via": f"hermes_{path}",
        }
        trace.append({"agent": "hermes_dispatch", "message": f"Ollama success: model={LOCAL_MODEL}"})
        return result, None, trace

    elif path == "gateway":
        # Direct to OfficialGW
        messages = []
        if request.context:
            messages.extend(request.context)
        messages.append({"role": "user", "content": request.prompt})

        payload = {"model": "openclaw/default", "messages": messages, "max_tokens": 512}
        if request.tools:
            payload["tools"] = request.tools
            payload["tool_choice"] = request.tool_choice or "auto"

        headers = {"Content-Type": "application/json"}
        gw_token = os.getenv("OPENCLAW_TOKEN", "")
        if gw_token:
            headers["Authorization"] = f"Bearer {gw_token}"

        trace.append({"agent": "hermes_dispatch", "message": f"Direct to OfficialGW: {OFFICIAL_GW_URL}"})

        async with httpx.AsyncClient(timeout=OFFICIAL_GW_TIMEOUT) as client:
            resp = await client.post(f"{OFFICIAL_GW_URL}/v1/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        result = {
            "model_name": data.get("model", "openclaw/default"),
            "model_type": "openclaw-agent",
            "provider": "openclaw",
            "output": choice.get("message", {}).get("content", ""),
            "tool_calls": choice.get("message", {}).get("tool_calls"),
            "latency_ms": 0,
            "usage": data.get("usage", {}),
            "finish_reason": choice.get("finish_reason", "stop"),
            "routed_via": "hermes_official_gw",
        }
        # Include openclaw_metadata for traceability (request_id, volcano_job, etc.)
        if data.get("openclaw_metadata"):
            result["_raw_openclaw_metadata"] = data["openclaw_metadata"]
        # Extract OpenClaw response ID for traceability (real OpenClaw returns id like chatcmpl_xxx)
        if data.get("id"):
            result["_raw_openclaw_metadata"] = result.get("_raw_openclaw_metadata", {})
            result["_raw_openclaw_metadata"]["request_id"] = data["id"]
            result["_raw_openclaw_metadata"]["routing_trace"] = ["hermes", "gateway", "openclaw-official-gw"]
        trace.append({"agent": "hermes_dispatch", "message": f"OfficialGW success: model={result['model_name']}"})
        return result, None, trace

    elif path == "multimodal":
        # Direct to local multimodal model
        payload = {
            "model": MULTIMODAL_MODEL,
            "messages": [{"role": "user", "content": request.prompt}],
            "stream": False,
            "options": {"num_ctx": 4096, "temperature": 0.7},
        }
        trace.append({"agent": "hermes_dispatch", "message": f"Direct to multimodal model: {MULTIMODAL_MODEL}"})

        try:
            async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
                resp = await client.post(f"{OLLAMA_URL}/v1/chat/completions", json=payload)
                resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            result = {
                "model_name": MULTIMODAL_MODEL,
                "model_type": "multimodal",
                "output": content,
                "latency_ms": 0,
                "usage": data.get("usage", {}),
                "finish_reason": data.get("choices", [{}])[0].get("finish_reason", "stop"),
                "routed_via": "hermes_multimodal",
            }
            trace.append({"agent": "hermes_dispatch", "message": f"Multimodal success: model={MULTIMODAL_MODEL}"})
            return result, None, trace
        except Exception as e:
            # Fallback to standard local model
            trace.append({"agent": "hermes_dispatch", "message": f"Multimodal model unavailable, fallback to local: {e}"})
            fallback_payload = {
                "model": LOCAL_MODEL,
                "messages": [{"role": "user", "content": request.prompt}],
                "stream": False,
                "options": {"num_ctx": 4096, "temperature": 0.7},
            }
            async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
                resp = await client.post(f"{OLLAMA_URL}/v1/chat/completions", json=fallback_payload)
                resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            result = {
                "model_name": LOCAL_MODEL,
                "model_type": "local",
                "output": content,
                "latency_ms": 0,
                "usage": data.get("usage", {}),
                "finish_reason": data.get("choices", [{}])[0].get("finish_reason", "stop"),
                "routed_via": "hermes_multimodal_fallback_local",
            }
            return result, None, trace

    else:
        raise RuntimeError(f"Unknown route path: {path}")


@app.get("/health", summary="Hermes health check")
async def health():
    return {
        "status": "healthy",
        "service": "hermes-router",
        "version": "3.0.0",
        "learning_iterations": hermes.learning.learning_iterations,
        "complexity_threshold": hermes.complexity_threshold,
        "exploration_rate": hermes.learning.exploration_rate,
        "ollama_url": OLLAMA_URL,
        "official_gw_url": OFFICIAL_GW_URL,
        "memory_records": hermes.memory.get_total_count(),
        "skills_count": len(hermes.memory.get_all_skills()),
        "llm_enhancer_enabled": llm_enhancer.enabled,
        "hermes_agent_enabled": hermes_agent.enabled,
        "agent_skills_count": len(hermes_agent.skills),
    }


@app.get("/state", summary="Hermes learning state")
async def get_state():
    state = hermes.get_state()
    return state


@app.get("/stats", summary="Hermes request statistics")
async def get_stats():
    total = _stats["total_requests"]
    # Merge dispatch worker stats
    worker_stats = dispatch_worker.get_stats()
    combined_total = total + worker_stats.get("processed", 0)
    combined_success = _stats["success_requests"] + worker_stats.get("success", 0)
    combined_failed = _stats["failed_requests"] + worker_stats.get("failed", 0)
    combined_latency = _stats["total_latency_ms"] + worker_stats.get("total_latency_ms", 0)
    return {
        "requests": {
            "total": combined_total,
            "success": combined_success,
            "failed": combined_failed,
            "success_rate": round(combined_success / combined_total, 4) if combined_total > 0 else 0,
            "avg_latency_ms": round(combined_latency / combined_total) if combined_total > 0 else 0,
            "by_route": worker_stats.get("by_route", {}),
        },
        "system": {
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "memory_total_gb": round(psutil.virtual_memory().total / (1024 ** 3), 1),
            "memory_used_gb": round(psutil.virtual_memory().used / (1024 ** 3), 2),
            "memory_percent": round(psutil.virtual_memory().percent, 1),
        },
        "hermes": hermes.get_state(),
    }


@app.post("/route/analyze", summary="Analyze request complexity without dispatching")
async def analyze_request(request: HermesDispatchRequest):
    score = hermes.score_complexity(request.dict())

    if hermes.hermes_agent and hermes.hermes_agent.enabled:
        request_with_score = dict(request.dict())
        request_with_score["_rule_complexity_score"] = score.total
        agent_decision = hermes.hermes_agent.decide(request_with_score)

        # Apply 4-category routing strategy
        req_type = request.type or "chat"
        has_tools = bool(request.tools)
        constraints = request.constraints or {}
        require_local = constraints.get("require_local", False)
        prompt_text = request.prompt or ""

        # Rule 1: Privacy override (absolute priority)
        if require_local:
            correct_path = "local_inference"
        else:
            # Rule 2: Multimodal detection
            multimodal_keywords = [
                "图片", "图像", "照片", "截图", "OCR", "识别图片", "看图", "视觉",
                "音频", "语音", "录音", "视频", "画面", "摄像头",
                "image", "photo", "picture", "screenshot", "vision", "ocr",
                "audio", "voice", "video", "camera", "multimodal",
                "分析图片", "描述图片", "图片中", "图中", "截图中的",
            ]
            has_multimodal = any(kw in prompt_text for kw in multimodal_keywords)
            attachments = request.attachments or []
            if attachments:
                has_multimodal = True

            if has_multimodal:
                correct_path = "multimodal"
            else:
                # Rule 3: Multi-step batch / Volcano / Agent-related → gateway
                multi_step_keywords = [
                    "多步骤", "多步", "批处理", "批量", "自主", "设计", "规划", "执行计划",
                    "自主执行", "架构", "方案", "调研", "流程", "编排", "工作流", "pipeline",
                    "Volcano", "volcano", "分布式", "微服务", "部署", "发布",
                ]
                has_multi_step = any(kw in prompt_text for kw in multi_step_keywords)

                is_gateway_route = (
                    has_tools
                    or req_type in ("code", "code_execution", "tool_call")
                    or score.total >= hermes.complexity_threshold
                    or has_multi_step
                )
                correct_path = "gateway" if is_gateway_route else "direct_local"

        try:
            final_path = RoutePath(agent_decision.get("route_path", score.recommended_path.value))
        except ValueError:
            final_path = score.recommended_path

        # Override if LLM decision doesn't match 4-category routing
        if final_path.value != correct_path:
            original_path = final_path.value
            final_path = RoutePath(correct_path)
            if require_local:
                reason_detail = 'require_local'
            elif correct_path == 'multimodal':
                reason_detail = 'multimodal'
            elif correct_path == 'gateway':
                reason_detail = 'multi-step/agent'
            else:
                reason_detail = 'single-step Q&A'
            agent_decision["route_path"] = correct_path
            agent_decision["reason"] = f"4-category routing: {reason_detail} (was {original_path}, score={score.total})"

        result = {
            "complexity_score": score.total,
            "complexity_level": score.level.value,
            "breakdown": score.breakdown,
            "recommended_path": final_path.value,
            "threshold": hermes.complexity_threshold,
            "skill_matched": agent_decision.get("selected_skill") or agent_decision.get("skill_matched"),
            "memory_match": False,
            "memory_detail": None,
            "agent_routed": True,
            "agent_decision": agent_decision.get("agent_decision", "unknown"),
            "agent_confidence": agent_decision.get("confidence", 0.5) or agent_decision.get("agent_confidence", 0.5),
            "agent_reasoning": agent_decision.get("reasoning", "") or agent_decision.get("reason", ""),
            "agent_llm_latency_ms": agent_decision.get("agent_llm_latency_ms"),
            "memory_context_used": agent_decision.get("memory_context_used"),
            "official_agent_routed": agent_decision.get("official_agent_routed", False),
        }
        return result

    llm_result = None
    llm_enhanced = False
    if hermes.llm_enhancer and hermes.llm_enhancer.enabled:
        request_with_score = dict(request.dict())
        request_with_score["_rule_complexity_score"] = score.total
        llm_result = hermes.llm_enhancer.classify(request_with_score)
        if llm_result:
            llm_enhanced = True
            merged = hermes.llm_enhancer.merge_scores(
                {"total": score.total, "breakdown": score.breakdown},
                llm_result,
            )
            score = RoutingScore(
                total=merged["total"],
                breakdown=merged["breakdown"],
                level=hermes._recalc_level(merged["total"]),
                recommended_path=score.recommended_path,
            )

    skill = hermes._match_skill(request.dict())
    memory = hermes._learn_from_memory(request.dict())

    final_path = score.recommended_path

    # Apply 4-category routing strategy (same logic as agent path)
    req_type_fallback = request.type or "chat"
    has_tools_fallback = bool(request.tools)
    constraints_fallback = request.constraints or {}
    require_local_fallback = constraints_fallback.get("require_local", False)
    prompt_fallback = request.prompt or ""

    if require_local_fallback:
        final_path = RoutePath.LOCAL_INFERENCE
    else:
        # Multimodal detection
        multimodal_keywords_fb = [
            "图片", "图像", "照片", "截图", "OCR", "识别图片", "看图", "视觉",
            "音频", "语音", "录音", "视频", "画面", "摄像头",
            "image", "photo", "picture", "screenshot", "vision", "ocr",
            "audio", "voice", "video", "camera", "multimodal",
            "分析图片", "描述图片", "图片中", "图中", "截图中的",
        ]
        has_multimodal_fb = any(kw in prompt_fallback for kw in multimodal_keywords_fb)
        attachments_fb = request.attachments or []
        if attachments_fb:
            has_multimodal_fb = True

        if has_multimodal_fb:
            final_path = RoutePath.MULTIMODAL
        else:
            # Multi-step batch / Volcano / Agent-related
            multi_step_keywords_fb = [
                "多步骤", "多步", "批处理", "批量", "自主", "设计", "规划", "执行计划",
                "自主执行", "架构", "方案", "调研", "流程", "编排", "工作流", "pipeline",
                "Volcano", "volcano", "分布式", "微服务", "部署", "发布",
            ]
            has_multi_step_fb = any(kw in prompt_fallback for kw in multi_step_keywords_fb)
            is_gateway_fb = (
                has_tools_fallback
                or req_type_fallback in ("code", "code_execution", "tool_call")
                or score.total >= hermes.complexity_threshold
                or has_multi_step_fb
            )
            correct_path_fallback = "gateway" if is_gateway_fb else "direct_local"
            try:
                final_path = RoutePath(correct_path_fallback)
            except ValueError:
                pass

    result = {
        "complexity_score": score.total,
        "complexity_level": score.level.value,
        "breakdown": score.breakdown,
        "recommended_path": final_path.value,
        "threshold": hermes.complexity_threshold,
        "skill_matched": skill["skill_name"] if skill else None,
        "memory_match": bool(memory),
        "memory_detail": {
            "path": memory["route_path"],
            "latency_ms": memory["latency_ms"],
        } if memory else None,
    }

    if llm_enhanced and llm_result:
        result["llm_enhanced"] = True
        result["llm_intent"] = llm_result.get("intent")
        result["llm_complexity"] = llm_result.get("complexity")
        result["llm_confidence"] = llm_result.get("confidence")
        result["llm_suggested_path"] = llm_result.get("suggested_path")
        result["llm_requires_local"] = llm_result.get("requires_local")
        result["llm_requires_tools"] = llm_result.get("requires_tools")
        result["llm_key_concepts"] = llm_result.get("key_concepts")

    return result


@app.get("/skills", summary="List learned routing skills")
async def get_skills():
    skills = hermes.memory.get_all_skills()
    return {
        "total": len(skills),
        "skills": [
            {
                "name": s["skill_name"],
                "pattern_type": s["pattern_type"],
                "pattern_detail": s["pattern_detail"],
                "recommended_path": s["recommended_path"],
                "recommended_model": s["recommended_model"],
                "confidence": round(s["confidence"], 3),
                "hit_count": s["hit_count"],
                "success_count": s["success_count"],
                "created_at": s["created_at"],
                "updated_at": s["updated_at"],
            }
            for s in skills
        ],
    }


@app.get("/memory/recent", summary="Recent routing history from memory")
async def get_recent_memory(limit: int = 20):
    records = hermes.memory.get_recent(limit=limit)
    return {
        "total": len(records),
        "records": [
            {
                "id": r["id"],
                "request_type": r["request_type"],
                "prompt_preview": r["prompt_preview"][:100],
                "route_path": r["route_path"],
                "selected_model": r["selected_model"],
                "complexity_score": r["complexity_score"],
                "success": bool(r["success"]),
                "latency_ms": r["latency_ms"],
                "skill_matched": r["skill_matched"],
                "created_at": r["created_at"],
            }
            for r in records
        ],
    }


@app.get("/memory/search", summary="Search routing memory by prompt")
async def search_memory(q: str, limit: int = 5):
    results = hermes.memory.search_similar(q, limit=limit)
    return {
        "query": q,
        "total": len(results),
        "results": [
            {
                "id": r["id"],
                "request_type": r["request_type"],
                "prompt_preview": r["prompt_preview"][:100],
                "route_path": r["route_path"],
                "selected_model": r["selected_model"],
                "success": bool(r["success"]),
                "latency_ms": r["latency_ms"],
                "created_at": r["created_at"],
            }
            for r in results
        ],
    }


@app.get("/evolution", summary="Evolution log")
async def get_evolution(limit: int = 20):
    return {
        "log": hermes.memory.get_evolution_log(limit=limit),
    }


# ══════════════════════════════════════════════════════════════════════════════
@app.post("/reset", summary="Reset Hermes learning state")
async def reset_hermes():
    hermes.reset()
    _stats["total_requests"] = 0
    _stats["success_requests"] = 0
    _stats["failed_requests"] = 0
    _stats["total_latency_ms"] = 0
    _stats["dispatch_history"] = []
    _register_known_models()
    return {"status": "reset", "message": "Hermes learning state reset"}


@app.get("/llm-enhancer", summary="LLM enhancer status and stats")
async def get_llm_enhancer():
    return llm_enhancer.get_stats()


class LLMEnhancerConfig(BaseModel):
    enabled: Opt[bool] = None
    model: Opt[str] = None
    timeout: Opt[int] = None
    min_complexity: Opt[int] = None


@app.post("/llm-enhancer/config", summary="Update LLM enhancer configuration")
async def update_llm_enhancer(config: LLMEnhancerConfig):
    if config.enabled is not None:
        llm_enhancer.enabled = config.enabled
    if config.model is not None:
        llm_enhancer.model = config.model
    if config.timeout is not None:
        llm_enhancer.timeout = config.timeout
    if config.min_complexity is not None:
        llm_enhancer.min_complexity = config.min_complexity
    return {"status": "updated", "config": llm_enhancer.get_stats()}


@app.get("/agent", summary="Hermes Agent status and skills")
async def get_agent_status():
    return hermes_agent.get_stats()


class AgentConfig(BaseModel):
    enabled: Opt[bool] = None
    exploration_rate: Opt[float] = None
    min_confidence: Opt[float] = None


@app.post("/agent/config", summary="Update Hermes Agent configuration")
async def update_agent_config(config: AgentConfig):
    if config.enabled is not None:
        hermes_agent.enabled = config.enabled
    if config.exploration_rate is not None:
        hermes_agent._exploration_rate = max(0.01, min(0.3, config.exploration_rate))
    if config.min_confidence is not None:
        hermes_agent._min_confidence = max(0.1, min(0.9, config.min_confidence))
    return {"status": "updated", "config": hermes_agent.get_stats()}


@app.post("/agent/bootstrap", summary="Re-bootstrap Agent skills from routing rules")
async def bootstrap_agent():
    count = hermes_agent.bootstrap_from_rules()
    return {"status": "bootstrapped", "skills_count": count}


@app.get("/agent/skills", summary="List Agent routing skills")
async def get_agent_skills():
    stats = hermes_agent.get_stats()
    return stats.get("skills", {})


@app.post("/agent/skills", summary="Register a new Agent skill")
async def register_agent_skill(skill_data: dict):
    from hermes.agent import RoutingSkill, SkillCondition, SkillAction, SkillStatus
    conditions = []
    for c in skill_data.get("conditions", []):
        conditions.append(SkillCondition(
            pattern_type=c["type"],
            pattern_value=c["value"],
            weight=c.get("weight", 1.0),
        ))
    action_data = skill_data.get("action", {})
    action = SkillAction(
        route_path=action_data.get("route_path", "gateway"),
        model_hint=action_data.get("model_hint"),
        fallback_path=action_data.get("fallback_path"),
    )
    skill = RoutingSkill(
        name=skill_data["name"],
        conditions=conditions,
        action=action,
        confidence=skill_data.get("confidence", 0.5),
        source=skill_data.get("source", "manual"),
        tags=skill_data.get("tags", []),
        created_at=time.time(),
        updated_at=time.time(),
    )
    hermes_agent.register_skill(skill)
    return {"status": "registered", "skill": skill.to_dict()}


@app.delete("/agent/skills/{skill_name}", summary="Unregister an Agent skill")
async def unregister_agent_skill(skill_name: str):
    if skill_name not in hermes_agent.skills:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")
    hermes_agent.unregister_skill(skill_name)
    return {"status": "unregistered", "skill_name": skill_name}


# ── Official Hermes Agent Endpoints ──────────────────────────────────────────

@app.get("/official-agent", summary="Official Hermes Agent status")
async def official_agent_status():
    stats = official_agent.get_stats()
    health = official_agent.detailed_health()
    return {
        "api_url": OFFICIAL_AGENT_URL,
        "available": stats.get("available", False),
        "health": health,
        "router_url": f"http://localhost:{HERMES_PORT}",
        "mode": stats.get("mode"),
        "mode_description": stats.get("mode_description", ""),
        "session_warmed_up": stats.get("session_warmed_up", False),
        "session_id": stats.get("session_id"),
    }


@app.post("/official-agent/route", summary="Route via Official Hermes Agent")
async def official_agent_route(request_data: dict):
    result = official_agent.route_via_agent(request_data)
    return result


@app.post("/official-agent/feedback", summary="Record feedback to Official Agent memory")
async def official_agent_feedback(feedback_data: dict):
    success = official_agent.record_feedback_via_memory(
        skill_name=feedback_data.get("skill_name"),
        route_path=feedback_data.get("route_path", ""),
        success=feedback_data.get("success", False),
        latency_ms=feedback_data.get("latency_ms", 0),
        request_summary=feedback_data.get("request_summary", ""),
    )
    return {"recorded": success}


@app.get("/official-agent/skills", summary="List Official Agent skills")
async def official_agent_skills():
    result = official_agent.get_skills()
    return result


@app.post("/official-agent/toggle", summary="Enable/disable Official Agent routing")
async def toggle_official_agent(config: dict):
    enabled = config.get("enabled", True)
    if enabled:
        hermes.official_agent = official_agent
    else:
        hermes.official_agent = None
    return {"official_agent_enabled": enabled}


@app.post("/official-agent/mode", summary="Switch routing mode")
async def switch_routing_mode(config: dict):
    """Switch between ollama_direct and hermes_agent routing modes.

    Request body: {"mode": "ollama_direct"} or {"mode": "hermes_agent"}

    Modes:
    - ollama_direct: Fast (~3.7s), stable, Ollama direct + Memory closed-loop
    - hermes_agent: Full framework (~5.0s), Hermes Agent + system_message + Agent cache
    """
    mode = config.get("mode", "")
    result = official_agent.set_mode(mode)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/official-agent/refresh-memory", summary="Force refresh Memory cache")
async def refresh_memory():
    """Force invalidate the Memory context cache so next request reads fresh data."""
    official_agent._memory_context_cached_at = 0
    return {"refreshed": True}


@app.get("/memory/content", summary="Get MEMORY.md content for Dashboard")
async def memory_content():
    """Return the raw content of MEMORY.md for the Dashboard Memory tab."""
    try:
        from hermes.official_agent_adapter import MEMORY_FILE
        if os.path.exists(MEMORY_FILE):
            content = open(MEMORY_FILE, "r", encoding="utf-8").read()
            stat = os.stat(MEMORY_FILE)
            return {"content": content, "path": MEMORY_FILE, "exists": True, "size": stat.st_size}
        return {"content": "", "path": MEMORY_FILE, "exists": False, "size": 0}
    except Exception as e:
        return {"content": "", "error": str(e), "exists": False, "size": 0}


# ══════════════════════════════════════════════════════════════════════════════
# Queue-based Dispatch API
# ══════════════════════════════════════════════════════════════════════════════

class QueueSubmitRequest(BaseModel):
    """Request model for queue-based submission."""
    appid: str = "default"
    type: str = "chat"
    prompt: str
    priority: int = 3
    model_hint: Opt[str] = None
    parameters: Opt[dict] = None
    context: Opt[list] = None
    tools: Opt[list] = None
    tool_choice: Opt[str] = None
    constraints: Opt[dict] = None


@app.post("/queue/submit", summary="Submit request to dispatch queue")
async def queue_submit(request: QueueSubmitRequest):
    """Submit a model request to the dispatch queue.

    The request will be processed asynchronously by the dispatch worker:
    1. Hermes Agent routes the request (Memory-driven)
    2. Dispatched to Gateway/Agent Chain/Ollama
    3. Result pushed to result queue
    4. Feedback recorded for Memory evolution

    Returns request_id for polling results.
    """
    import uuid
    request_id = str(uuid.uuid4())

    message = {
        "request_id": request_id,
        **request.dict(),
    }

    msg_queue.push(QUEUE_REQUESTS, message)

    return {
        "request_id": request_id,
        "status": "queued",
        "queue_size": msg_queue.size(QUEUE_REQUESTS),
        "message": "Request submitted to dispatch queue",
    }


@app.post("/queue/submit-sync", summary="Submit and wait for result")
async def queue_submit_sync(request: QueueSubmitRequest, timeout: float = 400.0):
    """Submit a request and wait for the result (synchronous mode).

    Uses threading.Event for cross-thread notification (dispatch workers
    are threads, asyncio.Event.set() is not thread-safe).
    Polls event.is_set() every 0.1s with periodic peek as safety net.
    """
    import uuid
    import asyncio
    request_id = str(uuid.uuid4())

    # Register pending sync result with threading.Event (thread-safe)
    event = threading.Event()
    holder = {"result": None}
    with _pending_sync_lock:
        _pending_sync[request_id] = {"event": event, "holder": holder}

    message = {
        "request_id": request_id,
        "_sync_mode": True,
        **request.dict(),
    }

    # Submit to queue
    msg_queue.push(QUEUE_REQUESTS, message)

    # Wait for event notification with short-poll + periodic peek fallback
    deadline = time.time() + timeout
    peek_interval = 10.0  # Peek every 10s as safety net
    last_peek = 0
    while time.time() < deadline:
        if event.is_set():
            with _pending_sync_lock:
                _pending_sync.pop(request_id, None)
            return holder["result"]

        # Periodic peek fallback (safety net for missed notifications)
        now = time.time()
        if now - last_peek >= peek_interval:
            last_peek = now
            peek_results = msg_queue.peek(QUEUE_RESULTS, limit=50)
            for i, r in enumerate(peek_results):
                if r.get("request_id") == request_id:
                    for _ in range(i + 1):
                        popped = msg_queue.pop(QUEUE_RESULTS)
                        if popped and popped.get("request_id") != request_id:
                            msg_queue.push(QUEUE_RESULTS, popped)
                    with _pending_sync_lock:
                        _pending_sync.pop(request_id, None)
                    return r

        await asyncio.sleep(0.1)

    # Timeout — clean up
    with _pending_sync_lock:
        _pending_sync.pop(request_id, None)

    return {
        "request_id": request_id,
        "status": "timeout",
        "error": {"code": "TIMEOUT", "message": f"Result not available within {timeout}s"},
    }


@app.get("/queue/results", summary="Get results from result queue")
async def queue_results(limit: int = 10, pop: bool = True):
    """Get results from the result queue.

    Args:
        limit: Maximum number of results to return
        pop: If True (default), remove results from queue after reading
    """
    results = []
    for _ in range(limit):
        if pop:
            msg = msg_queue.pop(QUEUE_RESULTS)
        else:
            msgs = msg_queue.peek(QUEUE_RESULTS, limit=1)
            msg = msgs[0] if msgs else None
        if msg is None:
            break
        results.append(msg)

    return {
        "count": len(results),
        "results": results,
        "remaining": msg_queue.size(QUEUE_RESULTS),
    }


@app.get("/queue/results/{request_id}", summary="Get specific result by request_id")
async def queue_result_by_id(request_id: str):
    """Find and return a specific result by request_id."""
    results = msg_queue.peek(QUEUE_RESULTS, limit=100)
    for r in results:
        if r.get("request_id") == request_id:
            return r
    return {"request_id": request_id, "status": "not_found"}


@app.get("/queue/status", summary="Queue status and dispatch worker stats")
async def queue_status():
    """Get queue sizes, worker stats, and backend health."""
    return {
        "queue_backend": msg_queue.health(),
        "worker": dispatch_worker.get_stats(),
        "routing_mode": "ollama_direct_with_memory" if official_agent.use_direct_ollama else "hermes_agent_system_message",
    }


@app.get("/queue/feedback", summary="Get recent feedback entries")
async def queue_feedback(limit: int = 20):
    """Get recent feedback entries from the feedback queue."""
    entries = msg_queue.peek(QUEUE_FEEDBACK, limit=limit)
    return {
        "count": len(entries),
        "feedback": entries,
    }


@app.post("/queue/flush", summary="Flush all queues")
async def queue_flush(queue_name: Opt[str] = None):
    """Flush messages from queues.

    Args:
        queue_name: Specific queue to flush (requests/results/feedback/all).
                   If not provided, flushes all queues.
    """
    name_map = {
        "requests": QUEUE_REQUESTS,
        "results": QUEUE_RESULTS,
        "feedback": QUEUE_FEEDBACK,
    }

    if queue_name and queue_name != "all":
        q = name_map.get(queue_name)
        if not q:
            raise HTTPException(status_code=400, detail=f"Unknown queue: {queue_name}")
        count = msg_queue.flush(q)
        return {"flushed": {queue_name: count}}

    flushed = {}
    for name, q in name_map.items():
        flushed[name] = msg_queue.flush(q)
    return {"flushed": flushed}


# ─── Service Management ───────────────────────────────────────────────
_managed_procs: Dict[str, Dict] = {}  # name → {process, pid, status, port}
_proc_lock = threading.Lock()

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _find_process_on_port(port: int) -> Optional[Dict]:
    """Find a process listening on the given port using psutil.

    Returns {"pid": int, "name": str} or None if no process found.
    This is used as a fallback when a service is not in _managed_procs
    (e.g., started externally) but we need to check if it's alive.
    """
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr.port == port and conn.status == "LISTEN":
                try:
                    proc = psutil.Process(conn.pid)
                    return {"pid": conn.pid, "name": proc.name()}
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
    except psutil.AccessDenied:
        # macOS: psutil.net_connections() may need root.
        # Fallback: use lsof to find the process.
        try:
            result = subprocess.run(
                ["lsof", "-i", f":{port}", "-sTCP:LISTEN", "-t", "-n"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0 and result.stdout.strip():
                pid = int(result.stdout.strip().split("\n")[0])
                try:
                    proc = psutil.Process(pid)
                    return {"pid": pid, "name": proc.name()}
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return {"pid": pid, "name": "unknown"}
        except Exception as e2:
            logger.debug("[PortDiscovery] lsof fallback failed for port %d: %s", port, e2)
    except Exception as e:
        logger.debug("[PortDiscovery] Failed to find process on port %d: %s", port, e)
    return None


def _find_openclaw_bin():
    """Find openclaw binary path."""
    candidates = [
        os.path.join(PROJECT_ROOT, "..", "OpenClaw", "openclaw", "openclaw.mjs"),
        os.path.join(os.path.expanduser("~"), ".openclaw", "openclaw.mjs"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    # Try npx
    return "npx"


class ServiceStartRequest(BaseModel):
    service: str  # officialGateway | ollama


class ServiceStopRequest(BaseModel):
    service: str


# ── Service Watchdog ──────────────────────────────────────────────
_watchdog_running = False
_watchdog_task = None

# Default config — overridden by watchdog_config.json if present
_DEFAULT_WATCHDOG_CONFIG = {
    "enabled": True,
    "interval_seconds": 30,
    "max_restart_attempts": 3,
    "restart_cooldown_seconds": 60,
    "startup_grace_seconds": 45,
    "shutdown_timeout_seconds": 5,
    "port_cleanup_wait_seconds": 3,
    "health_check_timeout_seconds": 5,  # short timeout: if agent doesn't respond in 5s, it's busy (not dead)
    "consecutive_failures_before_restart": 2,  # require 2 consecutive failures before restarting
}
_watchdog_config = dict(_DEFAULT_WATCHDOG_CONFIG)
_restart_history = {}  # service -> {"attempts": int, "last_attempt": float, "last_restart_time": float}
_service_start_times = {}  # service -> float (timestamp when last started/restarted)
_watchdog_health_cache = {}  # service -> {"healthy": bool, "status_code": int, "data": dict, "timestamp": float}


def _load_watchdog_config():
    """Load watchdog configuration from JSON file, falling back to defaults."""
    config_path = os.path.join(os.path.dirname(__file__), "watchdog_config.json")
    if not os.path.isfile(config_path):
        logger.info("[Watchdog] No config file found at %s, using defaults", config_path)
        return dict(_DEFAULT_WATCHDOG_CONFIG)
    try:
        import json as _json
        with open(config_path, "r") as f:
            file_cfg = _json.load(f)
        wd_cfg = file_cfg.get("watchdog", {})
        merged = dict(_DEFAULT_WATCHDOG_CONFIG)
        for key in _DEFAULT_WATCHDOG_CONFIG:
            if key in wd_cfg:
                merged[key] = wd_cfg[key]
        logger.info("[Watchdog] Loaded config from %s: %s", config_path, merged)
        return merged
    except Exception as e:
        logger.warning("[Watchdog] Failed to load config from %s: %s, using defaults", config_path, e)
        return dict(_DEFAULT_WATCHDOG_CONFIG)


def _get_openclaw_cmd():
    """Find the OpenClaw gateway executable."""
    openclaw_dir = os.path.expanduser("~/MyWork/OpenClaw/openclaw")
    openclaw_mjs = os.path.join(openclaw_dir, "openclaw.mjs")
    if os.path.isfile(openclaw_mjs):
        return ["node", openclaw_mjs, "gateway", "--port", "3005", "--force", "--allow-unconfigured"], openclaw_dir
    # Fallback to mock
    mock_mjs = os.path.join(PROJECT_ROOT, "openclaw-gw-mock.mjs")
    if os.path.isfile(mock_mjs):
        return ["node", mock_mjs], PROJECT_ROOT
    return None, None


SERVICE_CONFIGS = {}  # Populated lazily


def _get_service_configs():
    """Get service start configurations (lazy to allow env discovery at runtime)."""
    if SERVICE_CONFIGS:
        return SERVICE_CONFIGS
    openclaw_cmd, openclaw_cwd = _get_openclaw_cmd()
    SERVICE_CONFIGS["officialGateway"] = {
        "cmd": openclaw_cmd or ["node", os.path.join(PROJECT_ROOT, "openclaw-gw-mock.mjs")],
        "cwd": openclaw_cwd or PROJECT_ROOT,
        "port": 3005,
        "health_url": "http://127.0.0.1:3005/health",
    }
    SERVICE_CONFIGS["ollama"] = {
        "cmd": ["ollama", "serve"],
        "cwd": PROJECT_ROOT,
        "port": 11434,
        "health_url": "http://127.0.0.1:11434/api/tags",
    }
    # Hermes Agent (Official) — auto-discover from hermes-official-venv
    _hermes_venv = os.path.join(PROJECT_ROOT, "hermes-official-venv", "bin", "hermes")
    _hermes_cmd = [_hermes_venv, "gateway", "run", "--accept-hooks"] if os.path.isfile(_hermes_venv) else None
    SERVICE_CONFIGS["hermesAgent"] = {
        "cmd": _hermes_cmd,
        "cwd": PROJECT_ROOT,
        "port": 8642,
        "health_url": "http://127.0.0.1:8642/health",
        "startup_grace_override": 60,
        "env": {
            "API_SERVER_ENABLED": "true",
            "API_SERVER_KEY": OFFICIAL_AGENT_KEY or "hermes-local",
            "API_SERVER_PORT": "8642",
        },
    }
    # Override from config file if present
    config_path = os.path.join(os.path.dirname(__file__), "watchdog_config.json")
    if os.path.isfile(config_path):
        try:
            import json as _json
            with open(config_path, "r") as f:
                file_cfg = _json.load(f)
            svc_cfg = file_cfg.get("services", {})
            for name, overrides in svc_cfg.items():
                if name in SERVICE_CONFIGS:
                    for k, v in overrides.items():
                        if v is not None and k not in ("description",):
                            SERVICE_CONFIGS[name][k] = v
                elif overrides.get("cmd") and overrides.get("port"):
                    # New service from config
                    SERVICE_CONFIGS[name] = {
                        "cmd": overrides["cmd"],
                        "cwd": overrides.get("cwd") or PROJECT_ROOT,
                        "port": overrides["port"],
                        "health_url": overrides.get("health_url", f"http://127.0.0.1:{overrides['port']}/health"),
                    }
        except Exception as e:
            logger.warning("[Watchdog] Failed to load service configs from %s: %s", config_path, e)
    return SERVICE_CONFIGS


async def _check_service_health(name: str) -> bool:
    """Check if a service is healthy by hitting its health endpoint."""
    configs = _get_service_configs()
    cfg = configs.get(name)
    if not cfg:
        logger.debug("[HealthCheck] %s: 无配置, 跳过", name)
        return False
    health_url = cfg.get("health_url")
    if not health_url:
        logger.debug("[HealthCheck] %s: 无health_url, 跳过", name)
        return False
    timeout = _watchdog_config.get("health_check_timeout_seconds", 10)
    try:
        import httpx
        headers = {}
        if name == "hermesAgent" and OFFICIAL_AGENT_KEY:
            headers["Authorization"] = f"Bearer {OFFICIAL_AGENT_KEY}"
        start = time.time()
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(health_url, headers=headers)
        elapsed_ms = int((time.time() - start) * 1000)
        healthy = resp.status_code == 200
        logger.info("[HealthCheck] %s: %s status=%d latency=%dms url=%s",
                    name, "✓健康" if healthy else "✗异常", resp.status_code, elapsed_ms, health_url)
        return healthy
    except httpx.TimeoutException as e:
        logger.warning("[HealthCheck] %s: ✗超时 (%ds) url=%s error=%s", name, timeout, health_url, type(e).__name__)
        return False
    except httpx.ConnectError as e:
        logger.warning("[HealthCheck] %s: ✗连接失败 url=%s error=%s", name, health_url, str(e)[:80])
        return False
    except Exception as e:
        logger.warning("[HealthCheck] %s: ✗异常 url=%s error=%s: %s", name, health_url, type(e).__name__, str(e)[:80])
        return False


async def _watchdog_loop():
    """Background watchdog that periodically checks services and auto-restarts."""
    global _watchdog_running
    logger.info("[Watchdog] Starting service watchdog (interval=%ds, grace=%ds)",
                _watchdog_config["interval_seconds"], _watchdog_config["startup_grace_seconds"])
    check_round = 0
    _consecutive_failures = {}  # service -> int (consecutive health check failures)
    global _watchdog_health_cache
    _watchdog_health_cache = {}  # service -> {"healthy": bool, "status_code": int, "data": dict, "timestamp": float}
    while _watchdog_running:
        await asyncio.sleep(_watchdog_config["interval_seconds"])
        if not _watchdog_running:
            break
        check_round += 1
        configs = _get_service_configs()
        now = time.time()
        logger.debug("[Watchdog] === 第%d轮检查 === services=%s", check_round, list(configs.keys()))
        for name, cfg in configs.items():
            # Skip health check if service was recently restarted (grace period)
            start_time = _service_start_times.get(name, 0)
            grace = _watchdog_config.get("startup_grace_seconds", 45)
            # Use per-service grace override if set
            svc_grace = cfg.get("startup_grace_override") or grace
            if (now - start_time) < svc_grace:
                logger.debug("[Watchdog] %s: 启动宽限期内 (%.0fs剩余), 跳过",
                             name, svc_grace - (now - start_time))
                continue

            healthy = await _check_service_health(name)

            # Cache the health check result for /proxy/health to use.
            # If HTTP check failed but process is alive, mark as "busy" not "unhealthy".
            # This prevents Dashboard from showing false "unavailable" warnings.
            if healthy:
                _watchdog_health_cache[name] = {
                    "healthy": True,
                    "status_code": 200,
                    "data": {"status": "healthy"},
                    "timestamp": now,
                }
            else:
                # Check if process is still alive before marking as unhealthy
                # Priority: 1) _managed_procs  2) port-based discovery (psutil)
                alive_pid = None
                with _proc_lock:
                    existing = _managed_procs.get(name)
                    proc = existing.get("process") if existing else None
                    if proc and proc.poll() is None:
                        alive_pid = proc.pid

                if alive_pid is None:
                    # Fallback: discover process by listening port
                    port = cfg.get("port")
                    if port:
                        found = _find_process_on_port(port)
                        if found:
                            alive_pid = found["pid"]
                            logger.info("[Watchdog] %s: 端口发现进程(pid=%d)监听port=%d",
                                        name, alive_pid, port)

                if alive_pid is not None:
                    # Process alive but HTTP check failed — likely busy
                    logger.info("[Watchdog] %s: HTTP检查失败但进程存活(pid=%d), 缓存标记为busy",
                                name, alive_pid)
                    _watchdog_health_cache[name] = {
                        "healthy": True,
                        "status_code": 0,
                        "data": {"status": "busy", "pid": alive_pid},
                        "timestamp": now,
                    }
                    healthy = True  # Treat as healthy for restart logic
                else:
                    _watchdog_health_cache[name] = {
                        "healthy": False,
                        "status_code": 0,
                        "data": {"status": "unhealthy"},
                        "timestamp": now,
                    }

            if healthy:
                # Reset both restart attempts and consecutive failure count on healthy check
                fail_count = _consecutive_failures.get(name, 0)
                if fail_count > 0:
                    logger.info("[Watchdog] %s: 恢复健康, 重置连续失败计数 (was %d)",
                                name, fail_count)
                    _consecutive_failures[name] = 0
                if name in _restart_history and _restart_history[name]["attempts"] > 0:
                    logger.info("[Watchdog] %s: 恢复健康, 重置重启计数 (was %d attempts)",
                                name, _restart_history[name]["attempts"])
                    _restart_history[name]["attempts"] = 0
                continue

            # Service is unhealthy — track consecutive failures
            _consecutive_failures[name] = _consecutive_failures.get(name, 0) + 1
            required_failures = _watchdog_config.get("consecutive_failures_before_restart", 2)
            if _consecutive_failures[name] < required_failures:
                logger.warning("[Watchdog] %s: ✗不健康 (连续失败 %d/%d, 需 %d 次才重启)",
                               name, _consecutive_failures[name], required_failures, required_failures)
                continue

            # Required consecutive failures reached — check if we should restart
            hist = _restart_history.get(name, {"attempts": 0, "last_attempt": 0})

            if hist["attempts"] >= _watchdog_config["max_restart_attempts"]:
                logger.warning("[Watchdog] %s: max restart attempts (%d) reached, skipping",
                               name, _watchdog_config["max_restart_attempts"])
                continue

            if (now - hist["last_attempt"]) < _watchdog_config["restart_cooldown_seconds"]:
                continue  # Too soon to retry

            # CRITICAL: Check if the process is still running before restarting.
            # If the process is alive but health check fails, it's likely just busy
            # (e.g., hermesAgent processing a long gateway request). Don't kill it!
            alive_pid = None
            with _proc_lock:
                existing = _managed_procs.get(name)
                proc = existing.get("process") if existing else None
                if proc and proc.poll() is None:
                    alive_pid = proc.pid

            if alive_pid is None:
                # Fallback: check by port
                port = cfg.get("port")
                if port:
                    found = _find_process_on_port(port)
                    if found:
                        alive_pid = found["pid"]

            if alive_pid is not None:
                # Process is still running — health check failure is likely due to load,
                # not a crash. Log warning but do NOT restart.
                logger.warning("[Watchdog] %s: ✗不健康 but process alive (pid=%d), "
                               "likely busy — skipping restart (consecutive_failures=%d)",
                               name, alive_pid, _consecutive_failures[name])
                continue

            # Process is dead (crashed) — proceed with restart
            logger.info("[Watchdog] %s: process dead, attempting restart (attempt %d/%d)",
                        name, hist["attempts"] + 1, _watchdog_config["max_restart_attempts"])
            hist["attempts"] += 1
            hist["last_attempt"] = now
            _restart_history[name] = hist

            # Stop existing managed process gracefully (SIGTERM first, then SIGKILL)
            shutdown_timeout = _watchdog_config.get("shutdown_timeout_seconds", 5)
            with _proc_lock:
                existing = _managed_procs.get(name)
                if existing and existing.get("process"):
                    proc = existing["process"]
                    try:
                        proc.terminate()  # SIGTERM first — allow graceful shutdown
                        logger.info("[Watchdog] %s: sent SIGTERM to pid %d (timeout=%ds)",
                                    name, proc.pid, shutdown_timeout)
                        try:
                            proc.wait(timeout=shutdown_timeout)
                        except subprocess.TimeoutExpired:
                            proc.kill()  # SIGKILL if still running
                            logger.info("[Watchdog] %s: SIGTERM timeout (%ds), sent SIGKILL to pid %d",
                                        name, shutdown_timeout, proc.pid)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    # Close log file handles
                    for log_key in ("stdout_log", "stderr_log"):
                        fh = existing.get(log_key)
                        if fh and not fh.closed:
                            fh.close()
                    existing["process"] = None
                    existing["status"] = "stopped"

            # Also kill any leftover process on the port (graceful first)
            port = cfg.get("port")
            cleanup_wait = _watchdog_config.get("port_cleanup_wait_seconds", 3)
            if port:
                try:
                    import signal
                    pids_to_kill = []
                    for pid_str in os.popen(f"lsof -ti:{port} 2>/dev/null").read().strip().split():
                        if pid_str and int(pid_str) != os.getpid():
                            pids_to_kill.append(int(pid_str))
                    # SIGTERM first
                    for pid in pids_to_kill:
                        try:
                            os.kill(pid, signal.SIGTERM)
                            logger.info("[Watchdog] Sent SIGTERM to leftover pid %d on port %d", pid, port)
                        except ProcessLookupError:
                            pass
                    # Wait for graceful shutdown
                    await asyncio.sleep(cleanup_wait)
                    # SIGKILL any survivors
                    for pid in pids_to_kill:
                        try:
                            os.kill(pid, signal.SIGKILL)
                            logger.info("[Watchdog] Sent SIGKILL to surviving pid %d on port %d", pid, port)
                        except ProcessLookupError:
                            pass
                except Exception:
                    pass

            # Wait for port to be freed
            await asyncio.sleep(cleanup_wait)

            # Attempt to start the service
            try:
                req = ServiceStartRequest(service=name)
                await api_start_service(req)
                _service_start_times[name] = time.time()  # Record restart time for grace period
                logger.info("[Watchdog] %s: restart initiated (grace period=%ds)",
                            name, _watchdog_config.get("startup_grace_seconds", 45))
            except Exception as e:
                logger.error("[Watchdog] %s: restart failed: %s", name, e)


@app.post("/services/start", summary="Start a managed service")
async def api_start_service(req: ServiceStartRequest):
    name = req.service
    configs = _get_service_configs()
    with _proc_lock:
        if name in _managed_procs and _managed_procs[name].get("process"):
            return {"started": False, "message": f"{name} already running (pid={_managed_procs[name].get('pid')})"}

    if name not in configs:
        raise HTTPException(status_code=400, detail=f"Unknown service: {name}. Use: {list(configs.keys())}")

    cfg = configs[name]
    if not cfg.get("cmd"):
        raise HTTPException(status_code=400, detail=f"Service {name} has no start command configured (cmd is missing or auto-discovery failed)")
    try:
        # Redirect stdout/stderr to log files instead of PIPE to avoid buffer deadlock
        log_dir = os.path.join(PROJECT_ROOT, "logs")
        os.makedirs(log_dir, exist_ok=True)
        stdout_log = open(os.path.join(log_dir, f"{name}.stdout.log"), "a")
        stderr_log = open(os.path.join(log_dir, f"{name}.stderr.log"), "a")

        proc = subprocess.Popen(
            cfg["cmd"],
            cwd=cfg["cwd"],
            stdout=stdout_log,
            stderr=stderr_log,
            env={**os.environ, **cfg.get("env", {})},
        )
        with _proc_lock:
            _managed_procs[name] = {
                "process": proc,
                "pid": proc.pid,
                "status": "starting",
                "port": cfg["port"],
                "stdout_log": stdout_log,
                "stderr_log": stderr_log,
            }
            _service_start_times[name] = time.time()  # Record start time for grace period

        def _watch():
            proc.wait()
            rc = proc.returncode
            with _proc_lock:
                if name in _managed_procs:
                    _managed_procs[name]["status"] = "stopped"
                    _managed_procs[name]["process"] = None
                    _managed_procs[name]["exit_code"] = rc
                    # Close log file handles
                    for log_key in ("stdout_log", "stderr_log"):
                        fh = _managed_procs[name].get(log_key)
                        if fh and not fh.closed:
                            fh.close()
            logger.info(f"[Watchdog] {name} exited with code {rc}")

        threading.Thread(target=_watch, daemon=True).start()
        logger.info(f"Started {name}: pid={proc.pid}, port={cfg['port']}")
        return {"started": True, "pid": proc.pid, "port": cfg["port"], "message": f"{name} starting on port {cfg['port']}..."}

    except Exception as e:
        logger.error(f"Failed to start {name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/services/stop", summary="Stop a managed service")
async def api_stop_service(req: ServiceStopRequest):
    name = req.service
    with _proc_lock:
        info = _managed_procs.get(name)
        if not info or not info.get("process"):
            return {"stopped": False, "message": f"{name} not managed by Hermes"}

        proc = info["process"]
        pid = info.get("pid")
        proc.terminate()
        info["status"] = "stopping"
        # Close log file handles
        for log_key in ("stdout_log", "stderr_log"):
            fh = info.get(log_key)
            if fh and not fh.closed:
                fh.close()

    logger.info(f"[Watchdog] Stopped {name}: pid={pid}")
    return {"stopped": True, "pid": pid, "message": f"{name} stopping..."}


@app.get("/services/status", summary="Get all managed services status")
async def api_services_status():
    result = {}
    with _proc_lock:
        for name, info in _managed_procs.items():
            alive = info.get("process") is not None and info["process"].poll() is None
            result[name] = {
                "status": "running" if alive else info.get("status", "stopped"),
                "pid": info.get("pid") if alive else None,
                "port": info.get("port"),
                "exit_code": info.get("exit_code") if not alive else None,
            }
    # Include watchdog status
    result["_watchdog"] = {
        "enabled": _watchdog_running,
        "interval_seconds": _watchdog_config["interval_seconds"],
        "max_restart_attempts": _watchdog_config["max_restart_attempts"],
        "startup_grace_seconds": _watchdog_config.get("startup_grace_seconds", 45),
        "shutdown_timeout_seconds": _watchdog_config.get("shutdown_timeout_seconds", 5),
        "port_cleanup_wait_seconds": _watchdog_config.get("port_cleanup_wait_seconds", 3),
        "restart_history": {k: v for k, v in _restart_history.items() if v["attempts"] > 0},
        "service_start_times": {k: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
                                for k, v in _service_start_times.items()},
        "config_source": "watchdog_config.json" if os.path.isfile(
            os.path.join(os.path.dirname(__file__), "watchdog_config.json")) else "defaults",
    }
    return result


@app.post("/watchdog/start", summary="Start the service watchdog")
async def api_watchdog_start():
    global _watchdog_running, _watchdog_task
    if _watchdog_running:
        return {"started": False, "message": "Watchdog already running"}
    _watchdog_running = True
    _watchdog_task = asyncio.create_task(_watchdog_loop())
    logger.info("[Watchdog] Started by API request")
    return {"started": True, "message": "Watchdog started"}


@app.post("/watchdog/stop", summary="Stop the service watchdog")
async def api_watchdog_stop():
    global _watchdog_running
    if not _watchdog_running:
        return {"stopped": False, "message": "Watchdog not running"}
    _watchdog_running = False
    logger.info("[Watchdog] Stopped by API request")
    return {"stopped": True, "message": "Watchdog stopped"}


@app.get("/watchdog/status", summary="Get watchdog status and configuration")
async def api_watchdog_status():
    return {
        "running": _watchdog_running,
        "config": _watchdog_config,
        "restart_history": _restart_history,
        "service_start_times": {k: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
                                for k, v in _service_start_times.items()},
        "monitored_services": list(_get_service_configs().keys()),
    }


@app.post("/watchdog/config", summary="Update watchdog configuration")
async def api_watchdog_config_update(enabled: bool = True, interval_seconds: int = 30,
                                      max_restart_attempts: int = 3, restart_cooldown_seconds: int = 60,
                                      startup_grace_seconds: int = 45, shutdown_timeout_seconds: int = 5,
                                      port_cleanup_wait_seconds: int = 3,
                                      health_check_timeout_seconds: int = 10,
                                      consecutive_failures_before_restart: int = 2):
    _watchdog_config["enabled"] = enabled
    _watchdog_config["interval_seconds"] = max(10, interval_seconds)
    _watchdog_config["max_restart_attempts"] = max(1, max_restart_attempts)
    _watchdog_config["restart_cooldown_seconds"] = max(10, restart_cooldown_seconds)
    _watchdog_config["startup_grace_seconds"] = max(15, startup_grace_seconds)
    _watchdog_config["shutdown_timeout_seconds"] = max(1, shutdown_timeout_seconds)
    _watchdog_config["port_cleanup_wait_seconds"] = max(1, port_cleanup_wait_seconds)
    _watchdog_config["health_check_timeout_seconds"] = max(3, health_check_timeout_seconds)
    _watchdog_config["consecutive_failures_before_restart"] = max(1, consecutive_failures_before_restart)
    # Persist to config file
    try:
        import json as _json
        config_path = os.path.join(os.path.dirname(__file__), "watchdog_config.json")
        file_cfg = {}
        if os.path.isfile(config_path):
            with open(config_path, "r") as f:
                file_cfg = _json.load(f)
        file_cfg["watchdog"] = dict(_watchdog_config)
        with open(config_path, "w") as f:
            _json.dump(file_cfg, f, indent=2)
        logger.info("[Watchdog] Config updated and saved to %s", config_path)
    except Exception as e:
        logger.warning("[Watchdog] Failed to persist config: %s", e)
    return {"updated": True, "config": _watchdog_config}


@app.get("/proxy/health", summary="Proxy health check for all services (bypasses CORS)")
async def api_proxy_health():
    """Return health status of all services using Watchdog's cached results.
    This endpoint NEVER makes outbound HTTP requests — it reads the latest
    health check results from the Watchdog background loop, so it always
    responds instantly regardless of service load.
    """
    now = time.time()
    result = {}

    # Hermes self-check: we're running if this handler executes
    result["hermes"] = {"healthy": True, "status_code": 200, "data": {"status": "healthy"}}

    # Read from Watchdog's health cache (populated by _watchdog_loop)
    # If no cache yet (just started), check process liveness as fallback
    cache = _watchdog_health_cache
    configs = _get_service_configs()

    for name in ["hermesAgent", "officialGateway", "ollama"]:
        cached = cache.get(name)
        if cached and (now - cached.get("timestamp", 0)) < 60:
            # Fresh cache (< 60s old) — use it directly
            result[name] = {
                "healthy": cached["healthy"],
                "status_code": cached.get("status_code", 200 if cached["healthy"] else 0),
                "data": cached.get("data", {}),
            }
        else:
            # No cache or stale — check process liveness as fallback
            # Priority: 1) _managed_procs  2) port-based discovery (psutil)
            alive_pid = None
            with _proc_lock:
                existing = _managed_procs.get(name)
                proc = existing.get("process") if existing else None
                if proc and proc.poll() is None:
                    alive_pid = proc.pid

            if alive_pid is None:
                # Fallback: discover process by listening port
                cfg = configs.get(name, {})
                port = cfg.get("port")
                if port:
                    found = _find_process_on_port(port)
                    if found:
                        alive_pid = found["pid"]

            if alive_pid is not None:
                result[name] = {
                    "healthy": True,
                    "status_code": 0,
                    "data": {"status": "busy", "pid": alive_pid},
                }
            else:
                result[name] = {
                    "healthy": False,
                    "status_code": 0,
                    "data": {"status": "unknown"},
                }

    # Summary log
    unhealthy = [n for n, d in result.items() if not d.get("healthy")]
    if unhealthy:
        logger.warning("[ProxyHealth] 检查完成: %d/%d 异常 [%s]",
                       len(unhealthy), len(result), ", ".join(unhealthy))
    else:
        logger.debug("[ProxyHealth] 检查完成: 全部 %d 个服务健康", len(result))

    return result


@app.get("/proxy/ollama-ps", summary="Proxy Ollama /api/ps (bypasses CORS)")
async def api_proxy_ollama_ps():
    import urllib.request
    import json as _json
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/ps")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return _json.loads(resp.read())
    except Exception as e:
        return {"models": [], "error": str(e)}


@app.get("/proxy/agent-health", summary="Hermes Agent 8642 detailed health and evolution status")
async def api_proxy_agent_health():
    """Detailed health check for Hermes Agent (8642) including:
    - Agent availability and health (from Watchdog cache, never blocks)
    - Routing mode (ollama_direct vs hermes_agent)
    - Memory context status (MEMORY.md loaded)
    - Feedback queue size
    - Plugin status (intelligent-routing)
    - Evolution stats (rules learned, feedback recorded)
    """
    stats = official_agent.get_stats()

    # Use Watchdog cache for agent health — never make direct HTTP calls
    # that could block when the agent is busy processing long requests.
    now = time.time()
    cached = _watchdog_health_cache.get("hermesAgent")
    if cached and (now - cached.get("timestamp", 0)) < 60:
        agent_healthy = cached["healthy"]
        agent_health_data = cached.get("data", {})
    else:
        # Fallback: check process liveness by port
        found = _find_process_on_port(8642)
        if found:
            agent_healthy = True
            agent_health_data = {"status": "busy", "pid": found["pid"]}
        else:
            agent_healthy = False
            agent_health_data = {"status": "unknown"}

    # Plugin check — use cached stats instead of making HTTP call
    # Only attempt HTTP check if agent is healthy and not busy
    plugin_status = {"name": "intelligent-routing", "active": False, "tools": []}
    if agent_healthy and agent_health_data.get("status") == "healthy":
        try:
            import httpx as _httpx
            headers = {}
            if OFFICIAL_AGENT_KEY:
                headers["Authorization"] = f"Bearer {OFFICIAL_AGENT_KEY}"
            async with _httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{OFFICIAL_AGENT_URL}/v1/models", headers=headers)
                if resp.status_code == 200:
                    plugin_status["active"] = True
        except Exception:
            pass  # Agent busy — plugin status unknown, not an error
    elif agent_healthy and agent_health_data.get("status") == "busy":
        # Agent is busy — assume plugin is still active (it was before)
        plugin_status["active"] = True
        plugin_status["note"] = "assumed active (agent busy, HTTP check skipped)"

    # Memory file status
    from hermes.official_agent_adapter import MEMORY_FILE
    memory_status = {
        "file_exists": os.path.exists(MEMORY_FILE),
        "file_size": 0,
        "last_modified": None,
    }
    if os.path.exists(MEMORY_FILE):
        try:
            stat = os.stat(MEMORY_FILE)
            memory_status["file_size"] = stat.st_size
            memory_status["last_modified"] = datetime.fromtimestamp(stat.st_mtime).isoformat()
        except Exception:
            pass

    return {
        "agent": {
            "url": OFFICIAL_AGENT_URL,
            "healthy": agent_healthy,
            "health": agent_health_data,
            "routing_mode": stats.get("mode", "unknown"),
            "use_direct_ollama": stats.get("use_direct_ollama", True),
            "session_id": stats.get("session_id"),
            "feedback_queue_size": stats.get("feedback_queue_size", 0),
        },
        "plugin": plugin_status,
        "memory": memory_status,
        "evolution": {
            "memory_context_loaded": bool(official_agent._load_memory_context()),
            "rules_parsed": len(official_agent._parse_routing_rules_from_memory()),
        },
    }


@app.post("/queue/test-loop", summary="End-to-end test: queue request → route → execute → result queue")
async def queue_test_loop(request: HermesDispatchRequest):
    """Submit a request through the queue pipeline and wait for the result.

    Full test loop:
    1. Push request to request queue
    2. DispatchWorker picks it up → routes via Ollama direct
    3. Dispatches to downstream (Ollama/OfficialGW/multimodal)
    4. Result pushed to result queue
    5. Feedback recorded to MEMORY.md (evolution closed-loop)
    6. Return the complete result with trace

    This tests the entire pipeline: Queue → Router → Executor → Result → Feedback → MEMORY.md
    """
    request_id = str(uuid.uuid4())

    # Flush stale results to avoid scanning old entries
    flushed = msg_queue.flush(QUEUE_RESULTS)
    if flushed > 0:
        logger.info(f"Test-loop flushed {flushed} stale results from queue")

    # Enqueue the request
    queue_msg = {
        "request_id": request_id,
        "appid": request.appid,
        "type": request.type,
        "prompt": request.prompt,
        "priority": request.priority,
        "model_hint": request.model_hint,
        "parameters": request.parameters,
        "context": request.context,
        "tools": request.tools,
        "tool_choice": request.tool_choice,
        "constraints": request.constraints,
        "timeout_ms": request.timeout_ms,
        "route_mode": request.route_mode,
        "agent_id": request.agent_id,
        "attachments": request.attachments,
    }
    msg_queue.push(QUEUE_REQUESTS, queue_msg)

    # Wait for result (poll result queue with timeout)
    max_wait = (request.timeout_ms or 30000) / 1000.0
    deadline = time.time() + max_wait
    result = None

    while time.time() < deadline:
        # Check result queue for our request_id (scan all entries)
        queue_size = msg_queue.size(QUEUE_RESULTS)
        results = msg_queue.peek(QUEUE_RESULTS, limit=max(queue_size, 50))
        for r in results:
            if isinstance(r, dict) and r.get("request_id") == request_id:
                result = r
                break
        if result:
            break
        time.sleep(0.3)

    if not result:
        return {
            "request_id": request_id,
            "status": "timeout",
            "message": f"Result not received within {max_wait}s",
            "queue_sizes": {
                "requests": msg_queue.size(QUEUE_REQUESTS),
                "results": msg_queue.size(QUEUE_RESULTS),
                "feedback": msg_queue.size(QUEUE_FEEDBACK),
            },
        }

    return {
        "request_id": request_id,
        "status": "completed",
        "result": result,
        "queue_sizes": {
            "requests": msg_queue.size(QUEUE_REQUESTS),
            "results": msg_queue.size(QUEUE_RESULTS),
            "feedback": msg_queue.size(QUEUE_FEEDBACK),
        },
    }


if __name__ == "__main__":
    import uvicorn
    import logging.config

    # Custom StreamHandler that flushes after every emit — ensures worker thread
    # log output appears immediately instead of being buffered by uvicorn's event loop.
    class _FlushingStreamHandler(logging.StreamHandler):
        def emit(self, record):
            super().emit(record)
            self.flush()

    # Build log config that preserves hermes module loggers while using uvicorn's default format
    _log_level = os.getenv("HERMES_LOG_LEVEL", "DEBUG").upper()

    # Ensure logs directory exists
    _log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _log_file = os.path.join(_log_dir, "hermes_server.log")

    _log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
                "datefmt": "%H:%M:%S",
            },
            "file": {
                "format": "%(asctime)s.%(msecs)03d %(levelname)-5s [%(name)s] %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "()": _FlushingStreamHandler,
                "formatter": "default",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "file",
                "filename": _log_file,
                "maxBytes": 10485760,  # 10MB
                "backupCount": 3,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            # Hermes modules — all use the same DEBUG level for detailed diagnostics
            "hermes": {"level": _log_level, "handlers": ["default", "file"], "propagate": False},
            "hermes.server": {"level": _log_level, "handlers": ["default", "file"], "propagate": False},
            "hermes.official_agent_adapter": {"level": _log_level, "handlers": ["default", "file"], "propagate": False},
            "hermes.dispatch_worker": {"level": _log_level, "handlers": ["default", "file"], "propagate": False},
            "hermes.router": {"level": _log_level, "handlers": ["default", "file"], "propagate": False},
            "hermes.agent": {"level": _log_level, "handlers": ["default", "file"], "propagate": False},
            "hermes.message_queue": {"level": _log_level, "handlers": ["default", "file"], "propagate": False},
            # Suppress noisy libraries
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
            # Uvicorn loggers
            "uvicorn": {"level": "INFO", "handlers": ["default"], "propagate": False},
            "uvicorn.error": {"level": "INFO", "handlers": ["default"], "propagate": False},
            "uvicorn.access": {"level": "INFO", "handlers": ["default"], "propagate": False},
        },
        "root": {"level": "WARNING", "handlers": ["default"]},
    }
    uvicorn.run(
        "hermes.server:app",
        host="0.0.0.0",
        port=HERMES_PORT,
        log_config=_log_config,
        log_level="info",
        reload=False,
    )
