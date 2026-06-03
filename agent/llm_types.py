"""Provider-neutral LLM request and response types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ResponseMode = Literal["text", "json_object"]
ThinkingMode = Literal["disabled", "enabled"]
ReasoningEffort = Literal["high", "max"]
LLMErrorKind = Literal[
    "provider_bad_request",
    "provider_auth_error",
    "rate_limited",
    "provider_server_error",
    "provider_overloaded",
    "timeout",
    "network_error",
    "invalid_response",
    "unknown",
]


@dataclass(slots=True)
class LLMRequest:
    purpose: str
    messages: list[dict[str, Any]]
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    response_mode: ResponseMode = "text"
    thinking: ThinkingMode = "disabled"
    reasoning_effort: ReasoningEffort | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    timeout_s: float | None = None
    retry: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Phase 4: trace linkage
    turn_id: str = ""
    call_id: str = ""


@dataclass(slots=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0
    estimated: bool = False
    provider_details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LLMResponse:
    content: str
    provider: str
    model: str
    usage: LLMUsage
    latency_ms: int
    finish_reason: str = ""
    reasoning_content: str = ""
    tool_calls: list[Any] | None = None
    request_id: str = ""
    error: "LLMError | None" = None
    # Debug only: never persist or expose this in user-visible responses.
    raw_response: Any = None


@dataclass(slots=True)
class LLMError(Exception):
    kind: LLMErrorKind
    message: str
    provider: str
    status_code: int | None = None
    provider_code: str | None = None
    retryable: bool = False

    def __str__(self) -> str:
        parts = [self.kind, self.message]
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.provider_code:
            parts.append(f"code={self.provider_code}")
        return " | ".join(parts)
