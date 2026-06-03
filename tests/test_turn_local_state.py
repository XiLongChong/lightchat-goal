"""Phase 5: Turn-local state accessor tests.

Validates the explicit distinction between completed history and
this-turn sent messages, tool results, and delivery status.

Spec: docs/runtime-specs/01-context-builder-spec.zh.md §6.1, §6.2
      docs/runtime-specs/03-turn-engine-spec.zh.md INV-08, INV-09
Test matrix: CB-006 to CB-010
"""

from __future__ import annotations

from agent.state_machine import TurnScratchpad, VisibleMessage, DeliveryStatus as VMStatus
from runtime_contracts import CurrentTurnLocalState, DeliveryStatus as RTDeliveryStatus


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════


def make_turn(**overrides) -> TurnScratchpad:
    turn = TurnScratchpad()
    for k, v in overrides.items():
        setattr(turn, k, v)
    return turn


def add_visible_msg(turn: TurnScratchpad, text: str, delivery: str, tool_call_id: str = "") -> None:
    vm = VisibleMessage(
        visible_message_id=f"vm_{len(turn.visible_messages) + 1}",
        index=len(turn.visible_messages) + 1,
        original_text=text,
        guarded_text=text,
        delivery_status=delivery,
        tool_call_id=tool_call_id or f"tc_{len(turn.visible_messages) + 1}",
        sent_at_timestamp=1717400000.0 if delivery == VMStatus.SENT_CONFIRMED else 0.0,
        guard_status="PASSED",
    )
    turn.visible_messages.append(vm)
    if delivery == VMStatus.SENT_CONFIRMED:
        turn.sent_message_count += 1
        turn.proactive_delivered_count += 1
    elif delivery in (VMStatus.FAILED_BEFORE_SEND, VMStatus.FAILED_CONFIRMED):
        turn.send_failure_count += 1


# ═══════════════════════════════════════════════════════════════════
# I-4: Turn-local accumulation — sent messages stay local
# ═══════════════════════════════════════════════════════════════════


class TestTurnLocalAccumulation:
    def test_first_call_no_sent_messages(self):
        turn = make_turn()
        msgs = turn.get_sent_messages_this_turn()
        assert msgs == []
        assert turn.get_delivered_texts() == []

    def test_second_call_sees_first_delivered(self):
        """Same turn: send one message, then build context again — first
        message is in turn-local state, not yet in history."""
        turn = make_turn(llm_call_count=1)
        add_visible_msg(turn, "辛苦了宝宝", VMStatus.SENT_CONFIRMED)

        msgs = turn.get_sent_messages_this_turn()
        assert len(msgs) == 1
        assert msgs[0]["text"] == "辛苦了宝宝"
        assert msgs[0]["status"] == "delivered"

        # delivered texts: only one
        delivered = turn.get_delivered_texts()
        assert delivered == ["辛苦了宝宝"]

    def test_two_messages_second_call_sees_both(self):
        """Send two messages in same turn — both visible in turn-local."""
        turn = make_turn(llm_call_count=2)
        add_visible_msg(turn, "你好", VMStatus.SENT_CONFIRMED)
        add_visible_msg(turn, "在吗", VMStatus.SENT_CONFIRMED)

        msgs = turn.get_sent_messages_this_turn()
        assert len(msgs) == 2
        assert [m["text"] for m in msgs] == ["你好", "在吗"]
        assert turn.get_delivered_texts() == ["你好", "在吗"]

    def test_delivered_messages_never_in_failed_list(self):
        turn = make_turn()
        add_visible_msg(turn, "ok", VMStatus.SENT_CONFIRMED)
        assert turn.get_failed_texts() == []


# ═══════════════════════════════════════════════════════════════════
# I-5: Failed delivery NOT in writable history
# ═══════════════════════════════════════════════════════════════════


