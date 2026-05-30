"""Phase 1: Native tool call action tests.

Old JSON action tests removed — no longer primary protocol.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.chat_agent import ChatAgent
from agent.llm_types import LLMResponse, LLMUsage


def _make_response(content="", tool_calls=None):
    return LLMResponse(
        content=content,
        provider="deepseek",
        model="deepseek-v4-flash",
        usage=LLMUsage(
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
        ),
        latency_ms=100,
        finish_reason="stop",
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


class FakeGateway:
    def __init__(self, response=None):
        self.response = response
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.response


def _run(agent, user_message="test"):
    return asyncio.run(agent.chat(
        user_message=user_message, history=[], trigger="user",
    ))


class TestSendWechatMessages:
    def test_single_message(self):
        tc = _make_tc("send_wechat_messages", '{"messages":["hello"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
            test_mode=True,
        )
        result = _run(agent)
        assert len(result.tool_results) >= 1
        assert result.tool_results[0]["name"] == "send_wechat_messages"

    def test_multi_message(self):
        tc = _make_tc("send_wechat_messages",
                       '{"messages":["first","second","third"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
            test_mode=True,
        )
        result = _run(agent)
        assert len(result.tool_results) == 1

    def test_unknown_tool_triggers_protocol_retry(self):
        """v0.2: unknown tool → PROTOCOL_RETRY, not executed."""
        tc = _make_tc("hack_system", "{}")
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["get_time", "send_wechat_messages"],
            test_mode=True,
        )
        result = _run(agent)
        # Unknown tool → PROTOCOL_RETRY → eventually HARD_FALLBACK
        types = [e.get("type") for e in result.fallback_events]
        assert "illegal_tool_combination" in types
        assert "protocol_retry" in types

    def test_no_tool_calls_fallback(self):
        """v0.2: no tool_calls → PROTOCOL_RETRY → HARD_FALLBACK."""
        gw = FakeGateway(response=_make_response("plain text reply"))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
            test_mode=True,
        )
        result = _run(agent)
        assert len(result.visible_messages) >= 1
        types = [e.get("type") for e in result.fallback_events]
        assert "hard_fallback_enter" in types


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
