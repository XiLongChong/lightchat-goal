from __future__ import annotations

import pytest

from memory.long_term import (
    INDEX_NAME,
    LongTermMemoryError,
    LongTermMemoryService,
    MemoryOperation,
)


@pytest.mark.asyncio
async def test_create_view_replace_and_user_isolation(tmp_path):
    service = LongTermMemoryService(tmp_path)
    await service.execute("alice", MemoryOperation(
        command="create",
        path="user-background.md",
        file_text="# 用户背景\n\n更新时间：2026-06-11\n\n住在成都。\n",
    ))
    viewed = service.view("alice", "user-background.md")
    assert "成都" in viewed["message"]
    assert service.diagnostics("bob")["entries"] == []

    await service.execute("alice", MemoryOperation(
        command="str_replace",
        path="user-background.md",
        old_str="住在成都。",
        new_str="住在上海。",
    ))
    assert "上海" in service.view("alice", "user-background.md")["message"]


@pytest.mark.asyncio
async def test_rejects_invalid_paths_and_ambiguous_replace(tmp_path):
    service = LongTermMemoryService(tmp_path)
    with pytest.raises(LongTermMemoryError) as exc:
        service.view("alice", "../MEMORY.md")
    assert exc.value.code == "memory_invalid_path"

    await service.execute("alice", MemoryOperation(
        command="create",
        path="topic.md",
        file_text="# Topic\n\n更新时间：2026-06-11\n\nsame same\n",
    ))
    with pytest.raises(LongTermMemoryError) as exc:
        await service.execute("alice", MemoryOperation(
            command="str_replace", path="topic.md", old_str="same", new_str="new"
        ))
    assert exc.value.code == "memory_ambiguous_replace"


@pytest.mark.asyncio
async def test_batch_can_rollback_after_external_failure(tmp_path):
    service = LongTermMemoryService(tmp_path)
    receipt = await service.apply_batch("alice", [
        MemoryOperation(
            command="create",
            path="topic.md",
            file_text="# Topic\n\n更新时间：2026-06-11\n\nvalue\n",
        ),
        MemoryOperation(
            command="str_replace",
            path=INDEX_NAME,
            old_str="# 长期记忆索引",
            new_str="# 长期记忆索引\n\n- [Topic](topic.md) - value",
        ),
    ])
    assert service.user_dir("alice").joinpath("topic.md").exists()
    await receipt.rollback()
    assert not service.user_dir("alice").joinpath("topic.md").exists()
    assert "topic.md" not in service.read_index("alice")


@pytest.mark.asyncio
async def test_admin_delete_removes_index_entry(tmp_path):
    service = LongTermMemoryService(tmp_path)
    await service.apply_batch("alice", [
        MemoryOperation(
            command="create",
            path="topic.md",
            file_text="# Topic\n\n更新时间：2026-06-11\n\nvalue\n",
        ),
        MemoryOperation(
            command="str_replace",
            path=INDEX_NAME,
            old_str="# 长期记忆索引",
            new_str="# 长期记忆索引\n\n- [Topic](topic.md) - useful topic",
        ),
    ])
    result = await service.delete_for_admin("alice", "topic.md")
    assert result["code"] == "memory_deleted"
    assert "topic.md" not in service.read_index("alice")


def test_diagnostics_reports_dangling_and_orphan_files(tmp_path):
    service = LongTermMemoryService(tmp_path)
    directory = service.ensure_user("alice")
    (directory / INDEX_NAME).write_text(
        "# 长期记忆索引\n\n- [Missing](missing.md) - absent\n",
        encoding="utf-8",
    )
    (directory / "orphan.md").write_text("# Orphan\n", encoding="utf-8")
    codes = {item["code"] for item in service.diagnostics("alice")["warnings"]}
    assert codes == {"memory_dangling_index", "memory_orphan_file"}
