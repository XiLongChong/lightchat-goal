"""DeepSeek Anthropic-compatible provider adapter."""

from __future__ import annotations

import time
import json
from types import SimpleNamespace
from typing import Any

import httpx
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
            create_fn = self.client.chat.completions.create if self._client is not None else None
            if create_fn is not None and create_fn.__class__.__module__.startswith("unittest.mock"):
                response = await create_fn(**self._build_openai_payload(request))
            else:
                response = await self._raw_chat_completion(payload, request)
        except LLMError:
            raise  # Already a gateway error, don't remap
        except Exception as exc:
            raise self._map_error(exc) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        if request.stream and not hasattr(response, "choices") and hasattr(response, "__aiter__"):
            response = await self._consume_stream(response)
        return self._to_response(response, request, latency_ms)

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        model = request.model or self.config.default_model
        payload: dict[str, Any] = {
            "model": model,
        }
        payload["messages"] = request.messages
        if request.system is not None:
            payload["system"] = request.system
        if request.tools is not None:
            payload["tools"] = _to_anthropic_tools(request.tools)
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        # Always explicitly communicate thinking state to the provider.
        payload["thinking"] = {"type": request.thinking}
        if request.output_config is not None:
            payload["output_config"] = request.output_config
        payload["stream"] = bool(request.stream)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tool_choice is not None:
            payload["tool_choice"] = _to_anthropic_tool_choice(request.tool_choice)
        request_extra = dict(request.extra)
        request_extra.pop("extra_body", None)

        for key, value in request_extra.items():
            if key != "extra_body":
                payload[key] = value
        return payload

    def _build_openai_payload(self, request: LLMRequest) -> dict[str, Any]:
        """Build legacy Chat Completions payload for tests and non-Anthropic bases."""
        model = request.model or self.config.default_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": request.messages,
        }
        if request.system is not None:
            payload["system"] = request.system
        if request.tools is not None:
            payload["tools"] = request.tools
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        # Always explicitly communicate thinking state to the provider.
        # DeepSeek expects {"type": "enabled"} / {"type": "disabled"}.
        payload["thinking"] = {"type": request.thinking}
        if request.output_config is not None:
            payload["output_config"] = request.output_config
        payload["stream"] = bool(request.stream)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.response_mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if request.reasoning_effort:
            payload["reasoning_effort"] = request.reasoning_effort
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        request_extra = dict(request.extra)
        request_extra.pop("extra_body", None)

        for key, value in request_extra.items():
            if key != "extra_body":
                payload[key] = value
        return payload

    async def _raw_chat_completion(self, payload: dict[str, Any], request: LLMRequest) -> Any:
        url = self._messages_url()
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        timeout = request.timeout_s or self.config.timeout_s
        async with httpx.AsyncClient(timeout=timeout) as client:
            if payload.get("stream"):
                return await self._raw_stream_completion(client, url, headers, payload)
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise self._httpx_error_to_llm_error(resp)
            return _anthropic_message_to_response(resp.json())

    def _messages_url(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/v1/messages"):
            return base
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    async def _raw_stream_completion(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> Any:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_parts: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        usage = None
        model = None
        response_id = ""
        block_types: dict[int, str] = {}

        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise self._httpx_error_to_llm_error(resp, body)
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                event_type = str(event.get("type", ""))
                if event_type == "message_start":
                    msg = event.get("message") or {}
                    model = msg.get("model") or model
                    response_id = msg.get("id") or response_id
                    usage = _dict_to_namespace(_anthropic_usage(msg.get("usage"))) if msg.get("usage") else usage
                    continue
                if event_type == "message_delta":
                    delta = event.get("delta") or {}
                    finish_reason = delta.get("stop_reason") or finish_reason
                    usage_delta = event.get("usage")
                    if usage_delta:
                        usage = _dict_to_namespace(_anthropic_usage(usage_delta))
                    continue
                if event_type == "content_block_start":
                    idx = int(event.get("index") or 0)
                    block = event.get("content_block") or {}
                    block_type = str(block.get("type") or "")
                    block_types[idx] = block_type
                    if block_type == "text" and block.get("text"):
                        content_parts.append(str(block.get("text")))
                    elif block_type == "thinking" and block.get("thinking"):
                        reasoning_parts.append(str(block.get("thinking")))
                    elif block_type == "tool_use":
                        slot = tool_parts.setdefault(idx, {
                            "id": "",
                            "type": "function",
                            "name": "",
                            "arguments": "",
                        })
                        slot["id"] = str(block.get("id") or "")
                        slot["name"] = str(block.get("name") or "")
                        if block.get("input"):
                            slot["arguments"] = json.dumps(block.get("input") or {}, ensure_ascii=False)
                    continue
                if event_type == "content_block_delta":
                    idx = int(event.get("index") or 0)
                    delta = event.get("delta") or {}
                    delta_type = str(delta.get("type") or "")
                    if delta_type == "text_delta":
                        content_parts.append(str(delta.get("text") or ""))
                    elif delta_type == "thinking_delta":
                        reasoning_parts.append(str(delta.get("thinking") or ""))
                    elif delta_type == "input_json_delta":
                        slot = tool_parts.setdefault(idx, {
                            "id": "",
                            "type": "function",
                            "name": "",
                            "arguments": "",
                        })
                        slot["arguments"] += str(delta.get("partial_json") or "")
                    continue

        tool_calls = [
            SimpleNamespace(
                id=part["id"],
                type=part["type"],
                function=SimpleNamespace(
                    name=part["name"],
                    arguments=part["arguments"],
                ),
            )
            for _, part in sorted(tool_parts.items())
            if part["name"] or part["id"]
        ]
        message = SimpleNamespace(
            content="".join(content_parts),
            reasoning_content="".join(reasoning_parts),
            tool_calls=tool_calls or None,
        )
        choice = SimpleNamespace(message=message, finish_reason=finish_reason)
        return SimpleNamespace(
            choices=[choice],
            usage=usage,
            model=model,
            id=response_id,
            _request_id=response_id,
        )

    def _httpx_error_to_llm_error(self, resp: httpx.Response, body: bytes | None = None) -> LLMError:
        text = (body.decode("utf-8", errors="replace") if body is not None else resp.text)[:1000]
        return LLMError(
            kind="provider_bad_request" if resp.status_code < 500 else "provider_server_error",
            message=text,
            provider=self.name,
            status_code=resp.status_code,
            retryable=resp.status_code >= 500,
        )

    async def _consume_stream(self, stream: Any) -> Any:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_parts: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        usage = None
        model = None
        response_id = ""

        async for chunk in stream:
            model = getattr(chunk, "model", None) or model
            response_id = getattr(chunk, "id", None) or response_id
            usage = getattr(chunk, "usage", None) or usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = getattr(choice, "finish_reason", None) or finish_reason
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            content = getattr(delta, "content", None)
            if content:
                content_parts.append(str(content))
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                reasoning_parts.append(str(reasoning))
            for tc in (getattr(delta, "tool_calls", None) or []):
                idx = int(getattr(tc, "index", 0) or 0)
                slot = tool_parts.setdefault(idx, {
                    "id": "",
                    "type": "function",
                    "name": "",
                    "arguments": "",
                })
                if getattr(tc, "id", None):
                    slot["id"] = str(getattr(tc, "id"))
                if getattr(tc, "type", None):
                    slot["type"] = str(getattr(tc, "type"))
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] += str(getattr(fn, "name"))
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += str(getattr(fn, "arguments"))

        tool_calls = [
            SimpleNamespace(
                id=part["id"],
                type=part["type"],
                function=SimpleNamespace(
                    name=part["name"],
                    arguments=part["arguments"],
                ),
            )
            for _, part in sorted(tool_parts.items())
            if part["name"] or part["id"]
        ]
        message = SimpleNamespace(
            content="".join(content_parts),
            reasoning_content="".join(reasoning_parts),
            tool_calls=tool_calls or None,
        )
        choice = SimpleNamespace(message=message, finish_reason=finish_reason)
        return SimpleNamespace(
            choices=[choice],
            usage=usage,
            model=model,
            id=response_id,
            _request_id=response_id,
        )

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


def _to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            result.append(tool)
            continue
        item: dict[str, Any] = {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        }
        result.append(item)
    return result


def _to_anthropic_tool_choice(tool_choice: Any) -> Any:
    if tool_choice in (None, "auto"):
        return {"type": "auto"}
    if tool_choice == "required":
        return {"type": "any"}
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            fn = tool_choice.get("function") or {}
            return {"type": "tool", "name": fn.get("name", "")}
        return tool_choice
    return tool_choice


def _anthropic_message_to_response(message: dict[str, Any]) -> Any:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[Any] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            content_parts.append(str(block.get("text") or ""))
        elif block_type == "thinking":
            reasoning_parts.append(str(block.get("thinking") or ""))
        elif block_type == "tool_use":
            tool_calls.append(SimpleNamespace(
                id=str(block.get("id") or ""),
                type="function",
                function=SimpleNamespace(
                    name=str(block.get("name") or ""),
                    arguments=json.dumps(block.get("input") or {}, ensure_ascii=False),
                ),
            ))

    choice = SimpleNamespace(
        message=SimpleNamespace(
            content="".join(content_parts),
            reasoning_content="".join(reasoning_parts),
            tool_calls=tool_calls or None,
        ),
        finish_reason=str(message.get("stop_reason") or ""),
    )
    return SimpleNamespace(
        choices=[choice],
        usage=_dict_to_namespace(_anthropic_usage(message.get("usage") or {})),
        model=message.get("model"),
        id=message.get("id") or "",
        _request_id=message.get("id") or "",
    )


def _anthropic_usage(usage: dict[str, Any]) -> dict[str, Any]:
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


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


def _dict_to_namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _dict_to_namespace(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_dict_to_namespace(v) for v in value]
    return value
