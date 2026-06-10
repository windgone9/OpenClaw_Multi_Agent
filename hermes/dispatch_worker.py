"""
Hermes Dispatch Worker — Queue-driven request dispatch with Hermes Agent routing.

Architecture (simplified, no Bridge/Gateway middle layer):
  [Request Queue] → DispatchWorker → Hermes Agent 路由决策
                                      ├─ direct_local    → Ollama/vLLM 直连 (单步问答, 低延迟)
                                      ├─ gateway         → OfficialGW 直连 (多步批处理/Volcano/Agent)
                                      ├─ multimodal      → 本地多模态模型直连 (图片/音频/视频)
                                      └─ local_inference → Ollama/vLLM 直连 (隐私敏感, 本地执行)
                   ← [Result Queue] ← 结果写入
                   ← [Feedback Queue] ← 反馈 → MEMORY.md 自学习闭环

调用路径清晰，结果可追溯:
  - direct_local:    Hermes → Ollama(11434)
  - gateway:         Hermes → OfficialGW(3005) → Agent → Volcano
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
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "60.0"))
OFFICIAL_GW_TIMEOUT = float(os.getenv("OFFICIAL_GW_TIMEOUT", "120.0"))

# Multimodal model configuration
MULTIMODAL_MODEL = os.getenv("MULTIMODAL_MODEL", "llava:7b")
LOCAL_MODEL = os.getenv("LOCAL_MODEL", "qwen2.5:3b")


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
            # Step 1: Route via Hermes Agent (Memory-driven intelligent routing)
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

        4-category routing (no Bridge/Gateway middle layer):
          - direct_local    → Ollama/vLLM 直连 (单步问答, 低延迟)
          - gateway         → OfficialGW 直连 (多步批处理/Volcano/Agent)
          - multimodal      → 本地多模态模型直连 (图片/音频/视频)
          - local_inference → Ollama/vLLM 直连 (隐私敏感, 本地执行)
        """
        route_path = routing["route_path"]

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
        prompt = request.get("prompt", "")
        model = LOCAL_MODEL

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

    def _dispatch_to_official_gw(self, request: Dict, routing: Dict) -> Dict:
        """Dispatch directly to OpenClaw Official Gateway.

        OfficialGW handles:
        - Multi-step batch processing (Agent → Volcano scheduling)
        - Code execution tasks
        - Tool call / Agent chain tasks
        """
        prompt = request.get("prompt", "")
        context = request.get("context", [])
        messages = []
        if context:
            messages.extend(context)
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": "openclaw/default",
            "messages": messages,
            "max_tokens": 512,
        }
        if request.get("tools"):
            payload["tools"] = request["tools"]
            payload["tool_choice"] = request.get("tool_choice", "auto")

        headers = {"Content-Type": "application/json"}
        gw_token = os.getenv("OPENCLAW_TOKEN", "")
        if gw_token:
            headers["Authorization"] = f"Bearer {gw_token}"

        start = time.time()
        with httpx.Client(timeout=OFFICIAL_GW_TIMEOUT) as client:
            resp = client.post(
                f"{OFFICIAL_GW_URL}/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
        elapsed_ms = int((time.time() - start) * 1000)

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

    def _dispatch_to_multimodal(self, request: Dict, routing: Dict) -> Dict:
        """Dispatch to local multimodal model (image/audio/video processing).

        Uses Ollama with a multimodal-capable model (e.g., llava).
        Falls back to standard local model if multimodal model is unavailable.
        """
        prompt = request.get("prompt", "")
        model = MULTIMODAL_MODEL

        # Build messages with optional image content
        messages = [{"role": "user", "content": prompt}]
        # If request has attachments (images), add them to the message
        attachments = request.get("attachments", [])
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
            "options": {"num_ctx": 4096, "temperature": 0.7},
        }

        start = time.time()
        try:
            with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
                resp = client.post(f"{OLLAMA_URL}/v1/chat/completions", json=payload)
                resp.raise_for_status()
            elapsed_ms = int((time.time() - start) * 1000)

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
            # Fallback: if multimodal model unavailable, use standard local model
            logger.warning(f"Multimodal model '{model}' unavailable, "
                           f"falling back to local model: {e}")
            with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
                fallback_payload = {
                    "model": LOCAL_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"num_ctx": 4096, "temperature": 0.7},
                }
                resp = client.post(f"{OLLAMA_URL}/v1/chat/completions", json=fallback_payload)
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
                "fallback_reason": f"Multimodal model '{model}' unavailable: {str(e)[:80]}",
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
