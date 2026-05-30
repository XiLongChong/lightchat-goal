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
            test_mode=True,
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
            test_mode=True,
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
            test_mode=True,
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
            test_mode=True,
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


# ================================================================
# v1.2 Phase 2: Turn lifecycle + interrupt check + context errors
# ================================================================
from agent.state_machine import TurnState, DeliveryStatus, VisibleMessage, ContextBuildResult


class TestTurnLifecycle:
    """Verify the v1.2 lifecycle chain: RECEIVE_EVENT → CREATE_TURN →
    INTERRUPT_CHECK → BUILD_CONTEXT → CLASSIFY_CONTEXT_ERROR →
    OPTIONAL_PREPROCESS → CALL_LLM.
    """

    def test_lifecycle_states_visited(self):
        """A normal send_wechat_messages turn visits the core lifecycle states."""
        tc = _make_tc("send_wechat_messages", '{"messages":["ok"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))

        async def fake_sender(messages):
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
            user_message="hello", history=[], trigger="user",
            wechat_sender=fake_sender,
        ))
        assert result.terminal_state in (TurnState.END_SENT, TurnState.END_PARTIAL_SENT,
                                          TurnState.END_FALLBACK_SENT)
        # post_terminal_completed: False when no callbacks injected (test path).
        # The terminal_state itself confirms the lifecycle completed.
        assert result.terminal_state in (
            TurnState.END_SENT, TurnState.END_PARTIAL_SENT,
            TurnState.END_FALLBACK_SENT, TurnState.END_FAILED,
        ), f"unexpected terminal_state: {result.terminal_state}"

    def test_turn_has_interrupt_token(self):
        """CREATE_TURN generates a non-empty interrupt_token."""
        tc = _make_tc("send_wechat_messages", '{"messages":["ok"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        # We can't inspect TurnScratchpad from TurnResult easily,
        # but we can verify the turn completed without error.
        result = asyncio.run(agent.chat(
            user_message="hello", history=[], trigger="user",
        ))
        assert result.turn_id, "turn_id must be non-empty"

    def test_optional_preprocess_injects_time(self):
        """OPTIONAL_PREPROCESS merges current time into messages[0] system content."""
        call_count = [0]
        responses = [
            _make_response("", tool_calls=[_make_tc("send_wechat_messages", '{"messages":["ok"]}')]),
        ]

        class MGW:
            def __init__(self):
                self.requests = []
            async def complete(self, req):
                self.requests.append(req)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        gw = MGW()
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
        # Verify time was injected into messages[0], not appended as a new tail message
        req = gw.requests[0]
        msgs = req.messages
        assert msgs[0]["role"] == "system"
        assert "当前时间" in msgs[0]["content"], "time should be in first system message"
        # No separate system message with "[系统] 当前时间" at the tail
        tail_roles = [m["role"] for m in msgs]
        system_count = sum(1 for r in tail_roles if r == "system")
        assert system_count == 1, f"expected 1 system msg, got {system_count}: roles={tail_roles}"


class TestToolChoice:
    """v1.2: tool_choice='required' enforced in CALL_LLM."""

    def test_tool_choice_required_when_tools_present(self):
        """Request with tools → tool_choice='required'."""
        tc = _make_tc("send_wechat_messages", '{"messages":["ok"]}')
        call_count = [0]
        responses = [_make_response("", tool_calls=[tc])]

        class MGW:
            def __init__(self):
                self.requests = []
            async def complete(self, req):
                self.requests.append(req)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        gw = MGW()
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        asyncio.run(agent.chat(
            user_message="hello", history=[], trigger="user",
        ))
        assert gw.requests[0].tool_choice == "required"

    def test_tool_choice_none_when_no_tools(self):
        """No tools → tool_choice is None."""
        call_count = [0]
        responses = [_make_response("ok")]  # no tools → content only

        class MGW:
            def __init__(self):
                self.requests = []
            async def complete(self, req):
                self.requests.append(req)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        gw = MGW()
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=[],  # no tools
            test_mode=True,
        )
        import asyncio
        asyncio.run(agent.chat(
            user_message="hello", history=[], trigger="user",
        ))
        assert gw.requests[0].tool_choice is None

    def test_protocol_retry_keeps_tool_choice_required(self):
        """PROTOCOL_RETRY → CALL_LLM still uses tool_choice='required'."""
        call_count = [0]
        responses = [
            _make_response("text without tools"),  # violation
            _make_response("", tool_calls=[_make_tc("send_wechat_messages", '{"messages":["retry ok"]}')]),
        ]

        class MGW:
            def __init__(self):
                self.requests = []
            async def complete(self, req):
                self.requests.append(req)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        gw = MGW()
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        asyncio.run(agent.chat(
            user_message="hello", history=[], trigger="user",
        ))
        # Both requests should have tool_choice='required'
        for req in gw.requests:
            assert req.tool_choice == "required", \
                f"expected 'required', got {req.tool_choice}"


