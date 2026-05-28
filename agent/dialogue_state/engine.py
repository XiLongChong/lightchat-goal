"""Dialogue state engine — tracks conversation wait state transitions.

Pure-logic engine. Does NOT touch the database directly.
Callers persist via MemoryStore after the engine returns the updated state + event.
"""

import logging
import time
from typing import Any

from agent.dialogue_state.models import (
    DialogueState, DialogueStateEvent, WAIT_STATES,
    serialize_state,
)

logger = logging.getLogger("goal.dialogue_state")


class DialogueStateEngine:
    """Tracks and transitions dialogue wait states.

    State semantics:
    - assistant_waiting_user: assistant sent a message that needs user reply; assistant is waiting for user
    - user_waiting_assistant: user sent a message; user is waiting for assistant to reply
    - bot_silent: bot deliberately chose not to reply (specific form of user_waiting_assistant)
    - topic_closed: current topic concluded
    - paused_by_user: user explicitly asked bot to pause
    - none: no explicit waiting relationship

    Basic rules:
    - On user message: wait_state -> user_waiting_assistant (user waiting for bot reply)
    - On assistant reply needing user response: wait_state -> assistant_waiting_user
    - On assistant reply NOT needing response: wait_state -> none
    - On topic close: wait_state -> topic_closed
    - On pause: wait_state -> paused_by_user
    - On silent: wait_state -> bot_silent
    """

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.use_as_truth = bool(cfg.get("use_as_waiting_source_of_truth", True))
        self.compat_write_clingy = bool(cfg.get("compatibility_write_clingy_state", True))

        silence_cfg = config.get("silence", {}) if config else {}
        self.silence_enabled = bool(silence_cfg.get("enabled", True))
        self.default_max_silent_messages = int(silence_cfg.get("default_max_silent_messages", 2))
        self.max_silent_minutes = int(silence_cfg.get("max_silent_minutes", 30))
        self.force_reply_on_apology = bool(silence_cfg.get("force_reply_on_apology", True))
        self.force_reply_on_urgent = bool(silence_cfg.get("force_reply_on_urgent", True))
        self.max_silent_messages_by_mood: dict[str, int] = {
            k: int(v) for k, v in
            silence_cfg.get("max_silent_messages_by_mood", {}).items()
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_user_message(
        self,
        state: DialogueState,
        user_text: str,
        *,
        turn_id: int = 0,
        reason: str = "",
    ) -> tuple[DialogueState, DialogueStateEvent | None]:
        """User sent a message. Update timestamps, clear silent state, set wait_state."""
        if not self.enabled:
            return state, None

        old_json = serialize_state(state)
        now = time.time()

        state.last_user_message_at = now
        state.last_user_message_excerpt = user_text[:200]

        # Clear silent state on user message
        if state.wait_state == "bot_silent":
            state.silent_started_at = 0
            state.silent_reason = ""
            state.user_messages_since_silent += 1

        state.wait_state = "user_waiting_assistant"
        state.last_state_reason = reason or "user message"
        state.updated_at = now
        state.version += 1
        state.clamp()

        event = DialogueStateEvent(
            user_id=state.user_id,
            bot_id=state.bot_id,
            turn_id=turn_id,
            old_state_json=old_json,
            new_state_json=serialize_state(state),
            reason=f"user_message: {reason or 'inbound'}",
            created_at=now,
        )

        return state, event

    def record_assistant_message(
        self,
        state: DialogueState,
        assistant_text: str,
        *,
        needs_user_reply: bool = True,
        turn_id: int = 0,
        reason: str = "",
    ) -> tuple[DialogueState, DialogueStateEvent | None]:
        """Assistant sent a reply. Update timestamps, set wait_state.

        Args:
            needs_user_reply: If True, the assistant's message asks a question or
                expects a user response; wait_state becomes assistant_waiting_user.
                If False, the message is a closing remark; wait_state becomes none.
        """
        if not self.enabled:
            return state, None

        old_json = serialize_state(state)
        now = time.time()

        state.last_assistant_message_at = now
        state.last_assistant_message_excerpt = assistant_text[:200]
        if needs_user_reply:
            state.wait_state = "assistant_waiting_user"
        else:
            state.wait_state = "none"
        state.last_state_reason = reason or "assistant reply"
        state.updated_at = now
        state.version += 1
        state.clamp()

        event = DialogueStateEvent(
            user_id=state.user_id,
            bot_id=state.bot_id,
            turn_id=turn_id,
            old_state_json=old_json,
            new_state_json=serialize_state(state),
            reason=f"assistant_message: {reason or 'outbound'}",
            created_at=now,
        )

        return state, event

    def enter_silent(
        self,
        state: DialogueState,
        *,
        reason: str = "",
        max_silent_by_mood: str = "",
    ) -> tuple[DialogueState, DialogueStateEvent | None]:
        """Bot decides to go silent (e.g., too irritated to reply)."""
        if not self.enabled:
            return state, None

        old_json = serialize_state(state)
        now = time.time()

        state.wait_state = "bot_silent"
        state.silent_started_at = now
        state.silent_reason = reason or "bot chose silence"
        state.user_messages_since_silent = 0

        # Determine must_reply count based on mood
        mood_count = self.max_silent_messages_by_mood.get(max_silent_by_mood, 0)
        state.silent_must_reply_after_count = mood_count or self.default_max_silent_messages

        # Set time-based must-reply deadline
        state.silent_must_reply_after_ts = now + (self.max_silent_minutes * 60)

        state.last_state_reason = reason or "entered silent"
        state.updated_at = now
        state.version += 1
        state.clamp()

        event = DialogueStateEvent(
            user_id=state.user_id,
            bot_id=state.bot_id,
            turn_id=0,
            old_state_json=old_json,
            new_state_json=serialize_state(state),
            reason=f"enter_silent: {reason or 'triggered'}",
            created_at=now,
        )

        logger.info("dialogue %s/%s: enter_silent (must_reply_after=%d msgs or %.0f min)",
                     state.user_id, state.bot_id,
                     state.silent_must_reply_after_count, self.max_silent_minutes)

        return state, event

    def check_silent_break(
        self,
        state: DialogueState,
        *,
        user_text: str = "",
        is_apology: bool = False,
        is_urgent: bool = False,
    ) -> tuple[bool, str]:
        """Check whether bot should break silence given a user message.

        Returns (should_break, reason).
        """
        if state.wait_state != "bot_silent":
            return False, "not_silent"

        if self.force_reply_on_apology and is_apology:
            return True, "apology_detected"
        if self.force_reply_on_urgent and is_urgent:
            return True, "urgent_detected"

        count = state.user_messages_since_silent + 1
        if count >= state.silent_must_reply_after_count:
            return True, f"msg_count_reached ({count}/{state.silent_must_reply_after_count})"

        now = time.time()
        if state.silent_must_reply_after_ts > 0 and now >= state.silent_must_reply_after_ts:
            return True, "time_limit_reached"

        return False, "still_silent"

    def exit_silent(
        self,
        state: DialogueState,
        *,
        reason: str = "",
    ) -> tuple[DialogueState, DialogueStateEvent | None]:
        """Exit silent mode."""
        if not self.enabled:
            return state, None

        old_json = serialize_state(state)
        now = time.time()

        state.wait_state = "user_waiting_assistant"
        state.silent_started_at = 0
        state.silent_reason = ""
        state.user_messages_since_silent = 0
        state.silent_must_reply_after_ts = 0
        state.last_state_reason = reason or "exited silent"
        state.updated_at = now
        state.version += 1
        state.clamp()

        event = DialogueStateEvent(
            user_id=state.user_id,
            bot_id=state.bot_id,
            turn_id=0,
            old_state_json=old_json,
            new_state_json=serialize_state(state),
            reason=f"exit_silent: {reason or 'triggered'}",
            created_at=now,
        )

        return state, event

    def mark_topic_closed(
        self,
        state: DialogueState,
        *,
        reason: str = "",
    ) -> tuple[DialogueState, DialogueStateEvent | None]:
        """Mark the current topic as closed."""
        if not self.enabled:
            return state, None

        old_json = serialize_state(state)
        now = time.time()

        state.wait_state = "topic_closed"
        state.last_state_reason = reason or "topic closed"
        state.updated_at = now
        state.version += 1
        state.clamp()

        event = DialogueStateEvent(
            user_id=state.user_id,
            bot_id=state.bot_id,
            turn_id=0,
            old_state_json=old_json,
            new_state_json=serialize_state(state),
            reason=f"topic_closed: {reason or 'triggered'}",
            created_at=now,
        )

        return state, event

    def default_state(self, user_id: str, bot_id: str) -> DialogueState:
        """Return a fresh default state for a new user+bot pair."""
        now = time.time()
        return DialogueState(
            user_id=user_id,
            bot_id=bot_id,
            silent_must_reply_after_count=self.default_max_silent_messages,
            updated_at=now,
        )
