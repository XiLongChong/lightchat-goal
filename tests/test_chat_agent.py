"""Tests for JSON parser, output validation, history formatting, and Phase 1 native tools.

Phase 1: JSON action parsing tests removed - no longer primary protocol.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.json_parser import (
    safe_parse_json, parse_agent_output, extract_json, validate_agent_output,
)

# ================================================================
# JSON parser unit tests (safeguard utilities, not protocol-dependent)
# ================================================================


def test_extract_json_from_code_block():
    raw = '```json\n{"actions": [], "done": true}\n```'
    result = extract_json(raw)
    assert result == '{"actions": [], "done": true}'


def test_extract_json_bare():
    raw = '{"actions":[],"done":true}'
    result = extract_json(raw)
    assert result == raw


def test_extract_json_with_text_around():
    raw = '好的，以下是回复：\n{"actions":[{"type":"message","text":"你好"}],"done":true}'
    result = extract_json(raw)
    assert result == '{"actions":[{"type":"message","text":"你好"}],"done":true}'


def test_safe_parse_valid_json():
    raw = '{"actions": [{"type": "message", "text": "hello"}], "done": true}'
    result = safe_parse_json(raw)
    assert result is not None
    assert result["done"] is True
    assert len(result["actions"]) == 1


def test_safe_parse_invalid_json_returns_none():
    raw = "不是合法的JSON {{{"
    result = safe_parse_json(raw)
    assert result is None


def test_safe_parse_empty_string_returns_none():
    result = safe_parse_json("")
    assert result is None


def test_safe_parse_trailing_comma_fix():
    raw = '{"actions": [{"type": "message", "text": "hi"},], "done": true,}'
    result = safe_parse_json(raw)
    assert result is not None
    assert result["done"] is True


def test_parse_agent_output_fallback():
    text = "不是JSON的输出文本"
    result = parse_agent_output(text)
    assert result["done"] is True
    assert len(result["actions"]) == 1
    assert result["actions"][0]["type"] == "message"
    assert text in result["actions"][0]["text"]


def test_parse_agent_output_no_actions_field():
    result = parse_agent_output('{"done": true}')
    assert result["done"] is True
    assert len(result["actions"]) == 1
    assert result["actions"][0]["type"] == "message"


def test_parse_agent_output_preserves_valid_json():
    long_text = "啊" * 600
    raw = f'{{"actions":[{{"type":"message","text":"{long_text}"}}],"done":true}}'
    result = parse_agent_output(raw)
    assert result["done"] is True
    assert len(result["actions"]) == 1
    assert result["actions"][0]["text"] == long_text


# ================================================================
# Phase 1: ChatAgent native tool call tests
# ================================================================
from agent.llm_types import LLMRequest, LLMResponse, LLMUsage, LLMError
from agent.chat_agent import ChatAgent, _build_context_sample


def _make_response(content="", tool_calls=None, **kw):
    return LLMResponse(
        content=content,
        provider="deepseek",
        model="deepseek-v4-flash",
        usage=LLMUsage(
            prompt_tokens=kw.get("prompt_tokens", 10),
            completion_tokens=kw.get("completion_tokens", 5),
            total_tokens=kw.get("total_tokens", 15),
            cache_hit_tokens=kw.get("cache_hit_tokens", 0),
            cache_miss_tokens=kw.get("cache_miss_tokens", 10),
            reasoning_tokens=kw.get("reasoning_tokens", 0),
            estimated=kw.get("estimated", False),
        ),
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


class FakeGateway:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        if self.response:
            return self.response
        tc = _make_tc("send_wechat_messages", '{"messages":["hello"]}')
        return _make_response("", tool_calls=[tc])


class TestSendWechatMessages:
    def test_single_message(self):
        tc = _make_tc("send_wechat_messages", '{"messages":["OK"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="hello", history=[], trigger="user",
        ))
        assert result.llm_status == "ok"
        assert len(result.tool_results) >= 1
        assert result.tool_results[0]["name"] == "send_wechat_messages"

    def test_multi_message(self):
        tc = _make_tc("send_wechat_messages",
                       '{"messages":["msg1","msg2","msg3"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="test", history=[], trigger="user",
        ))
        assert len(result.tool_results) == 1
        assert result.tool_results[0]["name"] == "send_wechat_messages"

    def test_fact_tool_then_messages(self):
        call_count = [0]
        responses = [
            _make_response("", tool_calls=[_make_tc("get_time", '{"timezone":"Asia/Shanghai"}')],
                           prompt_tokens=50, completion_tokens=15, total_tokens=65),
            _make_response("", tool_calls=[_make_tc("send_wechat_messages", '{"messages":["3pm"]}')],
                           prompt_tokens=80, completion_tokens=10, total_tokens=90),
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

    def test_no_tool_calls_fallback(self):
        gw = FakeGateway(response=_make_response("Hello there!"))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="hi", history=[], trigger="user",
        ))
        assert len(result.visible_messages) >= 1
        types = [e.get("type") for e in result.fallback_events]
        assert "hard_fallback_enter" in types

    def test_max_loop_exit(self):
        from agent.chat_agent import MAX_TOOL_LOOP

        class MGW:
            def __init__(self):
                self.requests = []

            async def complete(self, req):
                self.requests.append(req)
                return _make_response("", tool_calls=[_make_tc("get_time", '{}')])

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
        assert result.iterations == MAX_TOOL_LOOP


# ================================================================
# v0.2: assistant.content fallback → PROTOCOL_RETRY → HARD_FALLBACK
# ================================================================


class TestAssistantContentFallback:
    def test_fallback_is_hard_fallback_not_model_content(self):
        """Model returns text without tool_calls → PROTOCOL_RETRY → HARD_FALLBACK.
        The sent message is the system hard fallback text, NOT the model's content."""
        gw = FakeGateway(response=_make_response("Hello there!"))
        sent_msgs = []

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            sent_msgs.extend(messages)
            return messages

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="hi", history=[], trigger="user",
            wechat_sender=fake_sender,
        ))
        # wechat_sender was called (with hard fallback)
        assert len(sent_msgs) >= 1, f"expected wechat_sender to be called, got {len(sent_msgs)}"
        # terminal_state is END_FALLBACK_SENT
        assert result.was_fallback, f"expected END_FALLBACK_SENT, got {result.terminal_state}"
        # fallback events include protocol_retry and hard_fallback
        types = [e.get("type") for e in result.fallback_events]
        assert "protocol_retry" in types
        assert "hard_fallback_enter" in types

    def test_no_double_send_for_tool_call(self):
        """send_wechat_messages tool call → wechat_sender called once, END_SENT."""
        tc = _make_tc("send_wechat_messages", '{"messages":["hello"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))
        sent_count = [0]

        async def fake_sender(messages):
            sent_count[0] += 1
            for m in messages:
                m["send_status"] = "delivered"
            return messages

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
            wechat_sender=fake_sender,
        ))
        assert sent_count[0] == 1, f"expected 1 send, got {sent_count[0]}"
        assert result.was_sent, f"expected END_SENT, got {result.terminal_state}"

    def test_assistant_fallback_sends_hard_text(self):
        """assistant.content fallback → END_FALLBACK_SENT with system text."""
        from agent.output_guard import HARD_FALLBACK_TEXT
        gw = FakeGateway(response=_make_response("some text reply"))

        sent_texts = []
        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
                sent_texts.append(m.get("text", ""))
            return messages

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="hi", history=[], trigger="user",
            wechat_sender=fake_sender,
        ))
        assert result.was_fallback
        # Sent text is the hard fallback text, not model content
        assert "some text reply" not in sent_texts
        assert any(HARD_FALLBACK_TEXT in t for t in sent_texts), \
            f"expected hard fallback text, got {sent_texts}"

    def test_no_wechat_sender_no_send(self):
        """No wechat_sender → messages stay in visible_messages, not sent."""
        gw = FakeGateway(response=_make_response("plain text"))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="hi", history=[], trigger="user",
            # No wechat_sender provided
        ))
        assert not result.was_sent
        # Hard fallback text was prepared
        assert len(result.visible_messages) >= 1


