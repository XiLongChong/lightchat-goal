"""Storage compatibility tests for legacy scheduler/proactive tables."""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.store import MemoryStore


def test_memory_store_concurrent_first_access_initializes_once(tmp_path):
    class SlowMigratingStore(MemoryStore):
        def __init__(self, db_path: str):
            super().__init__(db_path)
            self.migrate_calls = 0

        async def _migrate(self, db=None):
            self.migrate_calls += 1
            await asyncio.sleep(0.01)
            await super()._migrate(db)

    async def run():
        store = SlowMigratingStore(str(tmp_path / "memory.db"))
        try:
            db1, db2 = await asyncio.gather(store._get_db(), store._get_db())

            assert db1 is db2
            assert store.migrate_calls == 1
        finally:
            await store.close()

    asyncio.run(run())


def test_contact_state_round_trip(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        await store.update_contact_state("wxid_me", "ctx")
        contact = await store.get_contact_state()

        assert contact["user_id"] == "wxid_me"
        assert contact["context_token"] == "ctx"

        await store.close()

    asyncio.run(run())


def test_protocol_log_upgrade_drops_old_data_and_keeps_contact(tmp_path):
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE bot_contact_state (
               id INTEGER PRIMARY KEY DEFAULT 1,
               user_id TEXT NOT NULL DEFAULT '',
               context_token TEXT NOT NULL DEFAULT '',
               bot_id TEXT NOT NULL DEFAULT '',
               updated_at REAL NOT NULL DEFAULT 0
           )"""
    )
    conn.execute(
        """INSERT INTO bot_contact_state(id, user_id, context_token, bot_id, updated_at)
           VALUES (1, 'wxid_keep', 'ctx_keep', 'default_bot', 1)"""
    )
    conn.execute(
        """CREATE TABLE conversation_history (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id TEXT NOT NULL,
               role TEXT NOT NULL,
               content TEXT NOT NULL,
               source TEXT NOT NULL DEFAULT '',
               ts REAL NOT NULL,
               bot_id TEXT NOT NULL DEFAULT '',
               turn_id TEXT NOT NULL DEFAULT ''
           )"""
    )
    conn.execute(
        """INSERT INTO conversation_history
           (user_id, role, content, source, ts, bot_id, turn_id)
           VALUES ('wxid_keep', 'user', 'old test data', 'wechat', 1, 'default_bot', 'old')"""
    )
    conn.commit()
    conn.close()

    async def run():
        store = MemoryStore(str(db_path))
        try:
            contact = await store.get_contact_state()
            assert contact["user_id"] == "wxid_keep"
            assert contact["context_token"] == "ctx_keep"

            db = await store._get_db()
            old_table = await db.execute_fetchall(
                """SELECT name FROM sqlite_master
                   WHERE type = 'table' AND name = 'conversation_history'"""
            )
            assert old_table == []
            version = await db.execute_fetchall(
                """SELECT value FROM schema_meta
                   WHERE key = 'protocol_log_schema_version'"""
            )
            assert version[0]["value"] == "2"
            turn_columns = await db.execute_fetchall("PRAGMA table_info('turns')")
            column_names = {row["name"] for row in turn_columns}
            assert "user_messages" not in column_names
            assert "assistant_actions" not in column_names
            assert "tool_calls" not in column_names
        finally:
            await store.close()

    asyncio.run(run())


def test_turn_draft_accepts_interrupted_status(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        try:
            await store.create_turn_draft(
                turn_id="turn_1",
                user_id="u1",
                messages=[],
                source="wechat",
                route="ChatLite",
            )
            ok = await store.mark_turn_draft_status("turn_1", "interrupted")
            draft = await store.get_turn_draft("turn_1")

            assert ok is True
            assert draft["status"] == "interrupted"
        finally:
            await store.close()

    asyncio.run(run())


def test_create_scheduled_wake_up_uses_trigger_at(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        try:
            task_id = await store.create_scheduled_wake_up(
                "wxid_me",
                "default_bot",
                {
                    "message_hint": "later",
                    "trigger_at": "2026-06-01T10:30:00+08:00",
                    "wake_up_type": "proactive",
                },
            )

            tasks = await store.list_scheduled_messages(status="pending")
            task = next(item for item in tasks if item["id"] == task_id)

            assert task["due_at"] == 1780281000.0
        finally:
            await store.close()

    asyncio.run(run())


def test_cancel_pending_proactive_wake_ups_keeps_user_requested_tasks(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        try:
            await store.create_scheduled_wake_up(
                "wxid_me",
                "default_bot",
                {
                    "message_hint": "proactive",
                    "trigger_at": "2026-06-01T10:30:00+08:00",
                    "wake_up_type": "proactive",
                },
            )
            user_task_id = await store.create_scheduled_wake_up(
                "wxid_me",
                "default_bot",
                {
                    "message_hint": "user reminder",
                    "trigger_at": "2026-06-01T10:30:00+08:00",
                    "wake_up_type": "user_requested",
                },
            )

            cancelled = await store.cancel_pending_proactive_wake_ups("wxid_me", "default_bot")
            pending = await store.list_scheduled_messages(status="pending")
            cancelled_rows = await store.list_scheduled_messages(status="cancelled")

            assert cancelled == 1
            assert [task["id"] for task in pending] == [user_task_id]
            assert len(cancelled_rows) == 1
            assert await store.get_pending_proactive_wake_up("wxid_me", "default_bot") is None
        finally:
            await store.close()

    asyncio.run(run())


def _legacy_conversation_history_round_trip(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        await store.append_conversation_history(
            user_id="wxid_me",
            role="user",
            content="今天天气怎么样",
            source="wechat",
            ts=100.0,
        )
        await store.append_conversation_history(
            user_id="wxid_me",
            role="assistant",
            content="成都今天有点闷",
            source="assistant",
            ts=101.0,
        )

        history = await store.get_recent_conversation_history("wxid_me", limit=20)
        assert [item["role"] for item in history] == ["user", "assistant"]
        assert history[0]["content"] == "今天天气怎么样"

        deleted = await store.clear_conversation_history("wxid_me")
        assert deleted == 2
        assert await store.get_recent_conversation_history("wxid_me", limit=20) == []

        await store.close()

    asyncio.run(run())


def test_protocol_history_round_trip_preserves_complete_turns(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        try:
            await store.begin_protocol_turn(
                turn_id="turn_1",
                user_id="wxid_me",
                bot_id="default_bot",
                source="user_message",
                trigger="user",
                user_message="hello",
            )
            await store.append_protocol_message(
                turn_id="turn_1",
                user_id="wxid_me",
                bot_id="default_bot",
                role="assistant",
                content=[
                    {"type": "thinking", "thinking": "check"},
                    {"type": "tool_use", "id": "call_1", "name": "get_time", "input": {}},
                ],
                call_id="call_turn_1_1",
                message_kind="model_response",
            )
            await store.append_protocol_message(
                turn_id="turn_1",
                user_id="wxid_me",
                bot_id="default_bot",
                role="user",
                content=[
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "12:00"},
                ],
                message_kind="tool_result",
            )
            await store.record_outbound_event(
                "om_turn_1_call_1",
                "turn_1",
                "hello back",
                user_id="wxid_me",
                bot_id="default_bot",
                send_status="created",
            )
            await store.record_outbound_event(
                "om_turn_1_call_1",
                "turn_1",
                "hello back",
                user_id="wxid_me",
                bot_id="default_bot",
                send_status="delivered",
            )
            await store.finish_protocol_turn("turn_1", "completed", "delivered")

            await store.begin_protocol_turn(
                turn_id="turn_running",
                user_id="wxid_me",
                bot_id="default_bot",
                source="user_message",
                trigger="user",
                user_message="incomplete",
            )

            history = await store.get_recent_protocol_history(
                "wxid_me", bot_id="default_bot", turn_limit=20,
            )
            assert [item["role"] for item in history] == ["user", "assistant", "user"]
            assert history[1]["content"][0]["type"] == "thinking"
            assert history[2]["content"][0]["tool_use_id"] == "call_1"
            assert all(item["turn_id"] == "turn_1" for item in history)
            visible = await store.get_recent_visible_conversation(
                "wxid_me", bot_id="default_bot", turn_limit=20,
            )
            assert [(item["role"], item["content"]) for item in visible] == [
                ("user", "hello"),
                ("assistant", "hello back"),
            ]
            turns = await store.get_recent_turns("wxid_me", "default_bot")
            assert turns[0]["user_messages"] == "hello"
            assert json.loads(turns[0]["assistant_actions"])[0]["text"] == "hello back"
            assert json.loads(turns[0]["tool_calls"])[0]["name"] == "get_time"

            deleted = await store.clear_protocol_history("wxid_me", "default_bot")
            assert deleted == 2
            assert await store.get_recent_protocol_history(
                "wxid_me", "default_bot", turn_limit=20,
            ) == []
        finally:
            await store.close()

    asyncio.run(run())


def test_developer_reset_clears_protocol_log_but_keeps_contact(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        try:
            await store.update_contact_state("wxid_me", "ctx", bot_id="default_bot")
            await store.begin_protocol_turn(
                "turn_old",
                "wxid_me",
                "default_bot",
                source="user_message",
                trigger="user",
                user_message="old",
            )
            await store.finish_protocol_turn("turn_old", "completed", "delivered")

            counts = await store.clear_developer_memory_state()

            assert counts["protocol_messages"] == 1
            assert counts["turns"] == 1
            assert await store.get_recent_protocol_history(
                "wxid_me", "default_bot",
            ) == []
            contact = await store.get_contact_state()
            assert contact["user_id"] == "wxid_me"
            assert contact["context_token"] == "ctx"
        finally:
            await store.close()

    asyncio.run(run())


def test_usage_summary_round_trip(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        await store.record_usage_event(
            event_type="chat_turn",
            user_id="wxid_me",
            source="wechat",
            message_count=3,
            token_count=120,
            elapsed_ms=2500,
        )
        await store.record_usage_event(
            event_type="chat_turn",
            user_id="wxid_me",
            source="wechat",
            message_count=2,
            token_count=80,
            elapsed_ms=1000,
            fallback_type="invalid_json",
            metadata={"detail": "bad json"},
        )

        summary = await store.get_usage_summary(today_start_ts=0)

        assert summary["today"]["messages"] == 5
        assert summary["today"]["tokens"] == 200
        assert summary["today"]["fallbacks"] == 1
        assert summary["total"]["events"] == 2
        assert summary["recent_fallbacks"][0]["fallback_type"] == "invalid_json"
        assert summary["recent_fallbacks"][0]["detail"] == "bad json"

        await store.close()

    asyncio.run(run())


def test_legacy_scheduler_tables_still_migrate_for_existing_databases(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        db = await store._get_db()

        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('scheduled_messages', 'proactive_candidates', 'proactive_schedule_state', 'clingy_state')"
        )
        names = {row[0] for row in await cur.fetchall()}

        assert names == {
            "scheduled_messages",
            "proactive_candidates",
            "proactive_schedule_state",
            "clingy_state",
        }

        await store.close()

    asyncio.run(run())


def _legacy_developer_forget_clears_legacy_tables_and_usage(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        try:
            await store.save_memory_bot("用户喜欢夜里写代码", bot_id="default_bot", min_importance=0)
            await store.save_shared_fact("用户默认城市是成都")
            await store.append_conversation_history(
                "wxid_me", "user", "刚才那段对话", bot_id="default_bot"
            )
            await store.save_history_summary("wxid_me", "旧摘要", bot_id="default_bot")
            await store.save_recap("wxid_me", "default_bot", "旧 recap")
            await store.update_relationship_bot("default_bot", {"familiarity": 8})
            await store.upsert_active_topic("wxid_me", "default_bot", title="旧话题")
            await store.record_usage_event("test_event", user_id="wxid_me", message_count=1)

            db = await store._get_db()
            await db.execute(
                "INSERT INTO proactive_candidates(id, reason_type, reason_detail, created_at) "
                "VALUES ('pc_old', 'check_in', 'old', 1)"
            )
            await db.execute(
                "INSERT INTO proactive_schedule_state(id, user_id, bot_id, next_due_ts, updated_at) "
                "VALUES ('ps_old', 'wxid_me', 'default_bot', 123, 1)"
            )
            await db.execute(
                "INSERT INTO scheduled_messages(id, user_id, context_token, messages_json, due_at, status, created_at, updated_at) "
                "VALUES ('sm_old', 'wxid_me', 'ctx', '[]', 123, 'pending', 1, 1)"
            )
            await db.commit()

            counts = await store.clear_developer_memory_state()

            assert counts["memories"] == 1
            assert await store.list_all() == []
            assert await store.get_shared_facts() == []
            assert await store.get_recent_conversation_history("wxid_me", bot_id="default_bot") == []
            assert await store.get_history_summary("wxid_me", bot_id="default_bot") == ""
            assert await store.get_recaps("wxid_me", "default_bot") == []
            relationship = await store.get_relationship_bot("default_bot")
            assert relationship["familiarity"] == 5
            assert await store.get_active_topic("wxid_me", "default_bot") is None

            for table in ["proactive_candidates", "proactive_schedule_state", "scheduled_messages"]:
                cur = await db.execute(f"SELECT COUNT(*) FROM {table}")
                assert (await cur.fetchone())[0] == 0

            usage = await store.get_usage_summary(today_start_ts=0)
            assert usage["total"]["events"] == 0
        finally:
            await store.close()

    asyncio.run(run())
