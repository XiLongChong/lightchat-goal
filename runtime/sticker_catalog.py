"""Sticker catalog for AsPocket Phase 1 — agent-sent structured stickers.

Each sticker is represented as an attachment of ``type: "sticker"`` and is
canonicalised against this catalog.  Unknown sticker ids are rejected —
no dirty data is ever persisted.
"""

from __future__ import annotations

from typing import Any

# ── catalog ──────────────────────────────────────────────────────────

STICKER_CATALOG: dict[str, dict[str, str]] = {
    "happy_01": {"stickerId": "happy_01", "alt": "开心"},
    "laugh_01": {"stickerId": "laugh_01", "alt": "大笑"},
    "sad_01":   {"stickerId": "sad_01",   "alt": "委屈"},
    "angry_01": {"stickerId": "angry_01", "alt": "生气"},
    "shy_01":   {"stickerId": "shy_01",   "alt": "害羞"},
    "think_01": {"stickerId": "think_01", "alt": "思考"},
    "ok_01":    {"stickerId": "ok_01",    "alt": "好的"},
    "heart_01": {"stickerId": "heart_01", "alt": "喜欢"},
}

STICKER_IDS: set[str] = set(STICKER_CATALOG.keys())


# ── public helpers ───────────────────────────────────────────────────


def is_known_sticker(sticker_id: str) -> bool:
    """Return True when *sticker_id* is in the built-in catalog."""
    return sticker_id in STICKER_IDS


def canonicalize_sticker_attachment(att: dict[str, Any]) -> dict[str, Any]:
    """Validate and canonicalise a sticker attachment dict.

    Returns the canonical dict ``{"type": "sticker", "stickerId": ..., "alt": ...}``.

    Raises ``ValueError`` with a descriptive message when *att* is not a
    recognised sticker entry.
    """
    sticker_id = str(att.get("stickerId", "")).strip()
    if not sticker_id:
        raise ValueError("sticker attachment requires stickerId")
    entry = STICKER_CATALOG.get(sticker_id)
    if entry is None:
        raise ValueError(
            f"unknown stickerId: {sticker_id!r}. "
            f"Known stickers: {', '.join(sorted(STICKER_IDS))}"
        )
    return {"type": "sticker", "stickerId": entry["stickerId"], "alt": entry["alt"]}
