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
        """Rebuild messages array from database (restart / clear).

        Only active events are loaded — the DB already handles compression filtering.
        """
        del turn_limit
        cid = f"{self.user_id}:{self.bot_id}"
        events = await store.get_recent_events(cid)
        self._messages = [
            {"role": e["role"], "content": copy.deepcopy(e["content"])}
            for e in events
        ]

    def add_event(self, role: str, content: Any) -> None:
        self._messages.append({"role": role, "content": copy.deepcopy(content)})

    def replace_with_compressed(self, summary: str) -> None:
        """Replace the message array with a compressed summary.

        Stores a single user message with one text block: <summary>.
        ContextBuilder.build_for_chat() will merge it with the next
        incoming user message when building context.
        """
        self._messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": f"<summary>\n{summary}\n</summary>"},
            ]
        }]

    async def complete(self, store: MemoryStore) -> dict[str, int]:
        """Clear DB and reset in-memory state. Returns cleared table counts."""
        counts = await store.clear_all()
        self._messages = []
        self._turn_count = 0
        return counts
