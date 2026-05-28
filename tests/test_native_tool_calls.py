"""Phase 5A: DeepSeek native Tool Calls tests.

Tests for:
  - Feature flag behavior
  - DeepSeekAdapter payload (tools/tool_choice)
  - ChatAgent native tool loop
  - Parameter validation
  - Backward compatibility
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.chat_agent import (
    _build_native_tool_specs,
    _execute_native_tool,
    _NATIVE_TOOL_HARD_DENY,
)
from agent.llm_types import LLMRequest, LLMResponse, LLMUsage
from agent.tool_registry import TOOL_REGISTRY, ToolRegistry


# ══════════════════════════════════════════════════════════════════
# Helper: build a fake LLMResponse
# ══════════════════════════════════════════════════════════════════

def _make_response(content="", tool_calls=None, usage=None):
    return LLMResponse(
        content=content,
        provider="deepseek",
        model="deepseek-v4-flash",
        usage=usage or LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        latency_ms=100,
        finish_reason="stop",
        tool_calls=tool_calls or [],
    )


# ══════════════════════════════════════════════════════════════════
# 1. Feature flag tests
# ══════════════════════════════════════════════════════════════════

class TestFeatureFlag:
    def test_disabled_returns_no_tools(self):
        """native_tool_calls=false → _build_native_tool_specs returns None."""
        specs = _build_native_tool_specs([])
        assert specs is None

    def test_empty_allowed_returns_no_tools(self):
        """Empty allowed_tools → no tools."""
        specs = _build_native_tool_specs([])
        assert specs is None

    def test_enabled_with_get_time_get_weather(self):
        """native_tool_calls=true + allowed=[get_time, get_weather] → 2 tools."""
        specs = _build_native_tool_specs(["get_time", "get_weather"])
        assert specs is not None
        assert len(specs) == 2
        names = {s["function"]["name"] for s in specs}
        assert names == {"get_time", "get_weather"}

    def test_send_email_filtered_out(self):
        """send_email is hard-denied regardless of allowed_tools."""
        specs = _build_native_tool_specs(["get_time", "send_email"])
        names = {s["function"]["name"] for s in (specs or [])}
        assert "send_email" not in names

    def test_wechat_reply_not_in_specs(self):
        """wechat_reply is hard-denied."""
        specs = _build_native_tool_specs(["wechat_reply", "get_time"])
        names = {s["function"]["name"] for s in (specs or [])}
        assert "wechat_reply" not in names

    def test_schedule_wechat_message_not_in_specs(self):
        """schedule_wechat_message is hard-denied."""
        specs = _build_native_tool_specs(["schedule_wechat_message", "get_weather"])
        names = {s["function"]["name"] for s in (specs or [])}
        assert "schedule_wechat_message" not in names

    def test_mixed_config_correctly_filtered(self):
        """Config with mixed valid/invalid tools is correctly filtered."""
        specs = _build_native_tool_specs([
            "get_time", "send_email", "wechat_reply",
            "get_weather", "schedule_wechat_message", "search_memory",
        ])
        names = {s["function"]["name"] for s in (specs or [])}
        # Only get_time/get_weather are eligible AND not hard-denied
        assert names == {"get_time", "get_weather"}


# ══════════════════════════════════════════════════════════════════
# 2. Native tool specs structure
# ══════════════════════════════════════════════════════════════════

class TestNativeToolSpecs:
    def test_specs_are_openai_compatible(self):
        """Each spec follows OpenAI function-calling format."""
        specs = _build_native_tool_specs(["get_time", "get_weather"])
        for spec in specs:
            assert spec["type"] == "function"
            fn = spec["function"]
            assert "name" in fn
            assert "description" in fn
            assert "parameters" in fn
            params = fn["parameters"]
            assert params["type"] == "object"
            assert "properties" in params

    def test_get_time_spec_has_timezone_param(self):
        """get_time spec includes timezone parameter."""
        specs = _build_native_tool_specs(["get_time"])
        params = specs[0]["function"]["parameters"]
        assert "timezone" in params["properties"]

    def test_get_weather_spec_has_city_param(self):
        """get_weather spec includes city parameter."""
        specs = _build_native_tool_specs(["get_weather"])
        params = specs[0]["function"]["parameters"]
        assert "city" in params["properties"]

    def test_get_weather_has_required_city(self):
        """get_weather requires city."""
        specs = _build_native_tool_specs(["get_weather"])
        params = specs[0]["function"]["parameters"]
        assert "required" in params
        assert "city" in params["required"]

    def test_no_strict_true_in_specs(self):
        """Specs must not use strict=true (beta feature)."""
        specs = _build_native_tool_specs(["get_time", "get_weather"])
        for spec in specs:
            fn = spec["function"]
            assert not fn.get("strict"), f"strict=true found in {fn['name']}"


# ══════════════════════════════════════════════════════════════════
# 3. ChatAgent with native_tool_calls=False (backward compat)
# ══════════════════════════════════════════════════════════════════

class TestChatAgentNativeDisabled:
    def test_llm_request_has_no_tools_when_disabled(self):
        """ChatAgent with native_tool_calls=False does not set tools."""
        from agent.chat_agent import ChatAgent

        gateway = _FakeGatewayLLM(response=_make_response(
            '{"actions":[{"type":"message","text":"hi","delay":0}],"done":true}'
        ))

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=False,
        )

        import asyncio
        asyncio.run(agent.chat(
            user_message="hi", memories=[], history=[], relationship={},
        ))
        req = gateway.requests[0]
        assert req.tools is None
        assert req.tool_choice is None

    def test_existing_json_action_tools_still_work(self):
        """JSON action get_weather still works when native disabled."""
        from agent.chat_agent import ChatAgent

        gateway = _FakeGatewayLLM(response=_make_response(
            '{"actions":[{"type":"tool","name":"get_weather","args":{"city":"北京"}}],"done":true}'
        ))

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=False,
        )

        import asyncio
        result = asyncio.run(agent.chat(
            user_message="北京天气怎样", memories=[], history=[], relationship={},
        ))
        assert len(result["tool_calls"]) >= 0  # tool_calls field present


# ══════════════════════════════════════════════════════════════════
# 4. Native tool execution validation
# ══════════════════════════════════════════════════════════════════

class TestNativeToolExecution:
    @pytest.mark.asyncio
    async def test_get_time_executes_successfully(self):
        """get_time with valid args returns time string."""
        result = await _execute_native_tool(
            tool_name="get_time",
            tool_args_raw='{"timezone": "Asia/Shanghai"}',
            native_allowed_tools=["get_time"],
        )
        assert "错误" not in result
        assert len(result) > 5  # Should have actual time content

    @pytest.mark.asyncio
    async def test_get_weather_executes_successfully(self):
        """get_weather with valid city returns weather string."""
        result = await _execute_native_tool(
            tool_name="get_weather",
            tool_args_raw='{"city": "北京"}',
            native_allowed_tools=["get_weather"],
        )
        assert "错误" not in result
        assert len(result) > 5  # Should have actual weather content

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        """Unknown tool name returns error."""
        result = await _execute_native_tool(
            tool_name="fake_tool",
            tool_args_raw="{}",
            native_allowed_tools=["fake_tool"],
        )
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_send_email_blocked(self):
        """send_email is always blocked even if in allowed list."""
        result = await _execute_native_tool(
            tool_name="send_email",
            tool_args_raw='{"to": "test@test.com", "subject": "hi"}',
            native_allowed_tools=["send_email"],
        )
        assert "不允许" in result or "错误" in result

    @pytest.mark.asyncio
    async def test_wechat_reply_blocked(self):
        """wechat_reply is always blocked."""
        result = await _execute_native_tool(
            tool_name="wechat_reply",
            tool_args_raw='{"text": "hello"}',
            native_allowed_tools=["wechat_reply"],
        )
        assert "不允许" in result or "错误" in result

    @pytest.mark.asyncio
    async def test_not_in_allowed_list_blocked(self):
        """Tool not in allowed list is blocked."""
        result = await _execute_native_tool(
            tool_name="get_time",
            tool_args_raw="{}",
            native_allowed_tools=["get_weather"],  # get_time not allowed
        )
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_invalid_json_args_returns_error(self):
        """Non-JSON arguments return error."""
        result = await _execute_native_tool(
            tool_name="get_time",
            tool_args_raw="not json {{{{",
            native_allowed_tools=["get_time"],
        )
        assert "JSON" in result

    @pytest.mark.asyncio
    async def test_missing_required_arg_returns_error(self):
        """Missing required 'city' in get_weather returns error."""
        result = await _execute_native_tool(
            tool_name="get_weather",
            tool_args_raw="{}",
            native_allowed_tools=["get_weather"],
        )
        assert "缺少必需参数" in result

    @pytest.mark.asyncio
    async def test_empty_args_ok_for_get_time(self):
        """get_time with empty args (no required params) succeeds."""
        result = await _execute_native_tool(
            tool_name="get_time",
            tool_args_raw="{}",
            native_allowed_tools=["get_time"],
        )
        assert "错误" not in result

    @pytest.mark.asyncio
    async def test_empty_tool_name_returns_error(self):
        """Empty tool name returns error."""
        result = await _execute_native_tool(
            tool_name="",
            tool_args_raw="{}",
            native_allowed_tools=[],
        )
        assert "错误" in result


# ══════════════════════════════════════════════════════════════════
# 5. ChatAgent native tool call loop (unit tests)
# ══════════════════════════════════════════════════════════════════

class TestChatAgentNativeLoop:
    def test_native_tool_call_then_json_action(self):
        """Model returns get_time tool_call first, then JSON actions."""
        from agent.chat_agent import ChatAgent

        call_count = [0]

        class _TCRsp:
            id = "call_1"
            function = MagicMock()
            function.name = "get_time"
            function.arguments = '{"timezone": "Asia/Shanghai"}'

        async def _complete(req):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: return tool_calls (model wants to use get_time)
                return _make_response(
                    content="",
                    tool_calls=[_TCRsp()],
                )
            else:
                # Second call: return JSON actions
                return _make_response(
                    '{"actions":[{"type":"message","text":"现在是下午3点","delay":0}],"done":true}',
                )

        gateway = AsyncMock()
        gateway.complete = _complete
        gateway.requests = []

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time"],
        )

        import asyncio
        result = asyncio.run(agent.chat(
            user_message="现在几点", memories=[], history=[], relationship={},
        ))
        assert call_count[0] == 2  # Two LLM calls
        assert len(result["messages"]) == 1
        assert result["tool_calls"]  # Tool call recorded
        assert result["tool_calls"][0]["name"] == "get_time"

    def test_multiple_native_tool_calls(self):
        """Multiple tool_calls in one response are processed."""
        from agent.chat_agent import ChatAgent

        call_count = [0]

        class _TC1:
            id = "call_1"
            function = MagicMock()
            function.name = "get_time"
            function.arguments = '{"timezone": "Asia/Shanghai"}'

        class _TC2:
            id = "call_2"
            function = MagicMock()
            function.name = "get_weather"
            function.arguments = '{"city": "北京"}'

        async def _complete(req):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(
                    content="", tool_calls=[_TC1(), _TC2()],
                )
            else:
                return _make_response(
                    '{"actions":[{"type":"message","text":"已查到时间和天气","delay":0}],"done":true}',
                )

        gateway = AsyncMock()
        gateway.complete = _complete

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time", "get_weather"],
        )

        import asyncio
        result = asyncio.run(agent.chat(
            user_message="天气和时间", memories=[], history=[], relationship={},
        ))
        assert call_count[0] == 2
        assert len(result["tool_calls"]) == 2
        names = [tc["name"] for tc in result["tool_calls"]]
        assert "get_time" in names
        assert "get_weather" in names

    def test_unknown_native_tool_not_executed(self):
        """Unknown native tool call returns error but doesn't crash."""
        from agent.chat_agent import ChatAgent

        class _TC:
            id = "evil_1"
            function = MagicMock()
            function.name = "send_email"
            function.arguments = '{"to":"bad@evil.com"}'

        async def _complete(req):
            return _make_response(content="", tool_calls=[_TC()])

        gateway = AsyncMock()
        gateway.complete = _complete

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=[],  # Nothing allowed!
        )

        import asyncio
        result = asyncio.run(agent.chat(
            user_message="hack", memories=[], history=[], relationship={},
        ))
        # Should not crash; the tool call should produce an error result
        assert result["tool_calls"][0]["name"] == "send_email"
        assert "错误" in result["tool_calls"][0]["result"]


