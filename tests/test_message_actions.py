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
from agent.state_machine import TurnState


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

    async def complete(self, request, **kwargs):
        self.requests.append(request)
        return self.response


def _run(agent, user_message="test", wechat_sender=None):
    return asyncio.run(agent.chat(
        user_message=user_message, history=[], trigger="user",
        wechat_sender=wechat_sender,
    ))


class TestSendWechatMessages:
    def test_old_send_wechat_messages_rejected(self):
        """v1.5: deprecated send_wechat_messages → PROTOCOL_RETRY."""
        tc = _make_tc("send_wechat_messages", '{"messages":["hello"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_text_message", "finish_turn"],
            test_mode=True, routing_mode="manual_flash",
        )
        result = _run(agent)
        # Deprecated tool → PROTOCOL_RETRY → END_PROTOCOL_ERROR or END_FAILED
        assert result.terminal_state in (TurnState.END_FAILED, TurnState.END_PROTOCOL_ERROR, TurnState.END_FALLBACK_SENT)
        assert result.sent_message_count == 0

    def test_wechat_reply_single_bubble(self):
        """v1.5: wechat_reply sends one bubble, needs done to end."""
        call_count = [0]
        responses = [
            _make_response("", tool_calls=[_make_tc("send_text_message", '{"text":"hello"}')]),
            _make_response("", tool_calls=[_make_tc("finish_turn", '{"status":"replied"}')]),
        ]
        class MGW:
            def __init__(self): self.requests = []
            async def complete(self, req, **kwargs):
                self.requests.append(req)
                idx = min(call_count[0], len(responses) - 1); call_count[0] += 1
                return responses[idx]

        sent_msgs = []
        async def fake_sender(messages):
            for m in messages: m["send_status"] = "delivered"
            sent_msgs.extend(messages)
            return messages

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=MGW(),
            native_tool_calls=True,
            native_allowed_tools=["send_text_message", "finish_turn"],
            test_mode=True, routing_mode="manual_flash",
        )
        result = _run(agent, wechat_sender=fake_sender)
        assert result.terminal_state == TurnState.END_REPLIED, f"got {result.terminal_state}"
        assert result.sent_message_count == 1
        assert len(sent_msgs) == 1

    def test_unknown_tool_triggers_protocol_retry(self):
        """v1.5: unknown tool → PROTOCOL_RETRY."""
        tc = _make_tc("hack_system", "{}")
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_text_message", "finish_turn"],
            test_mode=True, routing_mode="manual_flash",
        )
        result = _run(agent)
        types = [e.get("type") for e in result.fallback_events]
        assert "protocol_retry" in types

    def test_no_tool_calls_fallback(self):
        """v1.5: no tool_calls → PROTOCOL_RETRY → HARD_FALLBACK."""
        gw = FakeGateway(response=_make_response("plain text reply"))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_text_message", "finish_turn"],
            test_mode=True, routing_mode="manual_flash",
        )
        result = _run(agent)
        assert len(result.visible_messages) >= 1
        types = [e.get("type") for e in result.fallback_events]
        assert "hard_fallback_enter" in types


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
