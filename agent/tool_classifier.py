"""Tool classifier — v0.2 Phase 1.

Classifies native tool_calls into FACT_TOOL / TERMINAL_TOOL / STATE_TOOL /
INVALID_TOOL, and checks parallel-tool-call legality per v0.2 design rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ToolCategory(StrEnum):
    FACT_TOOL = "fact_tool"
    TERMINAL_TOOL = "terminal_tool"
    STATE_TOOL = "state_tool"


# ---------------------------------------------------------------------------
# Parallel-call legality table (v0.2 design principles §4)
# ---------------------------------------------------------------------------
# | 多个 FACT_TOOL                    | 允许     |
# | 一个 TERMINAL_TOOL                | 允许     |
# | 多个 TERMINAL_TOOL                | 禁止     |
# | TERMINAL_TOOL + 任意其他工具       | 禁止     |
# | 多个 STATE_TOOL                   | 允许, 串行 |
# | STATE_TOOL + FACT_TOOL            | 允许但谨慎 |
# | 非法工具 / 未注册工具             | 禁止     |
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ClassifiedToolCalls:
    """Result of classifying one round of model tool_calls."""

    fact_tools: list[dict[str, Any]] = field(default_factory=list)
    terminal_tools: list[dict[str, Any]] = field(default_factory=list)
    state_tools: list[dict[str, Any]] = field(default_factory=list)
    unknown_tools: list[str] = field(default_factory=list)

    # Legality flags
    has_multiple_terminals: bool = False
    has_terminal_mixed_with_others: bool = False

    @property
    def total(self) -> int:
        return (
            len(self.fact_tools)
            + len(self.terminal_tools)
            + len(self.state_tools)
            + len(self.unknown_tools)
        )

    @property
    def is_empty(self) -> bool:
        return self.total == 0

    @property
    def is_legal(self) -> bool:
        """True when the tool-call combination obeys all parallel-call rules."""
        if self.unknown_tools:
            return False
        if self.has_multiple_terminals:
            return False
        if self.has_terminal_mixed_with_others:
            return False
        return True

    @property
    def has_terminal(self) -> bool:
        return len(self.terminal_tools) > 0

    @property
    def has_fact(self) -> bool:
        return len(self.fact_tools) > 0

    @property
    def has_state(self) -> bool:
        return len(self.state_tools) > 0

    def violation_reason(self) -> str:
        """Human-readable reason for is_legal == False (Chinese)."""
        parts: list[str] = []
        if self.unknown_tools:
            names = ", ".join(self.unknown_tools)
            parts.append(f"未知或未注册的工具: {names}")
        if self.has_multiple_terminals:
            names = [str(t.get("function", {}).get("name", "?")) for t in self.terminal_tools]
            parts.append(f"同一轮调用了多个终态工具: {names}")
        if self.has_terminal_mixed_with_others:
            terminal_names = [str(t.get("function", {}).get("name", "?")) for t in self.terminal_tools]
            other_count = len(self.fact_tools) + len(self.state_tools)
            parts.append(
                f"终态工具 {terminal_names} 与 {other_count} 个其他工具混用"
            )
        return "；".join(parts) if parts else ""

    def terminal_name(self) -> str:
        """Return the name of the single terminal tool, or empty string."""
        if len(self.terminal_tools) == 1:
            tc = self.terminal_tools[0]
            fn = tc.get("function", {})
            if isinstance(fn, dict):
                return str(fn.get("name", ""))
            return str(getattr(fn, "name", "") if hasattr(fn, "name") else "")
        return ""


def classify_tool_calls(
    tool_calls: list[Any],
    tool_registry: Any,  # ToolRegistry instance
) -> ClassifiedToolCalls:
    """Classify a batch of native tool_calls.

    Args:
        tool_calls: Native tool_call objects from LLM response.
                    Each must have .function.name or ["function"]["name"].
        tool_registry: ToolRegistry instance with .get(name) → ToolDef | None.

    Returns:
        ClassifiedToolCalls with tools sorted into categories and legality checked.
    """
    result = ClassifiedToolCalls()

    if not tool_calls:
        return result

    categories: dict[str, list[dict[str, Any]]] = {
        ToolCategory.FACT_TOOL: [],
        ToolCategory.TERMINAL_TOOL: [],
        ToolCategory.STATE_TOOL: [],
    }
    unknowns: list[str] = []

    for tc in tool_calls:
        # Accept both dict and object forms
        if isinstance(tc, dict):
            fn = tc.get("function", {})
            tool_name = str(fn.get("name", "")) if isinstance(fn, dict) else ""
            serialized = tc
        else:
            fn = getattr(tc, "function", None)
            tool_name = str(getattr(fn, "name", "") if fn else "")
            serialized = {
                "id": getattr(tc, "id", ""),
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": str(getattr(fn, "arguments", "") if fn else ""),
                },
            }

        if not tool_name:
            unknowns.append("(empty name)")
            continue

        tool_def = None
        if hasattr(tool_registry, "get"):
            tool_def = tool_registry.get(tool_name)

        if tool_def is None:
            unknowns.append(tool_name)
            continue

        category = getattr(tool_def, "tool_category", ToolCategory.FACT_TOOL)
        if category not in categories:
            unknowns.append(tool_name)
            continue

        categories[category].append(serialized)

    result.fact_tools = categories[ToolCategory.FACT_TOOL]
    result.terminal_tools = categories[ToolCategory.TERMINAL_TOOL]
    result.state_tools = categories[ToolCategory.STATE_TOOL]
    result.unknown_tools = unknowns

    # Legality checks
    result.has_multiple_terminals = len(result.terminal_tools) > 1
    result.has_terminal_mixed_with_others = (
        len(result.terminal_tools) > 0
        and (len(result.fact_tools) + len(result.state_tools)) > 0
    )

    return result