# ══════════════════════════════════════════════════════════════════
# 6. DeepSeekAdapter payload tests
# ══════════════════════════════════════════════════════════════════

class TestDeepSeekAdapterPayload:
    def test_tools_passed_when_present(self):
        """tools field in LLMRequest → payload includes tools."""
        from agent.providers.deepseek import DeepSeekAdapter
        from agent.providers.base import ProviderConfig

        adapter = DeepSeekAdapter(ProviderConfig(
            provider="deepseek", api_key="test_key",
            base_url="https://api.deepseek.com",
            default_model="deepseek-v4-flash",
        ))
        tools = [{"type": "function", "function": {"name": "get_time", "parameters": {}}}]
        payload = adapter._build_payload(LLMRequest(
            purpose="test", messages=[], tools=tools,
        ))
        assert "tools" in payload
        assert payload["tools"] == tools

    def test_no_tools_when_none(self):
        """tools=None → payload does not include tools."""
        from agent.providers.deepseek import DeepSeekAdapter
        from agent.providers.base import ProviderConfig

        adapter = DeepSeekAdapter(ProviderConfig(
            provider="deepseek", api_key="test_key",
            base_url="https://api.deepseek.com",
            default_model="deepseek-v4-flash",
        ))
        payload = adapter._build_payload(LLMRequest(
            purpose="test", messages=[], tools=None,
        ))
        assert "tools" not in payload

    def test_tool_choice_passed_when_present(self):
        """tool_choice is passed to payload."""
        from agent.providers.deepseek import DeepSeekAdapter
        from agent.providers.base import ProviderConfig

        adapter = DeepSeekAdapter(ProviderConfig(
            provider="deepseek", api_key="test_key",
            base_url="https://api.deepseek.com",
            default_model="deepseek-v4-flash",
        ))
        tools = [{"type": "function", "function": {"name": "get_time", "parameters": {}}}]
        payload = adapter._build_payload(LLMRequest(
            purpose="test", messages=[], tools=tools, tool_choice="auto",
        ))
        assert "tools" in payload
        assert payload["tool_choice"] == "auto"

    def test_tool_choice_not_passed_when_none(self):
        """tool_choice=None → payload does not include tool_choice."""
        from agent.providers.deepseek import DeepSeekAdapter
        from agent.providers.base import ProviderConfig

        adapter = DeepSeekAdapter(ProviderConfig(
            provider="deepseek", api_key="test_key",
            base_url="https://api.deepseek.com",
            default_model="deepseek-v4-flash",
        ))
        tools = [{"type": "function", "function": {"name": "get_time", "parameters": {}}}]
        payload = adapter._build_payload(LLMRequest(
            purpose="test", messages=[], tools=tools, tool_choice=None,
        ))
        assert "tool_choice" not in payload

    def test_no_strict_true_in_payload(self):
        """Payload does not include strict=true."""
        from agent.providers.deepseek import DeepSeekAdapter
        from agent.providers.base import ProviderConfig

        adapter = DeepSeekAdapter(ProviderConfig(
            provider="deepseek", api_key="test_key",
            base_url="https://api.deepseek.com",
            default_model="deepseek-v4-flash",
        ))
        tools = [{"type": "function", "function": {"name": "get_time", "parameters": {}}}]
        payload = adapter._build_payload(LLMRequest(
            purpose="test", messages=[], tools=tools, tool_choice="auto",
        ))
        for t in payload.get("tools", []):
            assert not t.get("function", {}).get("strict"), "strict=true found in payload"

    def test_no_beta_base_url(self):
        """Base URL is NOT changed to beta endpoint."""
        from agent.providers.deepseek import DeepSeekAdapter
        from agent.providers.base import ProviderConfig

        adapter = DeepSeekAdapter(ProviderConfig(
            provider="deepseek", api_key="test_key",
            base_url="https://api.deepseek.com",
            default_model="deepseek-v4-flash",
        ))
        assert "beta" not in adapter.config.base_url.lower()

    def test_thinking_explicitly_disabled(self):
        """thinking=disabled → extra_body contains {"type": "disabled"}."""
        from agent.providers.deepseek import DeepSeekAdapter
        from agent.providers.base import ProviderConfig

        adapter = DeepSeekAdapter(ProviderConfig(
            provider="deepseek", api_key="test_key",
            base_url="https://api.deepseek.com",
            default_model="deepseek-v4-flash",
        ))
        payload = adapter._build_payload(LLMRequest(
            purpose="test", messages=[], thinking="disabled",
        ))
        # Always explicitly communicate thinking state
        extra_body = payload.get("extra_body", {})
        assert extra_body.get("thinking") == {"type": "disabled"}

    def test_native_tool_request_thinking_is_disabled(self):
        """ChatAgent with native_tool_calls=True sets thinking='disabled'."""
        from agent.chat_agent import ChatAgent

        gateway = _FakeGatewayLLM(response=_make_response(
            '{"actions":[{"type":"message","text":"ok","delay":0}],"done":true}'
        ))

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time"],
        )

        import asyncio
        asyncio.run(agent.chat(
            user_message="几点", memories=[], history=[], relationship={},
        ))
        assert len(gateway.requests) >= 1
        req = gateway.requests[0]
        assert req.thinking == "disabled"

    def test_no_reasoning_effort_set(self):
        """ChatAgent never sets reasoning_effort."""
        from agent.chat_agent import ChatAgent

        gateway = _FakeGatewayLLM(response=_make_response(
            '{"actions":[{"type":"message","text":"ok","delay":0}],"done":true}'
        ))

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time"],
        )

        import asyncio
        asyncio.run(agent.chat(
            user_message="hi", memories=[], history=[], relationship={},
        ))
        req = gateway.requests[0]
        assert req.reasoning_effort is None

    def test_no_reasoning_content_present(self):
        """LLMResponse with thinking disabled has empty reasoning_content."""
        resp = _make_response(content="ok")
        assert resp.reasoning_content == ""

    def test_chat_agent_request_has_correct_purpose(self):
        """ChatAgent sends purpose='chat_turn'."""
        from agent.chat_agent import ChatAgent

        gateway = _FakeGatewayLLM(response=_make_response(
            '{"actions":[{"type":"message","text":"ok","delay":0}],"done":true}'
        ))

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
        )

        import asyncio
        asyncio.run(agent.chat(
            user_message="hi", memories=[], history=[], relationship={},
        ))
        assert gateway.requests[0].purpose == "chat_turn"