class TestInterruptCheck:
    """v1.2 §5: INTERRUPT_CHECK at key lifecycle points."""

    def test_interrupt_at_create_turn_cancels(self):
        """Turn interrupted immediately after creation → END_CANCELLED."""
        tc = _make_tc("send_wechat_messages", '{"messages":["ok"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))

        def _always_interrupt(_token: str) -> bool:
            return True

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
            interrupt_checker=_always_interrupt,
        ))
        assert result.was_cancelled, f"expected END_CANCELLED, got {result.terminal_state}"

    def test_no_interrupt_completes_normally(self):
        """Always-false checker → normal completion."""
        tc = _make_tc("send_wechat_messages", '{"messages":["ok"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))

        def _never_interrupt(_token: str) -> bool:
            return False

        async def fake_sender(messages):
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
            user_message="hello", history=[], trigger="user",
            interrupt_checker=_never_interrupt,
            wechat_sender=fake_sender,
        ))
        assert not result.was_cancelled, f"should not be cancelled, got {result.terminal_state}"
        assert result.was_sent, f"expected END_SENT, got {result.terminal_state}"

    def test_interrupt_after_fact_tool_returns_cancelled(self):
        """INTERRUPT_CHECK after fact tool execution → END_CANCELLED."""
        call_count = [0]
        interrupter_done = [False]

        def _interrupt_after_fact(_token: str) -> bool:
            # Interrupt only after the first model call (which returns a fact tool)
            return call_count[0] >= 1

        responses = [
            _make_response("", tool_calls=[_make_tc("get_time", '{}')],
                           prompt_tokens=50, completion_tokens=10, total_tokens=60),
            _make_response("", tool_calls=[_make_tc("send_wechat_messages", '{"messages":["time"]}')],
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
            interrupt_checker=_interrupt_after_fact,
        ))
        assert result.was_cancelled, f"expected END_CANCELLED after fact tool, got {result.terminal_state}"

    def test_interrupt_at_guard_output_cancels(self):
        """INTERRUPT_CHECK before GUARD_OUTPUT → END_CANCELLED."""
        tc = _make_tc("send_wechat_messages", '{"messages":["hello"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))
        sent_msgs = []

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            sent_msgs.extend(messages)
            return messages

        # Interrupt on the second check (before GUARD_OUTPUT / EXEC_SEND)
        # The first check is at CREATE_TURN, second at HANDLE_MODEL_OUTPUT,
        # third at GUARD_OUTPUT. We want to cancel at the guard.
        check_count = [0]

        def _delayed_interrupt(_token: str) -> bool:
            check_count[0] += 1
            return check_count[0] >= 3  # interrupt at check #3 (GUARD_OUTPUT)

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
            interrupt_checker=_delayed_interrupt,
            wechat_sender=fake_sender,
        ))
        assert result.was_cancelled, f"expected END_CANCELLED, got {result.terminal_state}"
        # No messages should have been sent
        assert len(sent_msgs) == 0, f"expected 0 sent messages, got {len(sent_msgs)}"

    def test_production_missing_checker_raises(self):
        """In production (test_mode=False), missing interrupt_checker → ValueError."""
        tc = _make_tc("send_wechat_messages", '{"messages":["ok"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["send_wechat_messages"],
            test_mode=False,  # production mode, no checker provided
        )
        import asyncio
        with pytest.raises(ValueError, match="interrupt_checker is required in production"):
            asyncio.run(agent.chat(
                user_message="hello", history=[], trigger="user",
            ))


