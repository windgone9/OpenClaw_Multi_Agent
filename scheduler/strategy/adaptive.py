import logging
from typing import Dict, List, Optional, Tuple

from scheduler.models.request import DispatchRequest, Priority, RequestType
from scheduler.models.model_config import ModelEndpoint, ModelType, ModelRegistry

logger = logging.getLogger(__name__)


class AdaptiveStrategy:
    LOCAL_FIRST = "local_first"
    CLOUD_FIRST = "cloud_first"
    COST_OPTIMIZED = "cost_optimized"
    LATENCY_OPTIMIZED = "latency_optimized"
    CAPABILITY_OPTIMIZED = "capability_optimized"
    PRIVACY_FIRST = "privacy_first"

    COMPLEXITY_INDICATORS = {"tool_call", "image", "audio"}
    CODE_INDICATORS = {"tool_call"}
    LONG_CONTEXT_THRESHOLD = 16000

    def __init__(self, registry: ModelRegistry):
        self.registry = registry
        self._latency_threshold_ms: int = 3000
        self._cost_threshold: float = 0.01
        self._local_success_streak: int = 0
        self._cloud_success_streak: int = 0
        self._local_fail_streak: int = 0
        self._cloud_fail_streak: int = 0
        self._adaptive_weights: Dict[str, float] = {
            "local": 0.6,
            "cloud": 0.4,
        }
        self._request_history: List[Dict] = []

    def route(self, request: DispatchRequest) -> Tuple[str, str]:
        decision = self._classify_request(request)
        strategy = self._select_strategy(decision, request)
        agent_type = self._determine_agent_type(strategy, request)
        return agent_type, strategy

    def _classify_request(self, request: DispatchRequest) -> str:
        if request.constraints and request.constraints.require_local:
            return self.PRIVACY_FIRST

        if request.constraints and request.constraints.require_gpu:
            return self.CAPABILITY_OPTIMIZED

        if request.constraints and request.constraints.preferred_providers:
            cloud_eps = self.registry.get_cloud_available()
            preferred_cloud = [ep for ep in cloud_eps if ep.provider.value in request.constraints.preferred_providers]
            if preferred_cloud:
                return self.CLOUD_FIRST

        if request.type in self.COMPLEXITY_INDICATORS:
            return self.CAPABILITY_OPTIMIZED

        if request.type == RequestType.EMBEDDING:
            return self.CAPABILITY_OPTIMIZED

        if request.priority in (Priority.CRITICAL, Priority.HIGH):
            return self.LATENCY_OPTIMIZED

        if request.priority in (Priority.LOW, Priority.BACKGROUND):
            return self.COST_OPTIMIZED

        if request.constraints and request.constraints.max_cost_per_request is not None:
            if request.constraints.max_cost_per_request <= 0:
                return self.COST_OPTIMIZED

        if request.constraints and request.constraints.max_latency_ms is not None:
            if request.constraints.max_latency_ms <= 1000:
                return self.LATENCY_OPTIMIZED

        context_length = self._estimate_context_length(request)
        if context_length > self.LONG_CONTEXT_THRESHOLD:
            return self.CAPABILITY_OPTIMIZED

        return self.LOCAL_FIRST

    def _select_strategy(self, decision: str, request: DispatchRequest) -> str:
        if decision == self.PRIVACY_FIRST:
            return self.PRIVACY_FIRST

        if decision == self.CAPABILITY_OPTIMIZED:
            cloud_eps = self.registry.get_cloud_available()
            local_eps = self.registry.get_local_available()
            if request.type in self.COMPLEXITY_INDICATORS:
                tool_capable = [ep for ep in cloud_eps if ep.supports_tools]
                if tool_capable:
                    return self.CAPABILITY_OPTIMIZED
            if local_eps and self._can_local_handle(request, local_eps):
                return self.LOCAL_FIRST
            return self.CLOUD_FIRST

        if decision == self.LATENCY_OPTIMIZED:
            local_eps = self.registry.get_local_available()
            if local_eps:
                best_local = min(local_eps, key=lambda ep: ep.avg_latency_ms if ep.avg_latency_ms > 0 else 99999)
                if best_local.avg_latency_ms > 0 and best_local.avg_latency_ms <= self._latency_threshold_ms:
                    return self.LATENCY_OPTIMIZED
            cloud_eps = self.registry.get_cloud_available()
            if cloud_eps:
                best_cloud = min(cloud_eps, key=lambda ep: ep.avg_latency_ms if ep.avg_latency_ms > 0 else 99999)
                if best_cloud.avg_latency_ms > 0 and best_cloud.avg_latency_ms <= self._latency_threshold_ms:
                    return self.LATENCY_OPTIMIZED
            if local_eps:
                return self.LOCAL_FIRST
            return self.CLOUD_FIRST

        if decision == self.COST_OPTIMIZED:
            local_eps = self.registry.get_local_available()
            if local_eps:
                return self.COST_OPTIMIZED
            free_cloud = [ep for ep in self.registry.get_cloud_available()
                          if ep.cost_per_1k_input_tokens <= self._cost_threshold]
            if free_cloud:
                return self.COST_OPTIMIZED
            return self.CLOUD_FIRST

        if decision == self.CLOUD_FIRST:
            cloud_eps = self.registry.get_cloud_available()
            if cloud_eps:
                return self.CLOUD_FIRST
            local_eps = self.registry.get_local_available()
            if local_eps:
                return self.LOCAL_FIRST
            return self.CLOUD_FIRST

        if decision == self.LOCAL_FIRST:
            local_eps = self.registry.get_local_available()
            if local_eps and self._local_fail_streak < 3:
                return self.LOCAL_FIRST
            if self._local_fail_streak >= 3:
                logger.info("AdaptiveStrategy: local fail streak >= 3, switching to cloud")
                return self.CLOUD_FIRST
            return self.CLOUD_FIRST

        return self.LOCAL_FIRST

    def _determine_agent_type(self, strategy: str, request: DispatchRequest) -> str:
        if strategy == self.PRIVACY_FIRST:
            local_eps = self.registry.get_local_available()
            if local_eps:
                return ModelType.LOCAL.value
            logger.warning("AdaptiveStrategy: require_local but no local available, degrading to cloud")
            return ModelType.CLOUD.value

        if strategy == self.LOCAL_FIRST:
            local_eps = self.registry.get_local_available()
            if local_eps:
                return ModelType.LOCAL.value
            return ModelType.CLOUD.value

        if strategy == self.CLOUD_FIRST:
            cloud_eps = self.registry.get_cloud_available()
            if cloud_eps:
                return ModelType.CLOUD.value
            return ModelType.LOCAL.value

        if strategy == self.LATENCY_OPTIMIZED:
            local_eps = self.registry.get_local_available()
            cloud_eps = self.registry.get_cloud_available()
            if local_eps and cloud_eps:
                best_local_latency = min(
                    (ep.avg_latency_ms for ep in local_eps if ep.avg_latency_ms > 0),
                    default=99999
                )
                best_cloud_latency = min(
                    (ep.avg_latency_ms for ep in cloud_eps if ep.avg_latency_ms > 0),
                    default=99999
                )
                if best_local_latency <= best_cloud_latency:
                    return ModelType.LOCAL.value
                return ModelType.CLOUD.value
            if local_eps:
                return ModelType.LOCAL.value
            return ModelType.CLOUD.value

        if strategy == self.COST_OPTIMIZED:
            local_eps = self.registry.get_local_available()
            if local_eps:
                return ModelType.LOCAL.value
            return ModelType.CLOUD.value

        if strategy == self.CAPABILITY_OPTIMIZED:
            cloud_eps = self.registry.get_cloud_available()
            if request.type in self.COMPLEXITY_INDICATORS:
                tool_capable = [ep for ep in cloud_eps if ep.supports_tools]
                if tool_capable:
                    return ModelType.CLOUD.value
            context_length = self._estimate_context_length(request)
            if context_length > self.LONG_CONTEXT_THRESHOLD:
                long_context = [ep for ep in cloud_eps if ep.max_context_length >= context_length]
                if long_context:
                    return ModelType.CLOUD.value
            local_eps = self.registry.get_local_available()
            if local_eps:
                return ModelType.LOCAL.value
            return ModelType.CLOUD.value

        return ModelType.LOCAL.value

    def record_result(self, model_type: str, success: bool, latency_ms: int) -> None:
        if model_type == ModelType.LOCAL.value:
            if success:
                self._local_success_streak += 1
                self._local_fail_streak = 0
            else:
                self._local_fail_streak += 1
                self._local_success_streak = 0
        else:
            if success:
                self._cloud_success_streak += 1
                self._cloud_fail_streak = 0
            else:
                self._cloud_fail_streak += 1
                self._cloud_success_streak = 0

        self._update_adaptive_weights(model_type, success, latency_ms)

    def _update_adaptive_weights(self, model_type: str, success: bool, latency_ms: int) -> None:
        alpha = 0.1
        if model_type == ModelType.LOCAL.value:
            if success and latency_ms <= self._latency_threshold_ms:
                self._adaptive_weights["local"] = min(
                    1.0, self._adaptive_weights["local"] + alpha
                )
                self._adaptive_weights["cloud"] = max(
                    0.0, self._adaptive_weights["cloud"] - alpha
                )
            elif not success:
                self._adaptive_weights["local"] = max(
                    0.1, self._adaptive_weights["local"] - alpha * 2
                )
                self._adaptive_weights["cloud"] = min(
                    0.9, self._adaptive_weights["cloud"] + alpha * 2
                )
        else:
            if success:
                self._adaptive_weights["cloud"] = min(
                    0.9, self._adaptive_weights["cloud"] + alpha * 0.5
                )
                self._adaptive_weights["local"] = max(
                    0.1, self._adaptive_weights["local"] - alpha * 0.5
                )
            elif not success:
                self._adaptive_weights["cloud"] = max(
                    0.1, self._adaptive_weights["cloud"] - alpha
                )
                self._adaptive_weights["local"] = min(
                    0.9, self._adaptive_weights["local"] + alpha
                )

    def _can_local_handle(self, request: DispatchRequest, local_eps: List[ModelEndpoint]) -> bool:
        if request.type in self.COMPLEXITY_INDICATORS:
            tool_capable = [ep for ep in local_eps if ep.supports_tools]
            if not tool_capable:
                return False

        context_length = self._estimate_context_length(request)
        capable = [ep for ep in local_eps if ep.max_context_length >= context_length]
        if not capable:
            return False

        return True

    def _estimate_context_length(self, request: DispatchRequest) -> int:
        estimated = len(request.prompt) * 2
        if request.context:
            for msg in request.context:
                content = msg.get("content", "")
                estimated += len(content) * 2
        if request.constraints and request.constraints.min_context_length:
            estimated = max(estimated, request.constraints.min_context_length)
        return estimated

    def get_adaptive_state(self) -> Dict:
        return {
            "adaptive_weights": self._adaptive_weights.copy(),
            "local_success_streak": self._local_success_streak,
            "cloud_success_streak": self._cloud_success_streak,
            "local_fail_streak": self._local_fail_streak,
            "cloud_fail_streak": self._cloud_fail_streak,
            "latency_threshold_ms": self._latency_threshold_ms,
            "cost_threshold": self._cost_threshold,
        }