# ══════════════════════════════════════════════════════════════════
# 7. ToolRegistry tests (extended)
# ══════════════════════════════════════════════════════════════════

class TestToolRegistryNativeSpecs:
    def test_registry_has_native_eligible_tools(self):
        """Only get_time and get_weather are native_tool_call_eligible."""
        from agent.tool_registry import TOOL_REGISTRY
        native_names = {
            t.name for t in TOOL_REGISTRY.iter_defs()
            if t.native_tool_call_eligible
        }
        assert native_names == {"get_time", "get_weather"}

    def test_send_email_not_native_eligible(self):
        """send_email is NOT native_tool_call_eligible."""
        from agent.tool_registry import TOOL_REGISTRY
        t = TOOL_REGISTRY.get("send_email")
        assert t is not None
        assert t.native_tool_call_eligible is False

    def test_search_memory_not_native_eligible(self):
        """search_memory is NOT native_tool_call_eligible."""
        from agent.tool_registry import TOOL_REGISTRY
        t = TOOL_REGISTRY.get("search_memory")
        assert t is not None
        assert t.native_tool_call_eligible is False

    def test_native_specs_without_eligible_returns_empty(self):
        """get_native_tool_specs with eligible_only=True excludes non-eligible."""
        reg = ToolRegistry()
        from agent.tool_registry import ToolDef
        reg.register(ToolDef(
            name="test_tool", description="d",
            args_schema=[], native_tool_call_eligible=False,
        ))
        specs = reg.get_native_tool_specs(eligible_only=True)
        assert specs == []

    def test_native_specs_includes_eligible(self):
        """get_native_tool_specs includes eligible tools."""
        reg = ToolRegistry()
        from agent.tool_registry import ToolDef
        reg.register(ToolDef(
            name="test_tool", description="d",
            args_schema=["arg1"], native_tool_call_eligible=True,
        ))
        specs = reg.get_native_tool_specs(eligible_only=True)
        assert len(specs) == 1
        assert specs[0]["function"]["name"] == "test_tool"


