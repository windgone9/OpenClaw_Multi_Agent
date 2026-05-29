import logging
from dataclasses import dataclass, field
from typing import List, Optional

from scheduler.models.request import DispatchRequest, Priority, RequestType
from scheduler.models.model_config import ModelEndpoint, ModelType, ModelRegistry
from scheduler.strategy.priority import PriorityStrategy
from scheduler.strategy.load_balance import LoadBalanceStrategy
from scheduler.strategy.adaptive import AdaptiveStrategy

logger = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    agent_type: str
    selected_endpoint: Optional[ModelEndpoint] = None
    fallback_endpoints: List[ModelEndpoint] = field(default_factory=list)
    strategy_name: str = "unknown"
    reason: str = ""
    use_openclaw: bool = False


class StrategyRouter:
    def __init__(self, registry: ModelRegistry, use_openclaw: bool = True):
        self.registry = registry
        self.priority_strategy = PriorityStrategy(registry)
        self.load_balance_strategy = LoadBalanceStrategy(registry)
        self.adaptive_strategy = AdaptiveStrategy(registry)
        self.use_openclaw = use_openclaw

    def route(self, request: DispatchRequest) -> RoutingDecision:
        if self.use_openclaw:
            return self._route_adaptive(request)

        return self._route_legacy(request)

    def _route_adaptive(self, request: DispatchRequest) -> RoutingDecision:
        agent_type, strategy = self.adaptive_strategy.route(request)

        if strategy == AdaptiveStrategy.PRIVACY_FIRST:
            return self._route_local(request, strategy_name="adaptive_privacy_first",
                                     reason="Privacy constraint: local model required",
                                     use_openclaw=True)

        if strategy == AdaptiveStrategy.LOCAL_FIRST:
            return self._route_local_first_adaptive(request)

        if strategy == AdaptiveStrategy.CLOUD_FIRST:
            return self._route_cloud_first_adaptive(request)

        if strategy == AdaptiveStrategy.LATENCY_OPTIMIZED:
            return self._route_latency_optimized(request)

        if strategy == AdaptiveStrategy.COST_OPTIMIZED:
            return self._route_cost_optimized(request)

        if strategy == AdaptiveStrategy.CAPABILITY_OPTIMIZED:
            return self._route_capability_optimized(request)

        return self._route_default(request)

    def _route_local_first_adaptive(self, request: DispatchRequest) -> RoutingDecision:
        local_eps = self.registry.get_local_available()
        cloud_eps = self.registry.get_cloud_available()

        if local_eps:
            selected = self.load_balance_strategy.select(
                request, local_eps, algorithm=LoadBalanceStrategy.POWER_OF_TWO
            )
            fallback_local = self.load_balance_strategy.distribute(request, local_eps, count=3)[1:]
            fallback = fallback_local + cloud_eps[:2]
            return RoutingDecision(
                agent_type=ModelType.LOCAL.value,
                selected_endpoint=selected,
                fallback_endpoints=fallback,
                strategy_name="adaptive_local_first",
                reason=f"Adaptive: local model {selected.name} selected (zero cost, low latency)",
                use_openclaw=True,
            )

        if cloud_eps:
            selected = self.load_balance_strategy.select(
                request, cloud_eps, algorithm=LoadBalanceStrategy.LEAST_CONNECTIONS
            )
            fallback = self.load_balance_strategy.distribute(request, cloud_eps, count=3)[1:]
            return RoutingDecision(
                agent_type=ModelType.CLOUD.value,
                selected_endpoint=selected,
                fallback_endpoints=fallback,
                strategy_name="adaptive_local_first_cloud_fallback",
                reason=f"Adaptive: no local available, cloud model {selected.name} selected",
                use_openclaw=True,
            )

        return RoutingDecision(
            agent_type=ModelType.LOCAL.value,
            selected_endpoint=None,
            strategy_name="adaptive_no_endpoint",
            reason="No available model endpoints",
            use_openclaw=True,
        )

    def _route_cloud_first_adaptive(self, request: DispatchRequest) -> RoutingDecision:
        cloud_eps = self.registry.get_cloud_available()
        local_eps = self.registry.get_local_available()

        if cloud_eps:
            if request.type == RequestType.TOOL_CALL:
                tool_capable = [ep for ep in cloud_eps if ep.supports_tools]
                if tool_capable:
                    selected = self.priority_strategy.select(request, tool_capable)
                    fallback = [ep for ep in cloud_eps if ep.name != selected.name][:2]
                    fallback.extend(local_eps[:1])
                    return RoutingDecision(
                        agent_type=ModelType.CLOUD.value,
                        selected_endpoint=selected,
                        fallback_endpoints=fallback,
                        strategy_name="adaptive_cloud_tool_call",
                        reason=f"Adaptive: tool-capable cloud model {selected.name} for tool_call",
                        use_openclaw=True,
                    )

            selected = self.priority_strategy.select(request, cloud_eps)
            if selected:
                fallback = self.priority_strategy.rank(request, cloud_eps)[1:3]
                fallback.extend(local_eps[:1])
                return RoutingDecision(
                    agent_type=ModelType.CLOUD.value,
                    selected_endpoint=selected,
                    fallback_endpoints=fallback,
                    strategy_name="adaptive_cloud_first",
                    reason=f"Adaptive: cloud model {selected.name} selected",
                    use_openclaw=True,
                )

        if local_eps:
            selected = self.load_balance_strategy.select(request, local_eps)
            fallback = self.load_balance_strategy.distribute(request, local_eps, count=3)[1:]
            return RoutingDecision(
                agent_type=ModelType.LOCAL.value,
                selected_endpoint=selected,
                fallback_endpoints=fallback,
                strategy_name="adaptive_cloud_first_local_fallback",
                reason=f"Adaptive: no cloud available, local model {selected.name} selected",
                use_openclaw=True,
            )

        return RoutingDecision(
            agent_type=ModelType.CLOUD.value,
            selected_endpoint=None,
            strategy_name="adaptive_no_endpoint",
            reason="No available model endpoints",
            use_openclaw=True,
        )

    def _route_latency_optimized(self, request: DispatchRequest) -> RoutingDecision:
        local_eps = self.registry.get_local_available()
        cloud_eps = self.registry.get_cloud_available()

        all_eps = local_eps + cloud_eps
        if not all_eps:
            return RoutingDecision(
                agent_type=ModelType.LOCAL.value,
                selected_endpoint=None,
                strategy_name="adaptive_latency_none",
                reason="No available endpoints for latency-optimized routing",
                use_openclaw=True,
            )

        valid_eps = [ep for ep in all_eps if ep.avg_latency_ms > 0]
        if valid_eps:
            selected = min(valid_eps, key=lambda ep: ep.avg_latency_ms)
            fallback = sorted(
                [ep for ep in valid_eps if ep.name != selected.name],
                key=lambda ep: ep.avg_latency_ms
            )[:3]
            return RoutingDecision(
                agent_type=selected.model_type.value,
                selected_endpoint=selected,
                fallback_endpoints=fallback,
                strategy_name="adaptive_latency_optimized",
                reason=f"Adaptive: lowest latency model {selected.name} ({selected.avg_latency_ms}ms)",
                use_openclaw=True,
            )

        if local_eps:
            selected = self.load_balance_strategy.select(request, local_eps, algorithm=LoadBalanceStrategy.POWER_OF_TWO)
            fallback = self.load_balance_strategy.distribute(request, local_eps, count=3)[1:]
            fallback.extend(cloud_eps[:2])
            return RoutingDecision(
                agent_type=ModelType.LOCAL.value,
                selected_endpoint=selected,
                fallback_endpoints=fallback,
                strategy_name="adaptive_latency_local_default",
                reason=f"Adaptive: local model {selected.name} (likely lowest latency)",
                use_openclaw=True,
            )

        selected = cloud_eps[0] if cloud_eps else None
        return RoutingDecision(
            agent_type=ModelType.CLOUD.value,
            selected_endpoint=selected,
            fallback_endpoints=cloud_eps[1:3] if len(cloud_eps) > 1 else [],
            strategy_name="adaptive_latency_cloud_default",
            reason="Adaptive: cloud model (only option)",
            use_openclaw=True,
        )

    def _route_cost_optimized(self, request: DispatchRequest) -> RoutingDecision:
        local_eps = self.registry.get_local_available()
        if local_eps:
            selected = self.load_balance_strategy.select(request, local_eps, algorithm=LoadBalanceStrategy.POWER_OF_TWO)
            fallback = self.load_balance_strategy.distribute(request, local_eps, count=3)[1:]
            cloud_eps = self.registry.get_cloud_available()
            free_cloud = [ep for ep in cloud_eps if ep.cost_per_1k_input_tokens <= self.adaptive_strategy._cost_threshold]
            fallback.extend(free_cloud[:2])
            return RoutingDecision(
                agent_type=ModelType.LOCAL.value,
                selected_endpoint=selected,
                fallback_endpoints=fallback,
                strategy_name="adaptive_cost_optimized",
                reason=f"Adaptive: free local model {selected.name} (zero cost)",
                use_openclaw=True,
            )

        cloud_eps = self.registry.get_cloud_available()
        free_cloud = [ep for ep in cloud_eps if ep.cost_per_1k_input_tokens <= self.adaptive_strategy._cost_threshold]
        if free_cloud:
            selected = min(free_cloud, key=lambda ep: ep.cost_per_1k_input_tokens)
            fallback = [ep for ep in free_cloud if ep.name != selected.name][:2]
            return RoutingDecision(
                agent_type=ModelType.CLOUD.value,
                selected_endpoint=selected,
                fallback_endpoints=fallback,
                strategy_name="adaptive_cost_optimized_cloud",
                reason=f"Adaptive: cheapest cloud model {selected.name} (${selected.cost_per_1k_input_tokens}/1k)",
                use_openclaw=True,
            )

        if cloud_eps:
            selected = min(cloud_eps, key=lambda ep: ep.cost_per_1k_input_tokens)
            fallback = [ep for ep in cloud_eps if ep.name != selected.name][:2]
            return RoutingDecision(
                agent_type=ModelType.CLOUD.value,
                selected_endpoint=selected,
                fallback_endpoints=fallback,
                strategy_name="adaptive_cost_cheapest_cloud",
                reason=f"Adaptive: cheapest available cloud model {selected.name}",
                use_openclaw=True,
            )

        return RoutingDecision(
            agent_type=ModelType.LOCAL.value,
            selected_endpoint=None,
            strategy_name="adaptive_cost_no_endpoint",
            reason="No available endpoints for cost-optimized routing",
            use_openclaw=True,
        )

    def _route_capability_optimized(self, request: DispatchRequest) -> RoutingDecision:
        cloud_eps = self.registry.get_cloud_available()
        local_eps = self.registry.get_local_available()

        if request.type == RequestType.TOOL_CALL:
            tool_capable = [ep for ep in cloud_eps if ep.supports_tools]
            if tool_capable:
                selected = self.priority_strategy.select(request, tool_capable)
                fallback = [ep for ep in tool_capable if ep.name != selected.name][:2]
                fallback.extend(local_eps[:1])
                return RoutingDecision(
                    agent_type=ModelType.CLOUD.value,
                    selected_endpoint=selected,
                    fallback_endpoints=fallback,
                    strategy_name="adaptive_capability_tool_call",
                    reason=f"Adaptive: tool-capable model {selected.name} for tool_call",
                    use_openclaw=True,
                )

        context_length = self.adaptive_strategy._estimate_context_length(request)
        if context_length > self.adaptive_strategy.LONG_CONTEXT_THRESHOLD:
            long_context_cloud = [ep for ep in cloud_eps if ep.max_context_length >= context_length]
            if long_context_cloud:
                selected = self.priority_strategy.select(request, long_context_cloud)
                fallback = [ep for ep in long_context_cloud if ep.name != selected.name][:2]
                return RoutingDecision(
                    agent_type=ModelType.CLOUD.value,
                    selected_endpoint=selected,
                    fallback_endpoints=fallback,
                    strategy_name="adaptive_capability_long_context",
                    reason=f"Adaptive: long-context model {selected.name} ({selected.max_context_length} tokens)",
                    use_openclaw=True,
                )

        if request.type in (RequestType.EMBEDDING, RequestType.IMAGE, RequestType.AUDIO):
            return self._route_specialized(request)

        if local_eps and self.adaptive_strategy._can_local_handle(request, local_eps):
            selected = self.load_balance_strategy.select(request, local_eps, algorithm=LoadBalanceStrategy.POWER_OF_TWO)
            fallback = self.load_balance_strategy.distribute(request, local_eps, count=3)[1:]
            fallback.extend(cloud_eps[:2])
            return RoutingDecision(
                agent_type=ModelType.LOCAL.value,
                selected_endpoint=selected,
                fallback_endpoints=fallback,
                strategy_name="adaptive_capability_local_sufficient",
                reason=f"Adaptive: local model {selected.name} sufficient for capability",
                use_openclaw=True,
            )

        if cloud_eps:
            selected = self.priority_strategy.select(request, cloud_eps)
            fallback = self.priority_strategy.rank(request, cloud_eps)[1:3]
            return RoutingDecision(
                agent_type=ModelType.CLOUD.value,
                selected_endpoint=selected,
                fallback_endpoints=fallback,
                strategy_name="adaptive_capability_cloud",
                reason=f"Adaptive: cloud model {selected.name} for capability requirement",
                use_openclaw=True,
            )

        return RoutingDecision(
            agent_type=ModelType.LOCAL.value,
            selected_endpoint=None,
            strategy_name="adaptive_capability_no_endpoint",
            reason="No endpoints meet capability requirements",
            use_openclaw=True,
        )

    def _route_local(self, request: DispatchRequest, strategy_name: str = "local_required",
                     reason: str = "", use_openclaw: bool = False) -> RoutingDecision:
        local_eps = self.registry.get_local_available()
        if not local_eps:
            cloud_eps = self.registry.get_cloud_available()
            if cloud_eps:
                selected = self.load_balance_strategy.select(request, cloud_eps)
                fallback = self.load_balance_strategy.distribute(request, cloud_eps, count=2)[1:]
                return RoutingDecision(
                    agent_type=ModelType.CLOUD.value,
                    selected_endpoint=selected,
                    fallback_endpoints=fallback,
                    strategy_name=f"{strategy_name}_cloud_fallback",
                    reason="No local models available, falling back to cloud",
                    use_openclaw=use_openclaw,
                )
            return RoutingDecision(
                agent_type=ModelType.LOCAL.value,
                selected_endpoint=None,
                strategy_name=f"{strategy_name}_none_available",
                reason="No local or cloud models available",
                use_openclaw=use_openclaw,
            )

        selected = self.load_balance_strategy.select(request, local_eps, algorithm=LoadBalanceStrategy.LEAST_CONNECTIONS)
        fallback = self.load_balance_strategy.distribute(request, local_eps, count=3)[1:]
        cloud_eps = self.registry.get_cloud_available()
        if cloud_eps:
            fallback.extend(cloud_eps[:2])

        return RoutingDecision(
            agent_type=ModelType.LOCAL.value,
            selected_endpoint=selected,
            fallback_endpoints=fallback,
            strategy_name=strategy_name,
            reason=reason or f"Local model required, selected {selected.name}",
            use_openclaw=use_openclaw,
        )

    def _route_specialized(self, request: DispatchRequest) -> RoutingDecision:
        all_eps = self.registry.get_available()

        matching = []
        for ep in all_eps:
            if request.type == RequestType.EMBEDDING and "embedding" in (ep.tags or []):
                matching.append(ep)
            elif request.type == RequestType.IMAGE and "image" in (ep.tags or []):
                matching.append(ep)
            elif request.type == RequestType.AUDIO and "audio" in (ep.tags or []):
                matching.append(ep)

        if not matching:
            matching = all_eps

        if not matching:
            return RoutingDecision(
                agent_type=ModelType.CLOUD.value,
                selected_endpoint=None,
                strategy_name="specialized_none_available",
                reason=f"No endpoints for specialized type {request.type.value}",
                use_openclaw=True,
            )

        selected = self.priority_strategy.select(request, matching)
        if selected is None:
            return RoutingDecision(
                agent_type=ModelType.CLOUD.value,
                selected_endpoint=None,
                strategy_name="specialized_no_selection",
                reason=f"Could not select endpoint for type {request.type.value}",
                use_openclaw=True,
            )

        fallback = [ep for ep in matching if ep.name != selected.name][:2]
        agent_type = selected.model_type.value

        return RoutingDecision(
            agent_type=agent_type,
            selected_endpoint=selected,
            fallback_endpoints=fallback,
            strategy_name=f"adaptive_specialized_{request.type.value}",
            reason=f"Adaptive: specialized routing for {request.type.value}, selected {selected.name}",
            use_openclaw=True,
        )

    def _route_default(self, request: DispatchRequest) -> RoutingDecision:
        local_eps = self.registry.get_local_available()
        cloud_eps = self.registry.get_cloud_available()

        all_eps = local_eps + cloud_eps
        if not all_eps:
            return RoutingDecision(
                agent_type=ModelType.LOCAL.value,
                selected_endpoint=None,
                strategy_name="default_none_available",
                reason="No model endpoints available",
                use_openclaw=True,
            )

        if local_eps:
            selected = self.load_balance_strategy.select(
                request, local_eps, algorithm=LoadBalanceStrategy.POWER_OF_TWO
            )
            fallback_local = self.load_balance_strategy.distribute(request, local_eps, count=3)[1:]
            fallback = fallback_local + cloud_eps[:2]
            return RoutingDecision(
                agent_type=ModelType.LOCAL.value,
                selected_endpoint=selected,
                fallback_endpoints=fallback,
                strategy_name="adaptive_default_local_first",
                reason=f"Adaptive: local model {selected.name} selected",
                use_openclaw=True,
            )

        selected = self.load_balance_strategy.select(
            request, cloud_eps, algorithm=LoadBalanceStrategy.LEAST_CONNECTIONS
        )
        fallback = self.load_balance_strategy.distribute(request, cloud_eps, count=3)[1:]

        return RoutingDecision(
            agent_type=ModelType.CLOUD.value,
            selected_endpoint=selected,
            fallback_endpoints=fallback,
            strategy_name="adaptive_default_cloud_only",
            reason=f"Adaptive: cloud model {selected.name} selected (no local available)",
            use_openclaw=True,
        )

    def _route_legacy(self, request: DispatchRequest) -> RoutingDecision:
        if request.constraints and request.constraints.require_local:
            return self._route_local(request)

        if request.constraints and request.constraints.preferred_providers:
            preferred = set(request.constraints.preferred_providers)
            local_eps = self.registry.get_local_available()
            cloud_eps = self.registry.get_cloud_available()
            has_local_pref = any(ep.provider.value in preferred for ep in local_eps)
            has_cloud_pref = any(ep.provider.value in preferred for ep in cloud_eps)
            if has_local_pref and not has_cloud_pref:
                return self._route_local(request)
            if has_cloud_pref and not has_local_pref:
                return self._route_cloud(request)

        if request.priority in (Priority.CRITICAL, Priority.HIGH):
            return self._route_priority_high(request)

        if request.type in (RequestType.EMBEDDING, RequestType.IMAGE, RequestType.AUDIO):
            return self._route_specialized(request)

        return self._route_default(request)

    def _route_priority_high(self, request: DispatchRequest) -> RoutingDecision:
        local_eps = self.registry.get_local_available()
        cloud_eps = self.registry.get_cloud_available()

        if local_eps:
            ranked = self.priority_strategy.rank(request, local_eps)
            if ranked:
                selected = ranked[0]
                fallback = ranked[1:3] + cloud_eps[:2]
                return RoutingDecision(
                    agent_type=ModelType.LOCAL.value,
                    selected_endpoint=selected,
                    fallback_endpoints=fallback,
                    strategy_name="priority_high_local_first",
                    reason=f"High priority request routed to local model {selected.name}",
                )

        if cloud_eps:
            ranked = self.priority_strategy.rank(request, cloud_eps)
            if ranked:
                selected = ranked[0]
                fallback = ranked[1:3]
                return RoutingDecision(
                    agent_type=ModelType.CLOUD.value,
                    selected_endpoint=selected,
                    fallback_endpoints=fallback,
                    strategy_name="priority_high_cloud_fallback",
                    reason=f"High priority request routed to cloud model {selected.name} (no local available)",
                )

        return RoutingDecision(
            agent_type=ModelType.LOCAL.value,
            selected_endpoint=None,
            strategy_name="priority_high_no_endpoint",
            reason="No available endpoints for high priority request",
        )

    def _route_cloud(self, request: DispatchRequest) -> RoutingDecision:
        cloud_eps = self.registry.get_cloud_available()
        if not cloud_eps:
            return RoutingDecision(
                agent_type=ModelType.CLOUD.value,
                selected_endpoint=None,
                strategy_name="cloud_none_available",
                reason="No cloud models available",
            )

        selected = self.priority_strategy.select(request, cloud_eps)
        fallback = self.priority_strategy.rank(request, cloud_eps)[1:3]

        return RoutingDecision(
            agent_type=ModelType.CLOUD.value,
            selected_endpoint=selected,
            fallback_endpoints=fallback,
            strategy_name="cloud_priority",
            reason=f"Cloud model selected: {selected.name}",
        )

    def record_adaptive_result(self, model_type: str, success: bool, latency_ms: int) -> None:
        self.adaptive_strategy.record_result(model_type, success, latency_ms)

    def get_adaptive_state(self) -> dict:
        return self.adaptive_strategy.get_adaptive_state()
