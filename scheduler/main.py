import asyncio
import logging
import os
import subprocess
import time
import psutil
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from starlette.staticfiles import StaticFiles

from scheduler.models.request import DispatchRequest, RequestType, Priority
from scheduler.models.response import DispatchResponse, DispatchStatus
from scheduler.models.model_config import ModelEndpoint, ModelType, ModelProvider, ModelRegistry
from scheduler.agents.main_agent import MainDispatcherAgent
from scheduler.hooks.hook_manager import HookManager
from scheduler.hooks.pre_hook import (
    RequestValidationHook,
    ConstraintEnrichmentHook,
    RateLimitHook,
    RequestLoggingHook,
)
from scheduler.hooks.post_hook import (
    ResponseSanitizationHook,
    CostCalculationHook,
    ResponseLoggingHook,
    RetryDecisionHook,
)
from scheduler.config.settings import get_settings

logger = logging.getLogger(__name__)

registry = ModelRegistry()
dispatcher: Optional[MainDispatcherAgent] = None
bridge_process: Optional[subprocess.Popen] = None
gateway_process: Optional[subprocess.Popen] = None
_stats: Dict = {
    "total_requests": 0,
    "success_requests": 0,
    "failed_requests": 0,
    "total_latency_ms": 0,
    "dispatch_history": [],
}
MAX_HISTORY = 50


def _init_default_models():
    registry.register(ModelEndpoint(
        name="ollama-qwen2.5",
        display_name="Ollama Qwen2.5 3B (Local)",
        model_type=ModelType.LOCAL,
        provider=ModelProvider.OLLAMA,
        base_url="http://localhost:11434/v1",
        model_id="qwen2.5:3b",
        max_context_length=32768,
        supports_streaming=True,
        supports_tools=False,
        cost_per_1k_input_tokens=0.0,
        cost_per_1k_output_tokens=0.0,
        priority=1,
        weight=3,
        max_concurrent=5,
        tags=["chat", "completion"],
    ))
    registry.register(ModelEndpoint(
        name="kimi-k2.6",
        display_name="Kimi K2.6 (Cloud)",
        model_type=ModelType.CLOUD,
        provider=ModelProvider.MOONSHOT,
        base_url="https://api.moonshot.cn/v1",
        api_key=os.getenv("MOONSHOT_API_KEY", ""),
        model_id="kimi-k2.6",
        max_context_length=262144,
        supports_streaming=True,
        supports_tools=True,
        cost_per_1k_input_tokens=0.76,
        cost_per_1k_output_tokens=3.2,
        priority=2,
        weight=2,
        max_concurrent=15,
        tags=["chat", "completion", "tool_call"],
    ))
    registry.register(ModelEndpoint(
        name="deepseek-chat",
        display_name="DeepSeek Chat (Cloud)",
        model_type=ModelType.CLOUD,
        provider=ModelProvider.DEEPSEEK,
        base_url="https://api.deepseek.com/v1",
        api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        model_id="deepseek-chat",
        max_context_length=64000,
        supports_streaming=True,
        supports_tools=False,
        cost_per_1k_input_tokens=0.00014,
        cost_per_1k_output_tokens=0.00028,
        priority=3,
        weight=3,
        max_concurrent=20,
        tags=["chat", "completion"],
    ))


def _init_hooks() -> HookManager:
    settings = get_settings()
    hook_manager = HookManager()
    hook_manager.register_pre_hook(RateLimitHook(
        max_requests_per_minute=settings.rate_limit_rpm,
        max_requests_per_appid=settings.rate_limit_per_appid,
    ))
    hook_manager.register_pre_hook(RequestValidationHook())
    hook_manager.register_pre_hook(ConstraintEnrichmentHook())
    hook_manager.register_pre_hook(RequestLoggingHook())
    hook_manager.register_post_hook(ResponseSanitizationHook())
    hook_manager.register_post_hook(CostCalculationHook())
    hook_manager.register_post_hook(RetryDecisionHook(max_retries=settings.max_retries))
    hook_manager.register_post_hook(ResponseLoggingHook())
    return hook_manager


