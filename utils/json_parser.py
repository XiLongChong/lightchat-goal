"""Safe JSON parsing with fallback for DeepSeek output."""

import json
import re
from typing import Any


def extract_json(text: str) -> str:
    """Extract JSON from text that may have markdown fences or extra text.

    Tries in order:
    1. ```json ... ``` code block
    2. ``` ... ``` code block
    3. First { ... } in the text
    4. Original text as-is
    """
    text = text.strip()

    # Strip markdown code blocks
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Find first JSON object
    depth = 0
    start = -1
    for i, c in enumerate(text):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : i + 1]

    return text


def safe_parse_json(raw: str) -> dict[str, Any] | None:
    """Parse model output as JSON. Returns None if parsing fails.

    The caller MUST handle None → fallback to plain-text message.
    This is Safeguard #1: never crash on malformed model output.
    """
    if not raw or not raw.strip():
        return None

    json_str = extract_json(raw)

    # Try direct parse first
    try:
        result = json.loads(json_str)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Try fixing common LLM JSON mistakes — apply all fixes cumulatively
    fixes = [
        # Remove trailing commas before }
        (r",(\s*})", r"\1"),
        # Remove trailing commas before ]
        (r",(\s*])", r"\1"),
        # Fix single quotes
        (r"'([^']*)':", r'"\1":'),
        # Fix unquoted keys
        (r"([{,])\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:", r'\1"\2":'),
    ]

    fixed = json_str
    for pattern, replacement in fixes:
        fixed = re.sub(pattern, replacement, fixed)

    try:
        result = json.loads(fixed)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    return None


def parse_agent_output(raw: str) -> dict[str, Any]:
    """Parse agent output with guaranteed return.

    Never raises. Always returns a valid action dict.
    Safeguard #1 (fallback): invalid JSON → treat as plain text message.
    """
    parsed = safe_parse_json(raw)

    if parsed is None:
        # Fallback: entire output becomes one text message
        text = raw.strip()
        if len(text) > 500:
            text = text[:497] + "..."

        return {
            "actions": [{"type": "message", "text": text, "delay": 0}],
            "done": True,
            "_fallback_reason": "invalid_json",
        }

    # Ensure required fields exist
    if "actions" not in parsed:
        # No actions field — treat as plain response
        text = raw.strip()[:500]
        return {
            "actions": [{"type": "message", "text": text, "delay": 0}],
            "done": True,
            "_fallback_reason": "missing_actions",
        }

    return parsed
