"""Tests for the V2 UnifiedOutputGuard module."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.output_guard import (
    guard_single_message,
    guard_messages,
    _looks_like_protocol_json,
    _unwrap_nested_protocol,
)


def test_looks_like_protocol_json_detects_leaked_json():
    leaked = '{"actions":[{"type":"tool","name":"wechat_reply","args":{"text":"hi"}}],"done":true}'
    assert _looks_like_protocol_json(leaked) is True


def test_looks_like_protocol_json_rejects_normal_text():
    assert _looks_like_protocol_json("你好，今天天气怎么样") is False
    assert _looks_like_protocol_json("") is False


def test_looks_like_protocol_json_rejects_text_with_only_actions():
    # Text contains "actions" but not protocol markers
    assert _looks_like_protocol_json("take actions to improve") is False


def test_unwrap_nested_protocol_extracts_text():
    nested = '{"actions":[{"type":"tool","name":"wechat_reply","args":{"text":"actual reply"}}],"done":true}'
    result = _unwrap_nested_protocol(nested)
    assert result == "actual reply"


def test_unwrap_nested_protocol_returns_empty_for_plain_text():
    assert _unwrap_nested_protocol("hello world") == ""


def test_unwrap_nested_protocol_skips_recursive_protocol():
    inner = '{"actions":[{"type":"message","text":"real text"}],"done":true}'
    outer = f'{{"actions":[{{"type":"tool","name":"wechat_reply","args":{{"text":"{inner}"}}}}],"done":true}}'
    result = _unwrap_nested_protocol(outer)
    # Inner text IS protocol JSON, so it gets rejected.
    # Unwrap extracts no natural-language content from recursive protocol.
    assert result == ""


def test_guard_single_message_hard_fallback_for_recursive_protocol():
    # When protocol JSON contains nested protocol JSON as text,
    # unwrap finds nothing and guard returns hard fallback.
    inner = '{"actions":[{"type":"message","text":"real text"}],"done":true}'
    outer = (
        '{"actions":[{"type":"tool","name":"wechat_reply","args":'
        f'{{"text":"{inner}"}}'
        '}],"done":true}'
    )
    result = guard_single_message(outer)
    assert result == "我刚刚卡了一下 重新说"


def test_guard_single_message_passes_normal_text():
    assert guard_single_message("今天天气不错") == "今天天气不错"


def test_guard_single_message_blocks_empty():
    assert guard_single_message("") == ""


def test_guard_single_message_unwraps_protocol():
    leaked = '{"actions":[{"type":"tool","name":"wechat_reply","args":{"text":"nested reply"}}],"done":true}'
    issues = []
    result = guard_single_message(leaked, issues=issues)
    assert result == "nested reply"
    assert "protocol_text_unwrapped" in issues


def test_guard_single_message_hard_fallback_for_bare_protocol():
    # Protocol JSON with no extractable natural text
    bare = '{"actions":[{"type":"tool","name":"get_weather","args":{"city":"beijing"}}],"done":true}'
    result = guard_single_message(bare)
    assert result == "我刚刚卡了一下 重新说"


def test_guard_single_message_truncates_long_text():
    long_text = "a" * 600
    result = guard_single_message(long_text)
    assert len(result) <= 500


def test_guard_messages_returns_empty_for_empty_input():
    assert guard_messages([]) == []


def test_guard_messages_filters_empty_after_guard():
    # Non-empty input where all messages have empty text → hard fallback
    result = guard_messages([{"text": "", "delay": 0}])
    assert result == [{"text": "我刚刚卡了一下 重新说", "delay": 0}]


def test_guard_messages_empty_input_stays_empty():
    # Truly empty input (schedule-only turn) stays empty
    assert guard_messages([]) == []


def test_guard_messages_limits_count():
    msgs = [{"text": f"msg {i}", "delay": 0} for i in range(10)]
    result = guard_messages(msgs, max_messages=3)
    assert len(result) == 3


def test_guard_messages_collects_issues():
    leaked = '{"actions":[{"type":"tool","name":"wechat_reply","args":{"text":"ok"}}],"done":true}'
    issues = []
    result = guard_messages([{"text": leaked, "delay": 0}], issues=issues)
    assert len(result) == 1
    assert result[0]["text"] == "ok"
    assert len(issues) > 0
