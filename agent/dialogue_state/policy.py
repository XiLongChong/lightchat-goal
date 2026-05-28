"""Policy decisions derived from dialogue wait state.

Pure functions — no side effects.
"""

from agent.dialogue_state.models import DialogueState


def is_waiting_for_user(state: DialogueState) -> bool:
    """Bot sent the last message and is waiting for user to respond."""
    return state.wait_state == "assistant_waiting_user"


def is_waiting_for_assistant(state: DialogueState) -> bool:
    """User sent the last message and bot should reply."""
    return state.wait_state == "user_waiting_assistant"


def is_bot_silent(state: DialogueState) -> bool:
    return state.wait_state == "bot_silent"


def is_topic_closed(state: DialogueState) -> bool:
    return state.wait_state == "topic_closed"


def is_paused(state: DialogueState) -> bool:
    return state.wait_state == "paused_by_user"


def can_send_proactive(state: DialogueState) -> bool:
    """Whether proactive messages are allowed given current dialogue state.

    Both bot_silent and user_waiting_assistant block proactive (enforced in scheduler.py).
    """
    if state.wait_state == "paused_by_user":
        return False
    if state.proactive_paused_until > 0:
        import time
        if time.time() < state.proactive_paused_until:
            return False
    return True


def should_skip_reply(state: DialogueState) -> bool:
    """Under what conditions bot should not reply to user."""
    return state.wait_state in ("paused_by_user", "topic_closed")