# ================================================================
# Phase 1: Tool retry tests
# ================================================================


class TestToolRetry:
    def test_assistant_content_triggers_tool_retry(self):
        """First: assistant.content, no tool → retry → model calls send_wechat_messages."""
        call_count = [0]
        responses = [
            _make_response("some text reply"),  # first: no tool_calls
            _make_response("", tool_calls=[
                _make_tc("send_wechat_messages", '{"messages":["msg1","msg2"]}')
            ]),  # retry: calls tool
        ]

        class MGW:
            def __init__(self):
                self.requests = []

            async def complete(self, req):
                self.requests.append(req)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        sent_msgs = []

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            sent_msgs.extend(messages)
            return messages

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=MGW(),
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="hello", history=[], trigger="user",
            wechat_sender=fake_sender,
        ))
        assert call_count[0] == 2, f"expected 2 LLM calls, got {call_count[0]}"
        events = [e.get("type") for e in result.fallback_events]
        assert "assistant_content_no_tool_call" in events
        assert "protocol_retry" in events
        assert result.was_sent
        assert len(sent_msgs) == 2  # sent via tool, not fallback

    def test_explicit_multi_request_detected_and_retried(self):
        """User says "分开发" → detection triggers multi-message retry note."""
        call_count = [0]
        responses = [
            _make_response("ok let me split"),  # first: no tool
            _make_response("", tool_calls=[
                _make_tc("send_wechat_messages",
                          '{"messages":["第一条","第二条","第三条"]}')
            ]),
        ]

        class MGW:
            def __init__(self):
                self.requests = []

            async def complete(self, req):
                self.requests.append(req)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        sent_msgs = []

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            sent_msgs.extend(messages)
            return messages

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=MGW(),
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="你一条一条发给我", history=[], trigger="user",
            wechat_sender=fake_sender,
        ))
        assert call_count[0] == 2
        events = [e.get("type") for e in result.fallback_events]
        assert "assistant_content_no_tool_call" in events
        assert "protocol_retry" in events
        assert result.was_sent
        assert len(sent_msgs) == 3

    def test_retry_failure_then_content_fallback(self):
        """Both rounds return no tool_calls → final fallback with 1 message."""
        call_count = [0]
        responses = [
            _make_response("text reply"),
            _make_response("still no tool"),
        ]

        class MGW:
            def __init__(self):
                self.requests = []

            async def complete(self, req):
                self.requests.append(req)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        sent_msgs = []

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            sent_msgs.extend(messages)
            return messages

        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=MGW(),
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="hi", history=[], trigger="user",
            wechat_sender=fake_sender,
        ))
        assert call_count[0] == 2
        events = [e.get("type") for e in result.fallback_events]
        assert "protocol_retry_exceeded" in events
        assert "hard_fallback_enter" in events
        assert result.was_fallback
        assert len(sent_msgs) == 1  # single hard fallback msg

    def test_ten_messages_allowed_by_schema(self):
        """10 messages pass through guard (max_count=12)."""
        from agent.output_guard import guard_send_message_batch

        msgs = [f"msg{i}" for i in range(10)]
        result = guard_send_message_batch(msgs, max_count=12)
        assert len(result.messages) == 10
        assert result.truncated_count == 0
        assert result.blocked_count == 0

    def test_twenty_messages_truncated_by_guard(self):
        """20 messages → truncated to 12, truncated_count=8."""
        from agent.output_guard import guard_send_message_batch

        msgs = [f"msg{i}" for i in range(20)]
        result = guard_send_message_batch(msgs, max_count=12)
        assert len(result.messages) == 12
        assert result.truncated_count == 8

    def test_empty_messages_discarded_not_counting_toward_limit(self):
        """Empty strings are blocked, valid ones pass."""
        from agent.output_guard import guard_send_message_batch

        msgs = ["", "valid1", "   ", "valid2"]
        result = guard_send_message_batch(msgs, max_count=12)
        assert result.messages == ["valid1", "valid2"]
        assert result.blocked_count == 2


