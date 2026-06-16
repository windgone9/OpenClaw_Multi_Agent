"""
Hermes Dispatch Worker — Queue-driven request dispatch with Hermes Agent routing.

Architecture (simplified, no Bridge/Gateway middle layer):
  [Request Queue] → DispatchWorker → Hermes Agent 路由决策
                                      ├─ direct_local    → Ollama/vLLM 直连 (单步问答, 低延迟)
                                      ├─ gateway         → OfficialGW 直连 (多步批处理/Volcano/Agent)
                                      ├─ k8s_gateway     → OpenClaw K8S 插件 (AIWorkload 提交/查询/删除)
                                      ├─ multimodal      → 本地多模态模型直连 (图片/音频/视频)
                                      └─ local_inference → Ollama/vLLM 直连 (隐私敏感, 本地执行)
                   ← [Result Queue] ← 结果写入
                   ← [Feedback Queue] ← 反馈 → MEMORY.md 自学习闭环

调用路径清晰，结果可追溯:
  - direct_local:    Hermes → Ollama(11434)
  - gateway:         Hermes → OfficialGW(3005) → Agent → Volcano
  - k8s_gateway:     Hermes → OpenClaw GW → /plugins/k8s/v1/workloads → K8S AIWorkload
  - multimodal:      Hermes → 本地多模态模型(11434)
  - local_inference: Hermes → Ollama(11434) (隐私约束)
"""

import logging
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Dict

import httpx

from hermes.message_queue import (
    MessageQueue,
    QUEUE_REQUESTS,
    QUEUE_RESULTS,
    QUEUE_FEEDBACK,
)
from hermes.official_agent_adapter import OfficialHermesAdapter

logger = logging.getLogger(__name__)

# Downstream service configuration (direct connection, no Bridge/Gateway middle layer)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OFFICIAL_GW_URL = os.getenv("OPENCLAW_OFFICIAL_GATEWAY_URL", "http://127.0.0.1:3005")

# Timeouts
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "120.0"))
OFFICIAL_GW_TIMEOUT = float(os.getenv("OFFICIAL_GW_TIMEOUT", "300.0"))
MULTIMODAL_TIMEOUT = float(os.getenv("MULTIMODAL_TIMEOUT", "180.0"))  # multimodal needs more time

# Semaphore to limit concurrent Ollama requests (GPU is the bottleneck)
# This covers BOTH routing decisions AND request execution, since they share the same GPU
_OLLAMA_MAX_CONCURRENT = int(os.getenv("OLLAMA_MAX_CONCURRENT", "2"))
_ollama_semaphore = threading.Semaphore(_OLLAMA_MAX_CONCURRENT)

# Semaphore to limit concurrent routing decisions (routing also uses Ollama GPU)
# Total GPU slots = routing + execution, so routing must be limited to avoid starvation
_ROUTING_MAX_CONCURRENT = int(os.getenv("ROUTING_MAX_CONCURRENT", "1"))
_routing_semaphore = threading.Semaphore(_ROUTING_MAX_CONCURRENT)

# Semaphore to limit concurrent OfficialGW requests
# OfficialGW (Volcano) has limited capacity; too many concurrent requests cause queuing and timeouts
_GW_MAX_CONCURRENT = int(os.getenv("GW_MAX_CONCURRENT", "3"))
_gw_semaphore = threading.Semaphore(_GW_MAX_CONCURRENT)

# Multimodal model configuration
MULTIMODAL_MODEL = os.getenv("MULTIMODAL_MODEL", "llava:7b")
LOCAL_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:3b")

# Cache available Ollama models to avoid hitting missing models (causes long timeouts)
_ollama_models_cache: list = []
_ollama_models_cache_ts: float = 0.0
_OLLAMA_MODELS_CACHE_TTL = 60.0  # refresh every 60s

# Persistent httpx client for Ollama (reuse connections, avoid per-request overhead)
# Using a single client with connection pooling reduces latency and avoids
# the "stuck request" problem where new connections queue behind timed-out ones
_ollama_http_client = httpx.Client(
    base_url=OLLAMA_URL,
    timeout=httpx.Timeout(connect=5.0, read=OLLAMA_TIMEOUT, write=5.0, pool=5.0),
    limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
)
_ollama_client_lock = threading.Lock()


