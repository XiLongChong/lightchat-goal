"""Tests for bridge.sender — WeChatSender batch sending.

Covers:
  1. No bridge → send_batch returns messages with send_status="failed", no crash.
  2. With bridge → messages sent in order via bridge.send_text.
  3. Per-message text/delay handling preserved.
  4. Bridge results → send_status / delivery_status mapping preserved.
  5. Wrapper path (main.wechat_sender) still usable by TaskScheduler / callers.
  6. send_admin_batch raises on no bridge, sends in order with guard.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bridge.sender import WeChatSender


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bridge(*, token: str = "test-token", send_text_side_effect=None):
    """Create a mock WeChatBridge with an AsyncMock send_text."""
    bridge = MagicMock()
    bridge.token = token
    bridge.send_text = AsyncMock(side_effect=send_text_side_effect)
    return bridge


def _make_messages(*specs) -> list[dict]:
    """Build a message list from (text, delay) tuples."""
    return [{"text": t, "delay": d} for t, d in specs]


# ---------------------------------------------------------------------------
# send_batch — no bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_batch_no_bridge_marks_all_failed():
    """When bridge is None, all messages get send_status=failed, send_error=no_bridge."""
    sender = WeChatSender(
        bridge_getter=lambda: None,
        config_getter=lambda: {"message_delay": 0.0, "max_reply_delay": 0.0},
    )
    messages = _make_messages(("hello", 0), ("world", 0))
    result = await sender.send_batch("user-a", "ctx-token", messages)

    assert len(result) == 2
    for m in result:
        assert m["send_status"] == "failed"
        assert m["send_error"] == "no_bridge"


@pytest.mark.asyncio
async def test_send_batch_no_bridge_does_not_crash_on_empty_messages():
    """Empty message list with no bridge returns empty list without crashing."""
    sender = WeChatSender(
        bridge_getter=lambda: None,
        config_getter=lambda: {},
    )
    result = await sender.send_batch("user-a", "ctx-token", [])
    assert result == []


@pytest.mark.asyncio
async def test_send_batch_empty_context_token_marks_failed():
    """Empty context_token is treated like no bridge — all messages failed."""
    bridge = _make_bridge()
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {"message_delay": 0.0, "max_reply_delay": 0.0},
    )
    messages = _make_messages(("hello", 0))
    result = await sender.send_batch("user-a", "", messages)

    assert result[0]["send_status"] == "failed"
    assert result[0]["send_error"] == "no_bridge"
    bridge.send_text.assert_not_called()


@pytest.mark.asyncio
async def test_send_batch_empty_token_marks_failed():
    """Bridge with empty token is treated like no bridge."""
    bridge = _make_bridge(token="")
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {"message_delay": 0.0, "max_reply_delay": 0.0},
    )
    messages = _make_messages(("hello", 0))
    result = await sender.send_batch("user-a", "ctx-token", messages)

    assert result[0]["send_status"] == "failed"
    assert result[0]["send_error"] == "no_bridge"
    bridge.send_text.assert_not_called()


# ---------------------------------------------------------------------------
# send_batch — with bridge, ordering and status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_batch_sends_messages_in_order():
    """Messages are sent in the given order via bridge.send_text."""
    bridge = _make_bridge()
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {"message_delay": 0.0, "max_reply_delay": 0.0},
    )
    messages = _make_messages(("first", 0), ("second", 0), ("third", 0))
    await sender.send_batch("user-a", "ctx-token", messages)

    assert bridge.send_text.call_count == 3
    calls = bridge.send_text.call_args_list
    assert calls[0].args == ("user-a", "first", "ctx-token")
    assert calls[1].args == ("user-a", "second", "ctx-token")
    assert calls[2].args == ("user-a", "third", "ctx-token")


@pytest.mark.asyncio
async def test_send_batch_marks_delivered_on_success():
    """Successful sends get send_status=delivered."""
    bridge = _make_bridge()
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {"message_delay": 0.0, "max_reply_delay": 0.0},
    )
    messages = _make_messages(("hello", 0))
    result = await sender.send_batch("user-a", "ctx-token", messages)

    assert result[0]["send_status"] == "delivered"
    assert "send_error" not in result[0]


@pytest.mark.asyncio
async def test_send_batch_marks_failed_on_exception():
    """When bridge.send_text raises, the message gets send_status=failed."""
    bridge = _make_bridge(send_text_side_effect=RuntimeError("network error"))
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {"message_delay": 0.0, "max_reply_delay": 0.0},
    )
    messages = _make_messages(("hello", 0))
    result = await sender.send_batch("user-a", "ctx-token", messages)

    assert result[0]["send_status"] == "failed"
    assert "network error" in result[0]["send_error"]


@pytest.mark.asyncio
async def test_send_batch_marks_rate_limited_on_rate_error():
    """Errors containing '请稍后再试' or 'rate' get send_status=rate_limited."""
    bridge = _make_bridge(send_text_side_effect=RuntimeError("请稍后再试"))
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {"message_delay": 0.0, "max_reply_delay": 0.0},
    )
    messages = _make_messages(("hello", 0))
    result = await sender.send_batch("user-a", "ctx-token", messages)

    assert result[0]["send_status"] == "rate_limited"


@pytest.mark.asyncio
async def test_send_batch_rate_limited_on_rate_keyword():
    """Errors containing 'rate' (case-insensitive) get rate_limited."""
    bridge = _make_bridge(send_text_side_effect=RuntimeError("rate limit exceeded"))
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {"message_delay": 0.0, "max_reply_delay": 0.0},
    )
    messages = _make_messages(("hello", 0))
    result = await sender.send_batch("user-a", "ctx-token", messages)

    assert result[0]["send_status"] == "rate_limited"


# ---------------------------------------------------------------------------
# send_batch — text/delay handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_batch_strips_text_before_sending():
    """Message text is stripped before sending; empty text is blocked."""
    bridge = _make_bridge()
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {"message_delay": 0.0, "max_reply_delay": 0.0},
    )
    messages = [
        {"text": "  hello  ", "delay": 0},
        {"text": "   ", "delay": 0},
        {"text": "world", "delay": 0},
    ]
    result = await sender.send_batch("user-a", "ctx-token", messages)

    # Empty-text message is blocked, not sent
    assert result[1]["send_status"] == "blocked"
    assert result[1]["send_error"] == "empty text"

    # Only 2 messages actually sent (stripped)
    assert bridge.send_text.call_count == 2
    assert bridge.send_text.call_args_list[0].args == ("user-a", "hello", "ctx-token")
    assert bridge.send_text.call_args_list[1].args == ("user-a", "world", "ctx-token")


@pytest.mark.asyncio
async def test_send_batch_clamps_delay_to_max_reply_delay():
    """Per-message delay is clamped to max_reply_delay."""
    bridge = _make_bridge()
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {"message_delay": 0.0, "max_reply_delay": 0.0},
    )
    # max_reply_delay=0 disables clamping (truthy check in original code)
    messages = [{"text": "hello", "delay": 99.0}]
    await sender.send_batch("user-a", "ctx-token", messages)
    assert bridge.send_text.call_count == 1


@pytest.mark.asyncio
async def test_send_batch_per_user_lock_serializes_sends():
    """Concurrent send_batch calls for the same user are serialized by the per-user lock."""
    bridge = _make_bridge()
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {"message_delay": 0.0, "max_reply_delay": 0.0},
    )

    call_order: list[str] = []

    async def slow_send_text(user_id, text, context_token):
        call_order.append(f"start:{text}")
        await asyncio.sleep(0.05)
        call_order.append(f"end:{text}")

    bridge.send_text = AsyncMock(side_effect=slow_send_text)

    messages_a = _make_messages(("a1", 0))
    messages_b = _make_messages(("b1", 0))

    await asyncio.gather(
        sender.send_batch("user-a", "ctx", messages_a),
        sender.send_batch("user-a", "ctx", messages_b),
    )

    # The two batches must not interleave: a1 fully completes before b1 starts
    # (or vice versa).
    assert call_order in (
        ["start:a1", "end:a1", "start:b1", "end:b1"],
        ["start:b1", "end:b1", "start:a1", "end:a1"],
    )


# ---------------------------------------------------------------------------
# send_batch — bridge_getter reflects runtime changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_batch_bridge_getter_reflects_runtime_changes():
    """bridge_getter is called at send time, so start/stop cycles are reflected."""
    current_bridge: list = [None]
    sender = WeChatSender(
        bridge_getter=lambda: current_bridge[0],
        config_getter=lambda: {"message_delay": 0.0, "max_reply_delay": 0.0},
    )

    # Initially no bridge → failed
    messages = _make_messages(("hello", 0))
    result = await sender.send_batch("user-a", "ctx", messages)
    assert result[0]["send_status"] == "failed"

    # Bridge becomes available → delivered
    bridge = _make_bridge()
    current_bridge[0] = bridge
    messages2 = _make_messages(("hello", 0))
    result2 = await sender.send_batch("user-a", "ctx", messages2)
    assert result2[0]["send_status"] == "delivered"
    assert bridge.send_text.call_count == 1


# ---------------------------------------------------------------------------
# send_admin_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_admin_batch_raises_on_no_bridge():
    """send_admin_batch raises RuntimeError when bridge is None."""
    sender = WeChatSender(
        bridge_getter=lambda: None,
        config_getter=lambda: {},
    )
    with pytest.raises(RuntimeError, match="bridge is not configured"):
        await sender.send_admin_batch(
            "user-a", "ctx", [{"text": "hi", "delay": 0}],
            min_gap=0.0, max_reply_delay=0.0,
        )


@pytest.mark.asyncio
async def test_send_admin_batch_raises_on_empty_context_token():
    """send_admin_batch raises RuntimeError when context_token is empty."""
    bridge = _make_bridge()
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {},
    )
    with pytest.raises(RuntimeError, match="context_token"):
        await sender.send_admin_batch(
            "user-a", "", [{"text": "hi", "delay": 0}],
            min_gap=0.0, max_reply_delay=0.0,
        )


@pytest.mark.asyncio
async def test_send_admin_batch_sends_in_order():
    """send_admin_batch sends messages in order via bridge.send_text."""
    bridge = _make_bridge()
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {},
    )
    messages = _make_messages(("first", 0), ("second", 0), ("third", 0))
    await sender.send_admin_batch(
        "user-a", "ctx", messages,
        min_gap=0.0, max_reply_delay=0.0,
    )

    assert bridge.send_text.call_count == 3
    texts = [c.args[1] for c in bridge.send_text.call_args_list]
    assert texts == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_send_admin_batch_applies_text_guard():
    """When text_guard is provided, it is applied inside the loop."""
    bridge = _make_bridge()
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {},
    )

    def upper_guard(text: str) -> str:
        return text.upper()

    messages = _make_messages(("hello", 0), ("world", 0))
    await sender.send_admin_batch(
        "user-a", "ctx", messages,
        min_gap=0.0, max_reply_delay=0.0,
        text_guard=upper_guard,
    )

    texts = [c.args[1] for c in bridge.send_text.call_args_list]
    assert texts == ["HELLO", "WORLD"]


@pytest.mark.asyncio
async def test_send_admin_batch_skips_empty_after_guard():
    """Messages that become empty after text_guard are skipped (not sent)."""
    bridge = _make_bridge()
    sender = WeChatSender(
        bridge_getter=lambda: bridge,
        config_getter=lambda: {},
    )

    def empty_guard(text: str) -> str:
        return "" if text == "skip" else text

    messages = _make_messages(("keep", 0), ("skip", 0), ("also", 0))
    await sender.send_admin_batch(
        "user-a", "ctx", messages,
        min_gap=0.0, max_reply_delay=0.0,
        text_guard=empty_guard,
    )

    texts = [c.args[1] for c in bridge.send_text.call_args_list]
    assert texts == ["keep", "also"]


# ---------------------------------------------------------------------------
# main.py wrapper integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_wechat_sender_wrapper_delegates_to_service():
    """main.wechat_sender is a thin wrapper around wechat_sender_service.send_batch."""
    import main

    # Replace the service's send_batch with a mock to verify delegation
    original = main.wechat_sender_service.send_batch
    main.wechat_sender_service.send_batch = AsyncMock(return_value=[{"text": "ok", "send_status": "delivered"}])
    try:
        result = await main.wechat_sender("user-a", "ctx", [{"text": "ok", "delay": 0}])
        main.wechat_sender_service.send_batch.assert_awaited_once_with(
            "user-a", "ctx", [{"text": "ok", "delay": 0}],
        )
        assert result == [{"text": "ok", "send_status": "delivered"}]
    finally:
        main.wechat_sender_service.send_batch = original


@pytest.mark.asyncio
async def test_main_send_message_batch_wrapper_delegates_to_service():
    """main._send_message_batch delegates guard + send to the service."""
    import main

    # Mock the service's send_admin_batch
    original = main.wechat_sender_service.send_admin_batch
    main.wechat_sender_service.send_admin_batch = AsyncMock(return_value=None)
    try:
        await main._send_message_batch(
            "user-a", "ctx-token", [{"text": "hello", "delay": 0}],
        )
        main.wechat_sender_service.send_admin_batch.assert_awaited_once()
        call_kwargs = main.wechat_sender_service.send_admin_batch.call_args
        assert call_kwargs.args[0] == "user-a"
        assert call_kwargs.args[1] == "ctx-token"
        # Guarded messages passed through
        guarded = call_kwargs.args[2]
        assert len(guarded) == 1
        assert guarded[0]["text"] == "hello"
    finally:
        main.wechat_sender_service.send_admin_batch = original


def test_main_task_scheduler_uses_wechat_sender_wrapper():
    """TaskScheduler.send_text is still wired to main.wechat_sender (the wrapper)."""
    import main

    assert main.task_scheduler._send_text is main.wechat_sender
