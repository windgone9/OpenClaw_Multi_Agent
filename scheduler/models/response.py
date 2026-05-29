from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from datetime import datetime


class DispatchStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    TIMEOUT = "timeout"
    REJECTED = "rejected"


class ModelResult(BaseModel):
    model_name: str = Field(..., description="Name of the model that processed the request")
    model_type: str = Field(..., description="Type of model (local/cloud)")
    provider: str = Field(..., description="Model provider name")
    output: Any = Field(..., description="Model output content")
    usage: Optional[Dict[str, Any]] = Field(None, description="Token usage statistics")
    latency_ms: int = Field(..., description="Processing latency in milliseconds")
    cost: Optional[float] = Field(None, description="Estimated cost in USD")
    finish_reason: Optional[str] = Field(None, description="Reason for completion")
    routed_via_gateway: bool = Field(False, description="Whether routed through OpenClaw Gateway")
    actual_model: Optional[str] = Field(None, description="Actual model ID used at Gateway")


class ErrorDetail(BaseModel):
    code: str = Field(..., description="Error code")
    message: str = Field(..., description="Error message")
    agent: Optional[str] = Field(None, description="Agent that encountered the error")
    retryable: bool = Field(True, description="Whether the request can be retried")


class DispatchResponse(BaseModel):
    request_id: str = Field(..., description="Unique request identifier")
    appid: str = Field(..., description="Application identifier")
    status: DispatchStatus = Field(..., description="Overall dispatch status")
    result: Optional[ModelResult] = Field(None, description="Primary model result")
    fallback_results: Optional[List[ModelResult]] = Field(None, description="Fallback model results if primary failed")
    error: Optional[ErrorDetail] = Field(None, description="Error details if failed")
    agent_trace: Optional[List[Dict[str, Any]]] = Field(None, description="Agent execution trace for debugging")
    hooks_applied: Optional[List[str]] = Field(None, description="List of hooks that were applied")
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat(), description="Response timestamp")
    total_latency_ms: Optional[int] = Field(None, description="Total end-to-end latency in milliseconds")
