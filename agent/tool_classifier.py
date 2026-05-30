"""Tool classifier — v1.5 strict single-tool-call mode.

Classifies exactly one native tool_call per LLM response.
Routing is done by tool_name in chat_agent, not by tool_category.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ToolCategory(StrEnum):
    FACT_TOOL = "fact_tool"
    ACTION_TOOL = "action_tool"
    TERMINAL_TOOL = "terminal_tool"
    STATE_TOOL = "state_tool"


# ── v1.5 deprecated tool hints ────────────────────────────────────
_DEPRECATED_HINTS: dict[str, str] = {
    "send_wechat_messages": (
        "send_wechat_messages 已废弃。请使用 wechat_reply 逐条发送，最后用 done 结束。"
    ),
    "skip_reply": (
        "skip_reply 已废弃。请使用 done(status='no_reply', note='原因')。"
    ),
}


@dataclass(slots=True)
class ClassifiedSingleToolCall:
    """Result of classifying exactly one model tool_call.

    v1.5: only one tool call per LLM response.
    Routing is by tool_name in chat_agent._dispatch(), not by registry_category.
    """

    tool_name: str = ""
    serialized: dict[str, Any] | None = None
    registry_category: str = ""          # TOOL_REGISTRY category (reference only)
    is_known: bool = False               # registered in TOOL_REGISTRY
    is_deprecated: bool = False          # old tool, model must not use
    deprecated_hint: str = ""            # migration hint for deprecated tools
    violation_reason: str = ""           # why illegal (Chinese)

    @property
    def is_legal(self) -> bool:
        return self.is_known and not self.is_deprecated and not self.violation_reason


def classify_single_tool_call(
    tool_call: Any,
    tool_registry: Any,
) -> ClassifiedSingleToolCall:
    """Classify a single native tool_call.

    Args:
        tool_call: Native tool_call object or dict from LLM response.
        tool_registry: ToolRegistry instance with .get(name) → ToolDef | None.

    Returns:
        ClassifiedSingleToolCall with tool_name, legality flags, and deprecated hints.
    """
    # ── Extract tool_name ────────────────────────────────────────
    if isinstance(tool_call, dict):
        fn = tool_call.get("function", {})
        tool_name = str(fn.get("name", "")) if isinstance(fn, dict) else ""
        serialized = tool_call
    else:
        fn = getattr(tool_call, "function", None)
        tool_name = str(getattr(fn, "name", "") if fn else "")
        serialized = {
            "id": getattr(tool_call, "id", ""),
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": str(getattr(fn, "arguments", "") if fn else ""),
            },
        }

    if not tool_name:
        return ClassifiedSingleToolCall(
            tool_name="(empty)",
            violation_reason="工具名为空",
        )

    # ── Look up in registry ──────────────────────────────────────
    tool_def = None
    if hasattr(tool_registry, "get"):
        tool_def = tool_registry.get(tool_name)

    if tool_def is None:
        return ClassifiedSingleToolCall(
            tool_name=tool_name,
            serialized=serialized,
            violation_reason=f"未知或未注册的工具: {tool_name}",
        )

    # ── Check deprecated ─────────────────────────────────────────
    if tool_name in _DEPRECATED_HINTS:
        return ClassifiedSingleToolCall(
            tool_name=tool_name,
            serialized=serialized,
            registry_category=getattr(tool_def, "tool_category", ""),
            is_known=True,
            is_deprecated=True,
            deprecated_hint=_DEPRECATED_HINTS[tool_name],
        )

    return ClassifiedSingleToolCall(
        tool_name=tool_name,
        serialized=serialized,
        registry_category=getattr(tool_def, "tool_category", ""),
        is_known=True,
    )
