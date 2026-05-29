"""Phase 1: Native tool calls tests — feature flags, specs, execution, safety."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.chat_agent import (
    _build_native_tool_specs,
    _execute_native_fact_tool,
)
from agent.llm_types import LLMRequest, LLMResponse, LLMUsage
from agent.tool_registry import TOOL_REGISTRY, ToolRegistry


def _make_response(content="", tool_calls=None, usage=None, **kw):
    return LLMResponse(
        content=content,
        provider="deepseek",
        model="deepseek-v4-flash",
        usage=usage or LLMUsage(prompt_tokens=kw.get("prompt_tokens", 10),
                                 completion_tokens=kw.get("completion_tokens", 5),
                                 total_tokens=kw.get("total_tokens", 15)),
        latency_ms=kw.get("latency_ms", 100),
        finish_reason=kw.get("finish_reason", "stop"),
        tool_calls=tool_calls or [],
    )


def _make_tc(name: str, arguments: str, call_id: str = "call_1"):
    fn = MagicMock()
    fn.name = name
    fn.arguments = arguments
    tc = MagicMock()
    tc.id = call_id
    tc.function = fn
    return tc


# 1. Feature flag tests
class TestFeatureFlag:
    def test_disabled_returns_no_tools(self):
        specs = _build_native_tool_specs([])
        assert specs is None

    def test_empty_allowed_returns_no_tools(self):
        specs = _build_native_tool_specs([])
        assert specs is None

    def test_enabled_with_get_time_get_weather(self):
        specs = _build_native_tool_specs(["get_time", "get_weather"])
        assert specs is not None
        names = {s["function"]["name"] for s in specs}
        assert names == {"get_time", "get_weather"}

    def test_send_email_filtered_out(self):
        specs = _build_native_tool_specs(["get_time", "send_email"])
        names = {s["function"]["name"] for s in (specs or [])}
        assert "send_email" not in names

    def test_send_wechat_messages_in_specs(self):
        specs = _build_native_tool_specs(["send_wechat_messages", "get_time"])
        names = {s["function"]["name"] for s in (specs or [])}
        assert "send_wechat_messages" in names
        assert "wechat_reply" not in names

    def test_schedule_wechat_message_not_in_specs(self):
        specs = _build_native_tool_specs(["schedule_wechat_message", "get_weather"])
        names = {s["function"]["name"] for s in (specs or [])}
        assert "schedule_wechat_message" not in names

    def test_mixed_config_correctly_filtered(self):
        specs = _build_native_tool_specs([
            "get_time", "send_email", "send_wechat_messages",
            "get_weather", "search_memory",
        ])
        names = {s["function"]["name"] for s in (specs or [])}
        assert names == {"send_wechat_messages", "get_time", "get_weather"}


# 2. Native tool specs structure
class TestNativeToolSpecs:
    def test_specs_are_openai_compatible(self):
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
        specs = _build_native_tool_specs(["get_time"])
        params = specs[0]["function"]["parameters"]
        assert "timezone" in params["properties"]

    def test_get_weather_spec_has_city_param(self):
        specs = _build_native_tool_specs(["get_weather"])
        params = specs[0]["function"]["parameters"]
        assert "city" in params["properties"]

    def test_get_weather_has_required_city(self):
        specs = _build_native_tool_specs(["get_weather"])
        params = specs[0]["function"]["parameters"]
        assert "required" in params
        assert "city" in params["required"]

    def test_no_strict_true_in_specs(self):
        specs = _build_native_tool_specs(["get_time", "get_weather"])
        for spec in specs:
            fn = spec["function"]
            assert not fn.get("strict")


# 3. Phase 1: ChatAgent native tool call integration
class TestChatAgentPhase1:
    def test_send_wechat_messages_native(self):
        from agent.chat_agent import ChatAgent

        tc = _make_tc("send_wechat_messages", '{"messages":["hello"]}')
        gw = _FakeGateway(response=_make_response("", tool_calls=[tc]))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="hi", history=[], trigger="user",
        ))
        assert len(result.tool_results) >= 1
        assert result.tool_results[0]["name"] == "send_wechat_messages"

    def test_fact_tool_then_send(self):
        from agent.chat_agent import ChatAgent

        call_count = [0]
        responses = [
            _make_response("", tool_calls=[_make_tc("get_time", '{}')],
                           prompt_tokens=50, total_tokens=65),
            _make_response("", tool_calls=[_make_tc("send_wechat_messages", '{"messages":["3pm"]}')],
                           prompt_tokens=80, total_tokens=90),
        ]

        class MGW:
            def __init__(self):
                self.requests = []

            async def complete(self, req):
                self.requests.append(req)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=MGW(),
            native_tool_calls=True,
            native_allowed_tools=["get_time", "send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="time?", history=[], trigger="user",
        ))
        assert result.iterations == 2
        assert result.tool_results[0]["name"] == "get_time"
        assert result.tool_results[1]["name"] == "send_wechat_messages"


# 4. Native tool execution validation
class TestNativeToolExecution:
    @pytest.mark.asyncio
    async def test_get_time_executes_successfully(self):
        result = await _execute_native_fact_tool(
            tool_name="get_time",
            tool_args_raw='{"timezone": "Asia/Shanghai"}',
            native_allowed_tools=["get_time"],
        )
        assert "error" not in result.lower() and "错误" not in result

    @pytest.mark.asyncio
    async def test_get_weather_executes_successfully(self):
        result = await _execute_native_fact_tool(
            tool_name="get_weather",
            tool_args_raw='{"city": "Beijing"}',
            native_allowed_tools=["get_weather"],
        )
        assert "error" not in result.lower() and "错误" not in result

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        result = await _execute_native_fact_tool(
            tool_name="fake_tool",
            tool_args_raw="{}",
            native_allowed_tools=["fake_tool"],
        )
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_send_email_blocked(self):
        result = await _execute_native_fact_tool(
            tool_name="send_email",
            tool_args_raw='{"to":"test@test.com"}',
            native_allowed_tools=["send_email"],
        )
        assert "错误" in result  # not native eligible

    @pytest.mark.asyncio
    async def test_wechat_reply_not_found(self):
        result = await _execute_native_fact_tool(
            tool_name="wechat_reply",
            tool_args_raw='{"text":"hello"}',
            native_allowed_tools=["wechat_reply"],
        )
        assert "错误" in result  # not in TOOL_REGISTRY

    @pytest.mark.asyncio
    async def test_not_in_allowed_list_blocked(self):
        result = await _execute_native_fact_tool(
            tool_name="get_time",
            tool_args_raw="{}",
            native_allowed_tools=["get_weather"],
        )
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_invalid_json_args_returns_error(self):
        result = await _execute_native_fact_tool(
            tool_name="get_time",
            tool_args_raw="not json {{{{",
            native_allowed_tools=["get_time"],
        )
        assert "JSON" in result

    @pytest.mark.asyncio
    async def test_missing_required_arg_returns_error(self):
        result = await _execute_native_fact_tool(
            tool_name="get_weather",
            tool_args_raw="{}",
            native_allowed_tools=["get_weather"],
        )
        assert "缺少必需参数" in result

    @pytest.mark.asyncio
    async def test_empty_args_ok_for_get_time(self):
        result = await _execute_native_fact_tool(
            tool_name="get_time",
            tool_args_raw="{}",
            native_allowed_tools=["get_time"],
        )
        assert "错误" not in result

    @pytest.mark.asyncio
    async def test_empty_tool_name_returns_error(self):
        result = await _execute_native_fact_tool(
            tool_name="",
            tool_args_raw="{}",
            native_allowed_tools=[],
        )
        assert "错误" in result


# 5. DeepSeekAdapter payload tests
class TestDeepSeekAdapterPayload:
    def test_tools_passed_when_present(self):
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
        assert payload["tool_choice"] == "auto"

    def test_tool_choice_not_passed_when_none(self):
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
            assert not t.get("function", {}).get("strict")

    def test_no_beta_base_url(self):
        from agent.providers.deepseek import DeepSeekAdapter
        from agent.providers.base import ProviderConfig

        adapter = DeepSeekAdapter(ProviderConfig(
            provider="deepseek", api_key="test_key",
            base_url="https://api.deepseek.com",
            default_model="deepseek-v4-flash",
        ))
        assert "beta" not in adapter.config.base_url.lower()

    def test_thinking_explicitly_disabled(self):
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
        extra_body = payload.get("extra_body", {})
        assert extra_body.get("thinking") == {"type": "disabled"}


# 6. ToolRegistry native specs tests
class TestToolRegistryNativeSpecs:
    def test_registry_has_native_eligible_tools(self):
        """v0.2: all 4 tools are native-tool-call-eligible."""
        native_names = {
            t.name for t in TOOL_REGISTRY.iter_defs()
            if t.native_tool_call_eligible
        }
        assert native_names == {"get_time", "get_weather", "send_wechat_messages", "skip_reply"}

    def test_send_email_not_registered(self):
        """v0.2: send_email is removed from the registry entirely."""
        t = TOOL_REGISTRY.get("send_email")
        assert t is None

    def test_search_memory_not_registered(self):
        """v0.2: search_memory is removed from the registry entirely."""
        t = TOOL_REGISTRY.get("search_memory")
        assert t is None

    def test_native_specs_without_eligible_returns_empty(self):
        reg = ToolRegistry()
        from agent.tool_registry import ToolDef
        reg.register(ToolDef(
            name="test_tool", description="d",
            args_schema=[], native_tool_call_eligible=False,
        ))
        specs = reg.get_native_tool_specs(eligible_only=True)
        assert specs == []

    def test_native_specs_includes_eligible(self):
        reg = ToolRegistry()
        from agent.tool_registry import ToolDef
        reg.register(ToolDef(
            name="test_tool", description="d",
            args_schema=["arg1"], native_tool_call_eligible=True,
        ))
        specs = reg.get_native_tool_specs(eligible_only=True)
        assert len(specs) == 1
        assert specs[0]["function"]["name"] == "test_tool"


# 7. Tool category safety — replaces old hard-deny-list
class TestToolCategorySafety:
    """v0.2: Tool categories enforce safety. send_email is not registered."""

    def test_send_email_not_registered(self):
        """send_email tool should NOT be in the registry at all."""
        assert TOOL_REGISTRY.get("send_email") is None

    def test_send_wechat_messages_is_terminal(self):
        tool = TOOL_REGISTRY.get("send_wechat_messages")
        assert tool is not None
        assert tool.tool_category == "terminal_tool"

    def test_skip_reply_is_terminal(self):
        tool = TOOL_REGISTRY.get("skip_reply")
        assert tool is not None
        assert tool.tool_category == "terminal_tool"

    def test_get_time_is_fact_tool(self):
        tool = TOOL_REGISTRY.get("get_time")
        assert tool is not None
        assert tool.tool_category == "fact_tool"

    def test_get_weather_is_fact_tool(self):
        tool = TOOL_REGISTRY.get("get_weather")
        assert tool is not None
        assert tool.tool_category == "fact_tool"

    def test_wechat_reply_not_registered(self):
        """wechat_reply should NOT be registered (removed in v0.2)."""
        assert TOOL_REGISTRY.get("wechat_reply") is None

    def test_skip_action_not_registered(self):
        """old skip action should NOT be registered."""
        assert TOOL_REGISTRY.get("skip") is None


# 8. Trace fields
class TestTraceFields:
    def test_tools_exposed_in_trace(self):
        from agent.providers.deepseek import DeepSeekAdapter
        from agent.providers.base import ProviderConfig

        cfg = ProviderConfig(
            provider="deepseek", api_key="sk-test",
            base_url="https://api.deepseek.com",
            default_model="deepseek-v4-flash",
        )
        adapter = DeepSeekAdapter(cfg)

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

        traces = []

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


# Helpers
class _FakeGateway:
    def __init__(self, response=None):
        self.response = response
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.response


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