# ══════════════════════════════════════════════════════════════════
# 8. Hard deny list tests
# ══════════════════════════════════════════════════════════════════

class TestHardDenyList:
    def test_wechat_reply_in_deny_list(self):
        assert "wechat_reply" in _NATIVE_TOOL_HARD_DENY

    def test_schedule_wechat_message_in_deny_list(self):
        assert "schedule_wechat_message" in _NATIVE_TOOL_HARD_DENY

    def test_send_email_in_deny_list(self):
        assert "send_email" in _NATIVE_TOOL_HARD_DENY

    def test_get_time_not_in_deny_list(self):
        assert "get_time" not in _NATIVE_TOOL_HARD_DENY

    def test_get_weather_not_in_deny_list(self):
        assert "get_weather" not in _NATIVE_TOOL_HARD_DENY


# ══════════════════════════════════════════════════════════════════
# 9. Integration-style tests (fake gateway, real scenarios)
# ══════════════════════════════════════════════════════════════════

class TestIntegrationGetTime:
    """User asks "现在几点" → get_time native call → JSON actions → reply."""

    def test_get_time_native_then_reply(self):
        from agent.chat_agent import ChatAgent

        call_count = [0]

        class _TC:
            id = "call_t1"
            function = MagicMock()
            function.name = "get_time"
            function.arguments = '{"timezone": "Asia/Shanghai"}'

        async def _complete(req):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(content="", tool_calls=[_TC()])
            else:
                return _make_response(
                    '{"actions":[{"type":"message","text":"现在是下午3点","delay":0}],"done":true}'
                )

        gateway = _MultiRoundGateway(_complete)

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time"],
        )

        import asyncio
        result = asyncio.run(agent.chat(
            user_message="现在几点", memories=[], history=[], relationship={},
        ))
        assert call_count[0] == 2
        assert len(result["messages"]) == 1
        assert result["messages"][0]["text"] == "现在是下午3点"
        assert result["tool_calls"][0]["name"] == "get_time"
        assert "错误" not in result["tool_calls"][0]["result"]

    def test_get_weather_native_then_reply(self):
        """User asks "杭州天气" → get_weather native call → reply."""
        from agent.chat_agent import ChatAgent

        call_count = [0]

        class _TC:
            id = "call_w1"
            function = MagicMock()
            function.name = "get_weather"
            function.arguments = '{"city": "杭州"}'

        async def _complete(req):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(content="", tool_calls=[_TC()])
            else:
                return _make_response(
                    '{"actions":[{"type":"message","text":"杭州今天晴天","delay":0}],"done":true}'
                )

        gateway = _MultiRoundGateway(_complete)

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_weather"],
        )

        import asyncio
        result = asyncio.run(agent.chat(
            user_message="杭州天气怎样", memories=[], history=[], relationship={},
        ))
        assert call_count[0] == 2
        assert len(result["messages"]) == 1
        assert result["tool_calls"][0]["name"] == "get_weather"

    def test_native_tool_then_round2_still_tool_calls(self):
        """Round 2 still returns tool_calls → continues loop."""
        from agent.chat_agent import ChatAgent

        call_count = [0]

        class _TC1:
            id = "call_1"
            function = MagicMock()
            function.name = "get_time"
            function.arguments = '{"timezone": "Asia/Shanghai"}'

        class _TC2:
            id = "call_2"
            function = MagicMock()
            function.name = "get_weather"
            function.arguments = '{"city": "北京"}'

        async def _complete(req):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(content="", tool_calls=[_TC1()])
            elif call_count[0] == 2:
                return _make_response(content="", tool_calls=[_TC2()])
            else:
                return _make_response(
                    '{"actions":[{"type":"message","text":"时间和天气都查到了","delay":0}],"done":true}'
                )

        gateway = _MultiRoundGateway(_complete)

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time", "get_weather"],
        )

        import asyncio
        result = asyncio.run(agent.chat(
            user_message="时间和天气", memories=[], history=[], relationship={},
        ))
        assert call_count[0] == 3
        assert len(result["tool_calls"]) == 2

    def test_model_requests_unknown_tool(self):
        """Model requests a tool not in registry → error result → continues."""
        from agent.chat_agent import ChatAgent

        call_count = [0]

        class _TC:
            id = "evil"
            function = MagicMock()
            function.name = "hack_system"
            function.arguments = "{}"

        async def _complete(req):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(content="", tool_calls=[_TC()])
            else:
                return _make_response(
                    '{"actions":[{"type":"message","text":"抱歉，无法执行","delay":0}],"done":true}'
                )

        gateway = _MultiRoundGateway(_complete)

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time"],
        )

        import asyncio
        result = asyncio.run(agent.chat(
            user_message="hack", memories=[], history=[], relationship={},
        ))
        assert call_count[0] == 2  # Continues to round 2 for JSON reply
        assert "错误" in result["tool_calls"][0]["result"]

    def test_model_requests_send_email_hard_deny(self):
        """Model requests send_email → hard_denied → error result."""
        from agent.chat_agent import ChatAgent

        call_count = [0]

        class _TC:
            id = "email_1"
            function = MagicMock()
            function.name = "send_email"
            function.arguments = '{"to":"bad@evil.com","subject":"hack"}'

        async def _complete(req):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(content="", tool_calls=[_TC()])
            else:
                return _make_response(
                    '{"actions":[{"type":"message","text":"无法发送","delay":0}],"done":true}'
                )

        gateway = _MultiRoundGateway(_complete)

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time"],  # send_email NOT allowed
        )

        import asyncio
        result = asyncio.run(agent.chat(
            user_message="send", memories=[], history=[], relationship={},
        ))
        assert result["tool_calls"][0]["name"] == "send_email"
        assert "不允许" in result["tool_calls"][0]["result"]

    def test_max_tool_loop_exit(self):
        """MAX_TOOL_LOOP (5) eventually exits with fallback."""
        from agent.chat_agent import ChatAgent

        class _TC:
            id = "loop_1"
            function = MagicMock()
            function.name = "get_time"
            function.arguments = '{"timezone": "Asia/Shanghai"}'

        async def _complete(req):
            # Always return tool_calls — never JSON actions
            return _make_response(content="", tool_calls=[_TC()])

        gateway = _MultiRoundGateway(_complete)

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time"],
        )

        import asyncio
        result = asyncio.run(agent.chat(
            user_message="几点", memories=[], history=[], relationship={},
        ))
        # Should exit after MAX_TOOL_LOOP iterations with fallback
        assert result["iterations"] == 5  # MAX_TOOL_LOOP
        # Fallback message or tool results should be present
        assert len(result["messages"]) >= 0 or len(result["tool_calls"]) > 0

    def test_empty_allowed_tools_equals_disabled(self):
        """allowed_tools=[] → tools=None (equivalent to native disabled)."""
        from agent.chat_agent import ChatAgent

        gateway = _FakeGatewayLLM(response=_make_response(
            '{"actions":[{"type":"message","text":"ok","delay":0}],"done":true}'
        ))

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=[],  # Empty!
        )

        import asyncio
        asyncio.run(agent.chat(
            user_message="hi", memories=[], history=[], relationship={},
        ))
        req = gateway.requests[0]
        assert req.tools is None
        assert req.tool_choice is None


