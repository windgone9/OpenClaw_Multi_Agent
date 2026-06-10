from dotenv import load_dotenv
load_dotenv()

import logging
import os
import subprocess
import threading
import time
import asyncio
import psutil
from contextlib import asynccontextmanager
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
USE_DIRECT_OLLAMA = os.getenv("USE_DIRECT_OLLAMA", "false").lower() == "true"

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
DISPATCH_WORKERS = int(os.getenv("DISPATCH_WORKERS", "4"))

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

# Pending sync results: request_id → threading.Event + result holder
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
    # Auto-start watchdog
    _watchdog_running = True
    _watchdog_task = asyncio.create_task(_watchdog_loop())
    logger.info("  Service watchdog started (interval=%ds)", _watchdog_config["interval_seconds"])
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
            return {"content": content, "path": MEMORY_FILE}
        return {"content": "", "path": MEMORY_FILE}
    except Exception as e:
        return {"content": "", "error": str(e)}


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
async def queue_submit_sync(request: QueueSubmitRequest, timeout: float = 30.0):
    """Submit a request and wait for the result (synchronous mode).

    Uses event-based waiting instead of polling for efficiency.
    The dispatch worker will set the event when the result is ready.
    """
    import uuid
    request_id = str(uuid.uuid4())

    # Register pending sync result
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

    # Wait for result (event is set by the result-checking background task)
    # We still poll the result queue, but only consume results matching our request_id
    deadline = time.time() + timeout
    while time.time() < deadline:
        # Check if our result has been delivered via the pending sync mechanism
        if event.is_set():
            with _pending_sync_lock:
                _pending_sync.pop(request_id, None)
            return holder["result"]

        # Also check the result queue for our request_id
        # Peek and search without consuming unrelated results
        peek_results = msg_queue.peek(QUEUE_RESULTS, limit=50)
        for i, r in enumerate(peek_results):
            if r.get("request_id") == request_id:
                # Found our result — drain up to this point
                for _ in range(i + 1):
                    popped = msg_queue.pop(QUEUE_RESULTS)
                    if popped and popped.get("request_id") != request_id:
                        # Put unrelated results back
                        msg_queue.push(QUEUE_RESULTS, popped)
                with _pending_sync_lock:
                    _pending_sync.pop(request_id, None)
                return r

        time.sleep(0.3)

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
_watchdog_config = {
    "enabled": True,
    "interval_seconds": 30,
    "max_restart_attempts": 3,
    "restart_cooldown_seconds": 60,
}
_restart_history = {}  # service -> {"attempts": int, "last_attempt": float}


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
    return SERVICE_CONFIGS


async def _check_service_health(name: str) -> bool:
    """Check if a service is healthy by hitting its health endpoint."""
    configs = _get_service_configs()
    cfg = configs.get(name)
    if not cfg:
        return False
    health_url = cfg.get("health_url")
    if not health_url:
        return False
    try:
        import urllib.request
        req = urllib.request.Request(health_url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


async def _watchdog_loop():
    """Background watchdog that periodically checks services and auto-restarts."""
    global _watchdog_running
    logger.info("[Watchdog] Starting service watchdog (interval=%ds)", _watchdog_config["interval_seconds"])
    while _watchdog_running:
        await asyncio.sleep(_watchdog_config["interval_seconds"])
        if not _watchdog_running:
            break
        configs = _get_service_configs()
        for name, cfg in configs.items():
            healthy = await _check_service_health(name)
            if healthy:
                # Reset restart attempts on healthy check
                if name in _restart_history:
                    _restart_history[name]["attempts"] = 0
                continue

            # Service is down — check if we should restart
            now = time.time()
            hist = _restart_history.get(name, {"attempts": 0, "last_attempt": 0})

            if hist["attempts"] >= _watchdog_config["max_restart_attempts"]:
                logger.warning("[Watchdog] %s: max restart attempts (%d) reached, skipping",
                               name, _watchdog_config["max_restart_attempts"])
                continue

            if (now - hist["last_attempt"]) < _watchdog_config["restart_cooldown_seconds"]:
                continue  # Too soon to retry

            logger.info("[Watchdog] %s: service down, attempting restart (attempt %d/%d)",
                        name, hist["attempts"] + 1, _watchdog_config["max_restart_attempts"])
            hist["attempts"] += 1
            hist["last_attempt"] = now
            _restart_history[name] = hist

            # Stop existing managed process if any
            with _proc_lock:
                existing = _managed_procs.get(name)
                if existing and existing.get("process"):
                    try:
                        existing["process"].kill()  # Use SIGKILL for faster cleanup
                    except Exception:
                        pass
                    # Close log file handles
                    for log_key in ("stdout_log", "stderr_log"):
                        fh = existing.get(log_key)
                        if fh and not fh.closed:
                            fh.close()
                    existing["process"] = None
                    existing["status"] = "stopped"

            # Also kill any leftover process on the port
            port = cfg.get("port")
            if port:
                try:
                    import signal
                    for pid_str in os.popen(f"lsof -ti:{port} 2>/dev/null").read().strip().split():
                        if pid_str and int(pid_str) != os.getpid():
                            os.kill(int(pid_str), signal.SIGKILL)
                            logger.info("[Watchdog] Killed leftover pid %s on port %d", pid_str, port)
                except Exception:
                    pass

            # Wait for port to be freed
            await asyncio.sleep(2)

            # Attempt to start the service
            try:
                req = ServiceStartRequest(service=name)
                await api_start_service(req)
                logger.info("[Watchdog] %s: restart initiated", name)
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
            env={**os.environ},
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
        "restart_history": {k: v for k, v in _restart_history.items() if v["attempts"] > 0},
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
        "monitored_services": list(_get_service_configs().keys()),
    }


@app.post("/watchdog/config", summary="Update watchdog configuration")
async def api_watchdog_config_update(enabled: bool = True, interval_seconds: int = 30,
                                      max_restart_attempts: int = 3, restart_cooldown_seconds: int = 60):
    _watchdog_config["enabled"] = enabled
    _watchdog_config["interval_seconds"] = max(10, interval_seconds)
    _watchdog_config["max_restart_attempts"] = max(1, max_restart_attempts)
    _watchdog_config["restart_cooldown_seconds"] = max(10, restart_cooldown_seconds)
    return {"updated": True, "config": _watchdog_config}


@app.get("/proxy/health", summary="Proxy health check for all services (bypasses CORS)")
async def api_proxy_health():
    import urllib.request
    import json as _json
    services = {
        "hermes": "http://127.0.0.1:8082/health",
        "officialGateway": "http://127.0.0.1:3005/health",
        "ollama": "http://127.0.0.1:11434/api/tags",
    }
    result = {}
    # Hermes self-check: we're running if this handler executes
    result["hermes"] = {"healthy": True, "status_code": 200, "data": {"status": "healthy"}}
    for name, url in services.items():
        if name == "hermes":
            continue  # Already checked above
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
                result[name] = {"healthy": resp.status == 200, "status_code": resp.status, "data": data}
        except Exception as e:
            result[name] = {"healthy": False, "error": str(e)}
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "hermes.server:app",
        host="0.0.0.0",
        port=HERMES_PORT,
        log_level="info",
        reload=False,
    )