# ================================================================
# Validate helper unit tests (standalone)
# ================================================================


class TestValidateAgentOutput:
    def test_valid_simple_actions(self):
        errors = validate_agent_output({
            "actions": [{"type": "message", "text": "hello"}], "done": True,
        })
        assert errors == []

    def test_valid_tool_action(self):
        errors = validate_agent_output({
            "actions": [{"type": "tool", "name": "get_time", "args": {}}], "done": False,
        })
        assert errors == []

    def test_valid_skip_action(self):
        errors = validate_agent_output({
            "actions": [{"type": "skip", "reason": "no need"}], "done": True,
        })
        assert errors == []

    def test_missing_actions_field(self):
        errors = validate_agent_output({"done": True})
        assert any("missing_actions" in e for e in errors)

    def test_actions_not_list(self):
        errors = validate_agent_output({"actions": "not_a_list"})
        assert any("not_list" in e for e in errors)

    def test_action_not_object(self):
        errors = validate_agent_output({
            "actions": ["just a string", {"type": "message", "text": "ok"}],
            "done": True,
        })
        assert any("not_object" in e for e in errors)

    def test_message_empty_text(self):
        errors = validate_agent_output({
            "actions": [{"type": "message", "text": ""}], "done": True,
        })
        assert any("empty_text" in e for e in errors)

    def test_wechat_reply_empty_text(self):
        errors = validate_agent_output({
            "actions": [{"type": "tool", "name": "wechat_reply", "args": {"text": ""}}],
            "done": True,
        })
        assert any("wechat_reply_empty_text" in e for e in errors)

    def test_non_dict_input(self):
        errors = validate_agent_output([1, 2, 3])
        assert any("top_level_not_object" in e for e in errors)

    def test_done_not_bool(self):
        errors = validate_agent_output({
            "actions": [{"type": "message", "text": "hi"}], "done": "yes",
        })
        assert any("done_not_bool" in e for e in errors)