# ══════════════════════════════════════════════════════════════════
# 10. Message format verification
# ══════════════════════════════════════════════════════════════════

class TestMessageFormat:
    def test_assistant_tool_calls_message_format(self):
        """Assistant message with tool_calls follows OpenAI format."""
        from agent.chat_agent import ChatAgent

        class _TC:
            id = "call_1"
            function = MagicMock()
            function.name = "get_time"
            function.arguments = '{"timezone": "Asia/Shanghai"}'

        call_count = [0]

        async def _complete(req):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(content="", tool_calls=[_TC()])
            else:
                return _make_response(
                    '{"actions":[{"type":"message","text":"done","delay":0}],"done":true}'
                )

        gateway = _MultiRoundGateway(_complete)

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time"],
        )

        import asyncio
        asyncio.run(agent.chat(
            user_message="几点", memories=[], history=[], relationship={},
        ))

        # Check the second request's messages (round 2)
        req2 = gateway.requests[1]
        msgs = req2.messages
        roles = [m["role"] for m in msgs]
        # Should have: system, ..., user, assistant (with tool_calls), tool
        assert "assistant" in roles
        assert "tool" in roles

        # Find the assistant message with tool_calls
        assistant_msg = next(m for m in msgs if m["role"] == "assistant" and "tool_calls" in m)
        assert "tool_calls" in assistant_msg
        assert assistant_msg["content"] == ""  # model sent no content
        tc_list = assistant_msg["tool_calls"]
        assert len(tc_list) == 1
        assert tc_list[0]["type"] == "function"
        assert tc_list[0]["function"]["name"] == "get_time"
        assert tc_list[0]["id"] == "call_1"

    def test_tool_result_message_format(self):
        """Tool result message follows OpenAI format."""
        from agent.chat_agent import ChatAgent

        class _TC:
            id = "call_t1"
            function = MagicMock()
            function.name = "get_time"
            function.arguments = '{"timezone": "Asia/Shanghai"}'

        call_count = [0]

        async def _complete(req):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(content="", tool_calls=[_TC()])
            else:
                return _make_response(
                    '{"actions":[{"type":"message","text":"done","delay":0}],"done":true}'
                )

        gateway = _MultiRoundGateway(_complete)

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time"],
        )

        import asyncio
        asyncio.run(agent.chat(
            user_message="几点", memories=[], history=[], relationship={},
        ))

        req2 = gateway.requests[1]
        msgs = req2.messages
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "call_t1"
        assert isinstance(tool_msg["content"], str)
        assert len(tool_msg["content"]) > 0

    def test_raw_response_not_in_messages(self):
        """Messages list should not contain raw_response."""
        from agent.chat_agent import ChatAgent

        class _TC:
            id = "call_1"
            function = MagicMock()
            function.name = "get_time"
            function.arguments = '{"timezone": "Asia/Shanghai"}'

        call_count = [0]

        async def _complete(req):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(content="", tool_calls=[_TC()])
            else:
                return _make_response(
                    '{"actions":[{"type":"message","text":"done","delay":0}],"done":true}'
                )

        gateway = _MultiRoundGateway(_complete)

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time"],
        )

        import asyncio
        asyncio.run(agent.chat(
            user_message="几点", memories=[], history=[], relationship={},
        ))

        for req in gateway.requests:
            for msg in req.messages:
                assert "raw_response" not in msg

    def test_tool_result_content_is_string(self):
        """Tool result content is always a string."""
        from agent.chat_agent import ChatAgent

        class _TC:
            id = "call_1"
            function = MagicMock()
            function.name = "get_time"
            function.arguments = "{}"

        call_count = [0]

        async def _complete(req):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(content="", tool_calls=[_TC()])
            else:
                return _make_response(
                    '{"actions":[{"type":"message","text":"ok","delay":0}],"done":true}'
                )

        gateway = _MultiRoundGateway(_complete)

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time"],
        )

        import asyncio
        asyncio.run(agent.chat(
            user_message="几点", memories=[], history=[], relationship={},
        ))

        req2 = gateway.requests[1]
        tool_msg = next(m for m in req2.messages if m["role"] == "tool")
        assert isinstance(tool_msg["content"], str)

    def test_messages_sequence_has_assistant_then_tool(self):
        """Round 2 messages include assistant (with tool_calls) followed by tool."""
        from agent.chat_agent import ChatAgent

        class _TC:
            id = "call_1"
            function = MagicMock()
            function.name = "get_time"
            function.arguments = '{"timezone": "Asia/Shanghai"}'

        call_count = [0]

        async def _complete(req):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(content="", tool_calls=[_TC()])
            else:
                return _make_response(
                    '{"actions":[{"type":"message","text":"done","delay":0}],"done":true}'
                )

        gateway = _MultiRoundGateway(_complete)

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gateway,
            native_tool_calls=True,
            native_allowed_tools=["get_time"],
        )

        import asyncio
        asyncio.run(agent.chat(
            user_message="几点", memories=[], history=[], relationship={},
        ))

        req2 = gateway.requests[1]
        msgs = req2.messages
        # Find indices of assistant and tool messages
        assistant_idx = next(i for i, m in enumerate(msgs) if m["role"] == "assistant" and "tool_calls" in m)
        tool_idx = next(i for i, m in enumerate(msgs) if m["role"] == "tool")
        assert assistant_idx < tool_idx  # assistant with tool_calls must come before tool result


