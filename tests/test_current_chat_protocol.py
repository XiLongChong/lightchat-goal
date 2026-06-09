from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agent.chat_agent import ChatAgent
from agent.runtime_context_builder import ContextBuilder
from agent.state_machine import ModelProfile, TurnState


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
