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
