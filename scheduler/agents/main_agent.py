import asyncio
import time
import uuid
import logging
from typing import Any, Dict, List, Optional

from scheduler.models.request import DispatchRequest, Priority, RequestType
from scheduler.models.response import DispatchResponse, DispatchStatus, ModelResult, ErrorDetail
from scheduler.models.model_config import ModelEndpoint, ModelType, ModelRegistry
from scheduler.agents.base_agent import BaseAgent
from scheduler.agents.local_agent import LocalModelAgent
from scheduler.agents.cloud_agent import CloudModelAgent
from scheduler.agents.openclaw_agent import OpenClawAgent
from scheduler.strategy.router import StrategyRouter
from scheduler.hooks.hook_manager import HookManager

logger = logging.getLogger(__name__)


class MainDispatcherAgent:
    def __init__(self, registry: ModelRegistry, hook_manager: Optional[HookManager] = None,
                 use_openclaw: bool = True, bridge_url: str = "http://localhost:3001"):
        self.registry = registry
        self.local_agent = LocalModelAgent(registry)
        self.cloud_agent = CloudModelAgent(registry)
        self.openclaw_agent = OpenClawAgent(registry, bridge_url=bridge_url)
        self.strategy_router = StrategyRouter(registry, use_openclaw=use_openclaw)
        self.hook_manager = hook_manager or HookManager()
        self.use_openclaw = use_openclaw
        self._agent_trace: List[Dict[str, Any]] = []

    async def dispatch(self, request: DispatchRequest) -> DispatchResponse:
        start_time = time.time()
        request_id = request.request_id or str(uuid.uuid4())
        self._agent_trace = []

        try:
            request = await self.hook_manager.execute_pre_hooks(request)
            self._trace("pre_hooks", "Executed pre-processing hooks")

            if self.use_openclaw:
                response = await self._dispatch_openclaw(request, request_id, start_time)
                if response is not None:
                    return response

            routing_decision = self.strategy_router.route(request)
            ep_info = f"name='{routing_decision.selected_endpoint.name}'" if routing_decision.selected_endpoint else "None"
            self._trace("strategy_router", f"Routing decision: agent={routing_decision.agent_type}, "
                         f"endpoint={ep_info}, "
                         f"strategy={routing_decision.strategy_name}, "
                         f"openclaw={routing_decision.use_openclaw}")

            agent = self._get_agent(routing_decision.agent_type)
            if agent is None:
                return self._build_error_response(
                    request_id, request.appid,
                    "NO_AGENT", f"No agent available for type {routing_decision.agent_type}",
                    retryable=False
                )

            endpoint = routing_decision.selected_endpoint
            if endpoint is None:
                return self._build_error_response(
                    request_id, request.appid,
                    "NO_ENDPOINT", "No suitable model endpoint found",
                    retryable=True
                )

            primary_result = await agent.execute(request, endpoint)
            self._trace(agent.name, f"Primary result from {endpoint.name}, latency={primary_result.latency_ms}ms")

            self.strategy_router.record_adaptive_result(
                routing_decision.agent_type, True, primary_result.latency_ms
            )

            fallback_results = None

            total_latency_ms = int((time.time() - start_time) * 1000)
            primary_result = await self.hook_manager.execute_post_hooks(request, primary_result)
            self._trace("post_hooks", "Executed post-processing hooks")

            response = DispatchResponse(
                request_id=request_id,
                appid=request.appid,
                status=DispatchStatus.SUCCESS,
                result=primary_result,
                fallback_results=fallback_results,
                agent_trace=self._agent_trace,
                hooks_applied=self.hook_manager.get_applied_hooks(),
                total_latency_ms=total_latency_ms,
            )

            return response

        except TimeoutError as e:
            total_latency_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Dispatch timeout for request {request_id}: {e}")
            self.strategy_router.record_adaptive_result("local", False, total_latency_ms)

            fallback_results = None
            if routing_decision.fallback_endpoints:
                self._trace("fallback", "Primary timed out, attempting fallback endpoints")
                fallback_results = await self._try_fallbacks(
                    request, self._get_agent(routing_decision.agent_type),
                    routing_decision.fallback_endpoints
                )

            if fallback_results:
                total_latency_ms = int((time.time() - start_time) * 1000)
                fb = fallback_results[0]
                fb = await self.hook_manager.execute_post_hooks(request, fb)
                self._trace("post_hooks", "Executed post-processing hooks")
                return DispatchResponse(
                    request_id=request_id,
                    appid=request.appid,
                    status=DispatchStatus.PARTIAL,
                    result=fb,
                    fallback_results=fallback_results[1:] if len(fallback_results) > 1 else None,
                    agent_trace=self._agent_trace,
                    hooks_applied=self.hook_manager.get_applied_hooks(),
                    total_latency_ms=total_latency_ms,
                )

            return DispatchResponse(
                request_id=request_id,
                appid=request.appid,
                status=DispatchStatus.TIMEOUT,
                error=ErrorDetail(code="TIMEOUT", message=str(e), retryable=True),
                agent_trace=self._agent_trace,
                hooks_applied=self.hook_manager.get_applied_hooks(),
                total_latency_ms=total_latency_ms,
            )

        except Exception as e:
            total_latency_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Dispatch error for request {request_id}: {e}")
            self.strategy_router.record_adaptive_result("cloud", False, total_latency_ms)

            fallback_results = None
            if routing_decision.fallback_endpoints:
                self._trace("fallback", "Primary failed, attempting fallback endpoints")
                fallback_results = await self._try_fallbacks(
                    request, self._get_agent(routing_decision.agent_type),
                    routing_decision.fallback_endpoints
                )

            if fallback_results:
                total_latency_ms = int((time.time() - start_time) * 1000)
                fb = fallback_results[0]
                fb = await self.hook_manager.execute_post_hooks(request, fb)
                self._trace("post_hooks", "Executed post-processing hooks")
                return DispatchResponse(
                    request_id=request_id,
                    appid=request.appid,
                    status=DispatchStatus.PARTIAL,
                    result=fb,
                    fallback_results=fallback_results[1:] if len(fallback_results) > 1 else None,
                    agent_trace=self._agent_trace,
                    hooks_applied=self.hook_manager.get_applied_hooks(),
                    total_latency_ms=total_latency_ms,
                )

            return DispatchResponse(
                request_id=request_id,
                appid=request.appid,
                status=DispatchStatus.FAILED,
                error=ErrorDetail(code="INTERNAL_ERROR", message=str(e), retryable=True),
                agent_trace=self._agent_trace,
                hooks_applied=self.hook_manager.get_applied_hooks(),
                total_latency_ms=total_latency_ms,
            )

    async def _dispatch_openclaw(self, request: DispatchRequest, request_id: str,
                                  start_time: float) -> Optional[DispatchResponse]:
        self._trace("openclaw_router", "Attempting OpenClaw adaptive routing")

        routing_decision = self.strategy_router.route(request)
        ep_info = f"name='{routing_decision.selected_endpoint.name}'" if routing_decision.selected_endpoint else "None"
        self._trace("openclaw_router", f"OpenClaw routing: agent={routing_decision.agent_type}, "
                     f"endpoint={ep_info}, strategy={routing_decision.strategy_name}")

        try:
            result = await self.openclaw_agent.execute(request, routing_decision.selected_endpoint)

            self.strategy_router.record_adaptive_result(
                routing_decision.agent_type, True, result.latency_ms
            )

            self._trace("openclaw_agent", f"OpenClaw result from {result.model_name}, latency={result.latency_ms}ms")

            fallback_results = None

            total_latency_ms = int((time.time() - start_time) * 1000)
            result = await self.hook_manager.execute_post_hooks(request, result)
            self._trace("post_hooks", "Executed post-processing hooks")

            return DispatchResponse(
                request_id=request_id,
                appid=request.appid,
                status=DispatchStatus.SUCCESS,
                result=result,
                fallback_results=fallback_results,
                agent_trace=self._agent_trace,
                hooks_applied=self.hook_manager.get_applied_hooks(),
                total_latency_ms=total_latency_ms,
            )

        except Exception as e:
            logger.warning(f"OpenClaw dispatch failed, falling back to direct: {e}")
            self._trace("openclaw_agent", f"OpenClaw dispatch failed: {e}, falling back to direct")
            self.strategy_router.record_adaptive_result(
                routing_decision.agent_type, False,
                int((time.time() - start_time) * 1000)
            )
            return None

    async def dispatch_agent_message(self, agent_id: str, prompt: str,
                                      context: Optional[List[Dict[str, str]]] = None,
                                      parameters: Optional[Dict[str, Any]] = None,
                                      timeout_ms: int = 30000) -> Dict[str, Any]:
        if not self.use_openclaw:
            return {"status": "error", "error": "OpenClaw integration is disabled"}

        return await self.openclaw_agent.send_agent_message(
            agent_id=agent_id,
            prompt=prompt,
            context=context,
            parameters=parameters,
            timeout_ms=timeout_ms,
        )

    async def _try_fallbacks(
        self,
        request: DispatchRequest,
        primary_agent: BaseAgent,
        fallback_endpoints: List[ModelEndpoint],
    ) -> List[ModelResult]:
        results = []
        for ep in fallback_endpoints[:2]:
            if not ep.is_available:
                continue
            try:
                agent = self._get_agent(ep.model_type.value)
                if agent is None:
                    continue
                result = await agent.execute(request, ep)
                results.append(result)
                self._trace("fallback", f"Fallback result from {ep.name}, latency={result.latency_ms}ms")
            except Exception as e:
                logger.warning(f"Fallback endpoint {ep.name} failed: {e}")
                self._trace("fallback", f"Fallback {ep.name} failed: {e}")
        return results if results else None

    def _get_agent(self, agent_type: str) -> Optional[BaseAgent]:
        if agent_type == ModelType.LOCAL.value:
            return self.local_agent
        elif agent_type == ModelType.CLOUD.value:
            return self.cloud_agent
        return None

    def _trace(self, agent: str, message: str) -> None:
        self._agent_trace.append({
            "agent": agent,
            "message": message,
            "timestamp": time.time(),
        })

    def _build_error_response(
        self,
        request_id: str,
        appid: str,
        code: str,
        message: str,
        retryable: bool = True,
    ) -> DispatchResponse:
        return DispatchResponse(
            request_id=request_id,
            appid=appid,
            status=DispatchStatus.FAILED,
            error=ErrorDetail(code=code, message=message, retryable=retryable),
            agent_trace=self._agent_trace,
            hooks_applied=self.hook_manager.get_applied_hooks(),
        )

    async def close(self) -> None:
        await self.local_agent.close()
        await self.cloud_agent.close()
        await self.openclaw_agent.close()
