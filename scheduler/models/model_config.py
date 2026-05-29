from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class ModelType(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"
    HYBRID = "hybrid"


class ModelProvider(str, Enum):
    OLLAMA = "ollama"
    LMSTUDIO = "lmstudio"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    AZURE = "azure"
    DEEPSEEK = "deepseek"
    MOONSHOT = "moonshot"
    CUSTOM = "custom"


class ModelEndpoint(BaseModel):
    name: str = Field(..., description="Unique model endpoint name")
    display_name: Optional[str] = Field(None, description="Human-readable model name")
    model_type: ModelType = Field(..., description="Whether this is a local or cloud model")
    provider: ModelProvider = Field(..., description="Model provider")
    base_url: str = Field(..., description="API base URL for the model endpoint")
    api_key: Optional[str] = Field(None, description="API key (if required)")
    model_id: str = Field(..., description="Model identifier used in API calls")
    max_context_length: int = Field(4096, description="Maximum context window length")
    supports_streaming: bool = Field(True, description="Whether streaming is supported")
    supports_tools: bool = Field(False, description="Whether tool/function calling is supported")
    cost_per_1k_input_tokens: float = Field(0.0, description="Cost per 1000 input tokens in USD")
    cost_per_1k_output_tokens: float = Field(0.0, description="Cost per 1000 output tokens in USD")
    priority: int = Field(5, description="Default priority (1=highest, 10=lowest)")
    weight: int = Field(1, description="Load balancing weight")
    max_concurrent: int = Field(10, description="Maximum concurrent requests")
    current_load: int = Field(0, description="Current number of active requests")
    avg_latency_ms: int = Field(0, description="Average latency in milliseconds")
    success_rate: float = Field(1.0, description="Success rate (0.0-1.0)")
    enabled: bool = Field(True, description="Whether this endpoint is enabled")
    tags: Optional[List[str]] = Field(None, description="Tags for categorization")
    metadata: Optional[Dict[str, str]] = Field(None, description="Additional metadata")

    @property
    def is_available(self) -> bool:
        return self.enabled and self.current_load < self.max_concurrent

    @property
    def load_factor(self) -> float:
        if self.max_concurrent == 0:
            return 1.0
        return self.current_load / self.max_concurrent

    def increment_load(self) -> None:
        self.current_load += 1

    def decrement_load(self) -> None:
        if self.current_load > 0:
            self.current_load -= 1

    def update_latency(self, latency_ms: int) -> None:
        if self.avg_latency_ms == 0:
            self.avg_latency_ms = latency_ms
        else:
            self.avg_latency_ms = int(0.7 * self.avg_latency_ms + 0.3 * latency_ms)

    def update_success_rate(self, success: bool) -> None:
        alpha = 0.95
        if success:
            self.success_rate = alpha * self.success_rate + (1 - alpha) * 1.0
        else:
            self.success_rate = alpha * self.success_rate + (1 - alpha) * 0.0


class ModelRegistry(BaseModel):
    endpoints: Dict[str, ModelEndpoint] = Field(default_factory=dict)

    def register(self, endpoint: ModelEndpoint) -> None:
        self.endpoints[endpoint.name] = endpoint

    def unregister(self, name: str) -> None:
        self.endpoints.pop(name, None)

    def get(self, name: str) -> Optional[ModelEndpoint]:
        return self.endpoints.get(name)

    def get_by_type(self, model_type: ModelType) -> List[ModelEndpoint]:
        return [ep for ep in self.endpoints.values() if ep.model_type == model_type and ep.enabled]

    def get_by_provider(self, provider: ModelProvider) -> List[ModelEndpoint]:
        return [ep for ep in self.endpoints.values() if ep.provider == provider and ep.enabled]

    def get_available(self) -> List[ModelEndpoint]:
        return [ep for ep in self.endpoints.values() if ep.is_available]

    def get_local_available(self) -> List[ModelEndpoint]:
        return [ep for ep in self.endpoints.values() if ep.is_available and ep.model_type == ModelType.LOCAL]

    def get_cloud_available(self) -> List[ModelEndpoint]:
        return [ep for ep in self.endpoints.values() if ep.is_available and ep.model_type == ModelType.CLOUD]

    def list_all(self) -> List[ModelEndpoint]:
        return list(self.endpoints.values())