def _start_bridge() -> Optional[subprocess.Popen]:
    import os
    bridge_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bridge", "orchestrator.mjs")
    if not os.path.exists(bridge_path):
        logger.warning(f"OpenClaw Bridge not found at {bridge_path}, skipping bridge startup")
        return None
    try:
        env = os.environ.copy()
        env["USE_OPENCLAW_GATEWAY"] = "true"
        proc = subprocess.Popen(
            ["node", bridge_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        logger.info(f"OpenClaw Bridge started (PID: {proc.pid}, USE_OPENCLAW_GATEWAY=true)")
        return proc
    except Exception as e:
        logger.warning(f"Failed to start OpenClaw Bridge: {e}")
        return None


def _start_gateway() -> Optional[subprocess.Popen]:
    import os
    gateway_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gateway", "gateway.mjs")
    if not os.path.exists(gateway_path):
        logger.warning(f"OpenClaw Gateway not found at {gateway_path}, skipping gateway startup")
        return None
    try:
        proc = subprocess.Popen(
            ["node", gateway_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        logger.info(f"OpenClaw Gateway started (PID: {proc.pid})")
        return proc
    except Exception as e:
        logger.warning(f"Failed to start OpenClaw Gateway: {e}")
        return None


def _stop_bridge(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
        logger.info("OpenClaw Bridge stopped")
    except Exception as e:
        logger.warning(f"Failed to stop OpenClaw Bridge gracefully: {e}")
        try:
            proc.kill()
        except Exception:
            pass


def _stop_gateway(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
        logger.info("OpenClaw Gateway stopped")
    except Exception as e:
        logger.warning(f"Failed to stop OpenClaw Gateway gracefully: {e}")
        try:
            proc.kill()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global dispatcher, bridge_process, gateway_process
    settings = get_settings()
    _init_default_models()
    hook_manager = _init_hooks()

    gateway_process = _start_gateway()
    import asyncio
    await asyncio.sleep(1)

    bridge_process = _start_bridge()
    await asyncio.sleep(1)

    bridge_url = settings.openclaw_gateway_url.replace(":3000", ":3001")
    dispatcher = MainDispatcherAgent(
        registry, hook_manager,
        use_openclaw=True,
        bridge_url=bridge_url,
    )
    logger.info(f"OpenClaw Model Scheduler initialized (bridge_url={bridge_url})")
    yield
    if dispatcher:
        await dispatcher.close()
    _stop_bridge(bridge_process)
    _stop_gateway(gateway_process)
    logger.info("OpenClaw Model Scheduler shutdown")


app = FastAPI(
    title="OpenClaw Model Scheduler",
    description="AI Model Dispatch & Scheduling Service based on OpenClaw",
    version="2026.4.27",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/dispatch", response_model=DispatchResponse, summary="Dispatch AI request to appropriate model")
async def dispatch_request(request: DispatchRequest):
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    response = await dispatcher.dispatch(request)
    _stats["total_requests"] += 1
    if response.status == DispatchStatus.SUCCESS:
        _stats["success_requests"] += 1
    else:
        _stats["failed_requests"] += 1
    if response.total_latency_ms:
        _stats["total_latency_ms"] += response.total_latency_ms
    history_entry = response.dict()
    history_entry["_ts"] = time.time()
    _stats["dispatch_history"].append(history_entry)
    if len(_stats["dispatch_history"]) > MAX_HISTORY:
        _stats["dispatch_history"] = _stats["dispatch_history"][-MAX_HISTORY:]
    if response.status == DispatchStatus.FAILED:
        if response.error and not response.error.retryable:
            raise HTTPException(status_code=400, detail=response.error.dict())
    return response


@app.post("/dispatch/batch", response_model=List[DispatchResponse], summary="Batch dispatch multiple requests")
async def dispatch_batch(requests: List[DispatchRequest]):
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    results = await asyncio.gather(*[dispatcher.dispatch(req) for req in requests])
    return list(results)


@app.get("/models", summary="List all registered model endpoints")
async def list_models(model_type: Optional[str] = Query(None, description="Filter by model type: local/cloud")):
    if model_type:
        mt = ModelType(model_type)
        endpoints = registry.get_by_type(mt)
    else:
        endpoints = registry.list_all()
    return [
        {
            "name": ep.name,
            "display_name": ep.display_name,
            "model_type": ep.model_type.value,
            "provider": ep.provider.value,
            "model_id": ep.model_id,
            "enabled": ep.enabled,
            "available": ep.is_available,
            "current_load": ep.current_load,
            "max_concurrent": ep.max_concurrent,
            "avg_latency_ms": ep.avg_latency_ms,
            "success_rate": round(ep.success_rate, 4),
            "priority": ep.priority,
            "weight": ep.weight,
            "load_factor": round(ep.load_factor, 4),
            "cost_per_1k_input_tokens": ep.cost_per_1k_input_tokens,
            "cost_per_1k_output_tokens": ep.cost_per_1k_output_tokens,
            "tags": ep.tags,
        }
        for ep in endpoints
    ]


@app.post("/models", summary="Register a new model endpoint")
async def register_model(endpoint: ModelEndpoint):
    registry.register(endpoint)
    return {"status": "registered", "name": endpoint.name}


@app.delete("/models/{name}", summary="Unregister a model endpoint")
async def unregister_model(name: str):
    ep = registry.get(name)
    if not ep:
        raise HTTPException(status_code=404, detail=f"Model endpoint '{name}' not found")
    registry.unregister(name)
    return {"status": "unregistered", "name": name}


@app.get("/models/{name}/status", summary="Get detailed status of a model endpoint")
async def model_status(name: str):
    ep = registry.get(name)
    if not ep:
        raise HTTPException(status_code=404, detail=f"Model endpoint '{name}' not found")
    return {
        "name": ep.name,
        "model_type": ep.model_type.value,
        "provider": ep.provider.value,
        "available": ep.is_available,
        "load_factor": round(ep.load_factor, 4),
        "current_load": ep.current_load,
        "max_concurrent": ep.max_concurrent,
        "avg_latency_ms": ep.avg_latency_ms,
        "success_rate": round(ep.success_rate, 4),
        "enabled": ep.enabled,
    }


@app.get("/hooks", summary="List all registered hooks")
async def list_hooks():
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    return dispatcher.hook_manager.list_hooks()


@app.post("/hooks/{name}/enable", summary="Enable a hook")
async def enable_hook(name: str):
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    dispatcher.hook_manager.enable_hook(name)
    return {"status": "enabled", "hook": name}


@app.post("/hooks/{name}/disable", summary="Disable a hook")
async def disable_hook(name: str):
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    dispatcher.hook_manager.disable_hook(name)
    return {"status": "disabled", "hook": name}


@app.get("/stats", summary="Get scheduler statistics and system resource info")
async def get_stats():
    cpu_percent = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    avg_latency = (
        _stats["total_latency_ms"] / _stats["total_requests"]
        if _stats["total_requests"] > 0 else 0
    )
    return {
        "requests": {
            "total": _stats["total_requests"],
            "success": _stats["success_requests"],
            "failed": _stats["failed_requests"],
            "success_rate": round(
                _stats["success_requests"] / _stats["total_requests"], 4
            ) if _stats["total_requests"] > 0 else 0,
            "avg_latency_ms": round(avg_latency, 1),
        },
        "system": {
            "cpu_percent": cpu_percent,
            "memory_total_gb": round(mem.total / (1024**3), 2),
            "memory_used_gb": round(mem.used / (1024**3), 2),
            "memory_percent": mem.percent,
        },
        "models": {
            ep.name: {
                "model_type": ep.model_type.value,
                "current_load": ep.current_load,
                "max_concurrent": ep.max_concurrent,
                "load_factor": round(ep.load_factor, 4),
                "avg_latency_ms": ep.avg_latency_ms,
                "success_rate": round(ep.success_rate, 4),
            }
            for ep in registry.list_all()
        },
    }


@app.get("/history", summary="Get recent dispatch history")
async def get_history(limit: int = Query(20, le=50)):
    return _stats["dispatch_history"][-limit:]


@app.get("/health", summary="Health check")
async def health_check():
    local_count = len(registry.get_local_available())
    cloud_count = len(registry.get_cloud_available())
    bridge_health = {}
    if dispatcher and dispatcher.use_openclaw:
        bridge_health = await dispatcher.openclaw_agent.get_bridge_health()
    return {
        "status": "healthy",
        "openclaw_version": "2026.4.27",
        "local_models": local_count,
        "cloud_models": cloud_count,
        "total_endpoints": len(registry.endpoints),
        "openclaw_bridge": bridge_health.get("status", "unknown") if bridge_health else "disabled",
        "adaptive_state": dispatcher.strategy_router.get_adaptive_state() if dispatcher else {},
    }


@app.get("/openclaw/bridge/health", summary="Check OpenClaw Bridge health")
async def openclaw_bridge_health():
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    if not dispatcher.use_openclaw:
        return {"status": "disabled", "message": "OpenClaw integration is disabled"}
    health = await dispatcher.openclaw_agent.get_bridge_health()
    return health


@app.get("/openclaw/bridge/models", summary="List models from OpenClaw Bridge")
async def openclaw_bridge_models(model_type: Optional[str] = Query(None)):
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    if not dispatcher.use_openclaw:
        return {"models": [], "message": "OpenClaw integration is disabled"}
    models = await dispatcher.openclaw_agent.get_bridge_models(model_type)
    return {"models": models}


@app.get("/openclaw/bridge/stats", summary="Get OpenClaw Bridge statistics")
async def openclaw_bridge_stats():
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    if not dispatcher.use_openclaw:
        return {"message": "OpenClaw integration is disabled"}
    stats = await dispatcher.openclaw_agent.get_bridge_stats()
    return stats


@app.post("/openclaw/agent/{agent_id}/message", summary="Send message to an OpenClaw agent")
async def openclaw_agent_message(
    agent_id: str,
    prompt: str,
    context: Optional[List[Dict[str, str]]] = None,
    parameters: Optional[Dict] = None,
    timeout_ms: int = 30000,
):
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    if not dispatcher.use_openclaw:
        raise HTTPException(status_code=400, detail="OpenClaw integration is disabled")
    result = await dispatcher.dispatch_agent_message(
        agent_id=agent_id,
        prompt=prompt,
        context=context,
        parameters=parameters,
        timeout_ms=timeout_ms,
    )
    return result


@app.get("/openclaw/adaptive/state", summary="Get adaptive scheduling state")
async def openclaw_adaptive_state():
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    return dispatcher.strategy_router.get_adaptive_state()


@app.post("/openclaw/toggle", summary="Toggle OpenClaw integration on/off")
async def openclaw_toggle(enabled: bool = Query(True)):
    global dispatcher
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    dispatcher.use_openclaw = enabled
    dispatcher.strategy_router.use_openclaw = enabled
    return {
        "openclaw_enabled": enabled,
        "message": f"OpenClaw integration {'enabled' if enabled else 'disabled'}",
    }


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    import os
    dashboard_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "dashboard.html")
    if os.path.exists(dashboard_path):
        with open(dashboard_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Dashboard not found</h1>")
