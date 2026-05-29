"""Context assembly — v0.2 Phase 3.

ContextBuilder assembles the LLM message array from persona, history, and the
current user message.  Extension points for memory, emotion, and summary are
reserved as injectable hooks (return empty results by default).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Hook signatures (all async, return str or None)
MemoryLoader = Callable[[str, str], Any]  # (user_id, user_message) -> str | None
EmotionLoader = Callable[[str], Any]  # (user_id) -> str | None
SummaryLoader = Callable[[str], Any]  # (user_id) -> str | None


class ContextBuilder:
    """Assemble the messages array for one chat turn.

    Usage::

        builder = ContextBuilder()
        messages = builder.build(
            system_prompt=sp,
            history=recent_history,
            user_message="你好",
            trigger="user",
            user_id="wxid_xxx",
        )
    """

    def __init__(self):
        self._memory_loader: MemoryLoader | None = None
        self._emotion_loader: EmotionLoader | None = None
        self._summary_loader: SummaryLoader | None = None

    # ── Extension point setters ──────────────────────────────────

    def set_memory_loader(self, loader: MemoryLoader | None) -> None:
        """Inject a memory context loader. Called during BUILD_CONTEXT.

        Signature: async (user_id: str, user_message: str) -> str | None
        Returns memory context text to prepend to system prompt, or None.
        """
        self._memory_loader = loader

    def set_emotion_loader(self, loader: EmotionLoader | None) -> None:
        """Inject an emotion state loader. Called during BUILD_CONTEXT.

        Signature: async (user_id: str) -> str | None
        Returns emotion state text to append to system prompt, or None.
        """
        self._emotion_loader = loader

    def set_summary_loader(self, loader: SummaryLoader | None) -> None:
        """Inject a summary context loader. Called during BUILD_CONTEXT.

        Signature: async (user_id: str) -> str | None
        Returns summary text to inject as a system message before history, or None.
        """
        self._summary_loader = loader

    # ── Main entry point ─────────────────────────────────────────

    async def build(
        self,
        *,
        system_prompt: str,
        history: list[dict[str, Any]],
        user_message: str,
        trigger: str = "user",
        user_id: str = "",
    ) -> list[dict[str, Any]]:
        """Assemble the full messages array for an LLM call.

        Order: system_prompt (+ memory context + emotion state)
               → summary context (as system message)
               → recent history
               → current user message (role=user or role=system)
        """
        messages: list[dict[str, Any]] = []

        # 1. System prompt with optional memory and emotion extensions
        enriched_prompt = system_prompt

        # LOAD_MEMORY_CONTEXT hook (reserved for future memory system)
        if self._memory_loader:
            try:
                memory_ctx = await self._memory_loader(user_id, user_message)
                if memory_ctx:
                    enriched_prompt = enriched_prompt + "\n\n" + str(memory_ctx)
            except Exception:
                logger.warning("ContextBuilder: memory loader failed, continuing without", exc_info=True)

        # LOAD_EMOTION_STATE hook (reserved for future emotion system)
        if self._emotion_loader:
            try:
                emotion_ctx = await self._emotion_loader(user_id)
                if emotion_ctx:
                    enriched_prompt = enriched_prompt + "\n\n" + str(emotion_ctx)
            except Exception:
                logger.warning("ContextBuilder: emotion loader failed, continuing without", exc_info=True)

        messages.append({"role": "system", "content": enriched_prompt})

        # 2. Summary context (reserved for future recap/summary system)
        if self._summary_loader:
            try:
                summary = await self._summary_loader(user_id)
                if summary:
                    messages.append({"role": "system", "content": str(summary)})
            except Exception:
                logger.warning("ContextBuilder: summary loader failed, continuing without", exc_info=True)

        # 3. Recent history (last 24 messages = ~12 turns)
        recent = history[-24:] if len(history) > 24 else history
        for h in recent:
            role = h.get("role", "user")
            if role not in ("system", "user", "assistant"):
                role = "user"
            content = _format_history_entry(h)
            if content.strip():
                messages.append({"role": role, "content": content})

        # 4. Current user message
        if trigger == "user":
            messages.append({"role": "user", "content": user_message})
        else:
            messages.append({"role": "system", "content": user_message})

        return messages


# ── History formatting (shared with chat_agent) ──────────────────

def _format_history_entry(item: dict[str, Any]) -> str:
    """Format one history entry for the LLM context.

    No labels, no metadata, no tool_call_id in content.
    """
    import re as _re

    content = str(item.get("content", ""))

    if not content.strip():
        return ""

    # Hard strip: any call_xxx pattern means contamination
    if _re.search(r"\bcall_[A-Za-z0-9_-]{8,}\b", content):
        return ""

    return content
