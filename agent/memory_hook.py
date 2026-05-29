"""Memory hook — v0.2 Phase 5.

Bridges the ContextBuilder extension points and POST_TURN_HOOKS to the
existing MemoryStore and MemoryWorker.

LOAD_MEMORY_CONTEXT:  searches recent memories relevant to the current message.
UPDATE_MEMORY:        triggers background memory extraction after the turn ends.

Controlled by config features.memory.  Does NOT expose memory tools to the model.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from memory.store import MemoryStore

logger = logging.getLogger(__name__)


class MemoryHook:
    """Wires memory retrieval and extraction into the v0.2 state machine hooks.

    Usage::

        hook = MemoryHook(store=memory_store, worker=memory_worker,
                          bot_id="default", max_context_memories=3)

        # At BUILD_CONTEXT:
        ctx_builder.set_memory_loader(hook.load_context)

        # At POST_TURN_HOOKS:
        await hook.update(user_message="...", assistant_text="...")
    """

    def __init__(
        self,
        store: MemoryStore,
        worker: Any = None,  # MemoryWorker | None
        *,
        bot_id: str = "default",
        max_context_memories: int = 3,
    ):
        self._store = store
        self._worker = worker
        self._bot_id = bot_id
        self._max_context = max(1, min(int(max_context_memories), 8))

    # ── LOAD_MEMORY_CONTEXT hook ──────────────────────────────────

    async def load_context(self, user_id: str, user_message: str) -> str | None:
        """Search recent memories relevant to the user's current message.

        Signature matches MemoryLoader: (user_id, user_message) -> str | None.
        Returns formatted memory context or None if nothing found.
        """
        if not user_id or not user_message:
            return None

        try:
            memories = await self._store.search_bot(
                query=user_message,
                bot_id=self._bot_id,
                top_k=self._max_context,
                include_shared=False,
            )
        except Exception:
            logger.warning("MemoryHook: search failed, returning no context", exc_info=True)
            return None

        if not memories:
            return None

        lines = ["## 相关记忆"]
        for i, m in enumerate(memories, 1):
            cat = m.get("category", "other")
            content = str(m.get("content", ""))[:200]
            lines.append(f"{i}. [{cat}] {content}")

        logger.debug("MemoryHook: loaded %d memories for context", len(memories))
        return "\n".join(lines)

    # ── UPDATE_MEMORY (POST_TURN_HOOKS) ──────────────────────────

    async def update(
        self,
        *,
        user_message: str = "",
        assistant_text: str = "",
        user_id: str = "",
    ) -> None:
        """Trigger background memory extraction after a turn ends.

        Fire-and-forget: schedules extraction via asyncio.create_task.
        Does NOT block the turn response.  Only runs if a worker is configured.
        """
        if not self._worker:
            return
        if not user_message.strip() or not assistant_text.strip():
            return

        try:
            asyncio.create_task(
                self._worker.extract(
                    user_message=user_message,
                    assistant_reply=assistant_text,
                    bot_id=self._bot_id,
                )
            )
            logger.debug("MemoryHook: enqueued memory extraction")
        except Exception:
            logger.warning("MemoryHook: failed to enqueue extraction", exc_info=True)