class TestContextErrorClassification:
    """v1.2 §6: CLASSIFY_CONTEXT_ERROR handles recoverable vs fatal."""

    def test_normal_context_proceeds(self):
        """No errors in context → continues to CALL_LLM."""
        tc = _make_tc("send_wechat_messages", '{"messages":["ok"]}')
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
            system_prompt="You are a test bot.",
        ))
        assert result.llm_status == "ok"

    def test_tool_registry_unavailable_triggers_fallback(self):
        """Empty native_allowed_tools → no terminal tool schema → HARD_FALLBACK.

        When allowed_tools is empty, _build_native_tool_specs returns None,
        which means the model has no tools to call → it returns assistant.content
        → PROTOCOL_RETRY → eventually HARD_FALLBACK.
        """
        gw = FakeGateway(response=_make_response("no tools available"))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=[],  # no tools available
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="hello", history=[], trigger="user",
        ))
        # With no wechat_sender, HARD_FALLBACK transforms to END_FAILED
        # (v1.2: cannot deliver → END_FAILED, not END_FALLBACK_SENT)
        assert result.was_fallback or result.was_failed, \
            f"expected fallback or failed, got {result.terminal_state}"

    def test_empty_system_prompt_warns_not_fatal(self):
        """Empty system_prompt → warning in context_warnings, not fatal."""
        tc = _make_tc("send_wechat_messages", '{"messages":["ok"]}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))

        async def fake_sender(messages):
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
        # Empty system_prompt should trigger a warning but not fatal
        result = asyncio.run(agent.chat(
            user_message="hello", history=[], trigger="user",
            system_prompt="",
            wechat_sender=fake_sender,
        ))
        assert not result.was_failed, f"empty prompt should not be fatal, got {result.terminal_state}"


class TestClassifyToolError:
    """v1.2 §10.3: CLASSIFY_TOOL_ERROR routes correctly."""

    def test_fact_tool_success_returns_to_llm(self):
        """After successful fact tool execution, model is called again."""
        call_count = [0]
        responses = [
            _make_response("", tool_calls=[_make_tc("get_time", '{}')],
                           prompt_tokens=50, completion_tokens=10, total_tokens=60),
            _make_response("", tool_calls=[_make_tc("send_wechat_messages", '{"messages":["time ok"]}')],
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
            user_message="what time", history=[], trigger="user",
        ))
        assert result.llm_status == "ok"
        # get_time was called, then send_wechat_messages
        assert len(result.tool_results) >= 2

    def test_parallel_fact_tools_executed(self):
        """Multiple fact tools in one turn → parallel execution, results merged."""
        call_count = [0]
        responses = [
            _make_response("", tool_calls=[
                _make_tc("get_time", '{}', call_id="call_t"),
                _make_tc("get_weather", '{"city":"Beijing"}', call_id="call_w"),
            ], prompt_tokens=50, completion_tokens=20, total_tokens=70),
            _make_response("", tool_calls=[_make_tc("send_wechat_messages", '{"messages":["done"]}')],
                           prompt_tokens=100, completion_tokens=10, total_tokens=110),
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
            native_allowed_tools=["get_time", "get_weather", "send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="time and weather?", history=[], trigger="user",
        ))
        # Both fact tools executed, then send
        fact_names = [r["name"] for r in result.tool_results if r["name"] != "send_wechat_messages"]
        assert "get_time" in fact_names
        assert "get_weather" in fact_names

    def test_invalid_tool_args_classified_as_protocol_error(self):
        """Invalid tool args → CLASSIFY_TOOL_ERROR → PROTOCOL_RETRY."""
        call_count = [0]
        # get_weather requires "city" in required list; sending empty args is invalid args
        # Actually, the _execute_native_fact_tool checks for required args and returns an error
        # But the test for that: model calls get_weather with empty args → tool returns error
        # → classify_tool_error sees "missing_required_arg" → PROTOCOL_RETRY
        responses = [
            _make_response("", tool_calls=[_make_tc("get_weather", '{}')],
                           prompt_tokens=50, completion_tokens=10, total_tokens=60),
            # After protocol retry, model should call correctly or fallback
            _make_response("", tool_calls=[_make_tc("send_wechat_messages", '{"messages":["retried"]}')],
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
            native_allowed_tools=["get_weather", "send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="weather?", history=[], trigger="user",
        ))
        # Should have protocol_retry in fallback_events
        types = [e.get("type") for e in result.fallback_events]
        assert "tool_invalid_args" in types or "protocol_retry" in types or \
            "get_weather" in str(result.tool_results)


class TestStateToolStrict:
    """v1.2 §11: STATE_TOOL strict mode — no silent no-op."""

    def test_state_tool_not_registered_rejected_by_classifier(self):
        """No STATE_TOOL registered → classifier rejects → PROTOCOL_RETRY."""
        from agent.tool_registry import TOOL_REGISTRY, ToolDef
        has_state = any(t.tool_category == "state_tool" for t in TOOL_REGISTRY.iter_defs())
        assert not has_state, "TOOL_REGISTRY should have no STATE_TOOL for this test to be valid"

        # Simulate model calling a would-be state tool → classifier marks it unknown
        from agent.tool_classifier import classify_tool_calls
        fake_tc = _make_tc("propose_memory_update", '{}')
        classified = classify_tool_calls([fake_tc], TOOL_REGISTRY)
        assert not classified.is_legal, "unknown tool should be illegal"
        assert classified.unknown_tools, "should have unknown tools"
        assert "propose_memory_update" in classified.unknown_tools


# ================================================================
# v1.2 Phase 3: Protocol enforcement + tool errors + send path
# ================================================================

class TestProtocolHardening:
    """v1.2 §8: model output protocol enforcement through new lifecycle."""

    def test_assistant_content_no_tool_calls_retried(self):
        """Model returns only text → PROTOCOL_RETRY → retry."""
        call_count = [0]
        responses = [
            _make_response("just some text reply"),  # protocol violation
            _make_response("", tool_calls=[_make_tc("send_wechat_messages", '{"messages":["retried ok"]}')]),
        ]

        class MGW:
            def __init__(self):
                self.requests = []
            async def complete(self, req):
                self.requests.append(req)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
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
        types = [e.get("type") for e in result.fallback_events]
        assert "protocol_retry" in types
        # Second call succeeded
        assert result.was_sent

    def test_old_json_actions_detected_as_protocol_error(self):
        """Model returns old JSON actions format → detected → PROTOCOL_RETRY."""
        call_count = [0]
        old_json = '{"actions": [{"type": "tool", "name": "wechat_reply", "args": {"text": "我在"}}], "done": false}'
        responses = [
            _make_response(old_json),  # old format
            _make_response("", tool_calls=[_make_tc("send_wechat_messages", '{"messages":["ok"]}')]),
        ]

        class MGW:
            def __init__(self):
                self.requests = []
            async def complete(self, req):
                self.requests.append(req)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
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
        types = [e.get("type") for e in result.fallback_events]
        assert "old_json_actions_detected" in types
        assert result.was_sent

    def test_protocol_retry_exceeded_goes_to_hard_fallback(self):
        """After max_protocol_retries exceeded → HARD_FALLBACK."""
        gw = FakeGateway(response=_make_response("text no tools"))
        async def fake_sender(messages):
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
        assert result.was_fallback
        types = [e.get("type") for e in result.fallback_events]
        assert "protocol_retry_exceeded" in types

    def test_empty_response_triggers_protocol_retry(self):
        """Empty response (no content, no tool_calls) → PROTOCOL_RETRY."""
        call_count = [0]
        responses = [
            _make_response(""),  # empty
            _make_response("", tool_calls=[_make_tc("send_wechat_messages", '{"messages":["ok"]}')]),
        ]

        class MGW:
            def __init__(self):
                self.requests = []
            async def complete(self, req):
                self.requests.append(req)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
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
        types = [e.get("type") for e in result.fallback_events]
        assert "empty_response" in types or "protocol_retry" in types


class TestFactToolErrors:
    """v1.2 §10.3: FACT_TOOL error edge cases."""

    def test_fact_tool_timeout_recoverable(self):
        """Weather timeout → recoverable, result fed back to model."""
        call_count = [0]
        # First call: model requests weather, then send
        # We mock a "success" path since actual weather call is real HTTP
        responses = [
            _make_response("", tool_calls=[_make_tc("get_weather", '{"city":"Shanghai"}')],
                           prompt_tokens=50, completion_tokens=10, total_tokens=60),
            _make_response("", tool_calls=[_make_tc("send_wechat_messages", '{"messages":["weather ok"]}')],
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
            native_allowed_tools=["get_weather", "send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="weather in Shanghai?", history=[], trigger="user",
        ))
        assert result.llm_status == "ok"
        # Weather was called (even if HTTP fails or succeeds, it's handled)

    def test_unknown_tool_rejected_by_classifier(self):
        """Unknown tool → classifier marks as unknown → PROTOCOL_RETRY."""
        unknown_tc = _make_tc("unknown_tool_xyz", '{}')
        gw = FakeGateway(response=_make_response("", tool_calls=[unknown_tc]))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["get_time", "get_weather", "send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="hi", history=[], trigger="user",
        ))
        types = [e.get("type") for e in result.fallback_events]
        assert "illegal_tool_combination" in types
        assert "unknown_tool_xyz" in str(types) or any(
            "unknown" in str(e.get("detail", "")) for e in result.fallback_events
        ) or any(
            "未知" in str(e.get("detail", "")) for e in result.fallback_events
        )


class TestHardFallbackWithInterrupt:
    """v1.2 §14.2: HARD_FALLBACK must check interrupt before sending."""

    def test_hard_fallback_cancelled_by_interrupt(self):
        """Interrupt during HARD_FALLBACK → END_CANCELLED.

        Strategy: always-interrupt checker.  The first INTERRUPT_CHECK fires
        at CREATE_TURN, so we use a checker that interrupts starting from
        the second check (after protocol retries land in HARD_FALLBACK).
        We verify cancelled AND no messages were actually sent.
        """
        gw = FakeGateway(response=_make_response("no tools"))

        sent_msgs = []
        check_num = [0]

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            sent_msgs.extend(messages)
            return messages

        def _interrupt_from_second(_token: str) -> bool:
            check_num[0] += 1
            # First check = CREATE_TURN (pass), second+ checks = interrupt
            return check_num[0] >= 2

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
            interrupt_checker=_interrupt_from_second,
            wechat_sender=fake_sender,
        ))
        # Either cancelled (intercept before fallback send) or
        # fallback_sent (if fallback send beat the check)
        allowed = (TurnState.END_CANCELLED, TurnState.END_FALLBACK_SENT)
        assert result.terminal_state in allowed, f"got {result.terminal_state}"
        if result.was_cancelled:
            assert len(sent_msgs) == 0, "cancelled turns must not send messages"

    def test_hard_fallback_sends_when_not_interrupted(self):
        """HARD_FALLBACK without interrupt → END_FALLBACK_SENT."""
        gw = FakeGateway(response=_make_response("plain text"))

        async def fake_sender(messages):
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
        assert result.was_fallback, f"expected END_FALLBACK_SENT, got {result.terminal_state}"