class TestFailedDeliveryExclusion:
    def test_failed_message_not_in_delivered_texts(self):
        turn = make_turn()
        add_visible_msg(turn, "失败了", VMStatus.FAILED_BEFORE_SEND)
        assert turn.get_delivered_texts() == []
        assert turn.get_failed_texts() == ["失败了"]

    def test_failed_message_not_in_history_writable(self):
        turn = make_turn()
        add_visible_msg(turn, "不应该保存", VMStatus.FAILED_CONFIRMED)
        writable = turn.get_history_writable_messages()
        assert writable == []

    def test_unknown_message_not_in_history_writable(self):
        turn = make_turn()
        add_visible_msg(turn, "未确认", VMStatus.UNKNOWN_AFTER_ATTEMPT)
        writable = turn.get_history_writable_messages()
        assert writable == []

    def test_mixed_delivered_and_failed(self):
        turn = make_turn()
        add_visible_msg(turn, "好的", VMStatus.SENT_CONFIRMED)
        add_visible_msg(turn, "坏的", VMStatus.FAILED_BEFORE_SEND)
        add_visible_msg(turn, "再好的", VMStatus.SENT_CONFIRMED)

        assert turn.get_delivered_texts() == ["好的", "再好的"]
        assert turn.get_failed_texts() == ["坏的"]

        writable = turn.get_history_writable_messages()
        assert len(writable) == 2
        assert all(m["role"] == "assistant" for m in writable)
        assert writable[0]["content"] == "好的"
        assert writable[1]["content"] == "再好的"

    def test_guard_blocked_not_in_delivered(self):
        turn = make_turn()
        add_visible_msg(turn, "不良内容", VMStatus.BLOCKED_BY_GUARD)
        assert turn.get_delivered_texts() == []
        writable = turn.get_history_writable_messages()
        assert writable == []


# ═══════════════════════════════════════════════════════════════════
# I-8: Turn-local state NOT in recent history
# ═══════════════════════════════════════════════════════════════════


class TestTurnLocalNotInHistory:
    def test_turn_local_state_marks_model_call_count(self):
        turn = make_turn(llm_call_count=0)
        tls = turn.to_turn_local_state()
        assert tls.model_call_count == 0
        assert tls.delivered_message_count == 0

    def test_turn_local_state_reflects_delivered_count(self):
        turn = make_turn(llm_call_count=1)
        add_visible_msg(turn, "msg1", VMStatus.SENT_CONFIRMED)
        add_visible_msg(turn, "msg2", VMStatus.FAILED_BEFORE_SEND)
        add_visible_msg(turn, "msg3", VMStatus.SENT_CONFIRMED)
        tls = turn.to_turn_local_state()
        assert tls.delivered_message_count == 2  # only delivered
        assert tls.failed_send_attempt_count == 1

    def test_empty_turn_local_state_before_any_send(self):
        turn = make_turn()
        tls = turn.to_turn_local_state()
        assert tls.turn_id == turn.turn_id
        assert tls.model_call_count == 0
        assert tls.delivered_message_count == 0
        assert tls.sent_messages_this_turn == []


# ═══════════════════════════════════════════════════════════════════
# I-6: Tool result NOT in user message
# ═══════════════════════════════════════════════════════════════════


class TestToolResultNotAsUser:
    def test_turn_local_tool_results_separate_from_messages(self):
        turn = make_turn()
        turn.tool_results = [{"tool_name": "get_weather", "result": "25°C"}]
        tls = turn.to_turn_local_state()
        # tool_results_this_turn is empty in mapping (not yet mapped)
        # but sent_messages has no weather content
        for m in tls.sent_messages_this_turn:
            assert "25" not in m.text

    def test_tool_results_do_not_enter_history_writable(self):
        turn = make_turn()
        turn.tool_results = [{"tool_name": "get_time", "result": "14:30"}]
        add_visible_msg(turn, "现在几点？", VMStatus.SENT_CONFIRMED)
        writable = turn.get_history_writable_messages()
        # only assistant text, no tool result content
        assert len(writable) == 1
        assert writable[0]["content"] == "现在几点？"


# ═══════════════════════════════════════════════════════════════════
# Budget state
# ═══════════════════════════════════════════════════════════════════


class TestBudgetState:
    def test_budget_reflects_remaining(self):
        turn = make_turn(
            llm_call_count=5, max_llm_steps_per_turn=20,
            sent_message_count=3, max_reply_messages_per_turn=15,
        )
        tls = turn.to_turn_local_state()
        assert tls.budget_state.model_calls_remaining == 15
        assert tls.budget_state.delivered_messages_remaining == 12

    def test_budget_never_negative(self):
        turn = make_turn(
            llm_call_count=25, max_llm_steps_per_turn=20,
            sent_message_count=20, max_reply_messages_per_turn=15,
        )
        tls = turn.to_turn_local_state()
        assert tls.budget_state.model_calls_remaining == 0
        assert tls.budget_state.delivered_messages_remaining == 0
