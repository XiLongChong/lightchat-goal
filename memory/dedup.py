"""CJK bigram deduplication — ported from emoRebot memory-dedup.ts.

Uses character bigram Jaccard similarity for Chinese text dedup.
This is fast, private, and requires no external services.
"""

from typing import Any


def _bigrams(text: str) -> set[str]:
    """Extract consecutive character bigrams from text.

    Works for CJK characters (each char is a meaningful unit)
    and also handles spaces/ASCII naturally.
    """
    text = text.strip()
    if len(text) < 2:
        return {text} if text else set()

    bigrams = set()
    for i in range(len(text) - 1):
        bigrams.add(text[i : i + 2])
    return bigrams


def jaccard_similarity(text_a: str, text_b: str) -> float:
    """Compute Jaccard similarity between two texts using character bigrams.

    Returns 0.0–1.0 where 1.0 means identical bigram sets.
    """
    a = _bigrams(text_a)
    b = _bigrams(text_b)

    if not a or not b:
        return 0.0

    intersection = len(a & b)
    union = len(a | b)

    if union == 0:
        return 0.0

    return intersection / union


def is_duplicate(
    new_content: str,
    existing_memories: list[dict[str, Any]],
    threshold: float = 0.75,
) -> bool:
    """Check if new content is too similar to any existing memory.

    Returns True if any existing memory exceeds the similarity threshold.
    """
    if not new_content or not existing_memories:
        return False

    for mem in existing_memories:
        existing_content = mem.get("content", "")
        if not existing_content:
            continue

        sim = jaccard_similarity(new_content, existing_content)

        # Tag-based bonus only for space-separated languages (English etc).
        # For CJK text (no spaces), split() returns the whole text as one
        # token, causing destructive blending. Skip when tokens < 3.
        new_tokens = new_content.split()
        old_tokens = existing_content.split()
        if len(new_tokens) >= 3 and len(old_tokens) >= 3:
            new_tags = set(new_tokens)
            old_tags = set(old_tokens)
            tag_intersection = len(new_tags & old_tags)
            tag_union = len(new_tags | old_tags)
            if tag_union > 0:
                tag_sim = tag_intersection / tag_union
                sim = sim * 0.7 + tag_sim * 0.3

        if sim >= threshold:
            return True

    return False


def compute_importance(
    content: str,
    existing_count: int,
    days_since_created: float = 0,
    decay_week: float = 0.95,
    decay_month: float = 0.90,
) -> float:
    """Compute importance score for a memory entry.

    Base score derived from content signals, then decayed by age.

    Content signals:
    - Contains preference keywords ("喜欢/不喜欢/讨厌/爱/想") → +0.2
    - Contains factual info keywords ("在/是/有/工作/住") → +0.15
    - Too short (< 10 chars) → -0.1
    - Mentions a person/name → +0.1
    - Repeated topic (existing_count > 0) → increases

    Then apply time decay:
    - Every 7 days: multiply by decay_week (0.95)
    - Every 30 days: multiply by decay_month (0.90)
    """
    base = 0.5

    # Preference signals
    preference_keywords = ["喜欢", "不喜欢", "讨厌", "爱", "想", "觉得",
                           "希望", "想要", "习惯", "经常"]
    for kw in preference_keywords:
        if kw in content:
            base += 0.20
            break

    # Factual info signals
    fact_keywords = ["在", "是", "工作", "住", "家", "公司", "学校",
                     "生日", "名字", "叫"]
    for kw in fact_keywords:
        if kw in content:
            base += 0.15
            break

    # Length penalty (too short = likely not substantive)
    if len(content) < 10:
        base -= 0.10

    # Name mention bonus
    if "他" in content or "她" in content:
        base += 0.05

    # Topic repetition: slightly increase importance for recurring topics
    if existing_count > 0:
        base += min(existing_count * 0.02, 0.10)

    # Clamp base
    base = max(0.1, min(0.95, base))

    # Time decay
    if days_since_created > 0:
        if days_since_created > 30:
            base *= decay_month
        elif days_since_created > 7:
            base *= decay_week

    return round(base, 4)
