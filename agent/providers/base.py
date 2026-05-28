"""Base interfaces for provider adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent.llm_types import LLMRequest, LLMResponse


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider: str
    api_key: str
    base_url: str
    default_model: str
    smart_model: str | None = None
    timeout_s: float = 60.0
    max_retries: int = 1
    thinking_default: str = "disabled"


class ProviderAdapter(Protocol):
    name: str

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Run a non-streaming chat completion."""
        ...
