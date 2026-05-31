"""Safe JSON extraction helpers."""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json(text: str) -> str:
    """Extract a JSON object from text that may include markdown fences."""
    text = text.strip()

    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    depth = 0
    start = -1
    for i, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = i
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : i + 1]

    return text


def safe_parse_json(raw: str) -> dict[str, Any] | None:
    """Parse JSON object text, applying a few common LLM-output repairs."""
    if not raw or not raw.strip():
        return None

    json_str = extract_json(raw)
    try:
        result = json.loads(json_str)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    fixes = [
        (r",(\s*})", r"\1"),
        (r",(\s*])", r"\1"),
        (r"'([^']*)':", r'"\1":'),
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
