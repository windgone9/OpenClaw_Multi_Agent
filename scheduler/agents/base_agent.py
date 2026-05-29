import asyncio
import time
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import httpx

from scheduler.models.request import DispatchRequest
from scheduler.models.response import ModelResult, ErrorDetail
from scheduler.models.model_config import ModelEndpoint, ModelRegistry

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    def __init__(self, name: str, registry: ModelRegistry):
        self.name = name
        self.registry = registry
        self._client: Optional[httpx.AsyncClient] = None

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @abstractmethod
    async def execute(self, request: DispatchRequest, endpoint: ModelEndpoint) -> ModelResult:
        pass

    @abstractmethod
    def can_handle(self, request: DispatchRequest) -> bool:
        pass

    async def call_model_api(
        self,
        endpoint: ModelEndpoint,
        request: DispatchRequest,
    ) -> ModelResult:
        start_time = time.time()
        endpoint.increment_load()
        try:
            client = await self.get_client()

            # Always directly connect to model API for fast response
            payload = self._build_payload(endpoint, request)
            target_url = f"{endpoint.base_url}/chat/completions"
            headers = self._build_headers(endpoint)
            timeout = (request.timeout_ms or 30000) / 1000.0

            response = await client.post(
                target_url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()

            latency_ms = int((time.time() - start_time) * 1000)
            endpoint.update_latency(latency_ms)
            endpoint.update_success_rate(True)

            result = self._parse_response(data, endpoint, latency_ms)
            result.routed_via_gateway = False
            result.actual_model = None

            return result

        except httpx.TimeoutException:
            latency_ms = int((time.time() - start_time) * 1000)
            endpoint.update_success_rate(False)
            raise TimeoutError(f"Model {endpoint.name} timed out after {latency_ms}ms")

        except httpx.HTTPStatusError as e:
            latency_ms = int((time.time() - start_time) * 1000)
            endpoint.update_success_rate(False)
            raise RuntimeError(f"Model {endpoint.name} returned HTTP {e.response.status_code}: {e.response.text}")

        except Exception as e:
            endpoint.update_success_rate(False)
            raise RuntimeError(f"Model {endpoint.name} error: {str(e)}")

        finally:
            endpoint.decrement_load()

    def _build_payload(self, endpoint: ModelEndpoint, request: DispatchRequest) -> Dict[str, Any]:
        messages = []
        if request.context:
            messages.extend(request.context)
        messages.append({"role": "user", "content": request.prompt})

        payload: Dict[str, Any] = {
            "model": endpoint.model_id,
            "messages": messages,
            "max_tokens": 1024,
        }

        if request.parameters:
            payload.update(request.parameters)

        if request.constraints and request.constraints.require_streaming:
            payload["stream"] = True

        if request.tools and endpoint.supports_tools:
            payload["tools"] = [t.dict() for t in request.tools]
            if request.tool_choice:
                payload["tool_choice"] = request.tool_choice
            else:
                payload["tool_choice"] = "auto"

        return payload

    def _build_headers(self, endpoint: ModelEndpoint) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        return headers

    def _parse_response(self, data: Dict[str, Any], endpoint: ModelEndpoint, latency_ms: int) -> ModelResult:
        choices = data.get("choices", [])
        output = ""
        finish_reason = None
        reasoning = None
        tool_calls = None
        if choices:
            message = choices[0].get("message", {})
            output = message.get("content", "")
            reasoning = message.get("reasoning_content")
            finish_reason = choices[0].get("finish_reason")
            tool_calls = message.get("tool_calls")
            if not output and reasoning:
                output = reasoning
                reasoning = None
            if not output and tool_calls:
                tc_summaries = []
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    tc_summaries.append(f"[Tool Call] {fn.get('name', '?')}({fn.get('arguments', '')})")
                output = "\n".join(tc_summaries)

        usage = data.get("usage")
        cost = None
        if usage:
            input_cost = (usage.get("prompt_tokens", 0) / 1000) * endpoint.cost_per_1k_input_tokens
            output_cost = (usage.get("completion_tokens", 0) / 1000) * endpoint.cost_per_1k_output_tokens
            cost = round(input_cost + output_cost, 6)

        return ModelResult(
            model_name=endpoint.name,
            model_type=endpoint.model_type.value,
            provider=endpoint.provider.value,
            output=output,
            usage=usage,
            latency_ms=latency_ms,
            cost=cost,
            finish_reason=finish_reason,
        )

    async def execute_with_fallback(
        self,
        request: DispatchRequest,
        endpoints: List[ModelEndpoint],
        max_retries: int = 2,
    ) -> ModelResult:
        last_error = None
        attempts = 0
        for endpoint in endpoints:
            if not endpoint.is_available:
                continue
            if attempts >= max_retries + 1:
                break
            try:
                result = await self.execute(request, endpoint)
                return result
            except Exception as e:
                last_error = e
                logger.warning(f"Agent {self.name}: endpoint {endpoint.name} failed: {e}")
                attempts += 1

        raise RuntimeError(f"All endpoints failed for agent {self.name}. Last error: {last_error}")
