import logging
from typing import List

from scheduler.models.request import DispatchRequest, RequestType
from scheduler.models.response import ModelResult
from scheduler.models.model_config import ModelEndpoint, ModelType, ModelRegistry
from scheduler.agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)


class CloudModelAgent(BaseAgent):
    def __init__(self, registry: ModelRegistry):
        super().__init__(name="cloud_model_agent", registry=registry)

    def can_handle(self, request: DispatchRequest) -> bool:
        available = self.registry.get_cloud_available()
        if not available:
            return False
        return True

    async def execute(self, request: DispatchRequest, endpoint: ModelEndpoint = None) -> ModelResult:
        if endpoint is None:
            endpoint = self._select_endpoint(request)
            if endpoint is None:
                raise RuntimeError("No available cloud model endpoint")

        logger.info(f"CloudAgent dispatching to {endpoint.name} (provider: {endpoint.provider.value})")
        return await self.call_model_api(endpoint, request)

    def _select_endpoint(self, request: DispatchRequest) -> ModelEndpoint:
        candidates = self.registry.get_cloud_available()
        if not candidates:
            return None

        if request.model_hint:
            for ep in candidates:
                if request.model_hint.lower() in ep.name.lower() or request.model_hint.lower() in ep.model_id.lower():
                    return ep

        if request.constraints and request.constraints.preferred_providers:
            for pref in request.constraints.preferred_providers:
                for ep in candidates:
                    if ep.provider.value == pref:
                        return ep

        if request.constraints and request.constraints.excluded_providers:
            excluded = set(request.constraints.excluded_providers)
            candidates = [ep for ep in candidates if ep.provider.value not in excluded]

        if request.constraints and request.constraints.max_cost_per_request is not None:
            candidates = [ep for ep in candidates if ep.cost_per_1k_input_tokens <= request.constraints.max_cost_per_request]

        if not candidates:
            return None

        candidates.sort(key=lambda ep: (ep.priority, ep.load_factor, ep.avg_latency_ms))
        return candidates[0]

    def get_available_endpoints(self) -> List[ModelEndpoint]:
        return self.registry.get_cloud_available()
