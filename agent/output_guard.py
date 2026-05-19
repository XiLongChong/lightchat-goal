"""Unified Output Guard — all user-visible text must pass through here.

Per V2 design doc: centralized pipeline that detects protocol leaks, unwraps
nested protocol JSON, prevents empty replies, clamps lengths/counts,
filters sensitive info, and performs final WeChat text cleanup.

All paths that produce user-visible text MUST call guard_messages() or
guard_single_message() before sending.
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Config constants — these are safe defaults; chat_agent.py overrides per-config
MAX_MESSAGE_LEN = 500
MAX_MESSAGE_COUNT = 8
MAX_TOTAL_CHARS = 2000
HARD_FALLBACK_TEXT = "我刚刚卡了一下 重新说"

# Sensitive patterns: concrete secrets only, not normal technical words
_SENSITIVE_PATTERNS: list[re.Pattern] = [
    # API keys (sk- prefix patterns from common providers)
    re.compile(r'sk-[a-zA-Z0-9]{20,}', re.IGNORECASE),
    # Bearer tokens with sk- prefix
    re.compile(r'Bearer\s+sk-[a-zA-Z0-9]{20,}', re.IGNORECASE),
    # Long hex/base64 tokens that look like API credentials (>40 chars)
    re.compile(r'(?:api[_-]?key|secret|token)["\s:=]+[a-zA-Z0-9+/=]{40,}', re.IGNORECASE),
]


def _looks_like_protocol_json(text: str) -> bool:
    """Check if text appears to be leaked internal protocol JSON.

    Detects text containing both "actions" and a protocol marker
    (wechat_reply or "done"), which are internal protocol fields
    that should never appear in user-visible messages.
    """
    sample = text.strip()
    if not sample:
        return False
    lowered = sample.lower()
    if '"actions"' not in lowered and "'actions'" not in lowered:
        return False
    if "wechat_reply" not in lowered and '"done"' not in lowered and "'done'" not in lowered:
        return False
    try:
        parsed = json.loads(sample)
    except (json.JSONDecodeError, TypeError, ValueError):
        return sample.startswith("{") and "actions" in lowered
    return isinstance(parsed, dict) and "actions" in parsed


def _unwrap_nested_protocol(text: str) -> str:
    """Recover natural-language replies when the model double-wraps protocol JSON.

    If the model put a full {"actions": [...], "done": true} payload inside
    a message text field, extract the inner message texts.

    Returns empty string if no natural-language content was found.
    """
    from utils.json_parser import safe_parse_json

    parsed = safe_parse_json(text)
    if not isinstance(parsed, dict):
        return ""

    replies: list[str] = []
    for action in parsed.get("actions", [])[:MAX_MESSAGE_COUNT]:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")
        if action_type == "message":
            candidate = str(action.get("text", "")).strip()
        elif action_type == "tool" and str(action.get("name", "")).strip() == "wechat_reply":
            args = action.get("args", {})
            candidate = str(args.get("text", "")).strip() if isinstance(args, dict) else ""
        else:
            candidate = ""

        if candidate and not _looks_like_protocol_json(candidate):
            replies.append(candidate)

    return "\n".join(replies).strip()


def _filter_sensitive(text: str) -> str:
    """Remove concrete internal secrets from text.

    Only filters actual credential patterns (API keys, tokens).
    Does NOT filter normal technical words like "token", "JSON", "prompt".
    """
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            text = pattern.sub("[redacted]", text)
    return text


def _wechat_cleanup(text: str) -> str:
    """Final WeChat-safe text cleanup: strip markdown, normalize whitespace."""
    # WeChat doesn't render markdown
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)  # bold/italic
    text = re.sub(r'`{1,3}([^`]+)`{1,3}', r'\1', text)     # code
    text = re.sub(r'~{2}([^~]+)~{2}', r'\1', text)          # strikethrough
    # Collapse repeated newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def guard_single_message(text: str, issues: list[str] | None = None) -> str:
    """Run all output guards on a single message text.

    Returns the sanitized text, or hard fallback text if unrecoverable.
    If issues list is provided, diagnostic issue types are appended.

    Pipeline:
    1. Protocol JSON leak detection
    2. Nested protocol unwrap
    3. Empty reply fallback
    4. Length limiter
    5. Sensitive info filter
    6. WeChat format cleaner
    """
    if not text or not str(text).strip():
        return ""

    text = str(text).strip()

    # Step 1-2: Protocol leak detection + nested unwrap
    if _looks_like_protocol_json(text):
        extracted = _unwrap_nested_protocol(text)
        if extracted:
            logger.warning("output_guard: unwrapped nested protocol JSON")
            if issues is not None:
                issues.append("protocol_text_unwrapped")
            text = extracted
        else:
            logger.warning("output_guard: blocked unrecoverable protocol JSON leak")
            if issues is not None:
                issues.append("protocol_text_blocked")
            return HARD_FALLBACK_TEXT

    # Step 3: Empty reply fallback (after unwrap, text might be empty)
    if not text.strip():
        return HARD_FALLBACK_TEXT

    # Step 4: Length limiter
    if len(text) > MAX_MESSAGE_LEN:
        text = text[:MAX_MESSAGE_LEN - 1] + "…"

    # Step 5: Sensitive info filter
    text = _filter_sensitive(text)

    # Step 6: WeChat format cleaner
    text = _wechat_cleanup(text)

    if not text.strip():
        return HARD_FALLBACK_TEXT

    return text


def guard_messages(
    messages: list[dict[str, Any]],
    max_messages: int = MAX_MESSAGE_COUNT,
    max_total_chars: int = MAX_TOTAL_CHARS,
    issues: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Run all output guards on a list of messages.

    Returns sanitized message list. May return fewer messages or a single
    hard-fallback message if all messages were blocked.

    Empty input (e.g. schedule-only turn) passes through as empty list.
    If issues list is provided, diagnostic issue types are appended.
    """
    if not messages:
        return []

    guarded: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        text = guard_single_message(str(m.get("text", "")), issues=issues)
        if text:
            guarded.append({
                "text": text,
                "delay": m.get("delay", 0),
            })

    # Multi-message count limiter
    if len(guarded) > max_messages:
        guarded = guarded[:max_messages]

    # Total character limiter
    total_chars = sum(len(m.get("text", "")) for m in guarded)
    if total_chars > max_total_chars and guarded:
        excess = total_chars - max_total_chars
        last_text = guarded[-1]["text"]
        new_len = max(1, len(last_text) - excess)
        guarded[-1]["text"] = last_text[:new_len].rstrip() + "…"

    if not guarded:
        return [{"text": HARD_FALLBACK_TEXT, "delay": 0}]

    return guarded


# Keep module-level aliases for backward compatibility with chat_agent.py
_sanitize_outbound_text = guard_single_message
_looks_like_protocol_json = _looks_like_protocol_json
_extract_nested_protocol_reply = _unwrap_nested_protocol
