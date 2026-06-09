"""Tests for LLMGateway, DeepSeekAdapter, and trace infrastructure."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from agent.llm_types import (
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    LLMErrorKind,
)
from agent.llm_gateway import LLMGateway
from agent.providers.base import ProviderConfig
from agent.providers.deepseek import DeepSeekAdapter


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# Helpers
# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def _make_config(**overrides):
    return ProviderConfig(
        provider="deepseek",
        api_key="sk-test-key",
        base_url="https://api.deepseek.com",
        default_model="deepseek-v4-flash",
        **overrides,
    )


def _make_mock_openai_response(
    content: str = "hello",
    model: str = "deepseek-v4-flash",
    reasoning_content: str = "",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    total_tokens: int = 15,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    finish_reason: str = "stop",
    tool_calls: list | None = None,
    request_id: str = "req-001",
):
    """Build a fake openai chat completion response object."""
    msg = MagicMock()
    msg.content = content
    msg.reasoning_content = reasoning_content
    msg.tool_calls = tool_calls

    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason

    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = total_tokens
    usage.prompt_tokens_details = MagicMock()
    usage.prompt_tokens_details.cached_tokens = cached_tokens
    usage.completion_tokens_details = MagicMock()
    usage.completion_tokens_details.reasoning_tokens = reasoning_tokens

    resp = MagicMock()
    resp.choices = [choice]
    resp.model = model
    resp.usage = usage
    resp.id = request_id
    return resp


def test_from_model_config_normalizes_deepseek_root_to_anthropic_base():
    gateway = LLMGateway.from_model_config({
        "provider": "deepseek",
        "api_key": "sk-test-key",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-v4-pro",
    })

    assert gateway.config.base_url == "https://api.deepseek.com/anthropic"


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 1. DeepSeekAdapter normal response 鈫?content
# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class TestDeepSeekAdapterContent:
    @pytest.mark.asyncio
    async def test_adapter_returns_content(self):
        adapter = DeepSeekAdapter(_make_config())
        mock_resp = _make_mock_openai_response(content="浣犲ソ涓栫晫")
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        resp = await adapter.complete(req)

        assert resp.content == "浣犲ソ涓栫晫"
        assert resp.provider == "deepseek"
        assert resp.model == "deepseek-v4-flash"
        assert resp.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_adapter_latency_ms_is_int(self):
        adapter = DeepSeekAdapter(_make_config())
        mock_resp = _make_mock_openai_response()
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        resp = await adapter.complete(req)

        assert isinstance(resp.latency_ms, int)
        assert resp.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_adapter_request_id_from_response(self):
        adapter = DeepSeekAdapter(_make_config())
        mock_resp = _make_mock_openai_response()
        mock_resp.id = "resp-abc123"
        mock_resp._request_id = None  # Prevents MagicMock auto-vivification
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        resp = await adapter.complete(req)

        assert resp.request_id == "resp-abc123"


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 2. Usage field extraction
# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class TestUsageExtraction:
    @pytest.mark.asyncio
    async def test_prompt_completion_total_tokens(self):
        adapter = DeepSeekAdapter(_make_config())
        mock_resp = _make_mock_openai_response(
            prompt_tokens=100, completion_tokens=50, total_tokens=150,
        )
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        resp = await adapter.complete(req)

        assert resp.usage.prompt_tokens == 100
        assert resp.usage.completion_tokens == 50
        assert resp.usage.total_tokens == 150
        assert resp.usage.estimated is False

    @pytest.mark.asyncio
    async def test_cache_hit_miss_tokens(self):
        adapter = DeepSeekAdapter(_make_config())
        mock_resp = _make_mock_openai_response(
            prompt_tokens=200, cached_tokens=50,
        )
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        resp = await adapter.complete(req)

        assert resp.usage.cache_hit_tokens == 50
        assert resp.usage.cache_miss_tokens == 150  # 200 - 50

    @pytest.mark.asyncio
    async def test_reasoning_tokens(self):
        adapter = DeepSeekAdapter(_make_config())
        mock_resp = _make_mock_openai_response(reasoning_tokens=80)
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        resp = await adapter.complete(req)

        assert resp.usage.reasoning_tokens == 80

    @pytest.mark.asyncio
    async def test_usage_none_gives_estimated(self):
        adapter = DeepSeekAdapter(_make_config())
        mock_resp = _make_mock_openai_response()
        mock_resp.usage = None  # No usage info
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        resp = await adapter.complete(req)

        assert resp.usage.estimated is True
        assert resp.usage.prompt_tokens == 0

    @pytest.mark.asyncio
    async def test_provider_details_populated(self):
        adapter = DeepSeekAdapter(_make_config())
        mock_resp = _make_mock_openai_response()
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        resp = await adapter.complete(req)

        assert "prompt_tokens_details" in resp.usage.provider_details
        assert "completion_tokens_details" in resp.usage.provider_details


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 3. Error mapping
# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class TestErrorMapping:
    """Each error type maps to the correct LLMErrorKind and retryable flag."""

    @pytest.mark.parametrize("exc_class_name,status_code,expected_kind,retryable", [
        ("BadRequestError", 400, "provider_bad_request", False),
        ("BadRequestError", 422, "provider_bad_request", False),
        ("AuthenticationError", 401, "provider_auth_error", False),
        ("AuthenticationError", 403, "provider_auth_error", False),
        ("RateLimitError", 429, "rate_limited", True),
        ("APITimeoutError", None, "timeout", True),
        ("APIConnectionError", None, "network_error", True),
    ])
    def test_error_mapping(self, exc_class_name, status_code, expected_kind, retryable):
        from openai import (
            APIConnectionError,
            APITimeoutError,
            AuthenticationError,
            BadRequestError,
            RateLimitError,
        )

        exc_map = {
            "BadRequestError": BadRequestError,
            "AuthenticationError": AuthenticationError,
            "RateLimitError": RateLimitError,
            "APITimeoutError": APITimeoutError,
            "APIConnectionError": APIConnectionError,
        }
        exc_cls = exc_map[exc_class_name]

        if status_code is not None:
            # BadRequestError / AuthenticationError / RateLimitError
            # Construct with (message, *, response, body)
            fake_resp = MagicMock()
            fake_resp.status_code = status_code
            fake_resp.headers = MagicMock()
            fake_resp.headers.get.return_value = None
            exc = exc_cls(message="test error", response=fake_resp, body={})
        else:
            # APITimeoutError(request) / APIConnectionError(*, message, request)
            fake_req = MagicMock()
            if exc_class_name == "APITimeoutError":
                exc = exc_cls(request=fake_req)
            else:
                exc = exc_cls(message="test error", request=fake_req)

        adapter = DeepSeekAdapter(_make_config())
        mapped = adapter._map_error(exc)

        assert mapped.kind == expected_kind, f"Expected {expected_kind}, got {mapped.kind}"
        assert mapped.retryable == retryable

    @pytest.mark.parametrize("status_code,expected_kind,retryable", [
        (500, "provider_server_error", True),
        (503, "provider_overloaded", True),
    ])
    def test_server_error_mapping_via_api_error(self, status_code, expected_kind, retryable):
        from openai import APIError

        fake_req = MagicMock()
        exc = APIError(message="server error", request=fake_req, body={})
        exc.status_code = status_code

        adapter = DeepSeekAdapter(_make_config())
        mapped = adapter._map_error(exc)

        assert mapped.kind == expected_kind
        assert mapped.retryable == retryable

    def test_unknown_error(self):
        from openai import APIError

        fake_req = MagicMock()
        exc = APIError(message="weird unknown error", request=fake_req, body={})
        exc.status_code = 418  # I'm a teapot

        adapter = DeepSeekAdapter(_make_config())
        mapped = adapter._map_error(exc)

        assert mapped.kind == "unknown"
        assert mapped.retryable is False  # 418 < 500


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 4. Thinking default disabled
# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class TestThinkingDefaultDisabled:
    @pytest.mark.asyncio
    async def test_thinking_disabled_by_default_in_request(self):
        """LLMRequest defaults thinking='disabled'."""
        req = LLMRequest(purpose="test", messages=[])
        assert req.thinking == "disabled"

    @pytest.mark.asyncio
    async def test_thinking_explicitly_disabled_in_payload(self):
        """When thinking is disabled, payload explicitly sends {"type": "disabled"}."""
        adapter = DeepSeekAdapter(_make_config())
        req = LLMRequest(purpose="test", messages=[], thinking="disabled")
        payload = adapter._build_payload(req)

        assert payload["thinking"] == {"type": "disabled"}

    @pytest.mark.asyncio
    async def test_thinking_explicitly_enabled_in_payload(self):
        """When thinking is enabled, payload sends {"type": "enabled"}."""
        adapter = DeepSeekAdapter(_make_config())
        req = LLMRequest(purpose="test", messages=[], thinking="enabled")
        payload = adapter._build_payload(req)

        assert payload["thinking"] == {"type": "enabled"}

    @pytest.mark.asyncio
    async def test_no_reasoning_effort_in_payload_when_none(self):
        """reasoning_effort=None 鈫?not included in payload."""
        adapter = DeepSeekAdapter(_make_config())
        req = LLMRequest(purpose="test", messages=[], thinking="disabled")
        payload = adapter._build_payload(req)

        assert "reasoning_effort" not in payload

    @pytest.mark.asyncio
    async def test_no_reasoning_content_dependency(self):
        """LLMResponse.reasoning_content is always '' when thinking is disabled."""
        # This test verifies the LLMResponse correctly stores reasoning_content
        # even if the provider returns it (which it shouldn't when thinking is off)
        from agent.providers.deepseek import DeepSeekAdapter
        mock_resp = _make_mock_openai_response(
            content="ok",
            reasoning_content="",  # should be empty with thinking disabled
        )
        adapter = DeepSeekAdapter(_make_config())
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)
        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        resp = await adapter.complete(req)
        assert resp.reasoning_content == ""

    def test_config_thinking_default_enabled_overrides_request(self):
        """Config thinking_default=enabled should override request's default disabled."""
        cfg = _make_config(thinking_default="enabled")
        adapter = DeepSeekAdapter(cfg)
        req = LLMRequest(purpose="test", messages=[])
        payload = adapter._build_payload(req)

        # The gateway.complete() method handles the override, not the adapter.
        # Adapter still sees thinking="disabled" and encodes it as such.
        assert payload["thinking"] == {"type": "disabled"}

    @pytest.mark.asyncio
    async def test_gateway_applies_config_thinking(self):
        """Gateway.complete() should enable thinking when config says so."""
        cfg = _make_config(thinking_default="enabled")
        adapter = DeepSeekAdapter(cfg)
        mock_resp = _make_mock_openai_response()
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)
        gateway = LLMGateway(adapter=adapter, config=cfg)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        await gateway.complete(req)

        # After gateway.complete(), request.thinking should be "enabled"
        assert req.thinking == "enabled"


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 5. response_mode=json_object 鈫?response_format
# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class TestResponseModeMapping:
    @pytest.mark.asyncio
    async def test_json_object_does_not_set_openai_response_format_for_anthropic(self):
        adapter = DeepSeekAdapter(_make_config())
        req = LLMRequest(purpose="test", messages=[], response_mode="json_object")
        payload = adapter._build_payload(req)

        assert "response_format" not in payload

    @pytest.mark.asyncio
    async def test_text_does_not_set_response_format(self):
        adapter = DeepSeekAdapter(_make_config())
        req = LLMRequest(purpose="test", messages=[], response_mode="text")
        payload = adapter._build_payload(req)

        assert "response_format" not in payload



# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 8. raw_response not persisted in traces
# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class TestRawResponseNotPersisted:
    @pytest.mark.asyncio
    async def test_trace_excludes_raw_response(self):
        """Trace dict must not contain raw_response or reasoning_content."""
        cfg = _make_config()
        adapter = DeepSeekAdapter(cfg)
        mock_resp = _make_mock_openai_response(
            content="response text",
            reasoning_content="internal chain of thought",  # should NOT be in trace
        )
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        # Capture trace via callback
        traces: list[dict] = []

        async def capture(trace):
            traces.append(trace)

        gateway = LLMGateway(adapter=adapter, config=cfg)
        gateway.on_trace(capture)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        await gateway.complete(req)

        assert len(traces) == 1
        trace = traces[0]
        # These must NOT be present
        assert "raw_response" not in trace
        assert "reasoning_content" not in trace
        # But usage tokens should be there
        assert trace["prompt_tokens"] == 10
        assert trace["completion_tokens"] == 5
        assert trace["total_tokens"] == 15
        assert trace["success"] is True

    @pytest.mark.asyncio
    async def test_trace_excludes_provider_error_message(self):
        """Trace on error must only include error_type/status_code, not raw message."""
        cfg = _make_config()
        adapter = DeepSeekAdapter(cfg)
        adapter.client.chat.completions.create = AsyncMock(
            side_effect=LLMError(
                kind="provider_auth_error",
                message="API key invalid: sk-abc123secret",  # raw error that should NOT leak
                provider="deepseek",
                status_code=401,
                retryable=False,
            )
        )

        traces: list[dict] = []

        async def capture(trace):
            traces.append(trace)

        gateway = LLMGateway(adapter=adapter, config=cfg)
        gateway.on_trace(capture)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])

        with pytest.raises(LLMError):
            await gateway.complete(req)

        assert len(traces) == 1
        trace = traces[0]
        assert trace["success"] is False
        assert trace["error_type"] == "provider_auth_error"
        assert trace["status_code"] == 401
        assert "sk-abc123secret" not in str(trace)
        assert "API key invalid" not in str(trace)

    @pytest.mark.asyncio
    async def test_llm_response_raw_response_field_exists_but_not_in_trace(self):
        """LLMResponse has raw_response for debugging, but trace callbacks don't see it."""
        config = _make_config()
        adapter = DeepSeekAdapter(config)
        mock_resp = _make_mock_openai_response(content="ok")
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        traces: list[dict] = []

        async def capture(trace):
            traces.append(trace)

        gateway = LLMGateway(adapter=adapter, config=config)
        gateway.on_trace(capture)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        resp = await gateway.complete(req)

        # raw_response IS set on LLMResponse (for debug)
        assert resp.raw_response is not None
        # But trace dict must NOT contain it
        assert "raw_response" not in traces[0]


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 9. Trace callback infrastructure
# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class TestTraceInfrastructure:
    @pytest.mark.asyncio
    async def test_success_trace_has_correct_fields(self):
        cfg = _make_config()
        adapter = DeepSeekAdapter(cfg)
        mock_resp = _make_mock_openai_response(
            content="ok",
            model="deepseek-v4-flash",
            prompt_tokens=20,
            completion_tokens=8,
            total_tokens=28,
            cached_tokens=5,
            reasoning_tokens=3,
        )
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        traces: list[dict] = []

        async def capture(trace):
            traces.append(trace)

        gateway = LLMGateway(adapter=adapter, config=cfg)
        gateway.on_trace(capture)

        req = LLMRequest(
            purpose="chat",
            messages=[{"role": "user", "content": "hi"}],
            response_mode="text",
        )
        await gateway.complete(req)

        trace = traces[0]
        assert trace["task_type"] == "chat"
        assert trace["provider"] == "deepseek"
        assert trace["model"] == "deepseek-v4-flash"
        assert trace["thinking"] == "disabled"
        assert trace["response_mode"] == "text"
        assert trace["latency_ms"] >= 0
        assert trace["attempt_count"] == 1
        assert trace["success"] is True
        assert trace["status_code"] is None
        assert trace["error_type"] is None
        assert trace["prompt_tokens"] == 20
        assert trace["completion_tokens"] == 8
        assert trace["total_tokens"] == 28
        assert trace["cache_hit_tokens"] == 5
        assert trace["cache_miss_tokens"] == 15  # 20 - 5
        assert trace["reasoning_tokens"] == 3
        assert "request_id" in trace
        assert len(trace["request_id"]) > 0

    @pytest.mark.asyncio
    async def test_multiple_callbacks_all_fire(self):
        cfg = _make_config()
        adapter = DeepSeekAdapter(cfg)
        adapter.client.chat.completions.create = AsyncMock(
            return_value=_make_mock_openai_response()
        )
        gateway = LLMGateway(adapter=adapter, config=cfg)

        results: list[str] = []

        async def cb_a(trace):
            results.append("a")

        async def cb_b(trace):
            results.append("b")

        gateway.on_trace(cb_a)
        gateway.on_trace(cb_b)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        await gateway.complete(req)

        assert results == ["a", "b"]

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_break_gateway(self):
        cfg = _make_config()
        adapter = DeepSeekAdapter(cfg)
        adapter.client.chat.completions.create = AsyncMock(
            return_value=_make_mock_openai_response()
        )
        gateway = LLMGateway(adapter=adapter, config=cfg)

        async def bad_cb(trace):
            raise RuntimeError("trace write failed!")

        gateway.on_trace(bad_cb)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])
        # Must not raise
        resp = await gateway.complete(req)
        assert resp.content == "hello"

    @pytest.mark.asyncio
    async def test_error_trace_has_correct_structure(self):
        cfg = _make_config()
        adapter = DeepSeekAdapter(cfg)
        adapter.client.chat.completions.create = AsyncMock(
            side_effect=LLMError(
                kind="rate_limited",
                message="too many requests",
                provider="deepseek",
                status_code=429,
                retryable=True,
            )
        )

        traces: list[dict] = []

        async def capture(trace):
            traces.append(trace)

        gateway = LLMGateway(adapter=adapter, config=cfg)
        gateway.on_trace(capture)

        req = LLMRequest(purpose="test", messages=[{"role": "user", "content": "hi"}])

        with pytest.raises(LLMError):
            await gateway.complete(req)

        trace = traces[0]
        assert trace["success"] is False
        assert trace["error_type"] == "rate_limited"
        assert trace["status_code"] == 429
        assert trace["prompt_tokens"] == 0
        assert trace["completion_tokens"] == 0
        assert trace["latency_ms"] >= 0


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 10. MemoryStore trace persistence (in-memory DB)
# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class TestMemoryStoreTracePersistence:
    @pytest.mark.asyncio
    async def test_save_and_verify_trace(self):
        from memory.store import MemoryStore

        store = MemoryStore(db_path=":memory:")
        await store._get_db()  # Initialize DB (creates tables)

        trace = {
            "request_id": "req-test-001",
            "turn_id": "turn-test-001",
            "call_id": "call-test-001",
            "task_type": "chat",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "thinking": "disabled",
            "response_mode": "text",
            "latency_ms": 1234,
            "attempt_count": 1,
            "success": 1,
            "status_code": None,
            "error_type": None,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cache_hit_tokens": 20,
            "cache_miss_tokens": 80,
            "reasoning_tokens": 5,
            "request_json": json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
            "response_json": json.dumps({"tool_calls": []}),
        }
        row_id = await store.save_llm_trace(trace)
        assert row_id is not None
        assert row_id > 0

        # Read back from DB
        db = await store._get_db()
        cursor = await db.execute("SELECT * FROM llm_traces WHERE id = ?", (row_id,))
        row = await cursor.fetchone()
        assert row is not None
        # Verify key fields
        row_dict = dict(row)
        assert row_dict["request_id"] == "req-test-001"
        assert row_dict["turn_id"] == "turn-test-001"
        assert row_dict["call_id"] == "call-test-001"
        assert row_dict["task_type"] == "chat"
        assert row_dict["success"] == 1
        assert row_dict["prompt_tokens"] == 100
        assert row_dict["cache_hit_tokens"] == 20
        assert row_dict["reasoning_tokens"] == 5
        assert row_dict["latency_ms"] == 1234
        assert json.loads(row_dict["request_json"])["messages"][0]["role"] == "user"
        assert json.loads(row_dict["response_json"])["tool_calls"] == []

        await store.close()

    @pytest.mark.asyncio
    async def test_trace_raw_response_not_in_db_schema(self):
        """Verify the llm_traces table has no raw_response column."""
        from memory.store import MemoryStore

        store = MemoryStore(db_path=":memory:")
        await store._get_db()
        db = await store._get_db()
        cursor = await db.execute("PRAGMA table_info('llm_traces')")
        cols = {row["name"] for row in await cursor.fetchall()}
        assert "turn_id" in cols
        assert "call_id" in cols
        assert "request_json" in cols
        assert "response_json" in cols
        assert "raw_response" not in cols
        assert "reasoning_content" not in cols
        assert "error_message" not in cols
        await store.close()


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 11. LLMGateway.from_model_config
# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class TestGatewayFactory:
    def test_from_model_config_deepseek(self):
        cfg = {
            "provider": "deepseek",
            "default_model": "deepseek-v4-flash",
            "smart_model": "deepseek-v4-pro",
            "api_key": "sk-test",
            "base_url": "https://api.deepseek.com",
            "timeout_s": 60,
            "max_retries": 2,
            "thinking": {"default": "disabled"},
        }
        gateway = LLMGateway.from_model_config(cfg)
        assert gateway.config.provider == "deepseek"
        assert gateway.config.default_model == "deepseek-v4-flash"
        assert gateway.config.smart_model == "deepseek-v4-pro"
        assert gateway.config.timeout_s == 60
        assert gateway.config.max_retries == 2
        assert gateway.config.thinking_default == "disabled"

    def test_from_model_config_unknown_provider_raises(self):
        cfg = {"provider": "openai", "api_key": "sk-test"}
        with pytest.raises(ValueError, match="Unsupported LLM provider"):
            LLMGateway.from_model_config(cfg)

    def test_from_model_config_string_timeout_converted(self):
        cfg = {
            "provider": "deepseek",
            "api_key": "sk-test",
            "timeout_s": "120",
        }
        gateway = LLMGateway.from_model_config(cfg)
        assert gateway.config.timeout_s == 120.0


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 12. LLMRequest fields
# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class TestLLMRequestFields:
    def test_all_fields_present(self):
        req = LLMRequest(
            purpose="test",
            messages=[],
            model="m",
            temperature=0.5,
            max_tokens=100,
            response_mode="json_object",
            thinking="disabled",
            reasoning_effort="high",
            tools=[{"type": "function", "name": "f"}],
            tool_choice="auto",
            timeout_s=30,
            retry=3,
            extra={"custom": "value"},
            metadata={"request_id": "abc"},
        )
        assert req.response_mode == "json_object"
        assert req.thinking == "disabled"
        assert req.reasoning_effort == "high"
        assert req.tools == [{"type": "function", "name": "f"}]
        assert req.tool_choice == "auto"
        assert req.extra == {"custom": "value"}
        assert req.metadata["request_id"] == "abc"

    def test_extra_fields_passed_to_payload(self):
        adapter = DeepSeekAdapter(_make_config())
        req = LLMRequest(
            purpose="test",
            messages=[],
            extra={"top_p": 0.9},
        )
        payload = adapter._build_payload(req)
        assert payload.get("top_p") == 0.9


# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# 13. LLMResponse / LLMUsage / LLMError data classes
# 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class TestDataClasses:
    def test_llm_usage_defaults(self):
        u = LLMUsage()
        assert u.prompt_tokens == 0
        assert u.completion_tokens == 0
        assert u.total_tokens == 0
        assert u.cache_hit_tokens == 0
        assert u.cache_miss_tokens == 0
        assert u.reasoning_tokens == 0
        assert u.estimated is False
        assert u.provider_details == {}

    def test_llm_response_all_fields(self):
        r = LLMResponse(
            content="hello",
            provider="deepseek",
            model="deepseek-v4-flash",
            usage=LLMUsage(prompt_tokens=5),
            latency_ms=100,
            finish_reason="stop",
            reasoning_content="let me think...",
            tool_calls=[{"name": "f"}],
            request_id="req-1",
            raw_response={"choices": []},
        )
        assert r.content == "hello"
        assert r.reasoning_content == "let me think..."
        assert r.tool_calls == [{"name": "f"}]
        assert r.request_id == "req-1"
        assert r.raw_response == {"choices": []}

    def test_llm_error_str(self):
        e = LLMError(
            kind="rate_limited",
            message="too fast",
            provider="deepseek",
            status_code=429,
            provider_code="rate_limit_exceeded",
            retryable=True,
        )
        s = str(e)
        assert "rate_limited" in s
        assert "too fast" in s
        assert "429" in s

    def test_llm_error_optional_fields_none(self):
        e = LLMError(kind="unknown", message="?", provider="x")
        s = str(e)
        assert "unknown" in s
        assert "?" in s
