"""Tests for v3.1 proactive_debug module — report generation and readability."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone, timedelta

import pytest

from agent.proactive_debug import (
    ProactiveDebugContext,
    ProactiveDebugReport,
    DecisionChainStage,
    _chain_entry,
    _ts_label,
    _build_suggestions,
    generate_report,
    collect_decision_chain_from_turn_result,
    create_simulated_sender,
    create_dry_run_write_history,
    create_dry_run_post_turn_hooks,
)

_TZ = timezone(timedelta(hours=8))


def _fake_turn_result(**overrides):
    """Build a mock TurnResult-like object."""
    class TR:
        terminal_state = "end_replied"
        turn_id = "turn_test123"
        proactive_delivered_count = 2
        proactive_delivered_texts = ["你好呀", "今天天气不错"]
        proactive_attempt_count = 2
        proactive_send_attempt_count = 2
        sent_message_count = 2
        visible_messages = [
            {"guarded_text": "你好呀", "original_text": "你好呀"},
            {"guarded_text": "今天天气不错", "original_text": "今天天气不错"},
        ]
        tool_results = [
            {"name": "wechat_reply", "result": "delivered"},
            {"name": "wechat_reply", "result": "delivered"},
            {"name": "done", "result": '{"status":"replied"}'},
        ]
        fallback_events = []
    tr = TR()
    for k, v in overrides.items():
        setattr(tr, k, v)
    return tr


def _fake_eligibility(eligible=True, checks=None):
    return {
        "eligible": eligible,
        "checks": checks or [
            {"name": "features.proactive", "passed": True, "actual": True, "required": True, "reason": None},
            {"name": "user_silent_window", "passed": True, "actual": "5000 seconds", "required": "60 seconds", "reason": None},
        ],
        "final_reason": "" if eligible else "features.proactive check failed",
    }


def _fake_impulse(selected=True, candidates=None):
    return {
        "eligible": True,
        "candidates": candidates or [
            {"type": "ECHO", "score": 82, "reason": "用户情绪关键词"},
            {"type": "PRESENCE", "score": 55, "reason": "长时间未互动"},
        ],
        "selected": {"impulse_type": "ECHO", "score": 82} if selected else None,
        "filtering_dedup": {"input": 2, "output": 2, "removed": 0},
        "filtering_expire": {"input": 2, "output": 2, "removed": 0},
        "filtering_score": {"input": 2, "scored": [{"type": "ECHO", "score": 82}, {"type": "PRESENCE", "score": 55}]},
        "filtering_threshold": {"min_score": 0, "passed": True},
        "selection_reason": "" if selected else "no candidate passed score threshold",
    }


# ══════════════════════════════════════════════════════════════════
# Test: generated_messages from TurnResult
# ══════════════════════════════════════════════════════════════════

class TestGeneratedMessages:
    def test_report_generated_messages_from_proactive_delivered_texts(self):
        """Primary source: proactive_delivered_texts."""
        ctx = ProactiveDebugContext(mode="dry_run", user_id="u1", bot_id="b1")
        tr = _fake_turn_result(proactive_delivered_texts=["msg1", "msg2"])
        chain = []
        report = generate_report(ctx, run_id="r1", decision_chain=chain,
                                 turn_result=tr,
                                 eligibility_result=_fake_eligibility(),
                                 impulse_result=_fake_impulse())
        assert report.generated_messages == ["msg1", "msg2"]

    def test_report_generated_messages_fallback_from_visible_messages(self):
        """Fallback: visible_messages when delivered_texts is empty."""
        ctx = ProactiveDebugContext(mode="dry_run", user_id="u1", bot_id="b1")
        tr = _fake_turn_result(
            proactive_delivered_texts=[],
            visible_messages=[
                {"guarded_text": "hello", "original_text": "hello"},
            ],
        )
        chain = []
        report = generate_report(ctx, run_id="r1", decision_chain=chain,
                                 turn_result=tr,
                                 eligibility_result=_fake_eligibility(),
                                 impulse_result=_fake_impulse())
        assert "hello" in report.generated_messages

    def test_report_generated_messages_empty_when_no_data(self):
        """No delivery + no tool_results → empty list."""
        ctx = ProactiveDebugContext(mode="dry_run", user_id="u1", bot_id="b1")
        tr = _fake_turn_result(
            proactive_delivered_texts=[],
            visible_messages=[],
            tool_results=[],  # no wechat_reply tool results to trigger fallback
            proactive_delivered_count=0,
        )
        chain = []
        report = generate_report(ctx, run_id="r1", decision_chain=chain,
                                 turn_result=tr,
                                 eligibility_result=_fake_eligibility(),
                                 impulse_result=_fake_impulse())
        assert report.generated_messages == []


# ══════════════════════════════════════════════════════════════════
# Test: guard_summary NOT send_json (bug fix)
# ══════════════════════════════════════════════════════════════════

class TestGuardSummary:
    def test_report_guard_summary_not_send_json(self):
        """guard field is guard info, NOT a copy of send data."""
        ctx = ProactiveDebugContext(mode="dry_run", user_id="u1", bot_id="b1")
        tr = _fake_turn_result()
        chain = collect_decision_chain_from_turn_result(
            tr, mode="dry_run",
            eligibility_result=_fake_eligibility(),
            impulse_result=_fake_impulse(),
        )
        report = generate_report(ctx, run_id="r1", decision_chain=chain,
                                 turn_result=tr,
                                 eligibility_result=_fake_eligibility(),
                                 impulse_result=_fake_impulse())

        # guard must have guard-specific keys, not send data
        assert "executed" in report.guard or "passed" in report.guard or "blocked" in report.guard
        # must NOT contain send-specific keys that the old bug had
        assert "real_send_attempted" not in report.guard
        assert "send_step_executed" not in report.guard
        # send field still has its own data
        assert "real_send_attempted" in report.send

    def test_guard_summary_passed_when_no_block(self):
        ctx = ProactiveDebugContext(mode="real_send", user_id="u1", bot_id="b1")
        tr = _fake_turn_result(fallback_events=[])
        chain = collect_decision_chain_from_turn_result(
            tr, mode="real_send",
            eligibility_result=_fake_eligibility(),
            impulse_result=_fake_impulse(),
        )
        report = generate_report(ctx, run_id="r1", decision_chain=chain,
                                 turn_result=tr,
                                 eligibility_result=_fake_eligibility(),
                                 impulse_result=_fake_impulse())
        assert report.guard.get("passed") is True
        assert report.guard.get("blocked") is False


# ══════════════════════════════════════════════════════════════════
# Test: summary reflects guard blocked
# ══════════════════════════════════════════════════════════════════

class TestSummaryGuardBlocked:
    def test_summary_guard_blocked_says_blocked(self):
        """When guard is blocked, summary says 'guard 拦截' not 'guard 通过'."""
        ctx = ProactiveDebugContext(mode="dry_run", user_id="u1", bot_id="b1")
        tr = _fake_turn_result(
            proactive_delivered_texts=["test_msg"],
            fallback_events=[{"type": "guard_block", "detail": "fabricated_observation"}],
        )
        # Need a guard=block in chain
        chain = [
            _chain_entry(DecisionChainStage.ELIGIBILITY, "pass", "", {}),
            _chain_entry(DecisionChainStage.IMPULSE_SELECTION, "ok", "", {}),
            _chain_entry(DecisionChainStage.GUARD, "block", "guard blocked", {}),
            _chain_entry(DecisionChainStage.TERMINAL, "end", "", {}),
        ]
        report = generate_report(ctx, run_id="r1", decision_chain=chain,
                                 turn_result=tr,
                                 eligibility_result=_fake_eligibility(),
                                 impulse_result=_fake_impulse())
        assert "guard 拦截" in report.summary

    def test_summary_guard_passed_says_passed(self):
        ctx = ProactiveDebugContext(mode="dry_run", user_id="u1", bot_id="b1")
        tr = _fake_turn_result(proactive_delivered_texts=["test_msg"])
        chain = [
            _chain_entry(DecisionChainStage.GUARD, "pass", "all guards passed", {}),
        ]
        report = generate_report(ctx, run_id="r1", decision_chain=chain,
                                 turn_result=tr,
                                 eligibility_result=_fake_eligibility(),
                                 impulse_result=_fake_impulse())
        assert "guard 通过" in report.summary


# ══════════════════════════════════════════════════════════════════
# Test: next_step_suggestions for no_impulse_selected
# ══════════════════════════════════════════════════════════════════

class TestSuggestions:
    def test_next_step_suggestions_for_no_impulse_selected(self):
        """When no impulse selected, suggestion should mention PRESENCE or ECHO."""
        ctx = ProactiveDebugContext(mode="dry_run", user_id="u1", bot_id="b1")
        chain = [
            _chain_entry(DecisionChainStage.ELIGIBILITY, "pass", "", {}),
            _chain_entry(DecisionChainStage.IMPULSE_SELECTION, "empty", "no_impulse_selected", {}),
        ]
        report = generate_report(
            ctx, run_id="r1", decision_chain=chain,
            eligibility_result=_fake_eligibility(),
            impulse_result=_fake_impulse(selected=False),
            execution_skip_reason="no_impulse_selected",
        )
        assert len(report.next_step_suggestions) >= 1
        combined = " ".join(report.next_step_suggestions)
        assert "PRESENCE" in combined or "ECHO" in combined or "候选" in combined

    def test_suggestions_for_proactive_disabled(self):
        ctx = ProactiveDebugContext(mode="dry_run", user_id="u1", bot_id="b1")
        chain = []
        elig = _fake_eligibility(eligible=False, checks=[
            {"name": "features.proactive", "passed": False, "actual": False, "required": True, "reason": "proactive feature is disabled"},
        ])
        report = generate_report(
            ctx, run_id="r1", decision_chain=chain,
            eligibility_result=elig,
            execution_skip_reason="proactive feature is disabled",
        )
        assert any("features.proactive" in s or "config.yaml" in s or "未开启" in s for s in report.next_step_suggestions)

    def test_suggestions_for_dry_run_success(self):
        ctx = ProactiveDebugContext(mode="dry_run", user_id="u1", bot_id="b1")
        tr = _fake_turn_result(proactive_delivered_texts=["你好"])
        chain = [
            _chain_entry(DecisionChainStage.GUARD, "pass", "", {}),
        ]
        report = generate_report(ctx, run_id="r1", decision_chain=chain,
                                 turn_result=tr,
                                 eligibility_result=_fake_eligibility(),
                                 impulse_result=_fake_impulse())
        assert any("real-send" in s.lower() or "real_send" in s.lower() for s in report.next_step_suggestions)


# ══════════════════════════════════════════════════════════════════
# Test: terminal_state translation
# ══════════════════════════════════════════════════════════════════

class TestTerminalStateLabels:
    def test_ts_label_end_replied(self):
        assert "已回复" in _ts_label("end_replied")

    def test_ts_label_end_cancelled(self):
        assert "取消" in _ts_label("end_cancelled")

    def test_ts_label_end_skipped_proactive(self):
        assert "跳过" in _ts_label("end_skipped_proactive")

    def test_ts_label_end_proactive_guard_blocked(self):
        assert "拦截" in _ts_label("end_proactive_guard_blocked")

    def test_ts_label_end_failed_no_fallback(self):
        assert "失败" in _ts_label("end_failed_no_fallback")

    def test_ts_label_end_proactive_max_reply_limit(self):
        assert "最大" in _ts_label("end_proactive_max_reply_limit") or "限制" in _ts_label("end_proactive_max_reply_limit")

    def test_ts_label_end_proactive_max_llm_steps(self):
        assert "LLM" in _ts_label("end_proactive_max_llm_steps") or "步数" in _ts_label("end_proactive_max_llm_steps")

    def test_ts_label_unknown_falls_through(self):
        assert _ts_label("unknown_state_xyz") == "unknown_state_xyz"


# ══════════════════════════════════════════════════════════════════
# Test: simulated sender contract
# ══════════════════════════════════════════════════════════════════

class TestSimulatedSender:
    def test_returns_list_of_dicts_with_delivered(self):
        sender = create_simulated_sender()
        messages = [{"text": "hello"}]
        result = asyncio.run(sender(messages))
        assert isinstance(result, list)
        assert result[0]["send_status"] == "delivered"

    def test_preserves_original_keys(self):
        sender = create_simulated_sender()
        messages = [{"text": "hello", "delay": 0.5}]
        result = asyncio.run(sender(messages))
        assert result[0]["text"] == "hello"
        assert result[0]["delay"] == 0.5


# ══════════════════════════════════════════════════════════════════
# Test: dry_run callbacks
# ══════════════════════════════════════════════════════════════════

class TestDryRunCallbacks:
    def test_write_history_callback_records_would_write(self):
        capture = []
        cb = create_dry_run_write_history(capture=capture)

        class FakeTurn:
            proactive_delivered_texts = ["msg1"]
        asyncio.run(cb(FakeTurn(), "end_replied"))
        assert len(capture) == 1
        assert capture[0]["would_write_assistant_proactive"] is True
        assert capture[0]["texts"] == ["msg1"]

    def test_post_turn_hooks_do_not_trigger_real_writes(self):
        capture = []
        hooks = create_dry_run_post_turn_hooks(capture=capture)

        class FakeTurn:
            turn_id = "test123"
            proactive_delivered_count = 1
            proactive_attempt_count = 1
            proactive_send_attempt_count = 1
        for hook in hooks:
            asyncio.run(hook(FakeTurn(), "end_replied"))
        assert len(capture) == 1
        assert "no production tables written" in capture[0]["note"]


# ══════════════════════════════════════════════════════════════════
# Test: ProactiveDebugContext validation
# ══════════════════════════════════════════════════════════════════

class TestProactiveDebugContextValidation:
    def test_valid_modes_accepted(self):
        ctx = ProactiveDebugContext(mode="dry_run", user_id="u1", bot_id="b1")
        assert ctx.mode == "dry_run"

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="mode"):
            ProactiveDebugContext(mode="invalid", user_id="u1", bot_id="b1")

    def test_invalid_impulse_type_raises(self):
        with pytest.raises(ValueError, match="forced_impulse_type"):
            ProactiveDebugContext(mode="dry_run", user_id="u1", bot_id="b1", forced_impulse_type="INVALID")
