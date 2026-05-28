"""Bot emotion engine — applies emotion rules based on classified triggers.

Core rules:
  1. User offense / insults → irritation up, mood -> irritated/angry
  2. User comparison (CC > bot) → jealousy up, mood -> jealous
  3. User silence after proactive → mood -> lonely/hurt
  4. User explanation → irritation down, mood softens
  5. User apology / soothing → irritation way down, affection up, mood softened/warm
  6. Positive chat → affection up, trust up, stage upgrade check
"""

import logging
import time
from typing import Any

from agent.emotion.models import (
    BotEmotionState, EmotionEvent, MOOD_VALUES, RELATIONSHIP_STAGES,
    serialize_state,
)
from agent.emotion.classifier import DetectionResult, classify_user_message

logger = logging.getLogger("goal.emotion")

# Relationship stage thresholds: (min_affection, min_trust)
STAGE_THRESHOLDS = [
    ("stranger", 0, 0),
    ("familiar", 25, 20),
    ("close", 55, 50),
    ("attached", 75, 70),
]


class EmotionEngine:
    """Applies emotion rule deltas to BotEmotionState.

    Does NOT touch the database directly — callers provide state and persist
    via MemoryStore after the engine returns the updated state + event.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.log_events = bool(cfg.get("log_events", True))

        default_profile = cfg.get("default_profile", {})
        self.initial_affection = int(default_profile.get("initial_affection", 10))
        self.initial_trust = int(default_profile.get("initial_trust", 10))
        self.initial_attachment = int(default_profile.get("initial_attachment", 5))
        self.baseline_mood = str(default_profile.get("baseline_mood", "cold"))
        self.jealousy_enabled = bool(default_profile.get("jealousy_enabled", True))

        decay_cfg = cfg.get("decay", {})
        self.decay_enabled = bool(decay_cfg.get("enabled", True))
        self.decay_halflives: dict[str, float] = {
            "irritation": float(decay_cfg.get("irritation_half_life_minutes", 180)),
            "jealousy": float(decay_cfg.get("jealousy_half_life_minutes", 240)),
            "lonely": float(decay_cfg.get("lonely_half_life_minutes", 360)),
            "hurt": float(decay_cfg.get("hurt_half_life_minutes", 240)),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply_trigger(
        self,
        state: BotEmotionState,
        user_text: str,
        *,
        turn_id: int = 0,
        source: str = "",
        trigger_type: str = "",
    ) -> tuple[BotEmotionState, EmotionEvent | None]:
        """Classify user_text and apply the matching emotion rule.

        Returns (updated_state, event_or_none). The caller persists both.
        """
        if not self.enabled:
            return state, None

        detection = classify_user_message(user_text)
        if not detection.trigger_type:
            # No emotion-relevant content detected — do silent decay only
            self._apply_decay(state)
            return state, None

        old_json = serialize_state(state)
        old_state = BotEmotionState.from_dict(state.to_dict())

        self._dispatch_rule(state, detection)

        state.clamp()
        self._recalc_stage(state)

        event = EmotionEvent(
            user_id=state.user_id,
            bot_id=state.bot_id,
            turn_id=turn_id,
            source=source,
            trigger_type=trigger_type or detection.trigger_type,
            event_type=detection.trigger_type,
            old_state_json=old_json,
            new_state_json=serialize_state(state),
            reason=detection.detail,
            confidence=detection.confidence,
            created_at=time.time(),
        )

        if self.log_events:
            logger.info(
                "emotion %s/%s: %s (conf=%.2f) → mood=%s irr=%d aff=%d",
                state.user_id, state.bot_id,
                detection.trigger_type, detection.confidence,
                state.bot_mood, state.irritation, state.affection,
            )

        return state, event

    def apply_silence_trigger(
        self,
        state: BotEmotionState,
        *,
        turn_id: int = 0,
        silence_duration_minutes: float = 0,
    ) -> tuple[BotEmotionState, EmotionEvent | None]:
        """Apply 'user silence after proactive message' rule.

        Call this from the scheduler or clingy layer when a silent gap is detected.
        """
        if not self.enabled:
            return state, None

        old_json = serialize_state(state)

        # Rule 3: silence → lonely / hurt
        state.attachment = min(100, state.attachment + 5)
        state.irritation = min(100, state.irritation + self._rand(5, 15))

        silence_hours = silence_duration_minutes / 60.0 if silence_duration_minutes else 1.0
        if silence_hours >= 6:
            state.bot_mood = "hurt"
            state.mood_intensity = min(100, state.mood_intensity + 15)
            detail = "长时间沉默(>=6h) → hurt"
        else:
            state.bot_mood = "lonely"
            state.mood_intensity = min(100, state.mood_intensity + 8)
            detail = "用户沉默 → lonely"

        state.last_emotion_reason = detail
        state.clamp()
        self._recalc_stage(state)

        event = EmotionEvent(
            user_id=state.user_id,
            bot_id=state.bot_id,
            turn_id=turn_id,
            source="proactive",
            trigger_type="silence",
            event_type="silence",
            old_state_json=old_json,
            new_state_json=serialize_state(state),
            reason=detail,
            confidence=0.70,
            created_at=time.time(),
        )

        return state, event

    def apply_decay_only(self, state: BotEmotionState) -> BotEmotionState:
        """Apply time-based decay without any trigger. Returns same object mutated."""
        self._apply_decay(state)
        state.clamp()
        return state

    def default_state(self, user_id: str, bot_id: str) -> BotEmotionState:
        """Return a fresh default state for a new user+bot pair."""
        now = time.time()
        return BotEmotionState(
            user_id=user_id,
            bot_id=bot_id,
            bot_mood=self.baseline_mood,
            affection=self.initial_affection,
            trust=self.initial_trust,
            attachment=self.initial_attachment,
            updated_at=now,
        )

    # ------------------------------------------------------------------
    # Rule dispatch
    # ------------------------------------------------------------------

    def _dispatch_rule(self, state: BotEmotionState, det: DetectionResult) -> None:
        mod = det.intensity_modifier

        if det.trigger_type == "offense":
            self._rule_offense(state, mod)
        elif det.trigger_type == "comparison":
            self._rule_comparison(state, mod)
        elif det.trigger_type == "apology":
            self._rule_apology(state, mod)
        elif det.trigger_type == "explanation":
            self._rule_explanation(state, mod)
        elif det.trigger_type == "positive":
            self._rule_positive(state, mod)

    # ------------------------------------------------------------------
    # Individual rules
    # ------------------------------------------------------------------

    def _rule_offense(self, state: BotEmotionState, mod: float) -> None:
        delta = self._rand_delta(30, 50, mod)
        state.irritation = min(100, state.irritation + delta)
        state.trust = max(0, state.trust - self._rand_delta(2, 8, mod))
        state.mood_intensity = min(100, state.mood_intensity + self._rand_delta(10, 25, mod))

        if state.irritation >= 60:
            state.bot_mood = "angry"
        else:
            state.bot_mood = "irritated"

        state.last_emotion_reason = f"用户冒犯 irrit+{delta}"

    def _rule_comparison(self, state: BotEmotionState, mod: float) -> None:
        if not self.jealousy_enabled:
            return

        state.jealousy = min(100, state.jealousy + self._rand_delta(15, 25, mod))
        state.irritation = min(100, state.irritation + self._rand_delta(8, 12, mod))
        state.bot_mood = "jealous"
        state.mood_intensity = min(100, state.mood_intensity + self._rand_delta(8, 18, mod))
        state.attachment = min(100, state.attachment + self._rand_delta(1, 5, mod))
        state.last_emotion_reason = f"比较/忽略 jealousy+"

    def _rule_apology(self, state: BotEmotionState, mod: float) -> None:
        delta = self._rand_delta(25, 40, mod)
        state.irritation = max(0, state.irritation - delta)
        state.affection = min(100, state.affection + self._rand_delta(8, 12, mod))
        state.trust = min(100, state.trust + self._rand_delta(3, 7, mod))

        if state.bot_mood in ("angry", "irritated"):
            state.bot_mood = "softened"
        else:
            state.bot_mood = "warm"

        state.mood_intensity = max(0, state.mood_intensity - self._rand_delta(10, 25, mod))
        state.last_emotion_reason = f"道歉/哄 irrit-{delta} aff+"

    def _rule_explanation(self, state: BotEmotionState, mod: float) -> None:
        delta = self._rand_delta(10, 20, mod)
        state.irritation = max(0, state.irritation - delta)
        state.trust = min(100, state.trust + self._rand_delta(1, 4, mod))

        if state.bot_mood == "angry":
            state.bot_mood = "irritated"
        elif state.bot_mood == "irritated":
            state.bot_mood = "softened"

        state.mood_intensity = max(0, state.mood_intensity - self._rand_delta(5, 15, mod))
        state.last_emotion_reason = f"解释 irrit-{delta}"

    def _rule_positive(self, state: BotEmotionState, mod: float) -> None:
        state.affection = min(100, state.affection + self._rand_delta(1, 3, mod))
        state.trust = min(100, state.trust + self._rand_delta(0, 2, mod))
        state.last_emotion_reason = "正向互动"

    # ------------------------------------------------------------------
    # Decay
    # ------------------------------------------------------------------

    def _apply_decay(self, state: BotEmotionState) -> None:
        if not self.decay_enabled:
            return

        now = time.time()
        elapsed_minutes = (now - state.updated_at) / 60.0
        if elapsed_minutes <= 0:
            return

        state.irritation = self._decay_value(
            state.irritation, elapsed_minutes,
            self.decay_halflives["irritation"],
        )
        state.jealousy = self._decay_value(
            state.jealousy, elapsed_minutes,
            self.decay_halflives["jealousy"],
        )

        # If irritation and jealousy are both low, and mood is negative, nudge toward baseline
        if state.irritation < 10 and state.jealousy < 10:
            if state.bot_mood in ("irritated", "angry", "jealous", "hurt", "lonely"):
                if state.bot_mood in ("angry", "irritated"):
                    state.bot_mood = "softened"
                elif state.bot_mood in ("jealous", "lonely", "hurt"):
                    state.bot_mood = "calm"
                state.mood_intensity = max(0, state.mood_intensity - 5)

        state.updated_at = now

    @staticmethod
    def _decay_value(current: int, elapsed_minutes: float, half_life_minutes: float) -> int:
        """Exponential decay: value decays by half every half_life_minutes."""
        if half_life_minutes <= 0 or current <= 0:
            return current
        factor = 0.5 ** (elapsed_minutes / half_life_minutes)
        return max(0, int(round(current * factor)))

    # ------------------------------------------------------------------
    # Stage recalculation
    # ------------------------------------------------------------------

    def _recalc_stage(self, state: BotEmotionState) -> None:
        """Recalculate relationship_stage based on affection + trust thresholds."""
        best = "stranger"
        for name, min_aff, min_trust in STAGE_THRESHOLDS:
            if state.affection >= min_aff and state.trust >= min_trust:
                best = name
        state.relationship_stage = best

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _rand(lo: int, hi: int) -> int:
        import random
        return random.randint(lo, hi)

    @staticmethod
    def _rand_delta(lo: int, hi: int, mod: float = 1.0) -> int:
        import random
        raw = random.randint(lo, hi)
        return max(1, int(round(raw * mod)))
