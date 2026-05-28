"""Text classifier for detecting emotion trigger types from user messages.

Regex-based, no LLM dependency. Returns structured detection results.
"""

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DetectionResult:
    """Result of classifying a user message for emotion triggers."""
    trigger_type: str = ""           # "offense" / "comparison" / "apology" / "explanation" / "positive" / "silence" / ""
    confidence: float = 0.0
    detail: str = ""                 # short description of what matched
    intensity_modifier: float = 1.0  # 0.5..1.5 multiplier on rule deltas


# ---- Offense patterns ----

_OFFENSE_PATTERNS: list[tuple[str, str, float]] = [
    # (regex pattern, detail, intensity)
    (r"傻[逼比屄]|sb|傻逼", "直接辱骂", 1.4),
    (r"废物|没用|垃圾|辣鸡", "贬低 bot 能力", 1.3),
    (r"你有[病猫]|你脑子|你没脑子|你懂什么", "攻击 bot 智力", 1.3),
    (r"烦[死了人]|好烦|别[烦吵]我|闭嘴|滚[开蛋]?", "表达厌烦/驱逐", 1.2),
    (r"你[怎么都不会]|你不行|你太[差烂菜]了", "否定 bot 能力", 1.1),
    (r"不想[和跟]你[说聊]|不聊了|别[回说]了|离我远[点些]", "拒绝交互", 1.0),
    (r"你[好很真]?烦|烦你|讨厌你", "表达讨厌", 1.1),
    (r"恶心|无语[了死了]?|真?受不了", "表达强烈不满", 1.2),
    (r"你[在就]?这[个样]?[有就]?[毛屁用]?", "否定 bot 价值", 1.2),
]


def _score_patterns(text: str, patterns: list[tuple[str, str, float]]) -> tuple[float, str, float]:
    """Return (max_confidence, combined_detail, max_intensity) from patterns that match."""
    best_conf = 0.0
    best_intensity = 1.0
    details: list[str] = []
    for pat, detail, intensity in patterns:
        if re.search(pat, text, re.IGNORECASE):
            conf = min(0.95, intensity * 0.6)
            if conf > best_conf:
                best_conf = conf
                best_intensity = intensity
            details.append(detail)
    return best_conf, "; ".join(details) if details else "", best_intensity


# ---- Comparison / jealousy patterns ----

_COMPARISON_PATTERNS: list[tuple[str, str, float]] = [
    (r"CC|cc", "提到 CC", 0.9),
    (r"CC[比就]?比你好|CC[比就]?比你[强厉害]", "CC 比 bot 好", 1.3),
    (r"还?不如CC|还是CC[好强厉害]|CC更[好强厉害]", "觉得 CC 更好", 1.3),
    (r"CC[才就]?[是会能]|有?CC[就在就]?[好行可以]", "偏好 CC", 1.2),
    (r"忽略[你了我]|没[看理睬][你我]|不[想]?[理睬回][你我]", "忽略 bot", 1.1),
    (r"刚才?[在和跟]CC|CC刚才?[来在了]", "注意力在 CC 身上", 1.1),
    (r"你[和跟]CC[比]?|你[哪有]?CC[那这]?么|CC[和跟]你", "比较 bot 和 CC", 1.2),
    (r"不重要|无所谓|随便[你我他]|不[在关]乎", "贬低 bot 重要性", 0.9),
]


# ---- Apology / soothing patterns ----

_APOLOGY_PATTERNS: list[tuple[str, str, float]] = [
    (r"对不起|抱歉|不好意思|我的错|我错了|是我?不对", "直接道歉", 1.3),
    (r"乖[了啦]|别生[气闷]|[别不要]?生气[了啦哦]|不气[了啦]", "哄 bot 不生气", 1.3),
    (r"宝贝|宝宝|亲爱的|乖乖|小可爱|傻瓜|笨蛋", "亲昵称呼", 1.0),
    (r"回来[吧啦]?|在[吗哪](?![a-zA-Z一-鿿])|理我[一下]?|说句话[吧啦]?", "呼唤 bot 回来", 1.1),
    (r"[好真很]喜欢[你我]|[好真很]爱[你我]|想[你我]了", "表达喜爱/想念", 1.2),
    (r"以后[不不会]?了|我[会要]?改|我保证|我发誓", "承诺改变", 1.1),
    (r"[好真很]?重[要视][你我]|[没少不]?了[你我]不行", "强调 bot 重要性", 1.1),
    (r"哄[你我了]?|来[哄陪][你我]|陪[你我][一下会]?", "主动示好", 1.0),
]


