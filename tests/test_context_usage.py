from __future__ import annotations

import sqlite3

import pytest

import main
from agent.chat_agent import _merge_usage
from agent.llm_types import LLMUsage
from memory.store import MemoryStore


def test_merge_usage_tracks_peak_without_changing_totals():
    target = {
        "prompt_tokens": 0,
        "peak_prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated": False,
    }

    _merge_usage(
        target,
        LLMUsage(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
        ),
    )
    _merge_usage(
        target,
        LLMUsage(
            prompt_tokens=160,
            completion_tokens=30,
            total_tokens=190,
            estimated=True,
        ),
    )
    _merge_usage(
        target,
        LLMUsage(
            prompt_tokens=140,
            completion_tokens=10,
            total_tokens=150,
        ),
    )

    assert target["prompt_tokens"] == 400
    assert target["total_tokens"] == 460
    assert target["peak_prompt_tokens"] == 160
    assert target["estimated"] is True


@pytest.mark.asyncio
async def test_context_usage_persists_and_isolated_by_contact(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.db"))
    try:
        conversation_a = await store.ensure_conversation("user-a", "default")
        await store.begin_turn("turn-a", conversation_a, "hello")
        await store.finish_turn(
            "turn-a",
            token_count=210,
            prompt_tokens_peak=180,
            context_window_tokens=1_000_000,
            model_name="deepseek-v4-pro",
            usage_estimated=True,
        )

        conversation_b = await store.ensure_conversation("user-b", "default")
        await store.begin_turn("turn-b", conversation_b, "hello")
        await store.finish_turn(
            "turn-b",
            prompt_tokens_peak=300,
            context_window_tokens=1_000_000,
            model_name="other-model",
        )

        usage = await store.get_latest_context_usage("user-a", "default")
        assert usage is not None
        assert usage["turn_id"] == "turn-a"
        assert usage["prompt_tokens_peak"] == 180
        assert usage["context_window_tokens"] == 1_000_000
        assert usage["model_name"] == "deepseek-v4-pro"
        assert usage["usage_estimated"] == 1

        await store.clear_all()
        assert await store.get_latest_context_usage("user-a", "default") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_latest_user_turn_time_ignores_compressed_synthetic_turn(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.db"))
    try:
        conversation_id = await store.ensure_conversation("user-a", "default")
        await store.begin_turn("turn-a", conversation_id, "hello")
        previous_at = await store.get_latest_user_turn_created_at(
            "user-a",
            "default",
        )
        assert previous_at is not None

        await store.apply_compression(
            conversation_id,
            [{
                "role": "user",
                "content": [{"type": "text", "text": "<summary>old</summary>"}],
            }],
        )

        assert await store.get_latest_user_turn_created_at(
            "user-a",
            "default",
        ) == previous_at
        assert await store.get_latest_user_turn_created_at(
            "user-b",
            "default",
        ) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_old_database_adds_metric_columns_without_losing_turns(tmp_path):
    db_path = tmp_path / "old.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE runtime_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO runtime_meta(key, value)
        VALUES('runtime_schema', 'v3_daily_compression');
        CREATE TABLE turns (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            token_count INTEGER NOT NULL DEFAULT 0,
            elapsed_ms INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            completed_at REAL NOT NULL DEFAULT 0
        );
        INSERT INTO turns(
            id, conversation_id, turn_number, status, token_count,
            elapsed_ms, created_at, completed_at
        ) VALUES('old-turn', 'user-a:default', 1, 'completed', 42, 10, 1, 2);
        """
    )
    connection.commit()
    connection.close()

    store = MemoryStore(str(db_path))
    try:
        db = await store._get_db()
        rows = await db.execute_fetchall(
            "SELECT id, token_count, prompt_tokens_peak FROM turns WHERE id='old-turn'"
        )
        assert dict(rows[0]) == {
            "id": "old-turn",
            "token_count": 42,
            "prompt_tokens_peak": 0,
        }
    finally:
        await store.close()


class AdminRequest:
    cookies = {main.ADMIN_COOKIE: "context-usage-admin"}


class FakeContextUsageStore:
    async def get_contact_state(self):
        return {"user_id": "user-a", "bot_id": "default"}

    async def get_latest_context_usage(self, user_id, bot_id):
        assert (user_id, bot_id) == ("user-a", "default")
        return {
            "turn_id": "turn-a",
            "model_name": "deepseek-v4-pro",
            "prompt_tokens_peak": 1_250_000,
            "context_window_tokens": 1_000_000,
            "usage_estimated": 0,
            "completed_at": 123.0,
        }


@pytest.mark.asyncio
async def test_admin_context_usage_calculates_unclamped_percent(monkeypatch):
    main.ADMIN_SESSIONS.add("context-usage-admin")
    monkeypatch.setattr(main, "memory_store", FakeContextUsageStore())
    try:
        result = await main.admin_context_usage(AdminRequest())
    finally:
        main.ADMIN_SESSIONS.discard("context-usage-admin")

    assert result["available"] is True
    assert result["usage_ratio"] == 1.25
    assert result["usage_percent"] == 125.0
    assert result["estimated"] is False


@pytest.mark.asyncio
async def test_admin_context_usage_requires_authentication():
    class AnonymousRequest:
        cookies = {}

    with pytest.raises(main.HTTPException) as exc_info:
        await main.admin_context_usage(AnonymousRequest())

    assert exc_info.value.status_code == 401
