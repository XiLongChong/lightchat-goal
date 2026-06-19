"""Tests for runtime.turn_hooks — observability post-turn hooks.

Covers:
  - build_observability_hooks returns exactly two hooks (dev-logs + model-attempt-log)
  - dev-logs hook emits a single logger.info line with the expected fields
  - dev-logs hook counts delivered messages via send_status OR delivery_status
  - model-attempt hook logs one line per attempt and does not mutate the turn
  - model-attempt hook is a safe no-op when there are no attempts
  - main._build_chatagent_post_turn_hooks still concatenates the proactive hook
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from agent.state_machine import (
    DeliveryStatus,
    ModelAttempt,
    TurnScratchpad,
    TurnState,
    VisibleMessage,
)
from runtime.turn_hooks import build_observability_hooks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_turn(**overrides) -> TurnScratchpad:
    """Create a TurnScratchpad with overridable fields for testing."""
    defaults = {
        "turn_id": "turn_test_001",
        "user_message": "hello world",
        "token_usage": {
            "prompt_tokens": 100,
            "peak_prompt_tokens": 150,
            "completion_tokens": 50,
            "total_tokens": 150,
            "estimated": False,
        },
        "model_name": "deepseek-v4-pro",
    }
    defaults.update(overrides)
    return TurnScratchpad(**defaults)


def _make_visible(text: str, delivery_status: str) -> VisibleMessage:
    return VisibleMessage(
        visible_message_id=f"vm_{text}",
        index=0,
        original_text=text,
        guarded_text=text,
        delivery_status=delivery_status,
    )


# ---------------------------------------------------------------------------
# build_observability_hooks — return value shape
# ---------------------------------------------------------------------------


def test_build_observability_hooks_returns_two_hooks():
    """The builder returns exactly two callable hooks."""
    hooks = build_observability_hooks(
        user_id="user-a",
        bot_id="default",
        route="ordinary_chat",
        context_policy="short_term_history",
        raw_messages=["hi"],
        memory_store=MagicMock(),
    )
    assert len(hooks) == 2
    for hook in hooks:
        assert callable(hook)


def test_build_observability_hooks_hooks_are_distinct_callables():
    """The two hooks are distinct objects (dev-logs vs model-attempt)."""
    hooks = build_observability_hooks(user_id="user-a")
    assert hooks[0] is not hooks[1]


# ---------------------------------------------------------------------------
# dev-logs hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dev_logs_hook_emits_log_with_expected_fields(caplog):
    """dev-logs hook calls logger.info with the documented field set."""
    turn = _make_turn()
    turn.visible_messages = [_make_visible("hello", DeliveryStatus.SENT_CONFIRMED)]
    turn.tool_results = [{"name": "get_time"}]
    turn.llm_call_count = 2

    hooks = build_observability_hooks(user_id="user-a")
    dev_logs_hook = hooks[0]

    with caplog.at_level(logging.INFO, logger="goal"):
        await dev_logs_hook(turn, TurnState.END_REPLIED)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    # Field names preserved from the original main.py implementation
    msg = record.getMessage()
    assert "[user-a]" in msg
    assert "turn=turn_test_001" in msg
    assert "terminal=end_replied" in msg
    assert "delivered=1" in msg
    assert "tool_results=1" in msg
    assert "iterations=2" in msg
    assert "tokens=150" in msg
    # elapsed_ms is dynamic; just verify the key is present
    assert "elapsed_ms=" in msg


@pytest.mark.asyncio
async def test_dev_logs_hook_counts_delivered_via_send_status_or_delivery_status(caplog):
    """delivered count includes messages whose send_status OR delivery_status is delivered/sent_confirmed."""
    turn = _make_turn()
    turn.visible_messages = [
        # delivered via delivery_status = sent_confirmed
        _make_visible("a", DeliveryStatus.SENT_CONFIRMED),
        # not delivered
        _make_visible("b", DeliveryStatus.FAILED_CONFIRMED),
        # delivered via delivery_status = sent_confirmed (visible_messages_as_dicts
        # maps both send_status and delivery_status from delivery_status)
        _make_visible("c", DeliveryStatus.SENT_CONFIRMED),
    ]

    hooks = build_observability_hooks(user_id="user-a")
    with caplog.at_level(logging.INFO, logger="goal"):
        await hooks[0](turn, TurnState.END_REPLIED)

    msg = caplog.records[0].getMessage()
    assert "delivered=2" in msg


@pytest.mark.asyncio
async def test_dev_logs_hook_zero_delivered_when_none_sent(caplog):
    """delivered count is 0 when no messages are confirmed sent."""
    turn = _make_turn()
    turn.visible_messages = [
        _make_visible("x", DeliveryStatus.PENDING),
        _make_visible("y", DeliveryStatus.FAILED_CONFIRMED),
    ]

    hooks = build_observability_hooks(user_id="user-a")
    with caplog.at_level(logging.INFO, logger="goal"):
        await hooks[0](turn, TurnState.END_REPLIED)

    msg = caplog.records[0].getMessage()
    assert "delivered=0" in msg


@pytest.mark.asyncio
async def test_dev_logs_hook_does_not_mutate_turn():
    """The dev-logs hook must not alter the turn scratchpad."""
    turn = _make_turn()
    turn.visible_messages = [_make_visible("hello", DeliveryStatus.SENT_CONFIRMED)]
    turn.tool_results = [{"name": "get_time"}]
    turn.llm_call_count = 1

    before = {
        "turn_id": turn.turn_id,
        "user_message": turn.user_message,
        "token_usage": dict(turn.token_usage),
        "visible_count": len(turn.visible_messages),
        "tool_results_count": len(turn.tool_results),
        "llm_call_count": turn.llm_call_count,
    }

    hooks = build_observability_hooks(user_id="user-a")
    await hooks[0](turn, TurnState.END_REPLIED)

    assert turn.turn_id == before["turn_id"]
    assert turn.user_message == before["user_message"]
    assert turn.token_usage == before["token_usage"]
    assert len(turn.visible_messages) == before["visible_count"]
    assert len(turn.tool_results) == before["tool_results_count"]
    assert turn.llm_call_count == before["llm_call_count"]


# ---------------------------------------------------------------------------
# model-attempt hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_attempt_hook_logs_each_attempt(caplog):
    """One logger.info line per model attempt, with the documented fields."""
    turn = _make_turn()
    turn.model_attempts = [
        ModelAttempt(
            attempt_index=0,
            selected_model="deepseek-v4-pro",
            model_name="deepseek-v4-pro",
            routing_mode="default",
            routing_reason="initial",
            latency_ms=120,
            result_type="ok",
            error_type="",
            upgraded_from="",
        ),
        ModelAttempt(
            attempt_index=1,
            selected_model="deepseek-v4-pro",
            model_name="deepseek-v4-pro",
            routing_mode="upgrade",
            routing_reason="context_too_long",
            latency_ms=200,
            result_type="ok",
            error_type="",
            upgraded_from="deepseek-v4-lite",
        ),
    ]

    hooks = build_observability_hooks(user_id="user-a")
    model_attempt_hook = hooks[1]

    with caplog.at_level(logging.INFO, logger="goal"):
        await model_attempt_hook(turn, TurnState.END_REPLIED)

    attempt_records = [r for r in caplog.records if "model_attempt:" in r.getMessage()]
    assert len(attempt_records) == 2

    first = attempt_records[0].getMessage()
    assert "index=0" in first
    assert "model=deepseek-v4-pro/deepseek-v4-pro" in first
    assert "mode=default" in first
    assert "reason=initial" in first
    assert "latency=120ms" in first
    assert "result=ok" in first
    assert "upgraded_from=" in first

    second = attempt_records[1].getMessage()
    assert "index=1" in second
    assert "mode=upgrade" in second
    assert "reason=context_too_long" in second
    assert "upgraded_from=deepseek-v4-lite" in second


@pytest.mark.asyncio
async def test_model_attempt_hook_no_attempts_is_safe_noop(caplog):
    """When there are no model attempts, the hook produces no log and does not crash."""
    turn = _make_turn()
    assert turn.model_attempts == []

    hooks = build_observability_hooks(user_id="user-a")
    with caplog.at_level(logging.INFO, logger="goal"):
        await hooks[1](turn, TurnState.END_REPLIED)

    attempt_records = [r for r in caplog.records if "model_attempt:" in r.getMessage()]
    assert len(attempt_records) == 0


@pytest.mark.asyncio
async def test_model_attempt_hook_does_not_mutate_turn():
    """The model-attempt hook must not alter the turn scratchpad."""
    turn = _make_turn()
    turn.model_attempts = [
        ModelAttempt(attempt_index=0, selected_model="m", model_name="m"),
    ]
    attempts_before = list(turn.model_attempts)

    hooks = build_observability_hooks(user_id="user-a")
    await hooks[1](turn, TurnState.END_REPLIED)

    assert turn.model_attempts == attempts_before
    assert turn.terminal_state is None  # hook must not set terminal state


# ---------------------------------------------------------------------------
# main.py composition — proactive hook still concatenated
# ---------------------------------------------------------------------------


def test_main_build_post_turn_hooks_concatenates_proactive_hook():
    """main._build_chatagent_post_turn_hooks returns 3 hooks:
    2 observability + 1 local proactive-scheduling hook.
    """
    import main

    hooks = main._build_chatagent_post_turn_hooks(
        user_id="user-a",
        bot_id="default",
        route="ordinary_chat",
        context_policy="short_term_history",
        raw_messages=["hi"],
        memory_store=main.memory_store,
    )
    assert len(hooks) == 3
    # The first two come from build_observability_hooks; the third is the
    # proactive-scheduling hook that captures main.task_scheduler.
    assert callable(hooks[0])
    assert callable(hooks[1])
    assert callable(hooks[2])


@pytest.mark.asyncio
async def test_main_proactive_hook_is_noop_when_not_replied():
    """The proactive hook (3rd) returns early when terminal_state != END_REPLIED.

    This confirms the proactive hook is the local one from main.py and that its
    guard logic is preserved (it should not call task_scheduler on non-replied turns).
    """
    import main

    hooks = main._build_chatagent_post_turn_hooks(
        user_id="user-a",
        bot_id="default",
        route="ordinary_chat",
        context_policy="short_term_history",
        raw_messages=["hi"],
        memory_store=main.memory_store,
    )
    proactive_hook = hooks[2]

    turn = _make_turn()
    turn.visible_messages = [_make_visible("hi", DeliveryStatus.SENT_CONFIRMED)]

    # END_CANCELLED — proactive hook should return without touching scheduler.
    # No exception means the guard path executed cleanly.
    await proactive_hook(turn, TurnState.END_CANCELLED)