# ---- Explanation patterns ----

_EXPLANATION_PATTERNS: list[tuple[str, str, float]] = [
    (r"因为|所以|刚[才好在]|之前|那段?时间", "给出原因解释", 0.95),
    (r"不是故[意愿]|没注[意到]|没看[见到]|忘[记了]", "解释疏忽", 1.05),
    (r"在忙|有[事点]?忙|工作|开会|加班|考试|复习", "解释忙/有事", 0.95),
    (r"睡[了着]|刚[醒起]|才[醒起]|太困|困[死]?了", "解释睡觉/困", 0.95),
    (r"手机没[电了]|没看手机|手机[关了静音]|不在[身边手]", "解释设备原因", 0.85),
]


# ---- Positive interaction patterns ----

_POSITIVE_PATTERNS: list[tuple[str, str, float]] = [
    (r"谢谢|多谢|感谢|thank", "表达感谢", 0.7),
    (r"[好真很]?[棒厉害牛强]|[好真很]?不错|[好真很]?靠谱", "夸奖 bot", 1.0),
    (r"哈哈|嘿嘿|嘻嘻|笑[死了]|好笑|逗", "被 bot 逗笑", 0.8),
    (r"有?道理|说得对|没错|确实|[是就]这样", "认同 bot", 0.7),
    (r"继续|然后[呢]?|接下[来去]?|还[有呢]?", "主动推进对话", 0.6),
    (r"跟[你我]说[说下]?|聊聊|唠[嗑会]?", "主动邀请聊天", 0.7),
    (r"今天|[今这]几天|最近|分享[一下]?", "分享日常", 0.6),
    (r"[好真很]?想你|想跟[你我]聊|只想[和跟][你我]", "表达想念 bot", 0.9),
]


def classify_user_message(text: str) -> DetectionResult:
    """Classify a user message for emotion-relevant trigger types.

    Returns the highest-confidence match across all categories.
    Checks in priority order: offense > comparison > apology > explanation > positive.
    """
    if not text or not text.strip():
        return DetectionResult()

    text = text.strip()

    # 1. Offense check (highest priority)
    conf, detail, intensity = _score_patterns(text, _OFFENSE_PATTERNS)
    if conf > 0.5:
        return DetectionResult(
            trigger_type="offense", confidence=conf,
            detail=f"offense: {detail}", intensity_modifier=intensity,
        )

    # 2. Comparison / jealousy check
    conf, detail, intensity = _score_patterns(text, _COMPARISON_PATTERNS)
    if conf > 0.5:
        return DetectionResult(
            trigger_type="comparison", confidence=conf,
            detail=f"comparison: {detail}", intensity_modifier=intensity,
        )

    # 3. Apology / soothing check
    conf, detail, intensity = _score_patterns(text, _APOLOGY_PATTERNS)
    if conf > 0.5:
        return DetectionResult(
            trigger_type="apology", confidence=conf,
            detail=f"apology: {detail}", intensity_modifier=intensity,
        )

    # 4. Explanation check
    conf, detail, intensity = _score_patterns(text, _EXPLANATION_PATTERNS)
    if conf > 0.45:
        return DetectionResult(
            trigger_type="explanation", confidence=conf,
            detail=f"explanation: {detail}", intensity_modifier=intensity,
        )

    # 5. Positive interaction check
    conf, detail, intensity = _score_patterns(text, _POSITIVE_PATTERNS)
    if conf > 0.4:
        return DetectionResult(
            trigger_type="positive", confidence=conf,
            detail=f"positive: {detail}", intensity_modifier=intensity,
        )

    return DetectionResult()
