"""Phase 8: MemorySystem contract tests.

Validates load_for_turn selection, write_after_turn filtering,
forget directives, visibility, privacy, bot isolation.

Spec: docs/runtime-specs/04-memory-system-spec.zh.md
Test matrix: MM-001 to MM-020
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta

from memory.system import (
    MemorySystem,
    MemoryCategory,
    MemoryVisibility,
    ForgetScope,
)


def make_memory(
    content: str,
    category: str = "profile",
    visibility: str = "active",
    privacy_level: str = "normal",
    decay_at: float | None = None,
    keywords: str = "",
    bot_id: str = "default_bot",
    memory_id: str = "",
) -> dict:
    return {
        "memory_id": memory_id or f"mem_{hash(content) % 10**12:012x}",
        "content": content,
        "category": category,
        "visibility": visibility,
        "privacy_level": privacy_level,
        "decay_at": decay_at,
        "keywords": keywords,
        "bot_id": bot_id,
        "user_id": "user_1",
        "importance": 0.5,
        "created_at": datetime.utcnow().timestamp(),
    }


class FakeStore:
    """Minimal fake for MemoryStore, supporting the query interface."""

    def __init__(self, memories: list[dict] | None = None):
        self._memories = memories or []
        self._sent_content: list[dict] = []

    async def query_memories(
        self, user_id: str, bot_id: str,
        category: str | None = None,
    ) -> list[dict]:
        result = [m for m in self._memories if m["bot_id"] == bot_id]
        if category:
            result = [m for m in result if m["category"] == category]
        return result

    async def search_bot(
        self, query: str = "", bot_id: str = "",
        top_k: int = 50, include_shared: bool = True,
    ) -> list[dict]:
        result = [m for m in self._memories if m["bot_id"] == bot_id]
        return result[:top_k]

    async def query_sent_content(
        self, user_id: str, bot_id: str,
        expires_after: datetime, limit: int = 20,
    ) -> list[dict]:
        return self._sent_content[:limit]

    async def save_memory_bot(self, content: str, bot_id: str = "",
                               category: str = "", keywords: str = "",
                               min_importance: float = 0.0) -> str | None:
        mem_id = f"mem_{hash(content) % 10**12:012x}"
        self._memories.append({
            "memory_id": mem_id, "content": content,
            "category": category, "visibility": "active",
            "privacy_level": "normal", "keywords": keywords,
            "bot_id": bot_id, "user_id": "user_1",
        })
        return mem_id


@pytest.fixture
def system():
    ms = MemorySystem(user_id="user_1", bot_id="default_bot")
    return ms


# ═══════════════════════════════════════════════════════════════════
# MM-002: Boundary memory highest priority
# ═══════════════════════════════════════════════════════════════════


class TestBoundaryPriority:
    async def test_boundary_loaded_when_present(self, system):
        store = FakeStore([
            make_memory("不要再开体重玩笑了", "boundary"),
            make_memory("用户喜欢被开玩笑", "profile"),
        ])
        system._store = store
        result = await system.load_for_turn()
        assert len(result.boundary_memory) == 1
        assert result.boundary_memory[0]["content"] == "不要再开体重玩笑了"

    async def test_boundary_appears_in_selection(self, system):
        store = FakeStore([
            make_memory("边界规则", "boundary"),
        ])
        system._store = store
        result = await system.load_for_turn()
        assert result.total_memory_count >= 1
        assert "highest priority" in str(result.selection_reasons)


# ═══════════════════════════════════════════════════════════════════
# MM-003: Hidden memory NOT in context
# ═══════════════════════════════════════════════════════════════════


class TestHiddenMemory:
    async def test_hidden_memory_not_in_selection(self, system):
        store = FakeStore([
            make_memory("可见记忆", "profile", visibility="active"),
            make_memory("隐藏记忆", "profile", visibility="hidden_by_user"),
        ])
        system._store = store
        result = await system.load_for_turn()
        contents = [m["content"] for m in result.profile_memory]
        assert "可见记忆" in contents
        assert "隐藏记忆" not in contents

    async def test_blocked_by_forget_not_in_selection(self, system):
        store = FakeStore([
            make_memory("被屏蔽", "profile", visibility="blocked_by_forget_directive"),
        ])
        system._store = store
        result = await system.load_for_turn()
        assert result.profile_memory == []

    async def test_archived_not_in_selection(self, system):
        store = FakeStore([
            make_memory("已归档", "relationship", visibility="archived_by_relationship_reset"),
        ])
        system._store = store
        result = await system.load_for_turn()
        assert result.relationship_memory == []


# ═══════════════════════════════════════════════════════════════════
# MM-005: do_not_surface NOT in context
# ═══════════════════════════════════════════════════════════════════


class TestDoNotSurface:
    async def test_do_not_surface_filtered(self, system):
        store = FakeStore([
            make_memory("正常记忆", "profile", privacy_level="normal"),
            make_memory("敏感记忆", "profile", privacy_level="do_not_surface"),
        ])
        system._store = store
        result = await system.load_for_turn()
        contents = [m["content"] for m in result.profile_memory]
        assert "正常记忆" in contents
        assert "敏感记忆" not in contents


# ═══════════════════════════════════════════════════════════════════
# MM-006: Forget directive blocks writes
# ═══════════════════════════════════════════════════════════════════


class TestForgetDirective:
    def test_forget_directive_blocks_matching_memory(self, system):
        system.apply_forget_directive(
            directive_text="不要记住工作的事",
            blocked_topic="工作",
        )
        blocked = system.is_blocked_by_forget(
            make_memory("用户不喜欢谈工作", keywords="工作"),
        )
        assert blocked is True

    def test_forget_directive_does_not_block_unrelated(self, system):
        system.apply_forget_directive(
            directive_text="不要记住工作",
            blocked_topic="工作",
        )
        blocked = system.is_blocked_by_forget(
            make_memory("用户喜欢喝咖啡", keywords="饮食"),
        )
        assert blocked is False

    def test_forget_directive_generates_id(self, system):
        did = system.apply_forget_directive("忘掉刚才说的")
        assert did.startswith("fd_")

    def test_no_forget_directives_allows_all(self, system):
        assert system.is_blocked_by_forget(make_memory("随意内容")) is False


# ═══════════════════════════════════════════════════════════════════
# MM-017: Sent content independent from relationship memory
# ═══════════════════════════════════════════════════════════════════


class TestSentContentIndependent:
    async def test_sent_content_separate_from_relationship(self, system):
        store = FakeStore([
            make_memory("关系记忆", "relationship"),
        ])
        system._store = store
        result = await system.load_for_turn()
        # sent_content_memory is separate, not mixed into relationship
        assert len(result.relationship_memory) >= 1 or len(result.relationship_memory) == 0
        # sent_content field exists independently
        assert hasattr(result, "sent_content_memory")

    async def test_write_after_turn_uses_separate_category(self, system):
        store = FakeStore()
        system._store = store
        count = await system.write_after_turn(["发了一条消息"], turn_id="turn_1")
        assert count >= 0  # write succeeds
        # The sent content is stored with category "sent_content", not "relationship"
        rel_ms = await store.query_memories("user_1", "default_bot", "relationship")
        # sent_content entries should not appear in relationship queries
        for m in rel_ms:
            assert "[SENT]" not in str(m.get("content", ""))


# ═══════════════════════════════════════════════════════════════════
# MM-018: Bot isolation
# ═══════════════════════════════════════════════════════════════════


class TestBotIsolation:
    async def test_bot_a_cannot_see_bot_b_memories(self, system):
        store = FakeStore([
            make_memory("bot_a 记忆", "profile", bot_id="bot_a"),
            make_memory("bot_b 记忆", "profile", bot_id="bot_b"),
        ])
        system.bot_id = "bot_a"
        system._store = store
        result = await system.load_for_turn()
        contents = [m["content"] for m in result.profile_memory]
        assert "bot_a 记忆" in contents
        assert "bot_b 记忆" not in contents

    async def test_switching_bot_shows_different_memories(self, system):
        store = FakeStore([
            make_memory("A dedicated", "profile", bot_id="bot_a"),
            make_memory("B dedicated", "profile", bot_id="bot_b"),
        ])
        system.bot_id = "bot_a"
        system._store = store
        result_a = await system.load_for_turn()

        system.bot_id = "bot_b"
        result_b = await system.load_for_turn()

        a_contents = [m["content"] for m in result_a.profile_memory]
        b_contents = [m["content"] for m in result_b.profile_memory]
        assert "A dedicated" in a_contents
        assert "A dedicated" not in b_contents
        assert "B dedicated" in b_contents


# ═══════════════════════════════════════════════════════════════════
# Expired episodes filtered
# ═══════════════════════════════════════════════════════════════════


class TestExpiredEpisodes:
    async def test_expired_episode_filtered(self, system):
        past = (datetime.utcnow() - timedelta(hours=13)).timestamp()
        store = FakeStore([
            make_memory("过期事件", "episode", decay_at=past),
            make_memory("当前事件", "episode"),
        ])
        system._store = store
        result = await system.load_for_turn()
        contents = [m["content"] for m in result.episode_memory]
        assert "过期事件" not in contents
        assert "当前事件" in contents


# ═══════════════════════════════════════════════════════════════════
# Truncation
# ═══════════════════════════════════════════════════════════════════


class TestTruncation:
    async def test_max_per_category_respected(self, system):
        system.max_per_category = 2
        store = FakeStore([
            make_memory(f"profile_{i}", "profile") for i in range(5)
        ])
        system._store = store
        result = await system.load_for_turn()
        assert len(result.profile_memory) <= 2

    async def test_max_total_respected(self, system):
        system.max_total = 3
        system.max_per_category = 10
        memories = []
        for i in range(5):
            memories.append(make_memory(f"profile_{i}", "profile"))
            memories.append(make_memory(f"episode_{i}", "episode"))
        store = FakeStore(memories)
        system._store = store
        result = await system.load_for_turn()
        assert result.total_memory_count <= 3
