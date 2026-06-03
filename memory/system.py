"""MemorySystem — two-phase memory service facade.

Phase 8: Wraps MemoryStore with semantic load/write/forget operations.
load_for_turn() → MemorySelection (before model call).
write_after_turn() → persist + dedup (after turn terminal).

Spec: docs/runtime-specs/04-memory-system-spec.zh.md
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from runtime_contracts import MemorySelection, TurnType


# ── Phase 8 enums ─────────────────────────────────────────────────


class MemoryCategory(StrEnum):
    PROFILE = "profile"
    RELATIONSHIP = "relationship"
    BOUNDARY = "boundary"
    EPISODE = "episode"
    OPEN_THREAD = "open_thread"


class MemoryVisibility(StrEnum):
    ACTIVE = "active"
    HIDDEN_BY_USER = "hidden_by_user"
    BLOCKED_BY_FORGET = "blocked_by_forget_directive"
    ARCHIVED = "archived_by_relationship_reset"


class ForgetScope(StrEnum):
    LOCAL_WINDOW = "local_window"
    TOPIC = "topic"
    RELATIONSHIP_RESET = "relationship_reset"


# ── MemorySystem ───────────────────────────────────────────────────


@dataclass
class MemorySystem:
    """Two-phase memory service: load before model, write after turn.

    Uses existing MemoryStore for persistence. All selection/write
    decisions go through this facade rather than direct store access.
    """

    user_id: str = ""
    bot_id: str = ""
    max_per_category: int = 5
    max_total: int = 25
    _store: Any = None  # MemoryStore instance (set after init)
    _forget_directives: list[dict[str, Any]] = field(default_factory=list)

    # ── Phase 1: load_for_turn ─────────────────────────────────────

    async def load_for_turn(
        self,
        turn_type: TurnType | None = None,
        inbound_text: str = "",
    ) -> MemorySelection:
        """Load memories for context assembly. NEVER writes."""
        store = self._store
        if store is None:
            return MemorySelection()

        # Load by category from store
        boundary = await self._query_category(store, MemoryCategory.BOUNDARY)
        profile = await self._query_category(store, MemoryCategory.PROFILE)
        relationship = await self._query_category(store, MemoryCategory.RELATIONSHIP)
        episodes = await self._query_category(store, MemoryCategory.EPISODE)
        open_threads = await self._query_category(store, MemoryCategory.OPEN_THREAD)

        # Filter by visibility: only ACTIVE
        boundary = [m for m in boundary if self._is_active(m)]
        profile = [m for m in profile if self._is_active(m)]
        relationship = [m for m in relationship if self._is_active(m)]
        episodes = [m for m in episodes if self._is_active(m)]
        open_threads = [m for m in open_threads if self._is_active(m)]

        # Filter expired episodes
        now = datetime.utcnow()
        episodes = [e for e in episodes if not self._is_expired(e, now)]

        # Filter privacy_level=do_not_surface
        boundary = [m for m in boundary if not self._is_do_not_surface(m)]
        profile = [m for m in profile if not self._is_do_not_surface(m)]
        relationship = [m for m in relationship if not self._is_do_not_surface(m)]
        episodes = [e for e in episodes if not self._is_do_not_surface(e)]
        open_threads = [t for t in open_threads if not self._is_do_not_surface(t)]

        # Dedup within categories
        boundary = self._dedup(boundary)
        profile = self._dedup(profile)
        relationship = self._dedup(relationship)
        episodes = self._dedup(episodes)
        open_threads = self._dedup(open_threads)

        # Truncate per category
        boundary = boundary[:self.max_per_category]
        profile = profile[:self.max_per_category]
        relationship = relationship[:self.max_per_category]
        episodes = episodes[:self.max_per_category]
        open_threads = open_threads[:self.max_per_category]

        # Truncate total (open_threads → episodes → profile → relationship → boundary last)
        total = len(boundary) + len(profile) + len(relationship) + len(episodes) + len(open_threads)
        if total > self.max_total:
            overflow = total - self.max_total
            for lst in [open_threads, episodes, profile, relationship]:
                while overflow > 0 and lst:
                    lst.pop()
                    overflow -= 1

        total_count = len(boundary) + len(profile) + len(relationship) + len(episodes) + len(open_threads)

        reasons: list[str] = []
        if boundary:
            reasons.append(f"Loaded {len(boundary)} boundary memories (highest priority)")
        if profile:
            reasons.append(f"Loaded {len(profile)} profile memories")
        if relationship:
            reasons.append(f"Loaded {len(relationship)} relationship memories")
        if episodes:
            reasons.append(f"Loaded {len(episodes)} episode memories (filtered expired)")
        if open_threads:
            reasons.append(f"Loaded {len(open_threads)} open thread memories")

        # Sent content memory — loaded separately, NOT treated as relationship memory
        sent_content: list[Any] = []
        try:
            if hasattr(store, "query_sent_content"):
                sent_content = await store.query_sent_content(
                    user_id=self.user_id, bot_id=self.bot_id,
                    expires_after=now, limit=20,
                )
        except Exception:
            pass

        token_est = (total_count * 50)  # rough CJK estimate

        return MemorySelection(
            profile_memory=profile,
            relationship_memory=relationship,
            boundary_memory=boundary,
            episode_memory=episodes,
            open_thread_memory=open_threads,
            sent_content_memory=sent_content,
            selection_reasons=reasons,
            total_memory_count=total_count,
            token_estimate=token_est,
        )

    # ── Phase 2: write_after_turn ───────────────────────────────────

    async def write_after_turn(
        self,
        delivered_texts: list[str],
        turn_id: str = "",
    ) -> int:
        """Write memories after turn terminal. Returns count written."""
        store = self._store
        if store is None or not delivered_texts:
            return 0

        count = 0
        for text in delivered_texts:
            if not text.strip():
                continue
            try:
                await store.save_memory_bot(
                    content=f"[SENT] {text[:200]}",
                    bot_id=self.bot_id,
                    category="sent_content",
                    keywords="sent",
                    min_importance=0.0,
                )
                count += 1
            except Exception:
                pass
        return count

    # ── Forget directive ────────────────────────────────────────────

    def apply_forget_directive(
        self,
        directive_text: str,
        scope: ForgetScope = ForgetScope.LOCAL_WINDOW,
        blocked_topic: str = "",
    ) -> str:
        """Apply a forget directive. Returns directive_id."""
        directive_id = f"fd_{uuid.uuid4().hex[:12]}"
        self._forget_directives.append({
            "directive_id": directive_id,
            "text": directive_text,
            "scope": scope.value,
            "blocked_topic": blocked_topic,
            "created_at": datetime.utcnow(),
        })
        return directive_id

    def is_blocked_by_forget(self, memory: dict[str, Any]) -> bool:
        """Check if a memory is blocked by any active forget directive."""
        if not self._forget_directives:
            return False
        mem_text = str(memory.get("content", "")).lower()
        mem_topics = str(memory.get("keywords", "")).lower()
        for fd in self._forget_directives:
            topic = str(fd.get("blocked_topic", "")).lower()
            if topic and (topic in mem_text or topic in mem_topics):
                return True
        return False

    # ── Helpers ────────────────────────────────────────────────────

    async def _query_category(self, store: Any, category: MemoryCategory) -> list[dict[str, Any]]:
        try:
            if hasattr(store, "query_memories"):
                return await store.query_memories(
                    user_id=self.user_id, bot_id=self.bot_id,
                    category=category.value,
                )
            # Fallback: use search_bot
            results = await store.search_bot(
                query="", bot_id=self.bot_id, top_k=50, include_shared=True,
            )
            return [r for r in results if r.get("category") == category.value]
        except Exception:
            return []

    @staticmethod
    def _is_active(m: dict[str, Any]) -> bool:
        vis = m.get("visibility", MemoryVisibility.ACTIVE.value)
        return vis == MemoryVisibility.ACTIVE.value

    @staticmethod
    def _is_do_not_surface(m: dict[str, Any]) -> bool:
        return m.get("privacy_level") == "do_not_surface"

    @staticmethod
    def _is_expired(m: dict[str, Any], now: datetime) -> bool:
        decay_at = m.get("decay_at")
        if decay_at is None:
            return False
        if isinstance(decay_at, (int, float)):
            decay_dt = datetime.utcfromtimestamp(decay_at)
            return now >= decay_dt
        return False

    @staticmethod
    def _dedup(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for m in memories:
            content = str(m.get("content", "")).strip()[:100]
            if content not in seen:
                seen.add(content)
                result.append(m)
        return result
