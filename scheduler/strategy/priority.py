import logging
from typing import List, Optional

from scheduler.models.request import DispatchRequest, Priority, RequestType
from scheduler.models.model_config import ModelEndpoint, ModelType, ModelRegistry

logger = logging.getLogger(__name__)


class PriorityStrategy:
    def __init__(self, registry: ModelRegistry):
        self.registry = registry

    def select(self, request: DispatchRequest, candidates: List[ModelEndpoint]) -> Optional[ModelEndpoint]:
        if not candidates:
            return None

        scored = [(ep, self._score(ep, request)) for ep in candidates]
        scored.sort(key=lambda x: x[1], reverse=True)

        logger.debug(f"PriorityStrategy scores: {[(ep.name, s) for ep, s in scored]}")
        return scored[0][0]

    def _score(self, endpoint: ModelEndpoint, request: DispatchRequest) -> float:
        score = 100.0

        score -= (endpoint.priority - 1) * 15.0

        score -= endpoint.load_factor * 20.0

        score += endpoint.success_rate * 10.0

        if endpoint.avg_latency_ms > 0:
            if request.constraints and request.constraints.max_latency_ms:
                if endpoint.avg_latency_ms > request.constraints.max_latency_ms:
                    score -= 50.0
                else:
                    latency_ratio = endpoint.avg_latency_ms / request.constraints.max_latency_ms
                    score += (1.0 - latency_ratio) * 15.0

        if request.constraints and request.constraints.require_local:
            if endpoint.model_type == ModelType.LOCAL:
                score += 30.0
            else:
                score -= 100.0

        if request.priority == Priority.CRITICAL:
            if endpoint.model_type == ModelType.LOCAL and endpoint.avg_latency_ms < 500:
                score += 20.0

        if request.priority in (Priority.LOW, Priority.BACKGROUND):
            if endpoint.cost_per_1k_input_tokens == 0:
                score += 15.0

        if request.model_hint:
            if request.model_hint.lower() in endpoint.name.lower():
                score += 25.0
            elif request.model_hint.lower() in endpoint.model_id.lower():
                score += 20.0

        if request.constraints and request.constraints.preferred_providers:
            if endpoint.provider.value in request.constraints.preferred_providers:
                score += 20.0

        if request.constraints and request.constraints.excluded_providers:
            if endpoint.provider.value in request.constraints.excluded_providers:
                score -= 100.0

        return score

    def rank(self, request: DispatchRequest, candidates: List[ModelEndpoint]) -> List[ModelEndpoint]:
        scored = [(ep, self._score(ep, request)) for ep in candidates if ep.is_available]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [ep for ep, _ in scored]
