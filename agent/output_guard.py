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

    # Step 0: Hard block tool_call_id leaks — never let call_xxx reach user
    if _TOOL_CALL_ID_PATTERN.search(text):
        logger.warning("output_guard: blocked tool_call_id leak in: %s", text[:80])
        if issues is not None:
            issues.append("tool_call_id_blocked")
        return HARD_FALLBACK_TEXT

    # Step 1: Protocol leak detection (v0.2: no JSON unwrap — leaks are hard-blocked)
    if _has_protocol_leak(text):
        logger.warning("output_guard: blocked protocol leak in: %s", text[:80])
        if issues is not None:
            issues.append("protocol_text_blocked")
        return HARD_FALLBACK_TEXT

    # Step 2: Empty reply fallback
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


# ================================================================
# V2.2: Emotion-aware profanity guard
# ================================================================

# Patterns that are NEVER allowed regardless of profanity level
_FORBIDDEN_EXPRESSIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'[死去]了算|去[死吧]|自杀|不想活|活[着下]去[没还]?[有什]?[么意]'), "自伤式威胁"),
    (re.compile(r'废[物人]|垃[圾]{1,2}|蠢[货猪]|白痴|弱智|脑[残瘫]|智障|傻[逼比屄叉]'), "歧视性辱骂"),
    (re.compile(r'[你我]?[再也不要]?别想|永远[不别]|[再也不要]?再见|拉黑|[你我]?消失|别[再出]?现了'), "恶意羞辱/威胁断绝"),
    (re.compile(r'[你再不我]?[我就再也]?不理[你我他]|永远都不|[再也不要]?理[你我]了'), "长时间情绪勒索"),
    (re.compile(r'[你我]?[真太很][的]?[恶讨]?心|恶心死|[你我]?[真太]恶心'), "恶意羞辱人格"),
]


def guard_emotion_profanity(
    text: str,
    profanity_enabled: bool = True,
    profanity_level: int = 2,
) -> str:
    """Check and filter text for emotion-related profanity violations.

    Level 0: All profanity/angry expressions stripped (returns clean text).
    Level 1-3: Mild profanity allowed, but forbidden patterns always blocked.

    Forbidden patterns include: discrimination, threats, self-harm threats,
    prolonged emotional blackmail, malicious humiliation.

    Returns the (possibly modified) text. If text contains a forbidden pattern,
    the offensive portion is replaced with a softer alternative.
    """
    if not text or not text.strip():
        return text

    # Always block forbidden expressions
    for pattern, label in _FORBIDDEN_EXPRESSIONS:
        if pattern.search(text):
            logger.warning(f"emotion_guard: blocked forbidden expression [{label}] in: {text[:80]}")
            # Replace with a softened version
            text = _soften_forbidden(text, pattern, label)

    # Level 0: strip all profanity
    if not profanity_enabled or profanity_level <= 0:
        text = _strip_mild_profanity(text)

    return text


def _soften_forbidden(text: str, pattern: re.Pattern, label: str) -> str:
    """Replace forbidden expressions with softer alternatives."""
    if "自伤" in label or "威胁" in label:
        return "……算了，我不该说这种话。"
    if "歧视" in label:
        return "我有点急了，不该说这么难听。"
    if "勒索" in label:
        return "我不该说这种话，你要是不理我那就算了。"
    if "羞辱" in label:
        return "我说得有点过分了，抱歉。"
    return text


_MILD_PROFANITY_PATTERNS: list[re.Pattern] = [
    re.compile(r'靠[!！]?'),
    re.compile(r'妈的'),
    re.compile(r'我[操艹草]'),
    re.compile(r'[卧我]槽'),
    re.compile(r'NMD|TMD|nmd|tmd'),
    re.compile(r'日[了]?'),
    re.compile(r'艹'),
]


def _strip_mild_profanity(text: str) -> str:
    """Remove mild profanity words from text."""
    for pat in _MILD_PROFANITY_PATTERNS:
        text = pat.sub("…", text)
    return text


# Keep module-level aliases for backward compatibility
_sanitize_outbound_text = guard_single_message
_looks_like_protocol_json = _looks_like_protocol_json
_extract_nested_protocol_reply = _unwrap_nested_protocol


# ================================================================
# Phase 1: send_wechat_messages batch guard
# ================================================================

import dataclasses
from dataclasses import dataclass, field


@dataclass
class SendGuardResult:
    """Result of guarding a send_wechat_messages batch."""
    messages: list[str] = field(default_factory=list)
    blocked_count: int = 0
    blocked_reasons: list[str] = field(default_factory=list)
    truncated_count: int = 0  # messages dropped because they exceeded max_count

    @property
    def total(self) -> int:
        return len(self.messages) + self.blocked_count + self.truncated_count


