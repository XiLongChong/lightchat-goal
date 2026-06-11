from __future__ import annotations

import pytest

import main
from memory.long_term import LongTermMemoryService, MemoryOperation


class AdminRequest:
    cookies = {main.ADMIN_COOKIE: "test-admin-token"}


class FakeStore:
    async def get_contact_state(self):
        return {"user_id": "user-a", "bot_id": "default"}


@pytest.fixture(autouse=True)
def admin_session():
    main.ADMIN_SESSIONS.add("test-admin-token")
    yield
    main.ADMIN_SESSIONS.discard("test-admin-token")


@pytest.mark.asyncio
async def test_admin_list_view_and_delete_memory(tmp_path, monkeypatch):
    service = LongTermMemoryService(tmp_path)
    await service.apply_batch("user-a", [
        MemoryOperation(
            command="create",
            path="topic.md",
            file_text="# Topic\n\n更新时间：2026-06-11\n\nvalue\n",
        ),
        MemoryOperation(
            command="str_replace",
            path="MEMORY.md",
            old_str="# 长期记忆索引",
            new_str="# 长期记忆索引\n\n- [Topic](topic.md) - useful",
        ),
    ])
    monkeypatch.setattr(main, "long_term_memory", service)
    monkeypatch.setattr(main, "memory_store", FakeStore())

    listed = await main.admin_list_memories(AdminRequest())
    assert listed["entries"][0]["filename"] == "topic.md"

    viewed = await main.admin_get_memory("topic.md", AdminRequest())
    assert "value" in viewed["message"]

    deleted = await main.admin_delete_memory("topic.md", AdminRequest())
    assert deleted["code"] == "memory_deleted"
    assert service.diagnostics("user-a")["entries"] == []


@pytest.mark.asyncio
async def test_reset_context_clears_long_term_memory(tmp_path, monkeypatch):
    service = LongTermMemoryService(tmp_path)
    await service.execute("user-a", MemoryOperation(
        command="create",
        path="topic.md",
        file_text="# Topic\n\n更新时间：2026-06-11\n\nvalue\n",
    ))

    class FakeConversation:
        async def complete(self, store):
            del store
            return {"raw_events": 3, "daily_summaries": 1}

    monkeypatch.setattr(main, "long_term_memory", service)
    monkeypatch.setattr(main, "memory_store", FakeStore())
    monkeypatch.setattr(main, "conversation_ctx", FakeConversation())

    result = await main.developer_reset_context(AdminRequest())
    assert result["long_term_memory"]["files"] == 2
    assert not service.user_dir("user-a").exists()
    assert result["kept"] == ["bot_contact_state"]
