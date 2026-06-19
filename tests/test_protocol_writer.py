"""Tests for runtime.protocol_writer — ProtocolEventWriter event persistence."""

from __future__ import annotations

import pytest

from agent.state_machine import TurnScratchpad, TurnState
from memory.conversation import ConversationContext
from memory.store import MemoryStore
from runtime.protocol_writer import ProtocolEventWriter


def _make_turn(**overrides) -> TurnScratchpad:
    """Create a TurnScratchpad with overridable fields for testing."""
    defaults = {
        "turn_id": "turn_test_001",
        "user_message": "hello world",
        "source": "user_message",
        "token_usage": {
            "prompt_tokens": 100,
            "peak_prompt_tokens": 150,
            "completion_tokens": 50,
            "total_tokens": 150,
            "estimated": False,
        },
        "context_window_tokens": 1_000_000,
        "model_name": "deepseek-v4-pro",
    }
    defaults.update(overrides)
    return TurnScratchpad(**defaults)


# ---------------------------------------------------------------------------
# turn_started
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_started_writes_db_and_context():
    """turn_started writes to DB (begin_turn) and appends user event to context."""
    store = MemoryStore(":memory:")
    try:
        ctx = ConversationContext("user-a", "default")
        writer = ProtocolEventWriter(
            store=store, context=ctx, user_id="user-a", bot_id="default",
        )

        turn = _make_turn()
        await writer.write(turn, "turn_started", {"user_message": "hello world"})

        # DB: the turn and user event should exist
        events = await store.get_recent_events("user-a:default")
        assert len(events) >= 1
        assert events[0]["role"] == "user"
        assert events[0]["content"] == "hello world"

        # Context: should have the user message
        assert len(ctx.messages) == 1
        assert ctx.messages[0]["role"] == "user"
        assert ctx.messages[0]["content"] == "hello world"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_turn_started_null_user_message_still_begins_turn():
    """When user_message is None, begin_turn is still called but context is NOT appended."""
    store = MemoryStore(":memory:")
    try:
        ctx = ConversationContext("user-a", "default")
        writer = ProtocolEventWriter(
            store=store, context=ctx, user_id="user-a", bot_id="default",
        )

        turn = _make_turn(user_message="")
        await writer.write(turn, "turn_started", {"user_message": None})

        # DB: turn should exist (verify via raw_events — user event with null content)
        events = await store.get_recent_events("user-a:default")
        assert len(events) >= 1

        # Context should still be empty (None user_message not appended)
        assert len(ctx.messages) == 0
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_writes_db_and_context():
    """A 'message' event writes to DB and appends to context."""
    store = MemoryStore(":memory:")
    try:
        ctx = ConversationContext("user-a", "default")
        writer = ProtocolEventWriter(
            store=store, context=ctx, user_id="user-a", bot_id="default",
        )

        turn = _make_turn()
        # Begin the turn first so the DB has the turn row
        await store.begin_turn(
            turn_id=turn.turn_id,
            conversation_id="user-a:default",
            user_message="hello",
        )

        payload = {
            "message": {"role": "assistant", "content": "Hi there!"},
            "call_id": "call_001",
        }
        await writer.write(turn, "message", payload)

        # Context should contain the assistant message
        assert len(ctx.messages) == 1
        assert ctx.messages[0]["role"] == "assistant"
        assert ctx.messages[0]["content"] == "Hi there!"

        # DB should have the event
        events = await store.get_recent_events("user-a:default")
        assistant_events = [e for e in events if e["role"] == "assistant"]
        assert len(assistant_events) == 1
        assert assistant_events[0]["content"] == "Hi there!"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_message_with_empty_payload_handled_gracefully():
    """Empty message payload should not crash — role defaults to '' and content is None."""
    store = MemoryStore(":memory:")
    try:
        ctx = ConversationContext("user-a", "default")
        writer = ProtocolEventWriter(
            store=store, context=ctx, user_id="user-a", bot_id="default",
        )

        turn = _make_turn()
        await store.begin_turn(
            turn_id=turn.turn_id,
            conversation_id="user-a:default",
            user_message="hello",
        )

        await writer.write(turn, "message", {})

        # Context should have one event with empty role
        assert len(ctx.messages) == 1
        assert ctx.messages[0]["role"] == ""
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_message_tool_result_writes_context():
    """Tool result messages are also persisted in context and DB."""
    store = MemoryStore(":memory:")
    try:
        ctx = ConversationContext("user-a", "default")
        writer = ProtocolEventWriter(
            store=store, context=ctx, user_id="user-a", bot_id="default",
        )

        turn = _make_turn()
        await store.begin_turn(
            turn_id=turn.turn_id,
            conversation_id="user-a:default",
            user_message="what time is it",
        )

        payload = {
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool_1", "content": "2026-06-19T10:00:00"},
                ],
            },
            "call_id": "call_002",
        }
        await writer.write(turn, "message", payload)

        assert len(ctx.messages) == 1
        assert ctx.messages[0]["role"] == "user"
        # Content should be a deep copy, not the same object
        assert ctx.messages[0]["content"] is not payload["message"]["content"]
        assert ctx.messages[0]["content"] == payload["message"]["content"]
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# turn_finished
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_finished_updates_terminal_state():
    """turn_finished writes terminal state and usage to the DB."""
    store = MemoryStore(":memory:")
    try:
        ctx = ConversationContext("user-a", "default")
        writer = ProtocolEventWriter(
            store=store, context=ctx, user_id="user-a", bot_id="default",
        )

        turn = _make_turn(
            token_usage={
                "prompt_tokens": 200,
                "peak_prompt_tokens": 200,
                "completion_tokens": 80,
                "total_tokens": 280,
                "estimated": True,
            },
            context_window_tokens=500_000,
            model_name="test-model",
        )
        await store.begin_turn(
            turn_id=turn.turn_id,
            conversation_id="user-a:default",
            user_message="hello",
        )

        await writer.write(
            turn,
            "turn_finished",
            {"terminal_state": TurnState.END_REPLIED.value},
        )

        # Verify via get_latest_context_usage which reads turn metadata
        usage = await store.get_latest_context_usage("user-a", "default")
        assert usage is not None
        assert usage["turn_id"] == "turn_test_001"
        assert usage["model_name"] == "test-model"
        assert usage["usage_estimated"] == 1
        assert usage["prompt_tokens_peak"] == 200

        # Verify token_count via direct DB query
        db = await store._get_db()
        rows = await db.execute_fetchall(
            "SELECT token_count FROM turns WHERE id=?",
            ("turn_test_001",),
        )
        assert len(rows) == 1
        assert rows[0]["token_count"] == 280
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_turn_finished_non_replied_state_preserves_status():
    """When terminal is not END_REPLIED, status is the terminal state string."""
    store = MemoryStore(":memory:")
    try:
        ctx = ConversationContext("user-a", "default")
        writer = ProtocolEventWriter(
            store=store, context=ctx, user_id="user-a", bot_id="default",
        )

        turn = _make_turn()
        await store.begin_turn(
            turn_id=turn.turn_id,
            conversation_id="user-a:default",
            user_message="hello",
        )

        await writer.write(
            turn,
            "turn_finished",
            {"terminal_state": TurnState.END_CANCELLED.value},
        )

        # Verify status via direct DB query
        db = await store._get_db()
        rows = await db.execute_fetchall(
            "SELECT status FROM turns WHERE id=?",
            ("turn_test_001",),
        )
        assert len(rows) == 1
        assert rows[0]["status"] == TurnState.END_CANCELLED.value
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_turn_finished_empty_terminal_defaults():
    """Empty terminal_state string defaults to '' status."""
    store = MemoryStore(":memory:")
    try:
        ctx = ConversationContext("user-a", "default")
        writer = ProtocolEventWriter(
            store=store, context=ctx, user_id="user-a", bot_id="default",
        )

        turn = _make_turn()
        await store.begin_turn(
            turn_id=turn.turn_id,
            conversation_id="user-a:default",
            user_message="hello",
        )

        await writer.write(turn, "turn_finished", {"terminal_state": ""})

        db = await store._get_db()
        rows = await db.execute_fetchall(
            "SELECT status FROM turns WHERE id=?",
            ("turn_test_001",),
        )
        assert len(rows) == 1
        assert rows[0]["status"] == ""
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# full flow: turn_started → message → turn_finished
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_turn_flow_context_has_user_and_assistant():
    """A complete turn flow appends both user and assistant events to context."""
    store = MemoryStore(":memory:")
    try:
        ctx = ConversationContext("user-a", "default")
        writer = ProtocolEventWriter(
            store=store, context=ctx, user_id="user-a", bot_id="default",
        )

        turn = _make_turn()

        # 1. turn_started
        await writer.write(turn, "turn_started", {"user_message": "Hello bot"})

        # 2. assistant message
        await writer.write(
            turn,
            "message",
            {
                "message": {"role": "assistant", "content": "Hi human!"},
                "call_id": "call_001",
            },
        )

        # 3. tool_result (user role)
        await writer.write(
            turn,
            "message",
            {
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "sent"},
                    ],
                },
                "call_id": "call_002",
            },
        )

        # 4. turn_finished
        await writer.write(
            turn,
            "turn_finished",
            {"terminal_state": TurnState.END_REPLIED.value},
        )

        # Context should have 3 events: user, assistant, user (tool_result)
        assert len(ctx.messages) == 3
        assert ctx.messages[0]["role"] == "user"
        assert ctx.messages[0]["content"] == "Hello bot"
        assert ctx.messages[1]["role"] == "assistant"
        assert ctx.messages[1]["content"] == "Hi human!"
        assert ctx.messages[2]["role"] == "user"
        assert isinstance(ctx.messages[2]["content"], list)

        # DB: turn should be completed
        usage = await store.get_latest_context_usage("user-a", "default")
        assert usage is not None
        assert usage["turn_id"] == "turn_test_001"
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# context isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_users_have_isolated_contexts():
    """Writers for different users write to separate contexts."""
    store = MemoryStore(":memory:")
    try:
        ctx_a = ConversationContext("user-a", "default")
        ctx_b = ConversationContext("user-b", "default")
        writer_a = ProtocolEventWriter(
            store=store, context=ctx_a, user_id="user-a", bot_id="default",
        )
        writer_b = ProtocolEventWriter(
            store=store, context=ctx_b, user_id="user-b", bot_id="default",
        )

        turn_a = _make_turn(turn_id="turn_a")
        turn_b = _make_turn(turn_id="turn_b")

        await writer_a.write(turn_a, "turn_started", {"user_message": "msg from A"})
        await writer_b.write(turn_b, "turn_started", {"user_message": "msg from B"})

        # Each context only has its own message
        assert len(ctx_a.messages) == 1
        assert ctx_a.messages[0]["content"] == "msg from A"
        assert len(ctx_b.messages) == 1
        assert ctx_b.messages[0]["content"] == "msg from B"
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# unknown event type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_event_type_is_noop():
    """Unknown event types are silently ignored (no crash)."""
    store = MemoryStore(":memory:")
    try:
        ctx = ConversationContext("user-a", "default")
        writer = ProtocolEventWriter(
            store=store, context=ctx, user_id="user-a", bot_id="default",
        )

        turn = _make_turn()
        # Should not raise
        await writer.write(turn, "some_unknown_event", {"data": 42})

        # Context unchanged
        assert len(ctx.messages) == 0
    finally:
        await store.close()
