from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class RequestType(str, Enum):
    CHAT = "chat"
    COMPLETION = "completion"
    EMBEDDING = "embedding"
    IMAGE = "image"
    AUDIO = "audio"
    TOOL_CALL = "tool_call"


class Priority(int, Enum):
    CRITICAL = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4
    BACKGROUND = 5


class ModelConstraint(BaseModel):
    max_latency_ms: Optional[int] = Field(None, description="Maximum acceptable latency in milliseconds")
    min_context_length: Optional[int] = Field(None, description="Minimum context window length required")
    require_streaming: Optional[bool] = Field(None, description="Whether streaming response is required")
    require_local: Optional[bool] = Field(None, description="Whether local model is required (data privacy)")
    require_gpu: Optional[bool] = Field(None, description="Whether GPU acceleration is required")
    preferred_providers: Optional[List[str]] = Field(None, description="Preferred model providers")
    excluded_providers: Optional[List[str]] = Field(None, description="Excluded model providers")
    max_cost_per_request: Optional[float] = Field(None, description="Maximum cost per request in USD")


class ToolFunction(BaseModel):
    name: str = Field(..., description="Function name")
    description: str = Field(..., description="Function description")
    parameters: Optional[Dict[str, Any]] = Field(None, description="JSON Schema for function parameters")


class ToolDefinition(BaseModel):
    type: str = Field("function", description="Tool type, always 'function'")
    function: ToolFunction = Field(..., description="Function definition")


class DispatchRequest(BaseModel):
    appid: str = Field(..., description="Application identifier for routing and tracking")
    type: RequestType = Field(..., description="Type of AI request")
    prompt: str = Field(..., description="The input prompt or query text")
    priority: Priority = Field(Priority.NORMAL, description="Request priority level")
    model_hint: Optional[str] = Field(None, description="Optional model name hint for routing")
    constraints: Optional[ModelConstraint] = Field(None, description="Model selection constraints")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Additional model parameters (temperature, top_p, etc.)")
    context: Optional[List[Dict[str, str]]] = Field(None, description="Conversation context messages")
    tools: Optional[List[ToolDefinition]] = Field(None, description="Tool definitions for tool_call requests (OpenAI tools format)")
    tool_choice: Optional[str] = Field(None, description="Tool choice strategy: 'auto', 'none', or specific tool name")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata for tracking")
    request_id: Optional[str] = Field(None, description="Optional request ID for idempotency")
    timeout_ms: Optional[int] = Field(30000, description="Request timeout in milliseconds")
