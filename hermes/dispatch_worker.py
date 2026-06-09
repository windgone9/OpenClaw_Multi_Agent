"""
Hermes Dispatch Worker — Queue-driven model scheduling with Hermes Agent routing.

Architecture:
  Frontend → [Request Queue] → DispatchWorker → Hermes Router → Gateway/Agent Chain → [Result Queue] → Frontend
                                       ↓                                                              ↓
                                  Feedback Queue ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ←

The worker:
1. Consumes JSON requests from the request queue
2. Routes each request through Hermes Agent (Memory-driven intelligent routing)
3. Dispatches to the appropriate downstream service:
   - direct_local: Local Ollama model
   - gateway: Bridge → Gateway → Model provider
   - agent_chain: Bridge → Agent Chain → OpenClaw Official GW → Volcano
   - local_inference: Local Ollama model (privacy-enforced)
4. Pushes results to the result queue
5. Records feedback for Memory evolution (closed-loop learning)
"""

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, Optional

import httpx

from hermes.message_queue import (
    MessageQueue,
    InProcessQueue,
    QUEUE_REQUESTS,
    QUEUE_RESULTS,
    QUEUE_FEEDBACK,
)
from hermes.official_agent_adapter import OfficialHermesAdapter

logger = logging.getLogger(__name__)

# Downstream service configuration
BRIDGE_URL = os.getenv("BRIDGE_URL", "http://localhost:3001")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:3000")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OFFICIAL_GW_URL = os.getenv("OPENCLAW_OFFICIAL_GATEWAY_URL", "http://127.0.0.1:3005")

# Timeouts
BRIDGE_TIMEOUT = float(os.getenv("BRIDGE_TIMEOUT", "120.0"))
GATEWAY_TIMEOUT = float(os.getenv("GATEWAY_TIMEOUT", "60.0"))
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "60.0"))


class DispatchWorker:
    """Consumes requests from queue, routes via Hermes, dispatches, returns results."""

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
        """Start worker threads."""
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
        """Stop worker threads."""
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
        while self._running:
            try:
                msg = self.queue.pop(QUEUE_REQUESTS, timeout=self.poll_interval)
                if msg is None:
                    continue
                self._process_request(msg)
            except Exception as e:
                logger.error(f"Worker error: {e}", exc_info=True)
                time.sleep(1)

    def _process_request(self, request: Dict):
        """Process a single request: route → dispatch → result → feedback."""
        request_id = request.get("request_id", str(uuid.uuid4()))
        start_time = time.time()

        logger.info(f"[{request_id}] Processing: type={request.get('type')}, "
                     f"prompt={request.get('prompt', '')[:50]}")

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
            # Step 1: Route via Hermes Agent
            routing = self.adapter.route_via_agent(request)
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

            # Step 2: Dispatch to downstream service
            dispatch_result = self._dispatch(request, routing)
            result["result"] = dispatch_result
            result["status"] = "success"

            # Step 3: Record feedback (success)
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

    # ── Dispatch ─────────────────────────────────────────────────────

    def _dispatch(self, request: Dict, routing: Dict) -> Dict:
        """Dispatch request based on simplified routing.

        Route mapping:
          - direct_local / local_inference → Ollama/vLLM directly (low latency)
          - gateway → Bridge → Gateway → OfficialGW (cloud/agent models)
          - agent_chain → Bridge → Agent Chain → Official OpenClaw GW
        Fallback: If Bridge is unavailable, gateway/agent_chain fall back to local model.
        """
        route_path = routing["route_path"]

        if route_path in ("direct_local", "local_inference"):
            # Simple requests → local model directly
            return self._dispatch_to_local(request, routing)
        elif route_path in ("gateway", "agent_chain"):
            # Agent-related requests → Gateway → OfficialGW (cloud)
            # Fallback to local model if Bridge is unavailable
            try:
                mode = "agent" if route_path == "agent_chain" else "gateway"
                return self._dispatch_to_bridge(request, routing, mode=mode)
            except Exception as e:
                logger.warning(f"Bridge dispatch failed for {route_path}, falling back to local: {e}")
                routing["route_path"] = "direct_local"
                routing["reason"] = f"Fallback: Bridge unavailable ({str(e)[:50]})"
                return self._dispatch_to_local(request, routing)
        else:
            # Default: try bridge, fallback to local
            try:
                return self._dispatch_to_bridge(request, routing, mode="smart")
            except Exception as e:
                logger.warning(f"Bridge dispatch failed, falling back to local: {e}")
                routing["route_path"] = "direct_local"
                return self._dispatch_to_local(request, routing)

    def _dispatch_to_local(self, request: Dict, routing: Dict) -> Dict:
        """Dispatch directly to local Ollama/vLLM (bypasses Bridge/Gateway for low latency)."""
        prompt = request.get("prompt", "")
        model = os.getenv("LOCAL_MODEL", "qwen2.5:3b")

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"num_ctx": 4096, "temperature": 0.7},
        }

        start = time.time()
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            resp = client.post(f"{OLLAMA_URL}/v1/chat/completions", json=payload)
            resp.raise_for_status()
        elapsed_ms = int((time.time() - start) * 1000)

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})

        return {
            "model_name": model,
            "model_type": "local",
            "output": content,
            "latency_ms": elapsed_ms,
            "usage": usage,
            "finish_reason": data.get("choices", [{}])[0].get("finish_reason", "stop"),
            "routed_via": "hermes_direct_local",
            "dispatch_latency_ms": elapsed_ms,
        }

    def _dispatch_to_bridge(self, request: Dict, routing: Dict, mode: str = "smart") -> Dict:
        """Dispatch to Bridge for Gateway/Agent Chain routing.

        Bridge handles:
        - Gateway path: model resolution → provider API call
        - Agent Chain path: Agent Soul → OpenClaw Official GW → Volcano scheduling
        """
        payload = dict(request)
        payload["hermes_routing"] = {
            "route_path": routing["route_path"],
            "complexity_score": routing.get("complexity_score", 0),
            "selected_model": routing.get("selected_model"),
            "reason": routing.get("reason", ""),
        }
        payload["route_mode"] = mode

        start = time.time()
        with httpx.Client(timeout=BRIDGE_TIMEOUT) as client:
            resp = client.post(f"{BRIDGE_URL}/dispatch", json=payload)
            resp.raise_for_status()
        elapsed_ms = int((time.time() - start) * 1000)

        data = resp.json()

        if data.get("status") == "success" and data.get("result"):
            result = data["result"]
            result["routed_via"] = f"hermes_bridge_{mode}"
            result["dispatch_latency_ms"] = elapsed_ms
            return result
        else:
            error_msg = data.get("error", {}).get("message", "Bridge dispatch failed") if data.get("error") else "Bridge dispatch failed"
            raise RuntimeError(f"Bridge error: {error_msg}")

    # ── Feedback ─────────────────────────────────────────────────────

    def _record_feedback(self, request_id: str, route_path: str,
                         success: bool, latency_ms: int,
                         request_summary: str):
        """Record feedback to both the feedback queue and Hermes Memory."""
        feedback = {
            "request_id": request_id,
            "route_path": route_path,
            "success": success,
            "latency_ms": latency_ms,
            "request_summary": request_summary,
            "timestamp": datetime.now().isoformat(),
        }

        # Push to feedback queue (for monitoring)
        self.queue.push(QUEUE_FEEDBACK, feedback)

        # Record via Hermes Memory (for evolution)
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
