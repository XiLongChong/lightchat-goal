"""Safe JSON parsing with fallback for DeepSeek output."""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


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
        # If the model explicitly requested skip/no_reply, honour that
        # instead of treating the raw JSON as a text message.
        if parsed.get("skip") or parsed.get("no_reply"):
            return {
                "actions": [],
                "done": parsed.get("done", True),
                "skip": True,
                "skip_reason": str(
                    parsed.get("reason") or parsed.get("skip_reason") or ""
                )[:200],
                "_fallback_reason": "missing_actions_preserved_skip",
            }

        # No actions field and no skip — treat as plain response
        text = raw.strip()[:500]
        return {
            "actions": [{"type": "message", "text": text, "delay": 0}],
            "done": True,
            "_fallback_reason": "missing_actions",
        }

    if not isinstance(parsed["actions"], list):
        text = raw.strip()[:500]
        return {
            "actions": [{"type": "message", "text": text, "delay": 0}],
            "done": True,
            "_fallback_reason": "actions_not_a_list",
        }

    return parsed


def parse_agent_json_output(raw: str) -> dict[str, Any]:
    """Strict JSON parsing for json_object mode (Phase 3A-fix).

    In strict mode, non-JSON or invalid content is NEVER sent as a user-visible
    message. The caller is responsible for producing a safe fallback message.

    Markdown code fences are stripped before parsing.

    Returns a dict that always contains a ``_fallback_reason`` key when the
    content could not be used as valid agent output.
    """
    if not raw or not raw.strip():
        return {
            "actions": [],
            "done": True,
            "_fallback_reason": "empty_content",
        }

    parsed = safe_parse_json(raw)

    if parsed is None:
        return {
            "actions": [],
            "done": True,
            "_fallback_reason": "non_json_content",
        }

    if "actions" not in parsed:
        if parsed.get("skip") or parsed.get("no_reply"):
            return {
                "actions": [],
                "done": parsed.get("done", True),
                "skip": True,
                "skip_reason": str(
                    parsed.get("reason") or parsed.get("skip_reason") or ""
                )[:200],
                "_fallback_reason": "missing_actions_preserved_skip",
            }
        return {
            "actions": [],
            "done": True,
            "_fallback_reason": "missing_actions",
        }

    if not isinstance(parsed["actions"], list):
        return {
            "actions": [],
            "done": True,
            "_fallback_reason": "actions_not_a_list",
        }

    return parsed


# Known action type/name fields across the project.
# Compatible with: type="message", type="tool" + name="wechat_reply",
# type="skip"/"no_reply", type="tool" + name="schedule_wechat_message", etc.
_VALID_ACTION_TYPES = frozenset({"message", "tool", "skip", "no_reply"})
_RUNTIME_TOOL_NAMES = frozenset({"wechat_reply", "schedule_wechat_message"})


def validate_agent_output(parsed: dict[str, Any]) -> list[str]:
    """Validate parsed agent JSON against the expected schema.

    Returns a list of error strings (empty = valid). Never raises.
    This is a schema guard, not a business-logic guard.
    """
    errors: list[str] = []

    if not isinstance(parsed, dict):
        errors.append("schema_error: top_level_not_object")
        return errors

    if "actions" not in parsed:
        errors.append("schema_error: missing_actions_field")
        return errors

    actions = parsed["actions"]
    if not isinstance(actions, list):
        errors.append("schema_error: actions_not_list")
        return errors

    # done: optional, but if present must be bool
    if "done" in parsed and not isinstance(parsed["done"], bool):
        errors.append("schema_error: done_not_bool")

    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            errors.append(f"schema_error: action[{i}]_not_object")
            continue

        # Determine action type: "type" field or legacy "name" for unknown types
        action_type = action.get("type", "")
        if not action_type:
            errors.append(f"schema_error: action[{i}]_missing_type")
            continue

        if action_type == "message":
            text = str(action.get("text", "")).strip()
            if not text:
                errors.append(f"schema_error: action[{i}]_message_empty_text")

        elif action_type == "tool":
            name = str(action.get("name", "")).strip()
            if not name:
                errors.append(f"schema_error: action[{i}]_tool_missing_name")
                continue
            args = action.get("args", {})
            if not isinstance(args, dict):
                errors.append(f"schema_error: action[{i}]_tool_args_not_dict")

            if name == "wechat_reply":
                text = str(args.get("text", "")).strip() if isinstance(args, dict) else ""
                if not text:
                    errors.append(f"schema_error: action[{i}]_wechat_reply_empty_text")

            elif name == "schedule_wechat_message":
                if isinstance(args, dict):
                    delay = args.get("delay_minutes", args.get("minutes"))
                    msgs = args.get("messages")
                    if delay is None:
                        errors.append(f"schema_error: action[{i}]_schedule_missing_delay")
                    if msgs is None:
                        errors.append(f"schema_error: action[{i}]_schedule_missing_messages")

        elif action_type in {"skip", "no_reply"}:
            pass  # valid, no further checks

        else:
            logger.warning(f"Unknown action type in agent output: {action_type}")
            errors.append(f"schema_error: action[{i}]_unknown_type_{action_type}")

    return errors
