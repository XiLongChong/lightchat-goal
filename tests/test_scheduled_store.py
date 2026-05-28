"""Test persistent scheduled message queue."""

import asyncio
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.store import MemoryStore


def test_scheduled_message_round_trip(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        task_id = await store.create_scheduled_message(
            user_id="wxid_me",
            context_token="ctx",
            messages=[{"text": "十分钟到了", "delay": 0}],
            due_at=time.time() - 1,
        )

        due = await store.get_due_scheduled_messages()
        assert len(due) == 1
        assert due[0]["id"] == task_id
        assert due[0]["messages"][0]["text"] == "十分钟到了"

        await store.mark_scheduled_message_status(task_id, "sent")
        due_after = await store.get_due_scheduled_messages()
        assert due_after == []

        all_tasks = await store.list_scheduled_messages()
        assert all_tasks[0]["status"] == "sent"

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


def test_proactive_outbound_delivery_status_round_trip(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        try:
            await store.record_outbound_messages(
                turn_id="turn_proactive",
                user_id="wxid_me",
                bot_id="default_bot",
                messages=[{"text": "还没睡？", "delay": 0}],
                source="proactive",
                author="assistant",
                trigger="proactive_reason",
                send_status="sending",
                candidate_id="pc_123",
                batch_id="turn_proactive",
            )
            await store.mark_outbound_batch_status("turn_proactive", "delivered")

            rows = await store.get_outbound_messages(
                turn_id="turn_proactive",
                user_id="wxid_me",
            )
            assert len(rows) == 1
            assert rows[0]["source"] == "proactive"
            assert rows[0]["author"] == "assistant"
            assert rows[0]["trigger"] == "proactive_reason"
            assert rows[0]["send_status"] == "delivered"
            assert rows[0]["candidate_id"] == "pc_123"
            assert rows[0]["batch_id"] == "turn_proactive"
            assert rows[0]["text"] == "还没睡？"
        finally:
            await store.close()

    asyncio.run(run())


def test_proactive_candidate_trace_fields_round_trip(tmp_path):
    async def run():
        store = MemoryStore(str(tmp_path / "memory.db"))
        try:
            candidate_id = await store.create_proactive_candidate(
                reason_type="self_share",
                reason_detail="no active topic",
                user_id="wxid_me",
                bot_id="default_bot",
            )
            await store.update_proactive_candidate(
                candidate_id,
                passed_gate=True,
                gate_detail="v2.1.1 due-time",
                generated_message="我刚才在想一个问题",
                sent=True,
                turn_id="turn_proactive",
                batch_id="turn_proactive",
                send_status="delivered",
            )

            rows = await store.get_recent_proactive_candidates(limit=1)
            assert rows[0]["id"] == candidate_id
            assert rows[0]["source"] == "proactive"
            assert rows[0]["trigger"] == "proactive_reason"
            assert rows[0]["send_status"] == "delivered"
            assert rows[0]["turn_id"] == "turn_proactive"
            assert rows[0]["batch_id"] == "turn_proactive"
            assert rows[0]["user_id"] == "wxid_me"
            assert rows[0]["bot_id"] == "default_bot"
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


def test_developer_forget_clears_model_state_but_keeps_usage(tmp_path):
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
            await store.create_proactive_candidate("check_in", "old")
            await store.update_proactive_schedule_state(
                "wxid_me", "default_bot", next_due_ts=123
            )
            await store.create_scheduled_message(
                "wxid_me",
                "ctx",
                [{"text": "old task"}],
                due_at=123,
            )
            await store.record_usage_event("test_event", user_id="wxid_me", message_count=1)

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
            assert await store.get_recent_proactive_candidates() == []
            schedule_state = await store.get_proactive_schedule_state("wxid_me", "default_bot")
            assert schedule_state["next_due_ts"] == 0
            assert await store.list_scheduled_messages() == []

            usage = await store.get_usage_summary(today_start_ts=0)
            assert usage["total"]["events"] == 1
        finally:
            await store.close()

    asyncio.run(run())
