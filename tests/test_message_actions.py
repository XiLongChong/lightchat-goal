"""Test structured message actions.

Message count must come from JSON actions, not punctuation or spacing.
"""

import asyncio
import json
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
        user_message="test",
        memories=[],
        history=[],
        relationship={},
    ))


def test_multiple_wechat_reply_actions_stay_multiple():
    raw = """
    {
      "actions": [
        {"type": "tool", "name": "wechat_reply", "args": {"text": "first", "delay": 0}},
        {"type": "tool", "name": "wechat_reply", "args": {"text": "second", "delay": 1}}
      ],
      "done": true
    }
    """

    result = _run_agent(raw)

    assert [m["text"] for m in result["messages"]] == ["first", "second"]


def test_single_message_with_spaces_is_not_split():
    raw = """
    {
      "actions": [
        {"type": "tool", "name": "wechat_reply", "args": {"text": "one message with spaces", "delay": 0}}
      ],
      "done": true
    }
    """

    result = _run_agent(raw)

    assert len(result["messages"]) == 1
    assert result["messages"][0]["text"] == "one message with spaces"


def test_schedule_wechat_message_action_is_collected():
    raw = """
    {
      "actions": [
        {"type": "tool", "name": "schedule_wechat_message", "args": {
          "delay_minutes": 10,
          "messages": [
            {"text": "drink water", "delay": 0},
            {"text": "stand up", "delay": 1}
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
        "drink water",
        "stand up",
    ]


def test_skip_action_records_no_reply_reason():
    raw = """
    {
      "actions": [
        {"type": "skip", "reason": "user explicitly said no reply needed"}
      ],
      "done": true
    }
    """

    result = _run_agent(raw)

    assert result["messages"] == []
    assert result["skip_reply"] is True
    assert result["skip_reason"] == "user explicitly said no reply needed"


def test_invalid_json_records_fallback_and_token_usage():
    result = _run_agent("plain text fallback")

    assert result["fallback_events"][0]["type"] == "invalid_json"
    assert result["token_usage"]["total_tokens"] > 0
    assert result["token_usage"]["estimated"] is True


def test_protocol_json_text_is_unwrapped_before_wechat():
    leaked = '{"actions":[{"type":"tool","name":"wechat_reply","args":{"text":"nested ok","delay":0}}],"done":true}'
    leaked_json = json.dumps(leaked, ensure_ascii=False)
    raw = f"""
    {{
      "actions": [
        {{"type": "tool", "name": "wechat_reply", "args": {{"text": {leaked_json}, "delay": 0}}}}
      ],
      "done": true
    }}
    """

    result = _run_agent(raw)

    # V2: messages no longer carry "type" key; output_guard unwraps protocol JSON
    assert result["messages"] == [
        {"text": "nested ok", "delay": 0}
    ]
    # V2: protocol unwrap is recorded as "protocol_text_unwrapped" by the guard
    assert any(e["type"] == "protocol_text_unwrapped" for e in result.get("fallback_events", []))
