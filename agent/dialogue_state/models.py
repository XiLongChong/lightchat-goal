"""Dialogue wait state data models."""

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any

WAIT_STATES = frozenset({
    "none",
    "assistant_waiting_user",
    "user_waiting_assistant",
    "topic_closed",
    "paused_by_user",
    "bot_silent",
})


@dataclass
class DialogueState:
    user_id: str = ""
    bot_id: str = ""

    wait_state: str = "none"
    last_state_reason: str = ""

    last_user_message_at: float = 0.0
    last_assistant_message_at: float = 0.0
    last_user_message_excerpt: str = ""
    last_assistant_message_excerpt: str = ""

    silent_started_at: float = 0.0
    silent_reason: str = ""
    user_messages_since_silent: int = 0
    silent_must_reply_after_count: int = 2
    silent_must_reply_after_ts: float = 0.0

    proactive_paused_until: float = 0.0

    updated_at: float = 0.0
    version: int = 1

    def clamp(self) -> None:
        if self.wait_state not in WAIT_STATES:
            self.wait_state = "none"
        self.user_messages_since_silent = max(0, int(self.user_messages_since_silent))
        self.silent_must_reply_after_count = max(0, int(self.silent_must_reply_after_count))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DialogueState":
        return cls(
            user_id=str(data.get("user_id", "")),
            bot_id=str(data.get("bot_id", "")),
            wait_state=str(data.get("wait_state", "none")),
            last_state_reason=str(data.get("last_state_reason", "")),
            last_user_message_at=float(data.get("last_user_message_at", 0)),
            last_assistant_message_at=float(data.get("last_assistant_message_at", 0)),
            last_user_message_excerpt=str(data.get("last_user_message_excerpt", "")),
            last_assistant_message_excerpt=str(data.get("last_assistant_message_excerpt", "")),
            silent_started_at=float(data.get("silent_started_at", 0)),
            silent_reason=str(data.get("silent_reason", "")),
            user_messages_since_silent=int(data.get("user_messages_since_silent", 0)),
            silent_must_reply_after_count=int(data.get("silent_must_reply_after_count", 2)),
            silent_must_reply_after_ts=float(data.get("silent_must_reply_after_ts", 0)),
            proactive_paused_until=float(data.get("proactive_paused_until", 0)),
            updated_at=float(data.get("updated_at", 0)),
            version=int(data.get("version", 1)),
        )

    @classmethod
    def default_for(cls, user_id: str, bot_id: str) -> "DialogueState":
        now = time.time()
        return cls(user_id=user_id, bot_id=bot_id, updated_at=now)


@dataclass
class DialogueStateEvent:
    id: int = 0
    user_id: str = ""
    bot_id: str = ""
    turn_id: int = 0
    old_state_json: str = "{}"
    new_state_json: str = "{}"
    reason: str = ""
    created_at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DialogueStateEvent":
        return cls(
            id=int(data.get("id", 0)),
            user_id=str(data.get("user_id", "")),
            bot_id=str(data.get("bot_id", "")),
            turn_id=int(data.get("turn_id", 0)),
            old_state_json=str(data.get("old_state_json", "{}")),
            new_state_json=str(data.get("new_state_json", "{}")),
            reason=str(data.get("reason", "")),
            created_at=float(data.get("created_at", 0)),
        )


def serialize_state(state: DialogueState) -> str:
    return json.dumps(state.to_dict(), ensure_ascii=False)


def deserialize_state(json_str: str) -> DialogueState:
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        data = {}
    return DialogueState.from_dict(data)