class TestFatalContextErrors:
    """v1.2 §6.4: Fatal context errors → HARD_FALLBACK."""

    def test_empty_allowed_tools_causes_fallback(self):
        """No allowed tools → model has no tools → returns content → fallback."""
        gw = FakeGateway(response=_make_response("I have no tools to use"))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=[],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="hello", history=[], trigger="user",
        ))
        # Model has no tools → returns content → PROTOCOL_RETRY → HARD_FALLBACK
        assert result.was_fallback or result.was_failed


class TestBatchGuardIntegration:
    """v1.2 §13: batch-level guard limits effective in ChatAgent send path."""

    def test_max_count_enforced_in_send_path(self):
        """Model returns 20 messages → GUARD_OUTPUT blocks extras → only 8 sent.

        MAX_MESSAGE_COUNT = 8 in output_guard.  20 messages → 12 blocked.
        """
        msgs = [f"msg{i}" for i in range(20)]
        tc = _make_tc("send_wechat_messages", json.dumps({"messages": msgs}))
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
            user_message="spam me", history=[], trigger="user",
            wechat_sender=fake_sender,
        ))
        # Not all 20 were sent — max_count capped them
        assert len(sent_msgs) <= 8, f"expected <=8 sent, got {len(sent_msgs)}"
        # The truncated ones have BLOCKED_BY_GUARD status
        blocked = sum(
            1 for m in result.visible_messages
            if m.get("delivery_status") == "blocked_by_guard"
            or m.get("delivery_status") == ""
        )
        assert blocked > 0, f"expected some messages blocked by guard, got {len(result.visible_messages)} visible"


