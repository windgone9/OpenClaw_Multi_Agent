import logging
import re
from typing import Optional

from scheduler.models.request import DispatchRequest
from scheduler.models.response import ModelResult
from scheduler.hooks.hook_manager import PostHook

logger = logging.getLogger(__name__)


class ResponseSanitizationHook(PostHook):
    def __init__(self):
        super().__init__(name="response_sanitization", priority=10)
        self._sensitive_patterns = [
            re.compile(r'(api[_-]?key\s*[:=]\s*)["\']?[\w\-]{20,}["\']?', re.IGNORECASE),
            re.compile(r'(password\s*[:=]\s*)["\']?[\w\-]{8,}["\']?', re.IGNORECASE),
            re.compile(r'(token\s*[:=]\s*)["\']?[\w\-]{20,}["\']?', re.IGNORECASE),
            re.compile(r'(secret\s*[:=]\s*)["\']?[\w\-]{20,}["\']?', re.IGNORECASE),
        ]

    async def execute(self, request: DispatchRequest, result: ModelResult) -> ModelResult:
        if isinstance(result.output, str):
            for pattern in self._sensitive_patterns:
                result.output = pattern.sub(r'\1[REDACTED]', result.output)
        return result


class CostCalculationHook(PostHook):
    def __init__(self):
        super().__init__(name="cost_calculation", priority=20)

    async def execute(self, request: DispatchRequest, result: ModelResult) -> ModelResult:
        if result.cost is not None:
            return result

        if result.usage:
            input_tokens = result.usage.get("prompt_tokens", 0)
            output_tokens = result.usage.get("completion_tokens", 0)
            estimated_cost = (input_tokens / 1000) * 0.001 + (output_tokens / 1000) * 0.002
            result.cost = round(estimated_cost, 6)

        return result


class ResponseLoggingHook(PostHook):
    def __init__(self):
        super().__init__(name="response_logging", priority=90)

    async def execute(self, request: DispatchRequest, result: ModelResult) -> ModelResult:
        output_preview = ""
        if isinstance(result.output, str):
            output_preview = result.output[:200] + "..." if len(result.output) > 200 else result.output

        logger.info(
            f"[RESULT] appid={request.appid} model={result.model_name} "
            f"provider={result.provider} latency={result.latency_ms}ms "
            f"cost={result.cost} output_len={len(str(result.output))}"
        )
        return result


class RetryDecisionHook(PostHook):
    def __init__(self, max_retries: int = 2):
        super().__init__(name="retry_decision", priority=30)
        self.max_retries = max_retries
        self._retry_count: dict = {}

    async def execute(self, request: DispatchRequest, result: ModelResult) -> ModelResult:
        request_id = request.request_id or request.appid

        if result.finish_reason == "error" or result.output is None or result.output == "":
            retries = self._retry_count.get(request_id, 0)
            if retries < self.max_retries:
                self._retry_count[request_id] = retries + 1
                logger.warning(
                    f"RetryDecisionHook: marking for retry, attempt {retries + 1}/{self.max_retries} "
                    f"for request {request_id}"
                )
            else:
                logger.error(
                    f"RetryDecisionHook: max retries ({self.max_retries}) exceeded for request {request_id}"
                )
                self._retry_count.pop(request_id, None)
        else:
            self._retry_count.pop(request_id, None)

        return result
