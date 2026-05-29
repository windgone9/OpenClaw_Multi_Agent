import logging
import time
from typing import Dict, Optional

from scheduler.models.request import DispatchRequest, RequestType
from scheduler.models.response import ModelResult
from scheduler.hooks.hook_manager import PreHook

logger = logging.getLogger(__name__)


class RequestValidationHook(PreHook):
    def __init__(self):
        super().__init__(name="request_validation", priority=10)

    async def execute(self, request: DispatchRequest) -> DispatchRequest:
        if not request.appid or not request.appid.strip():
            raise ValueError("appid is required and cannot be empty")

        if not request.prompt or not request.prompt.strip():
            raise ValueError("prompt is required and cannot be empty")

        if request.timeout_ms and request.timeout_ms < 1000:
            raise ValueError("timeout_ms must be at least 1000ms")

        if request.constraints:
            if request.constraints.max_latency_ms and request.constraints.max_latency_ms < 100:
                raise ValueError("max_latency_ms must be at least 100ms")

        return request


class ConstraintEnrichmentHook(PreHook):
    def __init__(self):
        super().__init__(name="constraint_enrichment", priority=20)
        self._appid_defaults: Dict[str, Dict] = {}

    def set_app_defaults(self, appid: str, defaults: Dict) -> None:
        self._appid_defaults[appid] = defaults

    async def execute(self, request: DispatchRequest) -> DispatchRequest:
        defaults = self._appid_defaults.get(request.appid, {})

        if request.constraints is None:
            from scheduler.models.request import ModelConstraint
            request.constraints = ModelConstraint()

        if request.constraints.max_latency_ms is None and "max_latency_ms" in defaults:
            request.constraints.max_latency_ms = defaults["max_latency_ms"]

        if request.constraints.require_local is None and "require_local" in defaults:
            request.constraints.require_local = defaults["require_local"]

        if request.constraints.preferred_providers is None and "preferred_providers" in defaults:
            request.constraints.preferred_providers = defaults["preferred_providers"]

        if request.constraints.excluded_providers is None and "excluded_providers" in defaults:
            request.constraints.excluded_providers = defaults["excluded_providers"]

        if request.priority.value >= 3 and request.constraints.max_cost_per_request is None:
            request.constraints.max_cost_per_request = 0.05

        return request


class RateLimitHook(PreHook):
    def __init__(self, max_requests_per_minute: int = 60, max_requests_per_appid: int = 30):
        super().__init__(name="rate_limit", priority=5)
        self.max_rpm = max_requests_per_minute
        self.max_per_appid = max_requests_per_appid
        self._request_times: Dict[str, list] = {}
        self._appid_times: Dict[str, list] = {}

    async def execute(self, request: DispatchRequest) -> DispatchRequest:
        now = time.time()
        window = 60.0

        self._request_times["global"] = [
            t for t in self._request_times.get("global", []) if now - t < window
        ]
        if len(self._request_times["global"]) >= self.max_rpm:
            raise RuntimeError(f"Global rate limit exceeded: {self.max_rpm} requests/minute")
        self._request_times.setdefault("global", []).append(now)

        self._appid_times[request.appid] = [
            t for t in self._appid_times.get(request.appid, []) if now - t < window
        ]
        if len(self._appid_times.get(request.appid, [])) >= self.max_per_appid:
            raise RuntimeError(
                f"Rate limit exceeded for appid '{request.appid}': {self.max_per_appid} requests/minute"
            )
        self._appid_times.setdefault(request.appid, []).append(now)

        return request


class RequestLoggingHook(PreHook):
    def __init__(self):
        super().__init__(name="request_logging", priority=90)

    async def execute(self, request: DispatchRequest) -> DispatchRequest:
        logger.info(
            f"[DISPATCH] appid={request.appid} type={request.type.value} "
            f"priority={request.priority.name} model_hint={request.model_hint} "
            f"prompt_len={len(request.prompt)}"
        )
        return request
