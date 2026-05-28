"""DeepSeek OpenAI-compatible provider adapter."""

from __future__ import annotations

import time
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

from agent.llm_types import LLMError, LLMRequest, LLMResponse, LLMUsage
from agent.providers.base import ProviderConfig


class DeepSeekAdapter:
    name = "deepseek"

    def __init__(self, config: ProviderConfig):
        self.config = config
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                timeout=self.config.timeout_s,
                max_retries=0,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        payload = self._build_payload(request)
        try:
            response = await self.client.chat.completions.create(**payload)
        except LLMError:
            raise  # Already a gateway error, don't remap
        except Exception as exc:
            raise self._map_error(exc) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._to_response(response, request, latency_ms)

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        model = request.model or self.config.default_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": request.messages,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.timeout_s is not None:
            payload["timeout"] = request.timeout_s
        if request.response_mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if request.tools is not None:
            payload["tools"] = request.tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice

        request_extra = dict(request.extra)
        extra_body = dict(request_extra.pop("extra_body", {}) if "extra_body" in request_extra else {})
        # Always explicitly communicate thinking state to the provider.
        # DeepSeek expects {"type": "enabled"} / {"type": "disabled"}.
        extra_body["thinking"] = {"type": request.thinking}
        if request.reasoning_effort:
            extra_body["reasoning_effort"] = request.reasoning_effort
        if extra_body:
            payload["extra_body"] = extra_body

        for key, value in request_extra.items():
            if key != "extra_body":
                payload[key] = value
        return payload

    def _to_response(self, response: Any, request: LLMRequest, latency_ms: int) -> LLMResponse:
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise LLMError(
                kind="invalid_response",
                message="Provider response did not include choices",
                provider=self.name,
                retryable=False,
            )

        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            raise LLMError(
                kind="invalid_response",
                message="Provider response choice did not include message",
                provider=self.name,
                retryable=False,
            )

        content = getattr(message, "content", None) or ""
        reasoning_content = getattr(message, "reasoning_content", None) or ""
        tool_calls = getattr(message, "tool_calls", None)
        usage = self._extract_usage(getattr(response, "usage", None))
        model = getattr(response, "model", None) or request.model or self.config.default_model
        request_id = (
            getattr(response, "_request_id", None)
            or getattr(response, "id", None)
            or ""
        )
        return LLMResponse(
            content=content,
            provider=self.name,
            model=model,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=getattr(choice, "finish_reason", None) or "",
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
            request_id=request_id,
            raw_response=response,
        )

    def _extract_usage(self, usage: Any) -> LLMUsage:
        if usage is None:
            return LLMUsage(estimated=True)

        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
        provider_details: dict[str, Any] = {}

        prompt_details = getattr(usage, "prompt_tokens_details", None)
        completion_details = getattr(usage, "completion_tokens_details", None)
        cache_hit_tokens = int(getattr(prompt_details, "cached_tokens", 0) or 0) if prompt_details else 0
        cache_miss_tokens = max(prompt_tokens - cache_hit_tokens, 0) if prompt_tokens else 0
        reasoning_tokens = int(getattr(completion_details, "reasoning_tokens", 0) or 0) if completion_details else 0

        if prompt_details is not None:
            provider_details["prompt_tokens_details"] = _public_attrs(prompt_details)
        if completion_details is not None:
            provider_details["completion_tokens_details"] = _public_attrs(completion_details)

        return LLMUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
            reasoning_tokens=reasoning_tokens,
            estimated=False,
            provider_details=provider_details,
        )

    def _map_error(self, exc: Exception) -> LLMError:
        status_code = getattr(exc, "status_code", None)
        provider_code = _provider_code(exc)
        message = str(exc)[:500]

        if isinstance(exc, BadRequestError):
            kind = "provider_bad_request"
            retryable = False
        elif isinstance(exc, AuthenticationError):
            kind = "provider_auth_error"
            retryable = False
        elif isinstance(exc, RateLimitError):
            kind = "rate_limited"
            retryable = True
        elif isinstance(exc, APITimeoutError):
            kind = "timeout"
            retryable = True
        elif isinstance(exc, APIConnectionError):
            kind = "network_error"
            retryable = True
        elif isinstance(exc, APIError) and status_code == 503:
            kind = "provider_overloaded"
            retryable = True
        elif isinstance(exc, APIError) and status_code and status_code >= 500:
            kind = "provider_server_error"
            retryable = True
        else:
            kind = "unknown"
            retryable = bool(status_code and status_code >= 500)

        return LLMError(
            kind=kind,
            message=message,
            provider=self.name,
            status_code=status_code,
            provider_code=provider_code,
            retryable=retryable,
        )


def _provider_code(exc: Exception) -> str | None:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code"):
            return str(error["code"])
        if body.get("code"):
            return str(body["code"])
    return getattr(exc, "code", None)


def _public_attrs(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            pass
    result: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        item = getattr(value, name, None)
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[name] = item
    return result
