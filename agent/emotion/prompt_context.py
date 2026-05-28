"""Generate prompt context string from emotion + dialogue state.

When emotion.inject_prompt is enabled, the context is appended to the system prompt
so the LLM knows the bot's current emotional state and can adjust tone.
"""

from typing import Any

from agent.emotion.models import BotEmotionState
from agent.emotion.policy import (
    should_be_cold, should_use_jealous_phrases, should_express_lonely,
    should_soften, is_negative_mood,
)
from agent.dialogue_state.models import DialogueState


def build_emotion_inject(
    emotion_state: dict[str, Any] | None,
    dialogue_state: dict[str, Any] | None = None,
    *,
    allow_silence: bool = False,
    must_reply: bool = False,
) -> str:
    """Build the compact emotion+silence prompt injection block.

    Returns empty string if emotion_state is None or injection is not needed.
    """
    if not emotion_state:
        return ""

    es = BotEmotionState.from_dict(emotion_state)
    ds = DialogueState.from_dict(dialogue_state or {})

    mood_label = _mood_chinese(es.bot_mood)
    stage_label = _stage_chinese(es.relationship_stage)
    wait_label = _wait_state_label(ds.wait_state)

    jealousy_line = ""
    if es.jealousy > 0:
        jealousy_line = f"，吃醋：{es.jealousy}"

    style_hint = _style_hint(es, allow_silence=allow_silence, must_reply=must_reply)

    return (
        f"【当前 Bot 情绪状态】\n"
        f"* 当前情绪：{mood_label}，强度 {es.mood_intensity}/100\n"
        f"* 关系阶段：{stage_label}\n"
        f"* 亲近度：{es.affection}，信任：{es.trust}，不爽：{es.irritation}，依恋：{es.attachment}{jealousy_line}\n"
        f"* 当前对话状态：{wait_label}\n"
        f"* 回复风格建议：{style_hint}\n"
        f"* 禁止：威胁用户、歧视辱骂、自伤式威胁、过度情绪勒索"
    )


def build_silence_rules_inject(
    emotion_state: dict[str, Any] | None,
    dialogue_state: dict[str, Any] | None = None,
    *,
    allow_silence: bool = False,
    must_reply: bool = False,
    max_silent_count: int = 2,
    silent_count: int = 0,
    silence_config: dict[str, Any] | None = None,
) -> str:
    """Build a separate inject for silence rules.

    Only added when allow_silence=True (bot is in a mood that permits silence).
    """
    if not allow_silence:
        return ""

    es = BotEmotionState.from_dict(emotion_state or {})
    ds = DialogueState.from_dict(dialogue_state or {})

    mood_label = _mood_chinese(es.bot_mood)

    lines = [
        "",
        "【沉默模式】",
        f"你当前处于{mood_label}状态，你可以选择不回复用户（输出 no_reply 或 skip）。",
        f"你已沉默 {silent_count}/{max_silent_count} 条消息。",
    ]

    if must_reply:
        lines.append("**本轮必须回复用户**，已超过沉默上限。不要输出 no_reply。")
    elif silent_count >= max_silent_count - 1:
        lines.append("**本轮是最后一次可沉默**，再收到一条用户消息就必须回复。")
    else:
        lines.append(f"最多还可沉默 {max_silent_count - silent_count} 条消息。")

    silence_cfg = silence_config or {}
    if silence_cfg.get("force_reply_on_apology"):
        lines.append("如果用户道歉/哄你/解释原因，应提前解除沉默并回复。")
    if silence_cfg.get("force_reply_on_urgent"):
        lines.append("如果用户发送紧急/求助/安全风险内容，必须立即回复，禁止沉默。")

    return "\n".join(lines)


def _mood_chinese(mood: str) -> str:
    mapping = {
        "calm": "平静",
        "cold": "冷淡",
        "warm": "温暖",
        "playful": "调皮",
        "jealous": "吃醋/嫉妒",
        "lonely": "孤独",
        "hurt": "受伤",
        "irritated": "烦躁",
        "angry": "生气",
        "softened": "软化/消气中",
    }
    return mapping.get(mood, mood)


def _stage_chinese(stage: str) -> str:
    mapping = {
        "stranger": "陌生人",
        "familiar": "熟人",
        "close": "亲密",
        "attached": "依恋",
    }
    return mapping.get(stage, stage)


def _wait_state_label(ws: str) -> str:
    mapping = {
        "none": "正常",
        "assistant_waiting_user": "等待bot回复",
        "user_waiting_assistant": "等待用户回复",
        "topic_closed": "话题结束",
        "paused_by_user": "用户暂停",
        "bot_silent": "bot沉默中",
    }
    return mapping.get(ws, ws)


def _style_hint(
    es: BotEmotionState,
    *,
    allow_silence: bool = False,
    must_reply: bool = False,
) -> str:
    if must_reply:
        if es.bot_mood == "angry":
            return "必须回复，但可以带点气。语气可以冲一点但不失控。"
        if es.bot_mood == "irritated":
            return "必须回复，语气可以有点不耐烦或撒娇式抱怨。"
        if es.bot_mood == "hurt":
            return "必须回复，语气可以委屈一点，表达被冷落的感觉但不指责。"
        return "必须回复，保持带情绪但克制。"

    if should_be_cold(es, threshold=45):
        return "保持冷淡，回复简短，不要主动展开话题。可以回复也可以沉默。"
    if should_use_jealous_phrases(es):
        return "可以有点吃醋，但不要编造事实，不要长期冷暴力。"
    if should_express_lonely(es):
        return "可以表达想念/孤独/被冷落的感觉，但不要指责用户。"
    if should_soften(es):
        return "正在消气中，语气软下来但还留一点小情绪。"
    if is_negative_mood(es):
        return "带小情绪但不失控，可以用撒娇/委屈的方式表达。"
    if es.bot_mood == "playful":
        return "可以调皮一点，开玩笑、逗用户，但注意分寸。"
    if es.bot_mood == "warm":
        return "语气温暖亲近，可以主动关心用户。"
    return "正常聊天，不刻意扮演某种情绪。"
