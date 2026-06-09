from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.chat_agent import ChatAgent
from agent.runtime_context_builder import ContextBuilder
from agent.state_machine import ModelProfile, TurnState
from agent.tool_registry import TOOL_REGISTRY
from runtime_contracts import (
    BuildContextRequest,
    ConversationHistoryMessage,
    SystemTime,
    TurnType,
)


@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction


class FakeGateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def response(*, content="", reasoning="", calls=None):
    return SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=list(calls or []),
        usage=None,
    )


def test_registry_contains_only_current_tools():
    assert TOOL_REGISTRY.names() == {
        "send_text_message",
        "get_time",
        "get_weather",
    }


def test_context_builder_preserves_structured_provider_history():
    structured = [
        {"type": "thinking", "thinking": "consider"},
        {
            "type": "tool_use",
            "id": "tool_1",
            "name": "send_text_message",
            "input": {"text": "你好"},
        },
    ]
    request = BuildContextRequest(
        turn_id="turn_1",
        call_id="call_1",
        turn_type=TurnType.USER_CHAT,
        user_id="user_1",
        bot_id="default",
        persona="persona",
        fixed_system_protocol="protocol",
        system_time=SystemTime(
            dt=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
            timezone="UTC",
            weekday_cn="周一",
        ),
        recent_history=[
            ConversationHistoryMessage(
                message_id="message_1",
                turn_id="turn_0",
                role="assistant",
                content=structured,
                source="protocol",
                delivery_status="delivered",
            )
        ],
    )

    built = ContextBuilder().build(request)

    assert built.provider_messages[0].content == structured
    assert [part["text"] for part in built.provider_system] == [
        "protocol",
        "persona",
        "Current time: 2026-06-08 12:00:00 周一 (UTC).",
    ]


@pytest.mark.asyncio
async def test_send_result_is_fed_back_then_turn_ends_implicitly():
    send_call = FakeToolCall(
        id="tool_1",
        function=FakeFunction(
            name="send_text_message",
            arguments='{"text":"你好"}',
        ),
    )
    gateway = FakeGateway([
        response(reasoning="reply", calls=[send_call]),
        response(content="internal note", reasoning="nothing else"),
    ])
    agent = ChatAgent(
        model="test-model",
        persona="persona",
        personality="",
        llm_gateway=gateway,
        pro_profile=ModelProfile(model_name="test-model"),
        context_builder=ContextBuilder(),
    )

    async def sender(messages):
        return [{**messages[0], "send_status": "delivered"}]

    result = await agent.chat(
        "你好",
        [],
        wechat_sender=sender,
        interrupt_checker=lambda _: False,
    )

    assert result.terminal_state == TurnState.END_REPLIED
    assert result.delivered_count == 1
    assert len(gateway.requests) == 2
    assert gateway.requests[0].tool_choice == "auto"
    assert gateway.requests[1].messages[-1] == {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "tool_1",
            "content": "sent",
        }],
    }
