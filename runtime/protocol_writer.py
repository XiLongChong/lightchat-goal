"""Protocol event writer — persists raw events and updates in-memory context.

Extracted from main.py — batch C of main.py responsibility splitting.
"""

from __future__ import annotations

from typing import Any

from agent.state_machine import TurnScratchpad, TurnState
from memory.conversation import ConversationContext
from memory.store import MemoryStore


class ProtocolEventWriter:
    """Writes turn events to both the DB and the in-memory ConversationContext.

    The ``.write`` callable matches :data:`agent.chat_agent.ProtocolEventWriter`
    (``Callable[[TurnScratchpad, str, dict], Coroutine[Any, Any, None]]``).
    """

    def __init__(
        self,
        *,
        store: MemoryStore,
        context: ConversationContext,
        user_id: str,
        bot_id: str,
    ) -> None:
        self._store = store
        self._context = context
        self._user_id = user_id
        self._bot_id = bot_id
        self._conversation_id = f"{user_id}:{bot_id}"

    async def write(
        self,
        turn: TurnScratchpad,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist one raw event and update the in-memory context."""
        if event_type == "turn_started":
            await self._write_turn_started(turn, payload)
        elif event_type == "message":
            await self._write_message(turn, payload)
        elif event_type == "turn_finished":
            await self._write_turn_finished(turn, payload)

    # ------------------------------------------------------------------
    # event handlers
    # ------------------------------------------------------------------

    async def _write_turn_started(
        self,
        turn: TurnScratchpad,
        payload: dict[str, Any],
    ) -> None:
        user_message = payload.get("user_message")
        await self._store.begin_turn(
            turn_id=turn.turn_id,
            conversation_id=self._conversation_id,
            user_message=None if user_message is None else str(user_message),
            source=turn.source,
            turn_type=turn.source if turn.source != "user_message" else "user_chat",
        )
        if user_message:
            self._context.add_event("user", str(user_message))

    async def _write_message(
        self,
        turn: TurnScratchpad,
        payload: dict[str, Any],
    ) -> None:
        message = payload.get("message") or {}
        role = str(message.get("role", ""))
        content = message.get("content")
        await self._store.append_event(
            turn_id=turn.turn_id,
            role=role,
            content=content,
            call_id=str(payload.get("call_id", "")),
        )
        self._context.add_event(role, content)

    async def _write_turn_finished(
        self,
        turn: TurnScratchpad,
        payload: dict[str, Any],
    ) -> None:
        terminal = str(payload.get("terminal_state", ""))
        status = (
            "completed"
            if terminal in {TurnState.END_REPLIED.value}
            else terminal
        )
        await self._store.finish_turn(
            turn_id=turn.turn_id,
            status=status,
            token_count=turn.token_usage.get("total_tokens", 0),
            elapsed_ms=turn.elapsed_ms,
            prompt_tokens_peak=turn.token_usage.get("peak_prompt_tokens", 0),
            context_window_tokens=turn.context_window_tokens,
            model_name=turn.model_name,
            usage_estimated=bool(turn.token_usage.get("estimated", False)),
        )
