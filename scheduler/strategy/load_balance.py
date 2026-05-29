import random
import logging
from typing import List, Optional

from scheduler.models.request import DispatchRequest
from scheduler.models.model_config import ModelEndpoint, ModelRegistry

logger = logging.getLogger(__name__)


class LoadBalanceStrategy:
    ROUND_ROBIN = "round_robin"
    WEIGHTED_RANDOM = "weighted_random"
    LEAST_CONNECTIONS = "least_connections"
    LEAST_LATENCY = "least_latency"
    POWER_OF_TWO = "power_of_two"

    def __init__(self, registry: ModelRegistry):
        self.registry = registry
        self._rr_index: dict = {}

    def select(
        self,
        request: DispatchRequest,
        candidates: List[ModelEndpoint],
        algorithm: str = LEAST_CONNECTIONS,
    ) -> Optional[ModelEndpoint]:
        available = [ep for ep in candidates if ep.is_available]
        if not available:
            return None

        if algorithm == self.ROUND_ROBIN:
            return self._round_robin(available)
        elif algorithm == self.WEIGHTED_RANDOM:
            return self._weighted_random(available)
        elif algorithm == self.LEAST_CONNECTIONS:
            return self._least_connections(available)
        elif algorithm == self.LEAST_LATENCY:
            return self._least_latency(available)
        elif algorithm == self.POWER_OF_TWO:
            return self._power_of_two(available)
        else:
            return self._least_connections(available)

    def _round_robin(self, candidates: List[ModelEndpoint]) -> ModelEndpoint:
        key = "default"
        idx = self._rr_index.get(key, 0)
        selected = candidates[idx % len(candidates)]
        self._rr_index[key] = idx + 1
        return selected

    def _weighted_random(self, candidates: List[ModelEndpoint]) -> ModelEndpoint:
        weights = [ep.weight for ep in candidates]
        total = sum(weights)
        if total == 0:
            return random.choice(candidates)
        r = random.uniform(0, total)
        cumulative = 0
        for ep, w in zip(candidates, weights):
            cumulative += w
            if r <= cumulative:
                return ep
        return candidates[-1]

    def _least_connections(self, candidates: List[ModelEndpoint]) -> ModelEndpoint:
        return min(candidates, key=lambda ep: ep.current_load)

    def _least_latency(self, candidates: List[ModelEndpoint]) -> ModelEndpoint:
        valid = [ep for ep in candidates if ep.avg_latency_ms > 0]
        if not valid:
            return candidates[0]
        return min(valid, key=lambda ep: ep.avg_latency_ms)

    def _power_of_two(self, candidates: List[ModelEndpoint]) -> ModelEndpoint:
        if len(candidates) <= 2:
            return self._least_connections(candidates)

        pair = random.sample(candidates, 2)
        return min(pair, key=lambda ep: ep.current_load)

    def distribute(
        self,
        request: DispatchRequest,
        candidates: List[ModelEndpoint],
        algorithm: str = LEAST_CONNECTIONS,
        count: int = 3,
    ) -> List[ModelEndpoint]:
        available = [ep for ep in candidates if ep.is_available]
        if not available:
            return []

        ranked = self._rank_by_load(available)
        return ranked[:count]

    def _rank_by_load(self, candidates: List[ModelEndpoint]) -> List[ModelEndpoint]:
        return sorted(candidates, key=lambda ep: (ep.load_factor, ep.avg_latency_ms))
