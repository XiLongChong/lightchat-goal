"""Test structured message actions.

Message count must come from JSON actions, not punctuation or spacing.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.chat_agent import ChatAgent


class _FakeCompletions:
    def __init__(self, content: str):
        self.content = content

    async def create(self, **kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content)
                )
            ]
        )


class _FakeClient:
    def __init__(self, content: str):
        self.chat = SimpleNamespace(
            completions=_FakeCompletions(content)
        )


def _run_agent(raw: str):
    agent = ChatAgent(
        client=_FakeClient(raw),
        model="test",
        persona="test persona",
        personality="test personality",
    )
    return asyncio.run(agent.chat(
        user_message="测试",
        memories=[],
        history=[],
        relationship={},
    ))


def test_multiple_wechat_reply_actions_stay_multiple():
    raw = """
    {
      "actions": [
        {"type": "tool", "name": "wechat_reply", "args": {"text": "第一条", "delay": 0}},
        {"type": "tool", "name": "wechat_reply", "args": {"text": "第二条", "delay": 1}}
      ],
      "done": true
    }
    """

    result = _run_agent(raw)

    assert [m["text"] for m in result["messages"]] == ["第一条", "第二条"]


def test_single_message_with_spaces_is_not_split():
    raw = """
    {
      "actions": [
        {"type": "tool", "name": "wechat_reply", "args": {"text": "还行 不算热 就是有点闷", "delay": 0}}
      ],
      "done": true
    }
    """

    result = _run_agent(raw)

    assert len(result["messages"]) == 1
    assert result["messages"][0]["text"] == "还行 不算热 就是有点闷"


def test_schedule_wechat_message_action_is_collected():
    raw = """
    {
      "actions": [
        {"type": "tool", "name": "schedule_wechat_message", "args": {
          "delay_minutes": 10,
          "messages": [
            {"text": "起来喝水", "delay": 0},
            {"text": "别装死", "delay": 1}
          ]
        }}
      ],
      "done": true
    }
    """

    result = _run_agent(raw)

    assert result["messages"] == []
    assert len(result["scheduled_messages"]) == 1
    assert result["scheduled_messages"][0]["delay_minutes"] == 10
    assert [m["text"] for m in result["scheduled_messages"][0]["messages"]] == [
        "起来喝水",
        "别装死",
    ]
