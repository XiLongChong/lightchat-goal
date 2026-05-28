"""Provider-neutral LLM gateway."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable, Coroutine

from agent.llm_types import LLMError, LLMRequest, LLMResponse
from agent.providers.base import ProviderAdapter, ProviderConfig
from agent.providers.deepseek import DeepSeekAdapter

logger = logging.getLogger(__name__)

TraceCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class LLMGateway:
    """Thin gateway that owns provider selection and common defaults."""

    def __init__(self, adapter: ProviderAdapter, config: ProviderConfig):
        self.adapter = adapter
        self.config = config
        self._trace_callbacks: list[TraceCallback] = []

    def on_trace(self, callback: TraceCallback) -> None:
        """Register an async callback for LLM request trace events.

        Callback receives a flat dict suitable for structured logging or DB storage.
        It must NOT include raw_response, reasoning_content, or provider raw errors.
        """
        self._trace_callbacks.append(callback)

    async def _emit_trace(self, trace: dict[str, Any]) -> None:
        for cb in self._trace_callbacks:
            try:
                await cb(trace)
            except Exception:
                logger.debug("trace callback error", exc_info=True)

    @classmethod
    def from_model_config(cls, model_cfg: dict) -> "LLMGateway":
        provider = str(model_cfg.get("provider", "deepseek")).lower()
        default_model = str(
            model_cfg.get("default_model")
            or model_cfg.get("name")
            or "deepseek-v4-flash"
        )
        config = ProviderConfig(
            provider=provider,
            api_key=str(model_cfg.get("api_key", "")),
            base_url=str(model_cfg.get("base_url", "")),
            default_model=default_model,
            smart_model=model_cfg.get("smart_model"),
            timeout_s=float(model_cfg.get("timeout_s", 60) or 60),
            max_retries=int(model_cfg.get("max_retries", 1) or 0),
            thinking_default=str(
                (model_cfg.get("thinking") or {}).get("default", "disabled")
            ),
        )
        adapter = _build_adapter(config)
        return cls(adapter=adapter, config=config)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if request.model is None:
            request.model = self.config.default_model
        if request.timeout_s is None:
            request.timeout_s = self.config.timeout_s
        if request.retry is None:
            request.retry = self.config.max_retries
        if request.thinking == "disabled" and self.config.thinking_default == "enabled":
            request.thinking = "enabled"

        req_id = request.metadata.get("request_id") or str(uuid.uuid4())[:12]
        started = time.perf_counter()
        attempts = max(0, request.retry or 0) + 1
        last_error: LLMError | None = None
        last_latency_ms: int | None = None

        for attempt in range(attempts):
            try:
                response = await self.adapter.complete(request)
                last_latency_ms = int((time.perf_counter() - started) * 1000)
                await self._emit_trace(self._build_trace(
                    request, req_id, True, attempt=attempt + 1,
                    latency_ms=last_latency_ms, usage=response.usage,
                    model_used=response.model, response=response,
                ))
                return response
            except LLMError as exc:
                last_error = exc
                if not exc.retryable or attempt >= attempts - 1:
                    last_latency_ms = int((time.perf_counter() - started) * 1000)
                    await self._emit_trace(self._build_trace(
                        request, req_id, False, attempt=attempt + 1,
                        latency_ms=last_latency_ms,
                        error=exc,
                    ))
                    raise
                await asyncio.sleep(min(0.5 * (2 ** attempt), 4.0))
        raise last_error or LLMError(
            kind="unknown",
            message="LLM request failed without provider error",
            provider=self.config.provider,
            retryable=False,
        )

    def _build_trace(
        self,
        request: LLMRequest,
        request_id: str,
        success: bool,
        *,
        attempt: int,
        latency_ms: int,
        usage: Any = None,
        model_used: str = "",
        error: LLMError | None = None,
        response: Any = None,
    ) -> dict[str, Any]:
        """Build a flat trace dict for observability.

        Deliberately excludes: raw_response, reasoning_content, provider raw errors,
        full tool arguments values, and tool result content.
        Includes native tool call observability fields when relevant.
        """
        trace: dict[str, Any] = {
            "request_id": request_id,
            "task_type": request.purpose,
            "provider": self.config.provider,
            "model": model_used or request.model or self.config.default_model,
            "thinking": request.thinking,
            "response_mode": request.response_mode,
            "latency_ms": latency_ms,
            "attempt_count": attempt,
            "success": success,
            "status_code": None,
            "error_type": None,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "reasoning_tokens": 0,
            # Native tool call observability (not persisted in DB,
            # but available to trace callbacks and structured logging):
            "tools_exposed_count": 0,
            "tools_exposed_names": "",
            "tool_calls_returned_count": 0,
            "tool_calls_returned_names": "",
        }
        if error is not None:
            trace["status_code"] = error.status_code
            trace["error_type"] = error.kind
        if usage is not None:
            trace["prompt_tokens"] = usage.prompt_tokens
            trace["completion_tokens"] = usage.completion_tokens
            trace["total_tokens"] = usage.total_tokens
            trace["cache_hit_tokens"] = usage.cache_hit_tokens
            trace["cache_miss_tokens"] = usage.cache_miss_tokens
            trace["reasoning_tokens"] = usage.reasoning_tokens
        # Native tool observability from request
        tools = getattr(request, "tools", None)
        if tools:
            trace["tools_exposed_count"] = len(tools)
            trace["tools_exposed_names"] = ",".join(
                t.get("function", {}).get("name", "?") for t in tools
            )
        if request.tool_choice is not None:
            trace["tool_choice"] = str(request.tool_choice)
        # Native tool observability from response
        if response is not None:
            tc = getattr(response, "tool_calls", None) or []
            if tc:
                trace["tool_calls_returned_count"] = len(tc)
                trace["tool_calls_returned_names"] = ",".join(
                    str(getattr(getattr(t, "function", None), "name", "") or "?")
                    for t in tc
                )
        return trace


def _build_adapter(config: ProviderConfig) -> ProviderAdapter:
    if config.provider == "deepseek":
        return DeepSeekAdapter(config)
    raise ValueError(f"Unsupported LLM provider: {config.provider}")
