"""Bot emotion state data models."""

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any

MOOD_VALUES = frozenset({
    "calm", "cold", "warm", "playful",
    "jealous", "lonely", "hurt", "irritated",
    "angry", "softened",
})

RELATIONSHIP_STAGES = frozenset({
    "stranger", "familiar", "close", "attached",
})


@dataclass
class BotEmotionState:
    user_id: str = ""
    bot_id: str = ""

    bot_mood: str = "calm"
    mood_intensity: int = 0
    relationship_stage: str = "stranger"

    affection: int = 10
    trust: int = 10
    irritation: int = 0
    attachment: int = 0
    jealousy: int = 0

    last_emotion_reason: str = ""
    updated_at: float = 0.0
    version: int = 1

    def clamp(self) -> None:
        """Clamp all numeric fields to [0, 100]."""
        for attr in ("affection", "trust", "irritation", "attachment",
                     "jealousy", "mood_intensity"):
            val = max(0, min(100, int(getattr(self, attr) or 0)))
            setattr(self, attr, val)
        if self.bot_mood not in MOOD_VALUES:
            self.bot_mood = "calm"
        if self.relationship_stage not in RELATIONSHIP_STAGES:
            self.relationship_stage = "stranger"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BotEmotionState":
        return cls(
            user_id=str(data.get("user_id", "")),
            bot_id=str(data.get("bot_id", "")),
            bot_mood=str(data.get("bot_mood", "calm")),
            mood_intensity=int(data.get("mood_intensity", 0)),
            relationship_stage=str(data.get("relationship_stage", "stranger")),
            affection=int(data.get("affection", 10)),
            trust=int(data.get("trust", 10)),
            irritation=int(data.get("irritation", 0)),
            attachment=int(data.get("attachment", 0)),
            jealousy=int(data.get("jealousy", 0)),
            last_emotion_reason=str(data.get("last_emotion_reason", "")),
            updated_at=float(data.get("updated_at", 0)),
            version=int(data.get("version", 1)),
        )

    @classmethod
    def default_for(cls, user_id: str, bot_id: str) -> "BotEmotionState":
        now = time.time()
        return cls(user_id=user_id, bot_id=bot_id, updated_at=now)


@dataclass
class EmotionEvent:
    id: int = 0
    user_id: str = ""
    bot_id: str = ""
    turn_id: int = 0
    source: str = ""
    trigger_type: str = ""
    event_type: str = ""
    old_state_json: str = "{}"
    new_state_json: str = "{}"
    reason: str = ""
    confidence: float = 0.0
    created_at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmotionEvent":
        return cls(
            id=int(data.get("id", 0)),
            user_id=str(data.get("user_id", "")),
            bot_id=str(data.get("bot_id", "")),
            turn_id=int(data.get("turn_id", 0)),
            source=str(data.get("source", "")),
            trigger_type=str(data.get("trigger_type", "")),
            event_type=str(data.get("event_type", "")),
            old_state_json=str(data.get("old_state_json", "{}")),
            new_state_json=str(data.get("new_state_json", "{}")),
            reason=str(data.get("reason", "")),
            confidence=float(data.get("confidence", 0.0)),
            created_at=float(data.get("created_at", 0.0)),
        )


def serialize_state(state: BotEmotionState) -> str:
    return json.dumps(state.to_dict(), ensure_ascii=False)


def deserialize_state(json_str: str) -> BotEmotionState:
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        data = {}
    return BotEmotionState.from_dict(data)
