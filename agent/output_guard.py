"""Sanitize text before it is sent to WeChat."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 500
MAX_MESSAGE_COUNT = 8
MAX_TOTAL_CHARS = 2000
HARD_FALLBACK_TEXT = "我刚刚卡了一下，重新说。"

_TOOL_CALL_ID_PATTERN = re.compile(r"\bcall_[A-Za-z0-9_-]{8,}\b")
_SENSITIVE_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9]{20,}", re.IGNORECASE),
    re.compile(r"Bearer\s+sk-[a-zA-Z0-9]{20,}", re.IGNORECASE),
    re.compile(
        r'(?:api[_-]?key|secret|token)["\s:=]+[a-zA-Z0-9+/=]{40,}',
        re.IGNORECASE,
    ),
]


def _filter_sensitive(text: str) -> str:
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _wechat_cleanup(text: str) -> str:
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"`{1,3}([^`]+)`{1,3}", r"\1", text)
    text = re.sub(r"~{2}([^~]+)~{2}", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def guard_single_message(text: str, issues: list[str] | None = None) -> str:
    if not text or not str(text).strip():
        return ""

    cleaned = str(text).strip()
    if _TOOL_CALL_ID_PATTERN.search(cleaned):
        logger.warning("output_guard: blocked tool call id leak")
        if issues is not None:
            issues.append("tool_call_id_blocked")
        return HARD_FALLBACK_TEXT

    if len(cleaned) > MAX_MESSAGE_LEN:
        cleaned = cleaned[: MAX_MESSAGE_LEN - 1] + "…"

    cleaned = _wechat_cleanup(_filter_sensitive(cleaned))
    return cleaned or HARD_FALLBACK_TEXT


def guard_messages(
    messages: list[dict[str, Any]],
    max_messages: int = MAX_MESSAGE_COUNT,
    max_total_chars: int = MAX_TOTAL_CHARS,
    issues: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not messages:
        return []

    guarded: list[dict[str, Any]] = []
    for message in messages[:max_messages]:
        if not isinstance(message, dict):
            continue
        text = guard_single_message(str(message.get("text", "")), issues=issues)
        if text:
            guarded.append({"text": text, "delay": message.get("delay", 0)})

    if not guarded:
        return [{"text": HARD_FALLBACK_TEXT, "delay": 0}]

    total_chars = sum(len(message["text"]) for message in guarded)
    if total_chars > max_total_chars:
        excess = total_chars - max_total_chars
        last = guarded[-1]["text"]
        guarded[-1]["text"] = last[: max(1, len(last) - excess)].rstrip() + "…"

    return guarded
