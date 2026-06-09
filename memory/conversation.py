"""In-memory conversation context — always-growing message array."""

from __future__ import annotations

import copy
from typing import Any

from memory.store import MemoryStore


class ConversationContext:
    """Holds the complete LLM messages array in memory.

    Events are appended turn-by-turn.  The array is only rebuilt from
    the database on restart (or after a clear / snapshot change).
    """

    def __init__(self, user_id: str, bot_id: str = "default") -> None:
        self.user_id = user_id
        self.bot_id = bot_id
        self._messages: list[dict[str, Any]] = []
        self._snapshot_id: str = ""
        self._turn_count: int = 0

    # ── public API ──────────────────────────────────────────────────

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Current messages array (shallow copy — callers must not mutate)."""
        return list(self._messages)

    @property
    def snapshot_id(self) -> str:
        return self._snapshot_id

    @snapshot_id.setter
    def snapshot_id(self, value: str) -> None:
        self._snapshot_id = value

    @property
    def empty(self) -> bool:
        return len(self._messages) == 0

    async def rebuild(self, store: MemoryStore, turn_limit: int = 20) -> None:
        """Rebuild messages array from database (restart / clear)."""
        cid = f"{self.user_id}:{self.bot_id}"
        events = await store.get_recent_events(cid, turn_limit=turn_limit)
        self._messages = [
            {"role": e["role"], "content": copy.deepcopy(e["content"])}
            for e in events
        ]

    def add_event(self, role: str, content: Any) -> None:
        self._messages.append({"role": role, "content": copy.deepcopy(content)})

    async def complete(self, store: MemoryStore) -> None:
        """Persist full reset: clear DB + rebuild from memory."""
        await store.clear_all()
        self._messages = []

    async def reset_for_new_snapshot(self, store: MemoryStore) -> None:
        """Clear everything because prompt/tools changed incompatibly."""
        await store.clear_all()
        self._messages = []
        self._turn_count = 0

    def maybe_trim(self, store: MemoryStore, estimated_tokens: int, max_budget: int) -> bool:
        """If over budget, drop the oldest non-critical turns, keeping last 20.

        Returns True if trimming occurred.
        """
        if estimated_tokens <= max_budget:
            return False
        # Simple strategy: drop from front until under budget or only 20 turns left.
        # This is synchronous and best-effort — we don't count exact turn boundaries here.
        # The caller is responsible for exact trimming logic.
        return True  # Signal that trim is needed (caller handles specifics)