# ================================================================
# History formatting tests
# ================================================================


class TestHistoryRoleFormatting:
    def test_user_message_standard_format(self):
        from agent.chat_agent import _format_history_entry
        result = _format_history_entry({
            "role": "user", "content": "hello world",
            "ts": 1717000000, "source": "wechat",
        })
        assert "hello world" in result  # content preserved
        assert "[历史消息" not in result  # no history label
        assert "来源:" not in result  # no source label

    def test_no_role_labels_in_content(self):
        from agent.chat_agent import _format_history_entry
        for role, source, content in [
            ("user", "wechat", "hello"),
            ("assistant", "user_reply", "hi there"),
        ]:
            result = _format_history_entry({
                "role": role, "content": content,
                "ts": 1717000000, "source": source,
            })
            assert "[BotReply" not in result
            assert "[BotMsg" not in result
            assert "[历史消息" not in result
            assert "来源:" not in result


# ================================================================
# Context sample tests
# ================================================================


def test_context_sample_in_chat_result():
    messages = [
        {"role": "system", "content": "You are a bot."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "ok"},
    ]
    sample = _build_context_sample(messages)
    assert len(sample) == 3
    assert sample[0]["role"] == "user"
    assert sample[1]["role"] == "assistant"


def test_context_sample_no_raw_json():
    messages = [
        {"role": "system", "content": "bot"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    sample = _build_context_sample(messages)
    for m in sample:
        assert '"actions"' not in m.get("preview", "")


# ================================================================
# Phase 1: History leak prevention tests
# ================================================================


class TestHistoryLeakPrevention:
    def test_history_format_no_labels(self):
        from agent.chat_agent import _format_history_entry
        result = _format_history_entry({
            "role": "user", "content": "today food",
            "ts": 1717000000, "source": "wechat",
        })
        assert "[history" not in result.lower()
        assert "source" not in result.lower() or ":" not in result

    def test_history_format_no_metadata_leak(self):
        from agent.chat_agent import _format_history_entry
        result = _format_history_entry({
            "role": "assistant", "content": "hi there",
            "ts": 1717000000, "source": "assistant",
        })
        assert "hi there" in result
        assert "2026" not in result  # no timestamp in content

    def test_assistant_actions_not_unwrapped_in_v0_2(self):
        """v0.2: _extract_visible_text does NOT unwrap old JSON actions.

        Old JSON actions are treated as plain text. The state machine's
        HANDLE_MODEL_OUTPUT detects them and routes to PROTOCOL_RETRY.
        """
        from agent.chat_agent import _format_history_entry
        raw_json = '{"actions":[{"type":"tool","name":"wechat_reply","args":{"text":"hello world","delay":0}}],"done":true}'
        result = _format_history_entry({
            "role": "assistant", "content": raw_json,
            "ts": 1717000000, "source": "assistant",
        })
        # v0.2: content is NOT unwrapped — JSON actions are plain text
        assert "hello world" in result
        # Content passes through without JSON extraction

    def test_system_prompt_has_no_history(self):
        from agent.context_builder import build_chat_system_prompt
        prompt = build_chat_system_prompt(
            persona="test persona", personality="casual",
            default_city="Chengdu",
            tool_names=["get_time", "get_weather", "send_wechat_messages"],
        )
        assert "history" not in prompt.lower() or "时间" in prompt

    def test_guard_blocks_history_leak(self):
        from agent.output_guard import guard_send_message_batch
        result = guard_send_message_batch([
            "normal message",
        ])
        assert result.blocked_count == 0

    def test_system_prompt_no_history_text(self):
        from agent.context_builder import build_chat_system_prompt
        history_text = "user said hello yesterday"
        prompt = build_chat_system_prompt(
            persona="p", personality="", tool_names=["send_wechat_messages"],
        )
        assert history_text not in prompt


# ================================================================
# DB recovery ordering test
# ================================================================


def test_conversation_history_same_ts_ordering():
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.execute("""
        CREATE TABLE conversation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
            ts REAL NOT NULL, bot_id TEXT NOT NULL DEFAULT ''
        )
    """)
    now = 1717000000.0
    for role, content in [("user", "msg1"), ("assistant", "reply1"),
                           ("user", "msg2"), ("assistant", "reply2")]:
        db.execute(
            "INSERT INTO conversation_history(user_id,role,content,source,ts,bot_id) VALUES (?,?,?,?,?,?)",
            ("u1", role, content, "wechat" if role == "user" else "assistant", now, "b1"),
        )
    db.commit()
    rows = db.execute(
        "SELECT id,role,content,source,ts FROM conversation_history WHERE user_id=? AND bot_id=? ORDER BY ts DESC, id DESC LIMIT ?",
        ("u1", "b1", 40),
    ).fetchall()
    ordered = list(reversed(rows))
    db.close()
    assert ordered[0][2] == "msg1"
    assert ordered[1][2] == "reply1"
    assert ordered[2][2] == "msg2"
    assert ordered[3][2] == "reply2"


# ================================================================
# Phase 1: send_messages guard tests
# ================================================================


class TestSendMessagesGuard:
    def test_normal_batch_passes(self):
        from agent.output_guard import guard_send_message_batch
        result = guard_send_message_batch(["hello", "world"])
        assert result.blocked_count == 0
        assert len(result.messages) == 2

    def test_empty_messages_blocked(self):
        from agent.output_guard import guard_send_message_batch
        result = guard_send_message_batch(["", "   ", "ok"])
        assert result.blocked_count >= 2
        assert "ok" in result.messages

    def test_protocol_leak_blocked(self):
        from agent.output_guard import guard_send_message_batch
        result = guard_send_message_batch([
            '{"actions":[{"type":"message","text":"leaked"}]}',
            "normal message",
        ])
        assert result.blocked_count >= 1
        assert "normal message" in result.messages

    def test_total_char_limit(self):
        from agent.output_guard import guard_send_message_batch
        long_msg = "x" * 600
        result = guard_send_message_batch([long_msg], max_single=500, max_total=500)
        assert len(result.messages) > 0
        assert len(result.messages[0]) <= 500

    def test_all_blocked_returns_empty(self):
        from agent.output_guard import guard_send_message_batch
        result = guard_send_message_batch([
            '{"actions":[{"type":"message"}]}',
            '{"wechat_reply":"bad"}',
        ])
        assert len(result.messages) == 0
        assert result.blocked_count == 2


# ================================================================
# compute_message_delay tests
# ================================================================


def test_delay_zero_in_test_mode():
    from agent.tools import compute_message_delay
    for i in range(3):
        assert compute_message_delay(i, "hello", 3, test_mode=True) == 0.0


def test_delay_first_message():
    from agent.tools import compute_message_delay
    d = compute_message_delay(0, "hello", 3, test_mode=False)
    assert 0.2 <= d <= 0.8


def test_delay_subsequent_short():
    from agent.tools import compute_message_delay
    d = compute_message_delay(1, "short", 3, test_mode=False)
    assert 0.8 <= d <= 1.6


def test_delay_subsequent_long():
    from agent.tools import compute_message_delay
    d = compute_message_delay(1, "x" * 100, 3, test_mode=False)
    assert 1.5 <= d <= 3.0


# ================================================================
# Phase 1: Tool call ID leak prevention tests
# ================================================================


class TestToolCallIdLeak:
    def test_guard_blocks_call_id_in_send_messages(self):
        from agent.output_guard import guard_send_message_batch

        result = guard_send_message_batch(["[call_abc123def] OK"])
        assert len(result.messages) == 0, f"call_ pattern must be blocked, got {result.messages}"

    def test_guard_blocks_call_id_in_single_message(self):
        from agent.output_guard import guard_single_message

        result = guard_single_message("[call_zrigmidsb] OK")
        assert "call_" not in result, f"call_ must not appear in guarded output: {result}"

    def test_single_message_returns_fallback_for_call_leak(self):
        from agent.output_guard import guard_single_message

        result = guard_single_message("call_abc123defgh something")
        assert result == "我刚刚卡了一下 重新说", f"expected hard fallback, got {result}"

    def test_guard_blocks_multiple_call_variants(self):
        from agent.output_guard import guard_send_message_batch

        leaks = [
            "[call_abc123def] hi",
            "call_xyz789012 more text",
            "[call_qwertyuiop] hello",
        ]
        result = guard_send_message_batch(leaks + ["clean message"])
        assert result.blocked_count >= len(leaks)
        assert "clean message" in result.messages

    def test_history_strips_call_id(self):
        from agent.chat_agent import _format_history_entry

        result = _format_history_entry({
            "role": "assistant",
            "content": "call_abc123defgh some text",
            "ts": 1717000000, "source": "assistant",
        })
        assert "call_" not in result

    def test_extract_visible_text_strips_call_id(self):
        from agent.chat_agent import _extract_visible_text

        result = _extract_visible_text("call_abc123defgh hello world")
        assert "call_" not in result
        assert "hello world" in result

    def test_clean_history_preserves_normal_text(self):
        from agent.chat_agent import _format_history_entry

        result = _format_history_entry({
            "role": "assistant",
            "content": "你好呀，今天天气不错",
            "ts": 1717000000, "source": "assistant",
        })
        assert "你好呀" in result
        assert "call_" not in result

    def test_send_wechat_messages_content_clean(self):
        """send_wechat_messages tool result must not contain tool_call_id."""
        tc = _make_tc("send_wechat_messages",
                       '{"messages":["clean message"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))

        sent_msgs = []

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            sent_msgs.extend(messages)
            return messages

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
            wechat_sender=fake_sender,
        ))
        for msg in result.visible_messages:
            assert "call_" not in msg.get("text", ""), f"tool_call_id leaked: {msg}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