def _reset_ollama_client():
    """Reset the Ollama httpx client after a timeout to clear stale connections."""
    global _ollama_http_client
    with _ollama_client_lock:
        try:
            _ollama_http_client.close()
        except Exception:
            pass
        _ollama_http_client = httpx.Client(
            base_url=OLLAMA_URL,
            timeout=httpx.Timeout(connect=5.0, read=OLLAMA_TIMEOUT, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )


def _get_ollama_models() -> list:
    """Get list of available Ollama models (cached)."""
    global _ollama_models_cache, _ollama_models_cache_ts
    now = time.time()
    if _ollama_models_cache and (now - _ollama_models_cache_ts) < _OLLAMA_MODELS_CACHE_TTL:
        return _ollama_models_cache
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            models = [m.get("name", "") for m in resp.json().get("models", [])]
            _ollama_models_cache = models
            _ollama_models_cache_ts = now
            logger.debug("[Ollama] Available models: %s", models)
            return models
    except Exception as e:
        logger.warning("[Ollama] Failed to list models: %s", e)
        return _ollama_models_cache  # return stale cache


class DispatchWorker:
    """Consumes requests from queue, routes via Hermes Agent, dispatches directly to downstream."""

    def __init__(
        self,
        queue: MessageQueue,
        adapter: OfficialHermesAdapter,
        max_workers: int = 4,
        poll_interval: float = 0.5,
    ):
        self.queue = queue
        self.adapter = adapter
        self.max_workers = max_workers
        self.poll_interval = poll_interval

        self._running = False
        self._workers: list = []
        self._lock = threading.Lock()

        # Stats
        self._stats = {
            "processed": 0,
            "success": 0,
            "failed": 0,
            "total_latency_ms": 0,
            "by_route": {},
        }

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        for i in range(self.max_workers):
            t = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name=f"hermes-worker-{i}",
            )
            t.start()
            self._workers.append(t)
        logger.info(f"DispatchWorker started with {self.max_workers} workers")

    def stop(self):
        self._running = False
        for t in self._workers:
            t.join(timeout=5)
        self._workers.clear()
        logger.info("DispatchWorker stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Worker Loop ──────────────────────────────────────────────────

    def _worker_loop(self):
        worker_name = threading.current_thread().name
        logger.info("[DispatchWorker] Worker thread started: %s", worker_name)
        while self._running:
            try:
                msg = self.queue.pop(QUEUE_REQUESTS, timeout=self.poll_interval)
                if msg is None:
                    continue
                logger.info("[DispatchWorker] [%s] Picked up request: %s",
                            worker_name, msg.get("request_id", "?")[:8])
                self._process_request(msg)
            except Exception as e:
                logger.error("[DispatchWorker] [%s] Worker error: %s", worker_name, e, exc_info=True)
                time.sleep(1)

    def _process_request(self, request: Dict):
        """Process a single request: route → dispatch → result → feedback."""
        request_id = request.get("request_id", str(uuid.uuid4()))
        start_time = time.time()

        logger.info("[DispatchWorker] 开始路由决策: appid=%s prompt='%.60s'",
                    request.get("appid", "default"), request.get("prompt", "")[:60])

        result = {
            "request_id": request_id,
            "appid": request.get("appid", "default"),
            "status": "pending",
            "routing": None,
            "result": None,
            "error": None,
            "total_latency_ms": 0,
            "timestamp": datetime.now().isoformat(),
        }

        try:
            # Step 1: Route via Hermes Agent (Memory-driven intelligent routing)
            # Routing uses its own semaphore (limit=1) to avoid GPU overload from routing
            # Execution uses separate Ollama semaphore (limit=2) — they don't block each other
            logger.info("[RoutingSemaphore] [%s] Waiting to acquire (limit=%d)...",
                        request_id, _ROUTING_MAX_CONCURRENT)
            with _routing_semaphore:
                logger.info("[RoutingSemaphore] [%s] Acquired, remaining=%d",
                            request_id, _routing_semaphore._value)
                routing = self.adapter.route_via_agent(request)
            logger.info("[DispatchWorker] 路由完成: route=%s complexity=%s post_validated=%s",
                        routing.get("route_path"), routing.get("complexity_score"), routing.get("post_validated"))
            result["routing"] = {
                "route_path": routing.get("route_path", "gateway"),
                "complexity_score": routing.get("complexity_score", 0),
                "selected_model": routing.get("selected_model"),
                "reason": routing.get("reason", ""),
                "agent_decision": routing.get("agent_decision", ""),
                "memory_context_used": routing.get("memory_context_used", False),
                "post_validated": routing.get("post_validated", False),
            }

            route_path = routing["route_path"]

            # Step 2: Dispatch to downstream service (direct connection)
            dispatch_result = self._dispatch(request, routing)
            result["result"] = dispatch_result
            result["status"] = "success"

            # Step 3: Record feedback (success) → Memory self-learning
            latency_ms = int((time.time() - start_time) * 1000)
            self._record_feedback(
                request_id, route_path, True, latency_ms,
                request.get("prompt", "")[:50],
            )

        except Exception as e:
            result["status"] = "failed"
            result["error"] = {"code": "DISPATCH_ERROR", "message": str(e)}
            latency_ms = int((time.time() - start_time) * 1000)

            route_path = result.get("routing", {}).get("route_path", "unknown") if result.get("routing") else "unknown"
            self._record_feedback(
                request_id, route_path, False, latency_ms,
                f"{request.get('prompt', '')[:30]} error:{str(e)[:30]}",
            )
            logger.error(f"[{request_id}] Dispatch failed: {e}")

        result["total_latency_ms"] = int((time.time() - start_time) * 1000)

        # Step 4: Push result to result queue
        self.queue.push(QUEUE_RESULTS, result)

        # Step 5: Notify pending sync waiters (submit-sync event-based waiting)
        # Uses threading.Event.set() — thread-safe and reliable
        if request.get("_sync_mode"):
            try:
                import hermes.server as srv
                with srv._pending_sync_lock:
                    entry = srv._pending_sync.get(request_id)
                    if entry:
                        entry["holder"]["result"] = result
                        entry["event"].set()
            except Exception as e:
                logger.warning("[DispatchWorker] Failed to notify sync waiter %s: %s", request_id, e)

        # Update stats
        with self._lock:
            self._stats["processed"] += 1
            if result["status"] == "success":
                self._stats["success"] += 1
            else:
                self._stats["failed"] += 1
            self._stats["total_latency_ms"] += result["total_latency_ms"]
            route = result.get("routing", {}).get("route_path", "unknown") if result.get("routing") else "unknown"
            self._stats["by_route"][route] = self._stats["by_route"].get(route, 0) + 1

        logger.info(f"[{request_id}] Done: status={result['status']}, "
                     f"route={route_path}, latency={result['total_latency_ms']}ms")

    # ── Dispatch (Direct Connection) ─────────────────────────────────

    def _dispatch(self, request: Dict, routing: Dict) -> Dict:
        """Dispatch request directly to downstream service based on routing decision.

        5-category routing (no Bridge/Gateway middle layer):
          - direct_local    → Ollama/vLLM 直连 (单步问答, 低延迟)
          - gateway         → OfficialGW 直连 (多步批处理/Volcano/Agent)
          - k8s_gateway     → OpenClaw K8S 插件 (AIWorkload 提交/查询/删除)
          - multimodal      → 本地多模态模型直连 (图片/音频/视频)
          - local_inference → Ollama/vLLM 直连 (隐私敏感, 本地执行)
        """
        route_path = routing["route_path"]

        # Check for K8S workload request — takes priority over regular gateway
        if request.get("type") == "k8s_workload" or request.get("k8s_workload"):
            k8s_spec = request.get("k8s_workload", {})
            logger.info("[K8S Dispatch] [%s] K8S请求拦截: type=%s k8s_action=%s route_path=%s → k8s_gateway",
                        request.get("request_id", "?"),
                        request.get("type", "?"),
                        k8s_spec.get("action", "submit"),
                        route_path)
            return self._dispatch_to_k8s_gateway(request, routing)

        if route_path == "direct_local":
            return self._dispatch_to_local(request, routing)
        elif route_path == "gateway":
            return self._dispatch_to_official_gw(request, routing)
        elif route_path == "multimodal":
            return self._dispatch_to_multimodal(request, routing)
        elif route_path == "local_inference":
            return self._dispatch_to_local(request, routing, privacy=True)
        else:
            # Unknown route → fallback to local model
            logger.warning(f"Unknown route_path '{route_path}', falling back to local")
            routing["route_path"] = "direct_local"
            return self._dispatch_to_local(request, routing)

    def _dispatch_to_local(self, request: Dict, routing: Dict,
                            privacy: bool = False) -> Dict:
        """Dispatch directly to local Ollama/vLLM.

        Args:
            privacy: If True, this is a local_inference route (privacy-enforced).
        """
        request_id = request.get("request_id", "unknown")
        prompt = request.get("prompt", "")
        model = LOCAL_MODEL
        route_tag = "local_inference" if privacy else "direct_local"

        logger.info("[Ollama] [%s] → 发送请求: route=%s model=%s timeout=%.0fs prompt='%.50s'",
                    request_id, route_tag, model, OLLAMA_TIMEOUT, prompt[:50])

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"num_ctx": 4096, "temperature": 0.7, "num_predict": 2048},
        }

        start = time.time()
        logger.info("[OllamaSemaphore] [%s] Waiting to acquire (%s, limit=%d)...",
                    request_id, route_tag, _OLLAMA_MAX_CONCURRENT)
        with _ollama_semaphore:
            logger.info("[OllamaSemaphore] [%s] Acquired (%s), remaining=%d",
                        request_id, route_tag, _ollama_semaphore._value)
            try:
                resp = _ollama_http_client.post("/v1/chat/completions", json=payload)
                resp.raise_for_status()
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                # Reset connection pool after timeout to avoid stale connections
                logger.warning("[Ollama] [%s] 连接超时 (%s), 重置连接池", request_id, type(e).__name__)
                _reset_ollama_client()
                raise
        elapsed_ms = int((time.time() - start) * 1000)
        logger.info("[Ollama] [%s] ✓ 响应成功: route=%s model=%s elapsed=%dms",
                    request_id, route_tag, model, elapsed_ms)

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})

        route_tag = "hermes_local_inference" if privacy else "hermes_direct_local"
        return {
            "model_name": model,
            "model_type": "local",
            "output": content,
            "latency_ms": elapsed_ms,
            "usage": usage,
            "finish_reason": data.get("choices", [{}])[0].get("finish_reason", "stop"),
            "routed_via": route_tag,
            "dispatch_latency_ms": elapsed_ms,
        }

    def _dispatch_to_k8s_gateway(self, request: Dict, routing: Dict) -> Dict:
        """Dispatch to OpenClaw K8S plugin for AIWorkload submission/query/delete.

        Routes to OpenClaw Gateway's /plugins/k8s/v1/workloads endpoint.
        Supports three operations based on k8s_workload.action:
          - submit (default): POST /plugins/k8s/v1/workloads
          - get: GET /plugins/k8s/v1/workloads/{workloadId}
          - delete: DELETE /plugins/k8s/v1/workloads/{workloadId}
        """
        request_id = request.get("request_id", "unknown")
        k8s_spec = request.get("k8s_workload", {})
        action = k8s_spec.get("action", "submit")

        headers = {"Content-Type": "application/json"}
        gw_token = os.getenv("OPENCLAW_TOKEN", "")
        if gw_token:
            headers["Authorization"] = f"Bearer {gw_token}"
            logger.debug("[K8SGateway] [%s] 认证: Bearer token 已设置 (len=%d)", request_id, len(gw_token))
        else:
            logger.warning("[K8SGateway] [%s] 认证: OPENCLAW_TOKEN 未设置，请求将不带认证头", request_id)

        # Determine OpenClaw Gateway URL for K8S plugin
        k8s_gw_url = os.getenv("OPENCLAW_K8S_GATEWAY_URL", OFFICIAL_GW_URL)
        k8s_base = f"{k8s_gw_url}/plugins/k8s/v1/workloads"
        logger.info("[K8SGateway] [%s] 初始化: action=%s gw_url=%s k8s_base=%s timeout=%s",
                    request_id, action, k8s_gw_url, k8s_base, OFFICIAL_GW_TIMEOUT)

        start = time.time()

        if action == "submit":
            # ── Submit AIWorkload ──
            payload = {
                "requestId": k8s_spec.get("requestId", request_id),
                "tenant": k8s_spec.get("tenant", {"id": request.get("appid", "default")}),
                "taskType": k8s_spec.get("taskType", "batch-inference"),
                "intent": k8s_spec.get("intent", {}),
            }
            if k8s_spec.get("sla"):
                payload["sla"] = k8s_spec["sla"]
            if k8s_spec.get("callback"):
                payload["callback"] = k8s_spec["callback"]

            logger.info("[K8SGateway] [%s] → 提交 AIWorkload: taskType=%s tenant=%s requestId=%s url=%s",
                        request_id, payload["taskType"], payload["tenant"], payload["requestId"], k8s_base)
            logger.debug("[K8SGateway] [%s] 提交 payload: %s", request_id, payload)

            with _gw_semaphore:
                try:
                    with httpx.Client(timeout=OFFICIAL_GW_TIMEOUT) as client:
                        logger.debug("[K8SGateway] [%s] 发送 POST %s (semaphore acquired)", request_id, k8s_base)
                        resp = client.post(k8s_base, headers=headers, json=payload)
                        logger.debug("[K8SGateway] [%s] 收到响应: status_code=%d content_type=%s content_len=%s",
                                     request_id, resp.status_code,
                                     resp.headers.get("content-type", "?"),
                                     resp.headers.get("content-length", "?"))
                        resp.raise_for_status()
                except httpx.TimeoutException as e:
                    elapsed_ms = int((time.time() - start) * 1000)
                    logger.error("[K8SGateway] [%s] ✗ 提交超时: elapsed=%dms timeout=%s type=%s",
                                 request_id, elapsed_ms, OFFICIAL_GW_TIMEOUT, type(e).__name__)
                    raise
                except httpx.HTTPStatusError as e:
                    elapsed_ms = int((time.time() - start) * 1000)
                    body = {}
                    try:
                        body = e.response.json()
                    except Exception:
                        body_text = e.response.text[:200] if hasattr(e.response, 'text') else ''
                        logger.debug("[K8SGateway] [%s] 错误响应体(非JSON): %s", request_id, body_text)
                    err = body.get("error", {})
                    logger.error("[K8SGateway] [%s] ✗ HTTP错误: status=%d code=%s msg=%s elapsed=%dms",
                                 request_id, e.response.status_code,
                                 err.get("code", "?"), err.get("message", "")[:80], elapsed_ms)
                    raise
                except httpx.ConnectError as e:
                    elapsed_ms = int((time.time() - start) * 1000)
                    logger.error("[K8SGateway] [%s] ✗ 连接失败: url=%s elapsed=%dms error=%s",
                                 request_id, k8s_base, elapsed_ms, str(e)[:100])
                    raise

            elapsed_ms = int((time.time() - start) * 1000)
            data = resp.json()
            k8s_payload = data.get("payload", {})
            workload_id = k8s_payload.get("workloadId", "?")
            wl_status = k8s_payload.get("status", "Unknown")
            is_duplicate = data.get("duplicate", False)

            logger.info("[K8SGateway] [%s] ✓ AIWorkload 已提交: workloadId=%s status=%s namespace=%s duplicate=%s elapsed=%dms",
                        request_id, workload_id, wl_status,
                        k8s_payload.get("namespace", "?"), is_duplicate, elapsed_ms)
            logger.debug("[K8SGateway] [%s] 响应 payload: %s", request_id, k8s_payload)

            return {
                "model_name": "openclaw/default",
                "model_type": "openclaw-agent",
                "output": f"已提交 K8S AIWorkload: {workload_id}，状态: {wl_status}",
                "latency_ms": elapsed_ms,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "finish_reason": "stop",
                "routed_via": "hermes_k8s_gateway",
                "dispatch_latency_ms": elapsed_ms,
                "k8s_workload": k8s_payload,
            }

        elif action == "get":
            # ── Query AIWorkload status ──
            workload_id = k8s_spec.get("workloadId", "")
            if not workload_id:
                logger.error("[K8SGateway] [%s] ✗ 查询失败: workloadId 缺失", request_id)
                raise ValueError("k8s_workload.workloadId is required for get action")
            params = {}
            if k8s_spec.get("namespace"):
                params["namespace"] = k8s_spec["namespace"]
            elif k8s_spec.get("tenantId"):
                params["tenantId"] = k8s_spec["tenantId"]

            url = f"{k8s_base}/{workload_id}"
            logger.info("[K8SGateway] [%s] → 查询 AIWorkload: workloadId=%s url=%s params=%s",
                        request_id, workload_id, url, params)

            with _gw_semaphore:
                try:
                    with httpx.Client(timeout=30.0) as client:
                        resp = client.get(url, headers=headers, params=params)
                        logger.debug("[K8SGateway] [%s] 查询响应: status_code=%d", request_id, resp.status_code)
                        resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    elapsed_ms = int((time.time() - start) * 1000)
                    logger.error("[K8SGateway] [%s] ✗ 查询HTTP错误: workloadId=%s status=%d elapsed=%dms",
                                 request_id, workload_id, e.response.status_code, elapsed_ms)
                    raise
                except Exception as e:
                    elapsed_ms = int((time.time() - start) * 1000)
                    logger.error("[K8SGateway] [%s] ✗ 查询失败: workloadId=%s error=%s elapsed=%dms",
                                 request_id, workload_id, str(e)[:100], elapsed_ms)
                    raise

            elapsed_ms = int((time.time() - start) * 1000)
            data = resp.json()
            k8s_payload = data.get("payload", {})
            wl_status = k8s_payload.get("status", "Unknown")

            logger.info("[K8SGateway] [%s] ✓ 查询成功: workloadId=%s status=%s namespace=%s elapsed=%dms",
                        request_id, workload_id, wl_status,
                        k8s_payload.get("namespace", "?"), elapsed_ms)

            return {
                "model_name": "openclaw/default",
                "model_type": "openclaw-agent",
                "output": f"工作负载 {workload_id} 状态: {wl_status}",
                "latency_ms": elapsed_ms,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "finish_reason": "stop",
                "routed_via": "hermes_k8s_gateway",
                "dispatch_latency_ms": elapsed_ms,
                "k8s_workload": k8s_payload,
            }

        elif action == "delete":
            # ── Delete AIWorkload ──
            workload_id = k8s_spec.get("workloadId", "")
            if not workload_id:
                logger.error("[K8SGateway] [%s] ✗ 删除失败: workloadId 缺失", request_id)
                raise ValueError("k8s_workload.workloadId is required for delete action")
            params = {}
            if k8s_spec.get("namespace"):
                params["namespace"] = k8s_spec["namespace"]
            elif k8s_spec.get("tenantId"):
                params["tenantId"] = k8s_spec["tenantId"]

            url = f"{k8s_base}/{workload_id}"
            logger.info("[K8SGateway] [%s] → 删除 AIWorkload: workloadId=%s url=%s params=%s",
                        request_id, workload_id, url, params)

            with _gw_semaphore:
                try:
                    with httpx.Client(timeout=30.0) as client:
                        resp = client.delete(url, headers=headers, params=params)
                        logger.debug("[K8SGateway] [%s] 删除响应: status_code=%d", request_id, resp.status_code)
                        resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    elapsed_ms = int((time.time() - start) * 1000)
                    logger.error("[K8SGateway] [%s] ✗ 删除HTTP错误: workloadId=%s status=%d elapsed=%dms",
                                 request_id, workload_id, e.response.status_code, elapsed_ms)
                    raise
                except Exception as e:
                    elapsed_ms = int((time.time() - start) * 1000)
                    logger.error("[K8SGateway] [%s] ✗ 删除失败: workloadId=%s error=%s elapsed=%dms",
                                 request_id, workload_id, str(e)[:100], elapsed_ms)
                    raise

            elapsed_ms = int((time.time() - start) * 1000)
            data = resp.json()
            k8s_payload = data.get("payload", {})

            logger.info("[K8SGateway] [%s] ✓ 删除成功: workloadId=%s status=%s namespace=%s elapsed=%dms",
                        request_id, workload_id,
                        k8s_payload.get("status", "Deleting"),
                        k8s_payload.get("namespace", "?"), elapsed_ms)

            return {
                "model_name": "openclaw/default",
                "model_type": "openclaw-agent",
                "output": f"工作负载 {workload_id} 已标记删除",
                "latency_ms": elapsed_ms,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "finish_reason": "stop",
                "routed_via": "hermes_k8s_gateway",
                "dispatch_latency_ms": elapsed_ms,
                "k8s_workload": k8s_payload,
            }

        else:
            raise ValueError(f"Unknown k8s_workload action: {action}")

    def _dispatch_to_official_gw(self, request: Dict, routing: Dict) -> Dict:
        """Dispatch directly to OpenClaw Official Gateway.

        OfficialGW handles:
        - Multi-step batch processing (Agent → Volcano scheduling)
        - Code execution tasks
        - Tool call / Agent chain tasks
        """
        request_id = request.get("request_id", "unknown")
        prompt = request.get("prompt", "")
        context = request.get("context", [])
        messages = []
        if context:
            messages.extend(context)
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": "openclaw/default",
            "messages": messages,
            "max_tokens": 2048,
        }
        if request.get("tools"):
            payload["tools"] = request["tools"]
            payload["tool_choice"] = request.get("tool_choice", "auto")

        headers = {"Content-Type": "application/json"}
        gw_token = os.getenv("OPENCLAW_TOKEN", "")
        if gw_token:
            headers["Authorization"] = f"Bearer {gw_token}"

        logger.info("[OfficialGW] [%s] → 发送请求: url=%s timeout=%.0fs prompt='%.50s'",
                    request_id, f"{OFFICIAL_GW_URL}/v1/chat/completions",
                    OFFICIAL_GW_TIMEOUT, prompt[:50])

        start = time.time()
        logger.info("[GWSemaphore] [%s] Waiting to acquire (limit=%d)...",
                    request_id, _GW_MAX_CONCURRENT)
        with _gw_semaphore:
            logger.info("[GWSemaphore] [%s] Acquired, remaining=%d",
                        request_id, _gw_semaphore._value)
            try:
                with httpx.Client(timeout=OFFICIAL_GW_TIMEOUT) as client:
                    resp = client.post(
                        f"{OFFICIAL_GW_URL}/v1/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    resp.raise_for_status()
            except httpx.TimeoutException as e:
                elapsed_ms = int((time.time() - start) * 1000)
                logger.error("[OfficialGW] [%s] ✗ 超时: type=%s elapsed=%dms timeout=%.0fs prompt='%.50s'",
                             request_id, type(e).__name__, elapsed_ms, OFFICIAL_GW_TIMEOUT, prompt[:50])
                self._log_gw_connection_state(request_id)
                raise
            except httpx.HTTPStatusError as e:
                elapsed_ms = int((time.time() - start) * 1000)
                logger.error("[OfficialGW] [%s] ✗ HTTP错误: status=%d elapsed=%dms prompt='%.50s'",
                             request_id, e.response.status_code, elapsed_ms, prompt[:50])
                raise
            except httpx.ConnectError as e:
                elapsed_ms = int((time.time() - start) * 1000)
                logger.error("[OfficialGW] [%s] ✗ 连接失败: elapsed=%dms error=%s",
                             request_id, elapsed_ms, str(e)[:100])
                self._log_gw_connection_state(request_id)
                raise
            except Exception as e:
                elapsed_ms = int((time.time() - start) * 1000)
                logger.error("[OfficialGW] [%s] ✗ 异常: type=%s elapsed=%dms error=%s",
                             request_id, type(e).__name__, elapsed_ms, str(e)[:100])
                raise

        elapsed_ms = int((time.time() - start) * 1000)
        logger.info("[OfficialGW] [%s] ✓ 响应成功: elapsed=%dms status_code=%d",
                    request_id, elapsed_ms, resp.status_code)

        data = resp.json()
        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")
        tool_calls = choice.get("message", {}).get("tool_calls")
        usage = data.get("usage", {})

        return {
            "model_name": data.get("model", "openclaw/default"),
            "model_type": "openclaw-agent",
            "provider": "openclaw",
            "output": content,
            "tool_calls": tool_calls,
            "latency_ms": elapsed_ms,
            "usage": usage,
            "finish_reason": choice.get("finish_reason", "stop"),
            "routed_via": "hermes_official_gw",
            "dispatch_latency_ms": elapsed_ms,
        }

    def _log_gw_connection_state(self, request_id: str):
        """Log OfficialGW connection state for diagnosing connection accumulation."""
        import subprocess
        try:
            # Parse port from OFFICIAL_GW_URL
            from urllib.parse import urlparse
            parsed = urlparse(OFFICIAL_GW_URL)
            port = parsed.port or 3005
            host = parsed.hostname or "127.0.0.1"

            # Count active connections to OfficialGW port
            result = subprocess.run(
                ["lsof", "-i", f":{port}", "-P", "-n"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
            total_lines = len(lines)
            established = sum(1 for l in lines if "ESTABLISHED" in l)
            close_wait = sum(1 for l in lines if "CLOSE_WAIT" in l)
            time_wait = sum(1 for l in lines if "TIME_WAIT" in l)
            listen = sum(1 for l in lines if "LISTEN" in l)

            logger.warning("[OfficialGW] [%s] 连接状态诊断: port=%d total=%d ESTABLISHED=%d CLOSE_WAIT=%d TIME_WAIT=%d LISTEN=%d",
                           request_id, port, total_lines, established, close_wait, time_wait, listen)

            # Log hermes worker threads making GW connections
            hermes_gw_conns = sum(1 for l in lines if "python" in l.lower() and "ESTABLISHED" in l)
            logger.warning("[OfficialGW] [%s] Hermes→GW 活跃连接: %d (worker可能阻塞)",
                           request_id, hermes_gw_conns)

            # Log top 5 connection details for debugging
            if total_lines > 10:
                detail_lines = [l for l in lines if "ESTABLISHED" in l or "CLOSE_WAIT" in l][:5]
                for dl in detail_lines:
                    logger.debug("[OfficialGW] [%s] 连接详情: %s", request_id, dl.strip()[:120])
        except Exception as e:
            logger.debug("[OfficialGW] [%s] 连接状态诊断失败: %s", request_id, e)

    def _dispatch_to_multimodal(self, request: Dict, routing: Dict) -> Dict:
        """Dispatch to local multimodal model (image/audio/video processing).

        Uses Ollama with a multimodal-capable model (e.g., llava).
        Falls back to standard local model if multimodal model is unavailable.
        Pre-checks model availability to avoid long timeouts on missing models.
        """
        prompt = request.get("prompt", "")
        model = MULTIMODAL_MODEL
        attachments = request.get("attachments", [])
        request_id = request.get("request_id", "unknown")

        # Pre-check: is the multimodal model available?
        available_models = _get_ollama_models()
        model_available = any(model in m or m.startswith(model.split(":")[0]) for m in available_models)

        if not model_available:
            logger.warning("[Multimodal] [%s] Model '%s' not found in Ollama (available: %s), "
                           "falling back to local model immediately", request_id, model, available_models)
            return self._multimodal_fallback(prompt, model, attachments,
                                             f"Model '{model}' not installed. Available: {', '.join(available_models[:5])}")

        logger.info("[Multimodal] [%s] → 发送请求: model=%s timeout=%.0fs prompt='%.50s'",
                    request_id, model, MULTIMODAL_TIMEOUT, prompt[:50])

        # Build messages with optional image content
        messages = [{"role": "user", "content": prompt}]
        if attachments:
            # Ollama multimodal format: content is an array of text + image parts
            content_parts = [{"type": "text", "text": prompt}]
            for att in attachments:
                if att.get("type") == "image_url" or att.get("url"):
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": att.get("url", "")},
                    })
            messages = [{"role": "user", "content": content_parts}]

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"num_ctx": 4096, "temperature": 0.7, "num_predict": 2048},
        }

        start = time.time()
        try:
            with _ollama_semaphore:
                logger.info("[OllamaSemaphore] [%s] Acquired (multimodal), remaining=%d",
                            request_id, _ollama_semaphore._value)
                # Use a separate client with longer timeout for multimodal requests
                with httpx.Client(
                    base_url=OLLAMA_URL,
                    timeout=httpx.Timeout(connect=10.0, read=MULTIMODAL_TIMEOUT, write=10.0, pool=10.0),
                    limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
                ) as mm_client:
                    resp = mm_client.post("/v1/chat/completions", json=payload)
                    resp.raise_for_status()
            elapsed_ms = int((time.time() - start) * 1000)
            logger.info("[Multimodal] [%s] ✓ 响应成功: model=%s elapsed=%dms",
                        request_id, model, elapsed_ms)

            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})

            return {
                "model_name": model,
                "model_type": "multimodal",
                "output": content,
                "latency_ms": elapsed_ms,
                "usage": usage,
                "finish_reason": data.get("choices", [{}])[0].get("finish_reason", "stop"),
                "routed_via": "hermes_multimodal",
                "dispatch_latency_ms": elapsed_ms,
            }
        except Exception as e:
            logger.warning("[Multimodal] Model '%s' failed: %s, falling back to local", model, e)
            return self._multimodal_fallback(prompt, model, attachments, str(e)[:80])

    def _multimodal_fallback(self, prompt: str, original_model: str,
                              attachments: list, reason: str) -> Dict:
        """Fallback for multimodal requests when the multimodal model is unavailable.

        Uses the standard local model with a clear notice that multimodal
        processing is not available.
        """
        fallback_prompt = (
            f"[系统提示：多模态模型 '{original_model}' 当前不可用（原因：{reason}），"
            f"无法处理图片/音频/视频内容。以下为原始问题：]\n\n{prompt}"
        )
        start = time.time()
        with _ollama_semaphore:
            payload = {
                "model": LOCAL_MODEL,
                "messages": [{"role": "user", "content": fallback_prompt}],
                "stream": False,
                "options": {"num_ctx": 4096, "temperature": 0.7, "num_predict": 2048},
            }
            # Use a separate client with longer timeout for multimodal fallback
            # (multimodal requests may take longer even when falling back to local model)
            with httpx.Client(
                base_url=OLLAMA_URL,
                timeout=httpx.Timeout(connect=10.0, read=MULTIMODAL_TIMEOUT, write=10.0, pool=10.0),
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            ) as fb_client:
                resp = fb_client.post("/v1/chat/completions", json=payload)
                resp.raise_for_status()
        elapsed_ms = int((time.time() - start) * 1000)

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        return {
            "model_name": LOCAL_MODEL,
            "model_type": "local",
            "output": content,
            "latency_ms": elapsed_ms,
            "usage": data.get("usage", {}),
            "finish_reason": data.get("choices", [{}])[0].get("finish_reason", "stop"),
            "routed_via": "hermes_multimodal_fallback_local",
            "dispatch_latency_ms": elapsed_ms,
            "fallback_reason": f"Multimodal model '{original_model}' unavailable: {reason}",
        }

    # ── Feedback → Memory Self-Learning ──────────────────────────────

    def _record_feedback(self, request_id: str, route_path: str,
                         success: bool, latency_ms: int,
                         request_summary: str):
        """Record feedback to feedback queue and Hermes Memory (self-learning closed-loop).

        Feedback flow: request → routing → execution → feedback → MEMORY.md → next routing optimization
        """
        feedback = {
            "request_id": request_id,
            "route_path": route_path,
            "success": success,
            "latency_ms": latency_ms,
            "request_summary": request_summary,
            "timestamp": datetime.now().isoformat(),
        }

        # Push to feedback queue (for monitoring/Dashboard)
        self.queue.push(QUEUE_FEEDBACK, feedback)

        # Record via Hermes Memory (for self-learning evolution)
        # This triggers: feedback write → latency stats update → rule evolution → pattern discovery → rule pruning
        try:
            self.adapter.record_feedback_via_memory(
                skill_name="routing-decision",
                route_path=route_path,
                success=success,
                latency_ms=latency_ms,
                request_summary=request_summary,
            )
        except Exception as e:
            logger.warning(f"Failed to record feedback via Memory: {e}")

    # ── Stats ────────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        with self._lock:
            stats = dict(self._stats)
        stats["is_running"] = self._running
        stats["max_workers"] = self.max_workers
        stats["queue_sizes"] = {
            "requests": self.queue.size(QUEUE_REQUESTS),
            "results": self.queue.size(QUEUE_RESULTS),
            "feedback": self.queue.size(QUEUE_FEEDBACK),
        }
        return stats
