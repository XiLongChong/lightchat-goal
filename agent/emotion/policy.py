"""Policy decisions derived from bot emotion state.

These are pure functions that answer "should the bot do X?" given a state.
Does NOT mutate state — only reads it.
"""

from agent.emotion.models import BotEmotionState


def should_be_cold(state: BotEmotionState, threshold: int = 50) -> bool:
    """Bot should respond coldly when highly irritated or angry."""
    return state.irritation >= threshold or state.bot_mood == "angry"


def should_use_jealous_phrases(state: BotEmotionState) -> bool:
    return state.bot_mood == "jealous" and state.jealousy >= 20


def should_express_lonely(state: BotEmotionState) -> bool:
    return state.bot_mood in ("lonely", "hurt")


def should_soften(state: BotEmotionState) -> bool:
    """Bot is cooling down from a negative mood — deserves gentler tone."""
    return state.bot_mood == "softened"


def is_negative_mood(state: BotEmotionState) -> bool:
    return state.bot_mood in ("angry", "irritated", "jealous", "hurt", "lonely")


def is_positive_mood(state: BotEmotionState) -> bool:
    return state.bot_mood in ("warm", "playful", "calm", "softened")


def mood_expression_level(state: BotEmotionState) -> str:
    """Return a coarse expression level for prompt injection: cold / neutral / warm."""
    if should_be_cold(state, threshold=45):
        return "cold"
    if state.bot_mood in ("warm", "playful"):
        return "warm"
    return "neutral"


def can_proactively_reach_out(state: BotEmotionState) -> bool:
    """Even if irritated, bot can still reach out — just with adjusted tone."""
    if state.bot_mood == "angry" and state.irritation >= 70:
        return False  # too angry, let user come back
    return True
