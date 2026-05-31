"""Tests for TemporalContextBuilder — 时间感知上下文层。

覆盖：
A. 5 分钟内连续聊天 → continuous_chat
B. 30 分钟后回来 → short_resume
C. 上午到下午 → same_day_resume
D. 跨天回来 → next_day_resume
E. 跨天但不足 24 小时 (22:00→09:00) → next_day_resume
F. 空历史 → has_previous_interaction=false
G. 主 system prompt 稳定性
H. messages 顺序
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.temporal_context import (
    CONTINUOUS_CHAT,
    LONG_GAP_RESUME,
    NEXT_DAY_RESUME,
    SAME_DAY_RESUME,
    SHORT_RESUME,
    TemporalContextBuilder,
    _format_time_gap,
    _time_period,
)

TZ = timezone(timedelta(hours=8))


# ── helpers ──────────────────────────────────────────────────────

def _ts(year=2026, month=5, day=31, hour=14, minute=0) -> float:
    """Create a Unix timestamp for testing."""
    return datetime(year, month, day, hour, minute, 0, tzinfo=TZ).timestamp()


def _history_item(role: str, content: str, ts: float, source: str = "user") -> dict:
    return {"role": role, "content": content, "ts": ts, "source": source}


def _builder() -> TemporalContextBuilder:
    return TemporalContextBuilder(timezone_offset_hours=8)


# ================================================================
# A. 5 分钟内连续聊天
# ================================================================

class TestContinuousChat:
    def test_no_divider_inserted(self):
        """Gap < 10 min: no time dividers."""
        now = datetime(2026, 5, 31, 14, 10, tzinfo=TZ)
        history = [
            _history_item("user", "你好", _ts(hour=14, minute=5)),
            _history_item("assistant", "你好呀", _ts(hour=14, minute=5) + 3),
        ]
        current_user_ts = _ts(hour=14, minute=8)

        result = _builder().build(now=now, history=history, current_user_ts=current_user_ts)

        assert result.diagnostics["resume_type"] == CONTINUOUS_CHAT
        assert result.diagnostics["has_previous_interaction"] is True
        assert result.diagnostics["divider_count"] == 0
        # No system messages with _temporal_divider flag
        dividers = [m for m in result.augmented_history if m.get("_temporal_divider")]
        assert len(dividers) == 0

    def test_resume_type_in_message(self):
        """Temporal message contains resume_type = continuous_chat."""
        now = datetime(2026, 5, 31, 14, 10, tzinfo=TZ)
        history = [
            _history_item("user", "早上好", _ts(hour=14, minute=5)),
            _history_item("assistant", "早上好！", _ts(hour=14, minute=5) + 5),
        ]
        result = _builder().build(now=now, history=history, current_user_ts=_ts(hour=14, minute=8))

        assert "resume_type: continuous_chat" in result.temporal_message


# ================================================================
# B. 30 分钟后回来
# ================================================================

class TestShortResume:
    def test_divider_inserted(self):
        """Gap 10-60 min: divider inserted between history items."""
        now = datetime(2026, 5, 31, 14, 30, tzinfo=TZ)
        history = [
            _history_item("user", "我去吃饭了", _ts(hour=14, minute=0)),
            _history_item("assistant", "好的，去吧", _ts(hour=14, minute=0) + 5),
        ]
        current_user_ts = _ts(hour=14, minute=30)

        result = _builder().build(now=now, history=history, current_user_ts=current_user_ts)

        assert result.diagnostics["resume_type"] == SHORT_RESUME
        assert result.diagnostics["divider_count"] == 1

        # The divider should be between assistant reply and nothing (end of history)
        # Actually: between last visible (assistant at 14:00:05) and nothing more,
        # so it appears after the assistant message
        dividers = [m for m in result.augmented_history if m.get("_temporal_divider")]
        assert len(dividers) == 1
        # Gap is 1795s (assistant at 14:00:05 → current at 14:30:00) = ~29 min
        assert "分钟" in dividers[0]["content"]
        assert dividers[0]["role"] == "system"

    def test_gap_in_temporal_message(self):
        """Temporal message mentions the gap."""
        now = datetime(2026, 5, 31, 14, 30, tzinfo=TZ)
        history = [
            _history_item("user", "我去吃饭了", _ts(hour=14, minute=0)),
            _history_item("assistant", "好的", _ts(hour=14, minute=0) + 5),
        ]
        result = _builder().build(now=now, history=history, current_user_ts=_ts(hour=14, minute=30))

        # Gap is 1795s (assistant at 14:00:05 → current at 14:30:00) = ~29 min
        assert "约 29 分钟" in result.temporal_message


# ================================================================
# C. 上午到下午
# ================================================================

class TestSameDayResume:
    def test_same_day_resume(self):
        """Gap > 1h, same calendar day: same_day_resume."""
        now = datetime(2026, 5, 31, 15, 0, tzinfo=TZ)
        history = [
            _history_item("user", "我去干活了", _ts(hour=9, minute=0)),
            _history_item("assistant", "加油！", _ts(hour=9, minute=0) + 10),
        ]
        current_user_ts = _ts(hour=15, minute=0)

        result = _builder().build(now=now, history=history, current_user_ts=current_user_ts)

        assert result.diagnostics["resume_type"] == SAME_DAY_RESUME

        dividers = [m for m in result.augmented_history if m.get("_temporal_divider")]
        assert len(dividers) == 1
        assert "下午" in dividers[0]["content"]
        assert "6 小时" in dividers[0]["content"] or "5 小时" in dividers[0]["content"]

    def test_divider_uses_time_period(self):
        """Divider includes time period (上午/下午/晚上)."""
        now = datetime(2026, 5, 31, 20, 0, tzinfo=TZ)
        history = [
            _history_item("user", "中午好", _ts(hour=12, minute=0)),
            _history_item("assistant", "中午好！", _ts(hour=12, minute=1)),
        ]
        result = _builder().build(now=now, history=history, current_user_ts=_ts(hour=20, minute=0))

        dividers = [m for m in result.augmented_history if m.get("_temporal_divider")]
        assert len(dividers) == 1
        assert "晚上" in dividers[0]["content"]


# ================================================================
# D. 跨天回来
# ================================================================

class TestNextDayResume:
    def test_next_day_resume(self):
        """Different calendar day: next_day_resume."""
        now = datetime(2026, 6, 1, 10, 0, tzinfo=TZ)
        history = [
            _history_item("user", "晚安", _ts(hour=23, minute=0)),
            _history_item("assistant", "晚安，好梦", _ts(hour=23, minute=0) + 5),
        ]
        current_user_ts = _ts(year=2026, month=6, day=1, hour=10, minute=0)

        result = _builder().build(now=now, history=history, current_user_ts=current_user_ts)

        assert result.diagnostics["resume_type"] == NEXT_DAY_RESUME

        dividers = [m for m in result.augmented_history if m.get("_temporal_divider")]
        assert len(dividers) == 1
        assert "1 天" in dividers[0]["content"]

    def test_multi_day_gap(self):
        """Multiple days: divider shows day count."""
        now = datetime(2026, 6, 3, 10, 0, tzinfo=TZ)
        history = [
            _history_item("user", "周末快乐", _ts(hour=18, minute=0)),
            _history_item("assistant", "你也是！", _ts(hour=18, minute=0) + 3),
        ]
        current_user_ts = _ts(year=2026, month=6, day=3, hour=10, minute=0)

        result = _builder().build(now=now, history=history, current_user_ts=current_user_ts)

        dividers = [m for m in result.augmented_history if m.get("_temporal_divider")]
        assert len(dividers) == 1
        assert "3 天" in dividers[0]["content"]


# ================================================================
# E. 跨天但不足 24 小时 (22:00 → 09:00)
# ================================================================

class TestCrossDayUnder24h:
    def test_22_to_09_is_next_day_resume(self):
        """22:00 → 09:00 next day: must be next_day_resume even though < 24h."""
        now = datetime(2026, 6, 1, 9, 0, tzinfo=TZ)
        history = [
            _history_item("user", "我去睡了", _ts(hour=22, minute=0)),
            _history_item("assistant", "晚安！", _ts(hour=22, minute=0) + 3),
        ]
        current_user_ts = _ts(year=2026, month=6, day=1, hour=9, minute=0)

        result = _builder().build(now=now, history=history, current_user_ts=current_user_ts)

        assert result.diagnostics["resume_type"] == NEXT_DAY_RESUME
        assert result.diagnostics["gap_seconds"] < 24 * 3600  # < 24h

        dividers = [m for m in result.augmented_history if m.get("_temporal_divider")]
        assert len(dividers) == 1
        assert "1 天" in dividers[0]["content"]

    def test_temporal_message_shows_cross_day(self):
        """Temporal message reflects cross-day interaction."""
        now = datetime(2026, 6, 1, 9, 0, tzinfo=TZ)
        history = [
            _history_item("user", "晚安", _ts(hour=22, minute=0)),
            _history_item("assistant", "晚安", _ts(hour=22, minute=0) + 2),
        ]
        result = _builder().build(now=now, history=history, current_user_ts=_ts(year=2026, month=6, day=1, hour=9, minute=0))

        assert "resume_type: next_day_resume" in result.temporal_message
        assert "05月31日" in result.temporal_message  # last user message date


# ================================================================
# F. 空历史
# ================================================================

class TestEmptyHistory:
    def test_empty_history_no_error(self):
        """Empty history: no crash, no dividers."""
        now = datetime(2026, 5, 31, 14, 0, tzinfo=TZ)
        result = _builder().build(now=now, history=[], current_user_ts=_ts(hour=14, minute=0))

        assert result.diagnostics["has_previous_interaction"] is False
        assert result.diagnostics["divider_count"] == 0
        assert result.diagnostics["resume_type"] == CONTINUOUS_CHAT
        assert result.augmented_history == []

    def test_temporal_message_for_first_interaction(self):
        """Temporal message marks first interaction."""
        now = datetime(2026, 5, 31, 14, 0, tzinfo=TZ)
        result = _builder().build(now=now, history=[], current_user_ts=_ts(hour=14, minute=0))

        assert "首次对话" in result.temporal_message
        assert "当前时间" in result.temporal_message

    def test_single_message_history(self):
        """History with only one message: no dividers, has_previous=True."""
        now = datetime(2026, 5, 31, 14, 5, tzinfo=TZ)
        history = [
            _history_item("user", "你好", _ts(hour=14, minute=0)),
        ]
        result = _builder().build(now=now, history=history, current_user_ts=_ts(hour=14, minute=5))

        assert result.diagnostics["has_previous_interaction"] is True
        assert result.diagnostics["divider_count"] == 0


# ================================================================
# G. 主 system prompt 稳定性
# ================================================================

class TestSystemPromptStability:
    def test_temporal_message_varies_with_now(self):
        """Temporal message changes with different 'now' values."""
        history = [
            _history_item("user", "你好", _ts(hour=10, minute=0)),
            _history_item("assistant", "你好呀", _ts(hour=10, minute=0) + 5),
        ]

        now1 = datetime(2026, 5, 31, 10, 5, tzinfo=TZ)
        now2 = datetime(2026, 5, 31, 15, 0, tzinfo=TZ)

        r1 = _builder().build(now=now1, history=history, current_user_ts=_ts(hour=10, minute=5))
        r2 = _builder().build(now=now2, history=history, current_user_ts=_ts(hour=15, minute=0))

        # Different now → different temporal messages
        assert r1.temporal_message != r2.temporal_message

    def test_temporal_message_is_independent_of_build_system_prompt(self):
        """Temporal message is produced by TemporalContextBuilder, not build_chat_system_prompt."""
        from agent.context_builder import build_chat_system_prompt

        prompt1 = build_chat_system_prompt("test persona", "test personality")
        prompt2 = build_chat_system_prompt("test persona", "test personality")

        # build_chat_system_prompt is deterministic (no time dependency)
        assert prompt1 == prompt2

        # And does NOT contain current time
        assert "当前时间" not in prompt1


# ================================================================
# H. messages 顺序（集成验证）
# ================================================================

class TestMessageOrder:
    def test_temporal_message_is_system_role(self):
        """Temporal message is system role."""
        now = datetime(2026, 5, 31, 14, 30, tzinfo=TZ)
        history = [
            _history_item("user", "你好", _ts(hour=14, minute=0)),
            _history_item("assistant", "你好呀", _ts(hour=14, minute=0) + 5),
        ]
        result = _builder().build(now=now, history=history, current_user_ts=_ts(hour=14, minute=30))

        # temporal_message is a string, intended for system role
        assert isinstance(result.temporal_message, str)
        assert "当前时间" in result.temporal_message

    def test_dividers_are_system_role_only(self):
        """All dividers are system role, never user or assistant."""
        now = datetime(2026, 5, 31, 15, 0, tzinfo=TZ)
        history = [
            _history_item("user", "早上好", _ts(hour=9, minute=0)),
            _history_item("assistant", "早上好！", _ts(hour=9, minute=0) + 5),
        ]
        result = _builder().build(now=now, history=history, current_user_ts=_ts(hour=15, minute=0))

        dividers = [m for m in result.augmented_history if m.get("_temporal_divider")]
        for d in dividers:
            assert d["role"] == "system"
            assert d["role"] != "user"
            assert d["role"] != "assistant"

    def test_augmented_preserves_original_messages(self):
        """Augmented history preserves all original messages in order."""
        now = datetime(2026, 5, 31, 15, 0, tzinfo=TZ)
        history = [
            _history_item("user", "msg1", _ts(hour=9, minute=0)),
            _history_item("assistant", "reply1", _ts(hour=9, minute=0) + 5),
            _history_item("user", "msg2", _ts(hour=14, minute=0)),
            _history_item("assistant", "reply2", _ts(hour=14, minute=0) + 3),
        ]
        result = _builder().build(now=now, history=history, current_user_ts=_ts(hour=15, minute=0))

        # Original messages are all present
        non_dividers = [m for m in result.augmented_history if not m.get("_temporal_divider")]
        assert len(non_dividers) == 4
        assert non_dividers[0]["content"] == "msg1"
        assert non_dividers[1]["content"] == "reply1"
        assert non_dividers[2]["content"] == "msg2"
        assert non_dividers[3]["content"] == "reply2"

    def test_multiple_gaps_multiple_dividers(self):
        """Multiple gaps → multiple dividers inserted at correct positions."""
        now = datetime(2026, 5, 31, 20, 0, tzinfo=TZ)
        history = [
            _history_item("user", "早上好", _ts(hour=9, minute=0)),
            _history_item("assistant", "早上好！", _ts(hour=9, minute=1)),
            _history_item("user", "下午好", _ts(hour=14, minute=0)),
            _history_item("assistant", "下午好！", _ts(hour=14, minute=1)),
        ]
        result = _builder().build(now=now, history=history, current_user_ts=_ts(hour=20, minute=0))

        dividers = [m for m in result.augmented_history if m.get("_temporal_divider")]
        # Gap 1: 9:01 → 14:00 = ~5h gap → divider within history
        # Gap 2: 14:01 → 20:00 (current_user_ts) = ~6h gap → divider at end
        assert len(dividers) == 2
        assert "5 小时" in dividers[0]["content"] or "4 小时" in dividers[0]["content"]
        assert "6 小时" in dividers[1]["content"] or "5 小时" in dividers[1]["content"]


# ================================================================
# I. last_visible_interaction_at 取 max
# ================================================================

class TestLastVisibleInteraction:
    def test_uses_max_of_user_and_assistant(self):
        """last_visible_interaction_at = max(user_ts, assistant_ts)."""
        now = datetime(2026, 5, 31, 14, 30, tzinfo=TZ)
        # User sent at 14:00, assistant replied at 14:05
        history = [
            _history_item("user", "你好", _ts(hour=14, minute=0)),
            _history_item("assistant", "你好呀", _ts(hour=14, minute=5)),
        ]
        result = _builder().build(now=now, history=history, current_user_ts=_ts(hour=14, minute=30))

        # last_visible should be assistant's timestamp (14:05), not user's (14:00)
        assert result.diagnostics["last_assistant_visible_message_at"] > result.diagnostics["last_user_message_at"]
        assert result.diagnostics["last_visible_interaction_at"] == result.diagnostics["last_assistant_visible_message_at"]

    def test_gap_from_last_visible_not_last_user(self):
        """Gap is calculated from last_visible, not last_user."""
        now = datetime(2026, 5, 31, 15, 0, tzinfo=TZ)
        # User sent at 13:00, assistant replied at 13:30
        history = [
            _history_item("user", "你好", _ts(hour=13, minute=0)),
            _history_item("assistant", "你好呀", _ts(hour=13, minute=30)),
        ]
        result = _builder().build(now=now, history=history, current_user_ts=_ts(hour=15, minute=0))

        # Gap should be from 13:30 (assistant) to 15:00 = 90 min
        # Not from 13:00 (user) to 15:00 = 120 min
        assert "1 小时 30 分钟" in result.temporal_message or "约 1 小时" in result.temporal_message


# ================================================================
# J. _format_time_gap unit tests
# ================================================================

class TestFormatTimeGap:
    def test_under_one_minute(self):
        assert _format_time_gap(30) == "不到 1 分钟"

    def test_minutes(self):
        assert _format_time_gap(300) == "约 5 分钟"

    def test_one_hour(self):
        assert _format_time_gap(3600) == "约 1 小时"

    def test_hours_and_minutes(self):
        assert _format_time_gap(5400) == "约 1 小时 30 分钟"

    def test_days(self):
        assert _format_time_gap(172800) == "2 天"


# ================================================================
# K. _time_period unit tests
# ================================================================

class TestTimePeriod:
    def test_morning(self):
        assert _time_period(datetime(2026, 5, 31, 8, 0)) == "上午"

    def test_afternoon(self):
        assert _time_period(datetime(2026, 5, 31, 14, 0)) == "下午"

    def test_evening(self):
        assert _time_period(datetime(2026, 5, 31, 20, 0)) == "晚上"

    def test_late_night(self):
        assert _time_period(datetime(2026, 5, 31, 2, 0)) == "凌晨"


# ================================================================
# L. Diagnostics completeness
# ================================================================

class TestDiagnostics:
    def test_diagnostics_has_all_fields(self):
        now = datetime(2026, 5, 31, 14, 30, tzinfo=TZ)
        history = [
            _history_item("user", "你好", _ts(hour=14, minute=0)),
            _history_item("assistant", "你好呀", _ts(hour=14, minute=0) + 5),
        ]
        result = _builder().build(now=now, history=history, current_user_ts=_ts(hour=14, minute=30))

        diag = result.diagnostics
        assert "has_previous_interaction" in diag
        assert "resume_type" in diag
        assert "last_user_message_at" in diag
        assert "last_assistant_visible_message_at" in diag
        assert "last_visible_interaction_at" in diag
        assert "gap_seconds" in diag
        assert "divider_count" in diag

    def test_gap_seconds_accuracy(self):
        now = datetime(2026, 5, 31, 14, 30, tzinfo=TZ)
        history = [
            _history_item("user", "你好", _ts(hour=14, minute=0)),
            _history_item("assistant", "你好呀", _ts(hour=14, minute=0) + 5),
        ]
        result = _builder().build(now=now, history=history, current_user_ts=_ts(hour=14, minute=30))

        # Gap from 14:00:05 (assistant) to 14:30:00 = 1795 seconds
        assert abs(result.diagnostics["gap_seconds"] - 1795) < 1


# ================================================================
# M. Long gap resume (> 7 days)
# ================================================================

class TestLongGapResume:
    def test_over_seven_days(self):
        now = datetime(2026, 6, 10, 10, 0, tzinfo=TZ)
        history = [
            _history_item("user", "好久不见", _ts(hour=10, minute=0)),
            _history_item("assistant", "是啊好久不见", _ts(hour=10, minute=0) + 5),
        ]
        current_user_ts = _ts(year=2026, month=6, day=10, hour=10, minute=0)

        result = _builder().build(now=now, history=history, current_user_ts=current_user_ts)

        assert result.diagnostics["resume_type"] == LONG_GAP_RESUME

        dividers = [m for m in result.augmented_history if m.get("_temporal_divider")]
        assert len(dividers) == 1
        assert "较久" in dividers[0]["content"]
