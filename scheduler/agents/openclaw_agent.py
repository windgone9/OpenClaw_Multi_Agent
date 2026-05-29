import time
import logging
from typing import Any, Dict, List, Optional

import httpx

from scheduler.models.request import DispatchRequest
from scheduler.models.response import ModelResult, ErrorDetail
from scheduler.models.model_config import ModelEndpoint, ModelRegistry
from scheduler.agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)


class OpenClawAgent(BaseAgent):
    def __init__(self, registry: ModelRegistry, bridge_url: str = "http://localhost:3001"):
        super().__init__(name="openclaw_agent", registry=registry)
        self.bridge_url = bridge_url.rstrip("/")

    async def execute(self, request: DispatchRequest, endpoint: ModelEndpoint = None) -> ModelResult:
        if endpoint is not None:
            return await self.call_model_api(endpoint, request)

        bridge_result = await self._dispatch_via_bridge(request)
        if bridge_result is not None:
            return bridge_result

        return await self._fallback_direct(request)

    def can_handle(self, request: DispatchRequest) -> bool:
        return True

    async def _dispatch_via_bridge(self, request: DispatchRequest) -> Optional[ModelResult]:
        start_time = time.time()
        try:
            client = await self.get_client()
            payload = self._build_bridge_payload(request)
            response = await client.post(
                f"{self.bridge_url}/dispatch",
                json=payload,
                timeout=(request.timeout_ms or 30000) / 1000.0,
            )

            if response.status_code >= 400:
                logger.warning(f"OpenClaw Bridge returned HTTP {response.status_code}")
                return None

            data = response.json()
            latency_ms = int((time.time() - start_time) * 1000)

            if data.get("status") == "success" and data.get("result"):
                result_data = data["result"]
                return ModelResult(
                    model_name=result_data.get("model_name", "openclaw"),
                    model_type=result_data.get("model_type", "cloud"),
                    provider=result_data.get("provider", "openclaw"),
                    output=result_data.get("output", ""),
                    usage=result_data.get("usage"),
                    latency_ms=result_data.get("latency_ms", latency_ms),
                    cost=result_data.get("cost"),
                    finish_reason=result_data.get("finish_reason"),
                )

            logger.warning(f"OpenClaw Bridge dispatch failed: {data.get('error', {}).get('message', 'unknown')}")
            return None

        except Exception as e:
            logger.warning(f"OpenClaw Bridge unavailable, falling back to direct dispatch: {e}")
            return None

    async def _fallback_direct(self, request: DispatchRequest) -> ModelResult:
        available = self.registry.get_available()
        if not available:
            raise RuntimeError("No available model endpoints (OpenClaw Bridge and direct both failed)")

        local_eps = self.registry.get_local_available()
        cloud_eps = self.registry.get_cloud_available()

        if request.constraints and request.constraints.require_local and local_eps:
            endpoint = self._select_best(local_eps, request)
        elif local_eps:
            endpoint = self._select_best(local_eps, request)
        elif cloud_eps:
            endpoint = self._select_best(cloud_eps, request)
        else:
            endpoint = available[0]

        if endpoint is None:
            raise RuntimeError("No suitable endpoint found for fallback")

        logger.info(f"OpenClawAgent fallback: dispatching to {endpoint.name}")
        return await self.call_model_api(endpoint, request)

    def _select_best(self, candidates: List[ModelEndpoint], request: DispatchRequest) -> Optional[ModelEndpoint]:
        if not candidates:
            return None

        if request.model_hint:
            for ep in candidates:
                if request.model_hint.lower() in ep.name.lower() or request.model_hint.lower() in ep.model_id.lower():
                    return ep

        candidates.sort(key=lambda ep: (ep.load_factor, ep.avg_latency_ms))
        return candidates[0]

    def _build_bridge_payload(self, request: DispatchRequest) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "appid": request.appid,
            "type": request.type.value,
            "prompt": request.prompt,
            "priority": request.priority.value,
        }

        if request.model_hint:
            payload["model_hint"] = request.model_hint
        if request.parameters:
            payload["parameters"] = request.parameters
        if request.context:
            payload["context"] = request.context
        if request.tools:
            payload["tools"] = [t.dict() for t in request.tools]
        if request.tool_choice:
            payload["tool_choice"] = request.tool_choice
        if request.request_id:
            payload["request_id"] = request.request_id
        if request.timeout_ms:
            payload["timeout_ms"] = request.timeout_ms
        if request.metadata:
            payload["metadata"] = request.metadata

        if request.constraints:
            constraints = {}
            if request.constraints.max_latency_ms is not None:
                constraints["max_latency_ms"] = request.constraints.max_latency_ms
            if request.constraints.require_local is not None:
                constraints["require_local"] = request.constraints.require_local
            if request.constraints.preferred_providers:
                constraints["preferred_providers"] = request.constraints.preferred_providers
            if request.constraints.excluded_providers:
                constraints["excluded_providers"] = request.constraints.excluded_providers
            if request.constraints.max_cost_per_request is not None:
                constraints["max_cost_per_request"] = request.constraints.max_cost_per_request
            if constraints:
                payload["constraints"] = constraints

        return payload

    async def send_agent_message(
        self,
        agent_id: str,
        prompt: str,
        context: Optional[List[Dict[str, str]]] = None,
        parameters: Optional[Dict[str, Any]] = None,
        timeout_ms: int = 30000,
    ) -> Dict[str, Any]:
        try:
            client = await self.get_client()
            payload = {
                "agent_id": agent_id,
                "prompt": prompt,
                "context": context,
                "parameters": parameters,
                "timeout_ms": timeout_ms,
            }
            response = await client.post(
                f"{self.bridge_url}/agent/message",
                json=payload,
                timeout=timeout_ms / 1000.0,
            )
            return response.json()
        except Exception as e:
            logger.error(f"Failed to send agent message to {agent_id}: {e}")
            return {"status": "error", "error": str(e)}

    async def get_bridge_health(self) -> Dict[str, Any]:
        try:
            client = await self.get_client()
            response = await client.get(f"{self.bridge_url}/health", timeout=5.0)
            return response.json()
        except Exception as e:
            logger.warning(f"OpenClaw Bridge health check failed: {e}")
            return {"status": "unavailable", "error": str(e)}

    async def get_bridge_models(self, model_type: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            client = await self.get_client()
            url = f"{self.bridge_url}/models"
            if model_type:
                url += f"?type={model_type}"
            response = await client.get(url, timeout=10.0)
            data = response.json()
            return data.get("models", [])
        except Exception as e:
            logger.warning(f"Failed to get bridge models: {e}")
            return []

    async def get_bridge_stats(self) -> Dict[str, Any]:
        try:
            client = await self.get_client()
            response = await client.get(f"{self.bridge_url}/stats", timeout=5.0)
            return response.json()
        except Exception as e:
            logger.warning(f"Failed to get bridge stats: {e}")
            return {}