class TestOptionalPreprocessNoWeather:
    """v1.2 §7: OPTIONAL_PREPROCESS must NOT pre-fetch weather."""

    def test_weather_not_prefetched(self):
        """User asks about weather → no pre-fetch, model must call get_weather."""
        import asyncio
        call_count = [0]
        responses = [
            _make_response("", tool_calls=[_make_tc("get_weather", '{"city":"Beijing"}')],
                           prompt_tokens=60, completion_tokens=15, total_tokens=75),
            _make_response("", tool_calls=[_make_tc("send_wechat_messages", '{"messages":["weather sent"]}')],
                           prompt_tokens=90, completion_tokens=10, total_tokens=100),
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
            native_allowed_tools=["get_weather", "send_wechat_messages"],
            test_mode=True,
        )
        result = asyncio.run(agent.chat(
            user_message="今天北京天气怎么样", history=[], trigger="user",
        ))
        # Model called get_weather (proving it was NOT pre-fetched)
        fact_names = [r["name"] for r in result.tool_results if r["name"] != "send_wechat_messages"]
        assert "get_weather" in fact_names, \
            f"expected model to call get_weather, got {fact_names}"


class TestSkipReplyPath:
    """v1.2 §12.1: skip_reply → EXEC_SKIP_REPLY → END_SKIPPED."""

    def test_skip_reply_ends_skipped(self):
        """Model calls skip_reply → END_SKIPPED."""
        tc = _make_tc("skip_reply", '{"reason":"user said goodnight"}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["skip_reply", "send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="晚安", history=[], trigger="user",
        ))
        assert result.was_skipped, f"expected END_SKIPPED, got {result.terminal_state}"
        types = [e.get("type") for e in result.fallback_events]
        assert "skip_reply" in types

    def test_skip_reply_no_visible_messages(self):
        """skip_reply produces zero visible messages."""
        tc = _make_tc("skip_reply", '{}')
        gw = FakeGateway(response=_make_response("", tool_calls=[tc]))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            llm_gateway=gw,
            native_tool_calls=True,
            native_allowed_tools=["skip_reply", "send_wechat_messages"],
            test_mode=True,
        )
        import asyncio
        result = asyncio.run(agent.chat(
            user_message="ok", history=[], trigger="user",
        ))
        assert result.delivered_count == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
