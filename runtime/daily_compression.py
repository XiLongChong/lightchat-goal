"""Daily compression runner — background scheduling orchestration.

Extracted from main.py — batch F of main.py responsibility splitting.

The compression *algorithm* lives in ``agent/compressor.py``; this module
only handles the background-trigger orchestration: reading the current
contact, acquiring the per-user lock, rebuilding the in-memory context
when necessary, and delegating to the injected ``run_compression`` callable.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from memory.conversation import ConversationContext
from memory.store import MemoryStore
from runtime.user_turns import UserTurnRegistry

logger = logging.getLogger("goal")


class DailyCompressionRunner:
    """Orchestrate daily compression for the current bot contact.

    The runner is wired as the ``daily_compression_callback`` on
    :class:`runtime.task_scheduler.TaskScheduler`.  Each invocation
    targets only the contact stored in ``bot_contact_state`` — multi-user
    scanning is intentionally out of scope.
    """

    def __init__(
        self,
        *,
        store: MemoryStore,
        context: ConversationContext,
        user_turns: UserTurnRegistry,
        run_compression: Callable[[str, str], Awaitable[dict[str, Any]]],
        default_bot_id: str = "default",
    ) -> None:
        self._store = store
        self._context = context
        self._user_turns = user_turns
        self._run_compression = run_compression
        self._default_bot_id = default_bot_id

    async def run_current_contact(self) -> dict[str, Any]:
        """Run daily compression for the current contact.

        Returns:
            ``{"status": "skipped", "reason": "no_current_contact"}`` when
            no contact is bound, otherwise the result of ``run_compression``.
        """
        contact = await self._store.get_contact_state()
        user_id = str(contact.get("user_id", "") or "").strip()
        bot_id = (
            str(contact.get("bot_id", "") or self._default_bot_id).strip()
            or self._default_bot_id
        )
        if not user_id:
            logger.info("daily compression skipped: no current contact")
            return {"status": "skipped", "reason": "no_current_contact"}

        lock = self._user_turns.get_lock(user_id)
        async with lock:
            if self._context.user_id != user_id or self._context.empty:
                self._context.user_id = user_id
                self._context.bot_id = bot_id
                await self._store.ensure_conversation(user_id, bot_id)
                await self._context.rebuild(self._store)
            return await self._run_compression(user_id, bot_id)
