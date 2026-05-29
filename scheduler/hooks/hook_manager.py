import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from scheduler.models.request import DispatchRequest
from scheduler.models.response import ModelResult

logger = logging.getLogger(__name__)


class HookType(str, Enum):
    PRE = "pre"
    POST = "post"


class BaseHook(ABC):
    def __init__(self, name: str, hook_type: HookType, priority: int = 100):
        self.name = name
        self.hook_type = hook_type
        self.priority = priority
        self.enabled = True

    @abstractmethod
    async def execute(self, *args, **kwargs):
        pass

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False


class PreHook(BaseHook):
    def __init__(self, name: str, priority: int = 100):
        super().__init__(name, HookType.PRE, priority)

    @abstractmethod
    async def execute(self, request: DispatchRequest) -> DispatchRequest:
        pass


class PostHook(BaseHook):
    def __init__(self, name: str, priority: int = 100):
        super().__init__(name, HookType.POST, priority)

    @abstractmethod
    async def execute(self, request: DispatchRequest, result: ModelResult) -> ModelResult:
        pass


class HookManager:
    def __init__(self):
        self._pre_hooks: List[PreHook] = []
        self._post_hooks: List[PostHook] = []
        self._applied_hooks: List[str] = []

    def register_pre_hook(self, hook: PreHook) -> None:
        self._pre_hooks.append(hook)
        self._pre_hooks.sort(key=lambda h: h.priority)

    def register_post_hook(self, hook: PostHook) -> None:
        self._post_hooks.append(hook)
        self._post_hooks.sort(key=lambda h: h.priority)

    def unregister_hook(self, name: str) -> None:
        self._pre_hooks = [h for h in self._pre_hooks if h.name != name]
        self._post_hooks = [h for h in self._post_hooks if h.name != name]

    async def execute_pre_hooks(self, request: DispatchRequest) -> DispatchRequest:
        self._applied_hooks = []
        for hook in self._pre_hooks:
            if not hook.enabled:
                continue
            try:
                request = await hook.execute(request)
                self._applied_hooks.append(f"pre:{hook.name}")
                logger.debug(f"Pre-hook '{hook.name}' executed successfully")
            except Exception as e:
                logger.error(f"Pre-hook '{hook.name}' failed: {e}")
                if hook.priority <= 50:
                    raise
        return request

    async def execute_post_hooks(self, request: DispatchRequest, result: ModelResult) -> ModelResult:
        for hook in self._post_hooks:
            if not hook.enabled:
                continue
            try:
                result = await hook.execute(request, result)
                self._applied_hooks.append(f"post:{hook.name}")
                logger.debug(f"Post-hook '{hook.name}' executed successfully")
            except Exception as e:
                logger.error(f"Post-hook '{hook.name}' failed: {e}")
                if hook.priority <= 50:
                    raise
        return result

    def get_applied_hooks(self) -> List[str]:
        return self._applied_hooks.copy()

    def list_hooks(self) -> Dict[str, List[Dict[str, Any]]]:
        return {
            "pre": [
                {"name": h.name, "priority": h.priority, "enabled": h.enabled}
                for h in self._pre_hooks
            ],
            "post": [
                {"name": h.name, "priority": h.priority, "enabled": h.enabled}
                for h in self._post_hooks
            ],
        }

    def enable_hook(self, name: str) -> None:
        for h in self._pre_hooks + self._post_hooks:
            if h.name == name:
                h.enable()
                return

    def disable_hook(self, name: str) -> None:
        for h in self._pre_hooks + self._post_hooks:
            if h.name == name:
                h.disable()
                return
