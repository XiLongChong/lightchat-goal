"""Tests for output guard — v0.2.

Old JSON actions unwrap logic (_unwrap_nested_protocol) deleted.
_looks_like_protocol_json is kept for protocol leak DETECTION only
(used by _has_protocol_leak in guard_send_message_batch) — it triggers
hard reject, never unwrap into sendable text.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.output_guard import (
    guard_single_message,
    guard_messages,
    _looks_like_protocol_json,
)


# ── _looks_like_protocol_json: detection only, never unwrap ──────

def test_looks_like_protocol_json_detects_leaked_json():
    """Used by _has_protocol_leak for detection → hard reject."""
    leaked = '{"actions":[{"type":"tool","name":"wechat_reply","args":{"text":"hi"}}],"done":true}'
    assert _looks_like_protocol_json(leaked) is True


def test_looks_like_protocol_json_rejects_normal_text():
    assert _looks_like_protocol_json("你好，今天天气怎么样") is False
    assert _looks_like_protocol_json("") is False


def test_looks_like_protocol_json_rejects_text_with_only_actions():
    assert _looks_like_protocol_json("take actions to improve") is False


# ── guard_single_message ─────────────────────────────────────────

def test_guard_single_message_passes_normal_text():
    assert guard_single_message("今天天气不错") == "今天天气不错"


def test_guard_single_message_blocks_empty():
    assert guard_single_message("") == ""


def test_guard_single_message_blocks_protocol_json():
    """v0.2: protocol JSON is hard-blocked (never unwrapped into sendable text)."""
    leaked = '{"actions":[{"type":"tool","name":"wechat_reply","args":{"text":"nested reply"}}],"done":true}'
    issues = []
    result = guard_single_message(leaked, issues=issues)
    assert result == "我刚刚卡了一下 重新说"
    assert "protocol_text_blocked" in issues


def test_guard_single_message_hard_fallback_for_bare_protocol():
    # Protocol JSON with no extractable natural text — hard-blocked
    bare = '{"actions":[{"type":"tool","name":"get_weather","args":{"city":"beijing"}}],"done":true}'
    result = guard_single_message(bare)
    assert result == "我刚刚卡了一下 重新说"


def test_guard_single_message_hard_fallback_for_nested_protocol():
    """v0.2: nested protocol JSON is hard-blocked, not recursively unwrapped."""
    inner = '{"actions":[{"type":"message","text":"real text"}],"done":true}'
    outer = (
        '{"actions":[{"type":"tool","name":"wechat_reply","args":'
        f'{{"text":"{inner}"}}'
        '}],"done":true}'
    )
    result = guard_single_message(outer)
    assert result == "我刚刚卡了一下 重新说"


def test_guard_single_message_truncates_long_text():
    long_text = "a" * 600
    result = guard_single_message(long_text)
    assert len(result) <= 500


# ── guard_messages ───────────────────────────────────────────────

def test_guard_messages_returns_empty_for_empty_input():
    assert guard_messages([]) == []


def test_guard_messages_filters_empty_after_guard():
    result = guard_messages([{"text": "", "delay": 0}])
    assert result == [{"text": "我刚刚卡了一下 重新说", "delay": 0}]


def test_guard_messages_empty_input_stays_empty():
    assert guard_messages([]) == []


def test_guard_messages_limits_count():
    msgs = [{"text": f"msg {i}", "delay": 0} for i in range(10)]
    result = guard_messages(msgs, max_messages=3)
    assert len(result) == 3


def test_guard_messages_collects_issues():
    """v0.2: protocol leaks are hard-blocked and collected in issues."""
    leaked = '{"actions":[{"type":"tool","name":"wechat_reply","args":{"text":"ok"}}],"done":true}'
    issues = []
    result = guard_messages([{"text": leaked, "delay": 0}], issues=issues)
    assert len(result) == 1
    assert result[0]["text"] == "我刚刚卡了一下 重新说"
    assert len(issues) > 0
    assert "protocol_text_blocked" in issues