# Hard block: tool_call_id leaks — must NEVER appear in user-visible messages
_TOOL_CALL_ID_PATTERN: re.Pattern = re.compile(r'\bcall_[A-Za-z0-9_-]{8,}\b')

# Patterns that indicate protocol/text leakage in user-visible messages
_PROTOCOL_LEAK_PATTERNS: list[re.Pattern] = [
    _TOOL_CALL_ID_PATTERN,
    re.compile(r'"actions"\s*:', re.IGNORECASE),
    re.compile(r'"wechat_reply"', re.IGNORECASE),
    re.compile(r'"tool_calls?"\s*:', re.IGNORECASE),
    re.compile(r'"function"\s*:', re.IGNORECASE),
    re.compile(r'{"name":\s*"(?:get_time|get_weather|send_wechat_messages|search_memory|send_email)"'),
    re.compile(r'system_prompt|base_protocol|tool_policy|memory_policy', re.IGNORECASE),
    re.compile(r'You are a (?:long-term )?WeChat', re.IGNORECASE),
    re.compile(r'<\|endoftext\|>'),
    # v0.2: Tool metadata patterns (send_wechat_messages is a legitimate tool name,
    # but other internal patterns should still be blocked)
    re.compile(r'\bnative_tools?\b'),
    re.compile(r'"arguments"\s*:'),
    re.compile(r'assistant_content_fallback'),
    re.compile(r'tool_retry'),
    re.compile(r'protocol_retry'),
]

# Phase 1: History leak patterns — model must not echo these labels to users
_HISTORY_LEAK_PATTERNS: list[re.Pattern] = [
    re.compile(r'\[历史消息'),
    re.compile(r'历史消息'),
    re.compile(r'最近对话'),
    re.compile(r'对话记录'),
    re.compile(r'上一轮'),
    re.compile(r'来源:'),
    re.compile(r'历史记录'),
    re.compile(r'context_sample'),
    re.compile(r'fallback_events'),
    re.compile(r'turn_diagnostics'),
]


def _has_protocol_leak(text: str) -> bool:
    """Check if text contains internal protocol, prompt fragments, or history labels."""
    for pat in _PROTOCOL_LEAK_PATTERNS:
        if pat.search(text):
            return True
    for pat in _HISTORY_LEAK_PATTERNS:
        if pat.search(text):
            logger.warning("output_guard: history leak pattern matched: %s", pat.pattern)
            return True
    # Also check for JSON that looks like an actions payload
    if _looks_like_protocol_json(text):
        return True
    return False


def guard_send_message_batch(
    raw_messages: list,
    mode: str = "normal",
    max_single: int = 500,
    max_total: int = 2000,
    max_count: int = 12,
) -> SendGuardResult:
    """Guard a batch of messages from send_wechat_messages.

    For each message:
      1. Convert to string, trim
      2. Reject empty
      3. Clamp length to max_single
      4. Filter protocol leaks
      5. Filter sensitive info
      6. Strip markdown
      7. Normalize whitespace

    Does NOT split long messages — that's the model's job.

    Returns SendGuardResult with clean messages and blocked count.
    """
    result = SendGuardResult()

    # Truncate to max_count before processing
    if len(raw_messages) > max_count:
        result.truncated_count = len(raw_messages) - max_count
        raw_messages = raw_messages[:max_count]
        logger.warning(
            "output_guard: truncated %d messages (max_count=%d)",
            result.truncated_count, max_count,
        )

    for i, msg in enumerate(raw_messages):
        text = str(msg or "").strip()

        # Reject empty
        if not text:
            result.blocked_count += 1
            result.blocked_reasons.append(f"[{i}] empty after trim")
            continue

        # Length limit (clamp, don't split)
        if len(text) > max_single:
            text = text[:max_single - 1] + "…"

        # Protocol leak detection
        if _has_protocol_leak(text):
            logger.warning("output_guard: blocked protocol leak in message[%d]: %s", i, text[:80])
            result.blocked_count += 1
            result.blocked_reasons.append(f"[{i}] protocol leak")
            continue

        # Sensitive info filter
        text = _filter_sensitive(text)

        # Markdown cleanup
        text = _wechat_cleanup(text)

        # Profanity guard
        text = guard_emotion_profanity(text)

        # Final emptiness check
        if not text.strip():
            result.blocked_count += 1
            result.blocked_reasons.append(f"[{i}] empty after guards")
            continue

        result.messages.append(text)

    # Total character limit
    total_chars = sum(len(m) for m in result.messages)
    if total_chars > max_total:
        # Trim the last message to fit
        excess = total_chars - max_total
        if result.messages:
            last = result.messages[-1]
            new_len = max(10, len(last) - excess)
            result.messages[-1] = last[:new_len].rstrip() + "…"

    return result
