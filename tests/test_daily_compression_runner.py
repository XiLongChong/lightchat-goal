"""Tests for runtime.daily_compression — DailyCompressionRunner.

Covers:
  1. No current contact → skipped with reason "no_current_contact".
  2. Has current contact → run_compression(user_id, bot_id) is called.
  3. Context user differs or is empty → ensure_conversation + rebuild.
  4. Context already current and non-empty → no rebuild.
  5. Per-user lock from user_turns protects the compression flow.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.conversation import ConversationContext
from memory.store import MemoryStore
from runtime.daily_compression import DailyCompressionRunner
from runtime.user_turns import UserTurnRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(*, contact_user_id: str = "", contact_bot_id: str = "") -> MagicMock:
    """Create a mock MemoryStore with configurable contact state."""
    store = MagicMock(spec=MemoryStore)
    store.get_contact_state = AsyncMock(
        return_value={
            "user_id": contact_user_id,
            "bot_id": contact_bot_id,
        }
    )
    store.ensure_conversation = AsyncMock(return_value=None)
    return store


def _make_context(*, user_id: str = "", bot_id: str = "default", messages=None) -> ConversationContext:
    """Create a ConversationContext, optionally pre-populated."""
    ctx = ConversationContext(user_id=user_id, bot_id=bot_id)
    if messages:
        for msg in messages:
            ctx.add_event(msg["role"], msg["content"])
    return ctx


# ---------------------------------------------------------------------------
# 1. No current contact → skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_current_contact_returns_skipped():
    """When contact has no user_id, the runner returns skipped."""
    store = _make_store(contact_user_id="", contact_bot_id="")
    ctx = _make_context()
    user_turns = UserTurnRegistry()
    run_compression = AsyncMock(return_value={"status": "should_not_be_called"})

    runner = DailyCompressionRunner(
        store=store,
        context=ctx,
        user_turns=user_turns,
        run_compression=run_compression,
    )
    result = await runner.run_current_contact()

    assert result == {"status": "skipped", "reason": "no_current_contact"}
    run_compression.assert_not_awaited()
    store.ensure_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_whitespace_only_user_id_treated_as_empty():
    """A whitespace-only user_id is treated as no contact."""
    store = _make_store(contact_user_id="   ", contact_bot_id="default")
    ctx = _make_context()
    user_turns = UserTurnRegistry()
    run_compression = AsyncMock()

    runner = DailyCompressionRunner(
        store=store,
        context=ctx,
        user_turns=user_turns,
        run_compression=run_compression,
    )
    result = await runner.run_current_contact()

    assert result["status"] == "skipped"
    assert result["reason"] == "no_current_contact"


# ---------------------------------------------------------------------------
# 2. Has current contact → run_compression called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_contact_calls_run_compression():
    """When a contact exists, run_compression(user_id, bot_id) is called."""
    store = _make_store(contact_user_id="user-a", contact_bot_id="default")
    ctx = _make_context(user_id="user-a", bot_id="default",
                        messages=[{"role": "user", "content": "hello"}])
    user_turns = UserTurnRegistry()
    expected = {"status": "compressed", "user_id": "user-a", "memory_operations": 2}
    run_compression = AsyncMock(return_value=expected)

    runner = DailyCompressionRunner(
        store=store,
        context=ctx,
        user_turns=user_turns,
        run_compression=run_compression,
    )
    result = await runner.run_current_contact()

    assert result == expected
    run_compression.assert_awaited_once_with("user-a", "default")


@pytest.mark.asyncio
async def test_contact_bot_id_falls_back_to_default():
    """When contact has no bot_id, the default_bot_id is used."""
    store = _make_store(contact_user_id="user-a", contact_bot_id="")
    ctx = _make_context(user_id="user-a", bot_id="my-bot",
                        messages=[{"role": "user", "content": "hi"}])
    user_turns = UserTurnRegistry()
    run_compression = AsyncMock(return_value={"status": "compressed"})

    runner = DailyCompressionRunner(
        store=store,
        context=ctx,
        user_turns=user_turns,
        run_compression=run_compression,
        default_bot_id="my-bot",
    )
    await runner.run_current_contact()

    run_compression.assert_awaited_once_with("user-a", "my-bot")


# ---------------------------------------------------------------------------
# 3. Context user differs or is empty → ensure_conversation + rebuild
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_user_differs_triggers_rebuild():
    """When context.user_id != contact user_id, ensure_conversation + rebuild are called."""
    store = _make_store(contact_user_id="user-b", contact_bot_id="default")
    # Context currently holds user-a, but contact is user-b
    ctx = _make_context(user_id="user-a", bot_id="default",
                        messages=[{"role": "user", "content": "old"}])
    ctx.rebuild = AsyncMock(return_value=None)
    user_turns = UserTurnRegistry()
    run_compression = AsyncMock(return_value={"status": "compressed"})

    runner = DailyCompressionRunner(
        store=store,
        context=ctx,
        user_turns=user_turns,
        run_compression=run_compression,
    )
    await runner.run_current_contact()

    store.ensure_conversation.assert_awaited_once_with("user-b", "default")
    ctx.rebuild.assert_awaited_once_with(store)
    # Context should be updated to the new user
    assert ctx.user_id == "user-b"
    assert ctx.bot_id == "default"


@pytest.mark.asyncio
async def test_context_empty_triggers_rebuild():
    """When context is empty (even if user_id matches), rebuild is triggered."""
    store = _make_store(contact_user_id="user-a", contact_bot_id="default")
    ctx = _make_context(user_id="user-a", bot_id="default")  # empty — no messages
    ctx.rebuild = AsyncMock(return_value=None)
    user_turns = UserTurnRegistry()
    run_compression = AsyncMock(return_value={"status": "compressed"})

    runner = DailyCompressionRunner(
        store=store,
        context=ctx,
        user_turns=user_turns,
        run_compression=run_compression,
    )
    await runner.run_current_contact()

    store.ensure_conversation.assert_awaited_once_with("user-a", "default")
    ctx.rebuild.assert_awaited_once_with(store)


# ---------------------------------------------------------------------------
# 4. Context already current and non-empty → no rebuild
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_already_current_no_rebuild():
    """When context.user_id matches and context is non-empty, no rebuild."""
    store = _make_store(contact_user_id="user-a", contact_bot_id="default")
    ctx = _make_context(user_id="user-a", bot_id="default",
                        messages=[{"role": "user", "content": "existing"}])
    ctx.rebuild = AsyncMock(return_value=None)
    user_turns = UserTurnRegistry()
    run_compression = AsyncMock(return_value={"status": "compressed"})

    runner = DailyCompressionRunner(
        store=store,
        context=ctx,
        user_turns=user_turns,
        run_compression=run_compression,
    )
    await runner.run_current_contact()

    store.ensure_conversation.assert_not_awaited()
    ctx.rebuild.assert_not_awaited()
    run_compression.assert_awaited_once_with("user-a", "default")


# ---------------------------------------------------------------------------
# 5. Per-user lock protects compression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uses_user_turns_lock():
    """The runner acquires the per-user lock from user_turns before compressing."""
    store = _make_store(contact_user_id="user-a", contact_bot_id="default")
    ctx = _make_context(user_id="user-a", bot_id="default",
                        messages=[{"role": "user", "content": "hi"}])
    user_turns = UserTurnRegistry()
    run_compression = AsyncMock(return_value={"status": "compressed"})

    runner = DailyCompressionRunner(
        store=store,
        context=ctx,
        user_turns=user_turns,
        run_compression=run_compression,
    )

    # Acquire the lock externally; the runner should wait until it's released
    lock = user_turns.get_lock("user-a")
    assert not lock.locked()

    await runner.run_current_contact()

    # After completion, the lock should be released
    assert not lock.locked()


@pytest.mark.asyncio
async def test_lock_serializes_concurrent_runs():
    """Two concurrent run_current_contact calls for the same user are serialized."""
    store = _make_store(contact_user_id="user-a", contact_bot_id="default")
    ctx = _make_context(user_id="user-a", bot_id="default",
                        messages=[{"role": "user", "content": "hi"}])
    user_turns = UserTurnRegistry()
    run_compression = AsyncMock(return_value={"status": "compressed"})

    runner = DailyCompressionRunner(
        store=store,
        context=ctx,
        user_turns=user_turns,
        run_compression=run_compression,
    )

    call_order: list[str] = []

    async def slow_compression(user_id, bot_id):
        call_order.append(f"start:{user_id}")
        await asyncio.sleep(0.05)
        call_order.append(f"end:{user_id}")
        return {"status": "compressed"}

    run_compression.side_effect = slow_compression

    await asyncio.gather(
        runner.run_current_contact(),
        runner.run_current_contact(),
    )

    # The two runs must not interleave
    assert call_order in (
        ["start:user-a", "end:user-a", "start:user-a", "end:user-a"],
        ["start:user-a", "end:user-a", "start:user-a", "end:user-a"],
    )
    assert run_compression.await_count == 2


# ---------------------------------------------------------------------------
# Integration: main.py wiring
# ---------------------------------------------------------------------------


def test_main_wires_runner_to_scheduler():
    """main.py creates a DailyCompressionRunner and wires it to task_scheduler."""
    import main

    # The runner instance exists
    assert hasattr(main, "daily_compression_runner")
    assert isinstance(main.daily_compression_runner, DailyCompressionRunner)

    # The scheduler's callback is the runner's bound method.
    # Bound methods create a new object on each access, so compare __func__
    # and __self__ instead of using ``is``.
    callback = main.task_scheduler._daily_compression_callback
    assert callback is not None
    assert callback.__func__ is DailyCompressionRunner.run_current_contact
    assert callback.__self__ is main.daily_compression_runner
