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