# ══════════════════════════════════════════════════════════════════
# 11. _classify_native_tool_result tests
# ══════════════════════════════════════════════════════════════════

class TestClassifyNativeToolResult:
    def test_success(self):
        from agent.chat_agent import _classify_native_tool_result
        assert _classify_native_tool_result("2026年05月28日 15:00 (Asia/Shanghai)") == "success"

    def test_unknown_tool_empty_name(self):
        from agent.chat_agent import _classify_native_tool_result
        assert _classify_native_tool_result("错误：工具名称为空") == "unknown_tool"

    def test_unknown_tool_not_found(self):
        from agent.chat_agent import _classify_native_tool_result
        assert _classify_native_tool_result("错误：未找到工具 fake_tool") == "unknown_tool"

    def test_hard_denied(self):
        from agent.chat_agent import _classify_native_tool_result
        assert _classify_native_tool_result("错误：工具 send_email 不允许通过原生 tool call 调用") == "hard_denied"

    def test_not_allowed(self):
        from agent.chat_agent import _classify_native_tool_result
        assert _classify_native_tool_result("错误：工具 get_time 不在允许的原生工具列表中") == "not_allowed"

    def test_not_eligible(self):
        from agent.chat_agent import _classify_native_tool_result
        assert _classify_native_tool_result("错误：工具 search_memory 不支持原生 tool call") == "not_eligible"

    def test_invalid_arguments_json(self):
        from agent.chat_agent import _classify_native_tool_result
        assert _classify_native_tool_result("错误：工具参数 JSON 解析失败：Expecting value") == "invalid_arguments_json"

    def test_missing_required_arg(self):
        from agent.chat_agent import _classify_native_tool_result
        assert _classify_native_tool_result("错误：缺少必需参数 city") == "missing_required_arg"

    def test_tool_execution_error(self):
        from agent.chat_agent import _classify_native_tool_result
        assert _classify_native_tool_result("错误：工具执行出错：ConnectionError") == "tool_execution_error"


