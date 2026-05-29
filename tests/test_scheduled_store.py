"""Storage compatibility tests for legacy scheduler/proactive tables."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.store import MemoryStore


def test_contact_state_round_trip(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        await store.update_contact_state("wxid_me", "ctx")
        contact = await store.get_contact_state()

        assert contact["user_id"] == "wxid_me"
        assert contact["context_token"] == "ctx"

        await store.close()

    asyncio.run(run())


def test_conversation_history_round_trip(tmp_path):
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


def test_developer_forget_clears_legacy_tables_but_keeps_usage(tmp_path):
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
            assert usage["total"]["events"] == 1
        finally:
            await store.close()

    asyncio.run(run())