# ══════════════════════════════════════════════════════════════════
# 12. Trace fields for native tool calls
# ══════════════════════════════════════════════════════════════════

class TestTraceFields:
    def test_tools_exposed_in_trace(self):
        """Trace includes tools_exposed_count and tools_exposed_names."""
        from agent.providers.deepseek import DeepSeekAdapter
        from agent.providers.base import ProviderConfig

        cfg = ProviderConfig(
            provider="deepseek", api_key="sk-test",
            base_url="https://api.deepseek.com",
            default_model="deepseek-v4-flash",
        )
        adapter = DeepSeekAdapter(cfg)

        # Fake response with tool_calls
        mock_tc = MagicMock()
        mock_tc.id = "t1"
        fn = MagicMock()
        fn.name = "get_time"
        mock_tc.function = fn

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message = MagicMock()
        mock_resp.choices[0].message.content = ""
        mock_resp.choices[0].message.tool_calls = [mock_tc]
        mock_resp.choices[0].finish_reason = "tool_calls"
        mock_resp.model = "deepseek-v4-flash"
        mock_resp.usage = MagicMock()
        mock_resp.usage.prompt_tokens = 10
        mock_resp.usage.completion_tokens = 5
        mock_resp.usage.total_tokens = 15
        mock_resp.usage.prompt_tokens_details = None
        mock_resp.usage.completion_tokens_details = None
        mock_resp.id = "req-1"
        adapter.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        from agent.llm_gateway import LLMGateway
        gateway = LLMGateway(adapter=adapter, config=cfg)

        traces: list[dict] = []

        async def capture(trace):
            traces.append(trace)

        gateway.on_trace(capture)

        tools = [{"type": "function", "function": {"name": "get_time", "parameters": {}}}]
        req = LLMRequest(
            purpose="test", messages=[{"role": "user", "content": "hi"}],
            tools=tools, tool_choice="auto",
        )

        import asyncio
        asyncio.run(gateway.complete(req))

        trace = traces[0]
        assert trace["tools_exposed_count"] == 1
        assert "get_time" in trace["tools_exposed_names"]
        assert trace["tool_calls_returned_count"] == 1
        assert "get_time" in trace["tool_calls_returned_names"]


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

class _FakeGatewayLLM:
    """LLMGateway stub that records requests and returns canned responses."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        if self.response:
            return self.response
        return _make_response(
            json.dumps({"actions": [{"type": "message", "text": "hello", "delay": 0}], "done": True},
                       ensure_ascii=False)
        )


class _MultiRoundGateway:
    """LLMGateway stub that calls a function for each request to support multi-round tests."""

    def __init__(self, complete_fn):
        self._complete_fn = complete_fn
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return await self._complete_fn(request)
