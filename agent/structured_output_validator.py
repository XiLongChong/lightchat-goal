"""Structured output validator — provider gateway boundary checks.

Phase 6: Validates model output before any tool execution.
Ensures only well-formed, exposed, legal tool calls reach side-effect code.

Spec: docs/runtime-specs/03-turn-engine-spec.zh.md INV-02 to INV-05, INV-12, INV-13
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ValidationResultKind(StrEnum):
    VALID = "valid"
    NO_TOOL_CALLS = "no_tool_calls"
    ASSISTANT_CONTENT_ONLY = "assistant_content_only"
    INVALID_ARGS = "invalid_args"
    TOOL_NOT_EXPOSED = "tool_not_exposed"
    MULTIPLE_TOOL_CALLS = "multiple_tool_calls"
    TERMINAL_COMBINATION_INVALID = "terminal_combination_invalid"
    PARALLEL_FACT_TOOLS = "parallel_fact_tools"


@dataclass(slots=True)
class ValidationResult:
    kind: ValidationResultKind = ValidationResultKind.VALID
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    assistant_content: str = ""
    violation_detail: str = ""
    retryable: bool = True


# ── Terminal tools ─────────────────────────────────────────────────

_TERMINAL_TOOLS = {"finish_turn", "done"}
_SKIP_TOOLS = {"skip_initiative", "skip_proactive"}
_SEND_TOOLS = {"send_text_message", "wechat_reply"}
_FACT_TOOLS = {"get_time", "get_weather"}

_ALL_TERMINAL_OR_SKIP = _TERMINAL_TOOLS | _SKIP_TOOLS


class StructuredOutputValidator:
    """Validates LLM output before any tool is executed.

    All checks are pure functions — no I/O, no side effects.
    """

    def __init__(
        self,
        exposed_tool_names: list[str] | None = None,
        max_retries: int = 1,
    ) -> None:
        self._exposed: set[str] = set(exposed_tool_names or [])
        self.max_retries = max_retries
        self.retry_count = 0

    # ── Public API ─────────────────────────────────────────────────

    def validate(
        self,
        tool_calls: list[dict[str, Any]],
        assistant_content: str = "",
    ) -> ValidationResult:
        """Primary validation entry point.

        Returns ValidationResult with kind=VALID only if all checks pass.
        """
        # Check 1: no tool calls at all
        if not tool_calls:
            if assistant_content.strip():
                return ValidationResult(
                    kind=ValidationResultKind.ASSISTANT_CONTENT_ONLY,
                    assistant_content=assistant_content,
                    violation_detail=f"model returned only text ({len(assistant_content)} chars), no tool calls",
                )
            return ValidationResult(
                kind=ValidationResultKind.NO_TOOL_CALLS,
                violation_detail="empty response: no tool calls and no content",
            )

        # Check 2: extract and validate tool call names
        parsed: list[dict[str, Any]] = []
        for tc in tool_calls:
            parsed_tc = self._parse_tool_call(tc)
            if parsed_tc is None:
                return ValidationResult(
                    kind=ValidationResultKind.INVALID_ARGS,
                    violation_detail="unable to parse tool call structure",
                )
            # Check 3: invalid JSON args
            if not self._validate_args_json(parsed_tc.get("arguments", "")):
                return ValidationResult(
                    kind=ValidationResultKind.INVALID_ARGS,
                    tool_calls=[parsed_tc],
                    violation_detail=f"invalid JSON arguments for {parsed_tc.get('name', '?')}",
                )
            parsed.append(parsed_tc)

        names = [tc["name"] for tc in parsed]

        # Check 4: tool not exposed
        for name in names:
            if self._exposed and name not in self._exposed:
                return ValidationResult(
                    kind=ValidationResultKind.TOOL_NOT_EXPOSED,
                    tool_calls=parsed,
                    violation_detail=f"tool '{name}' not in exposed set",
                )

        # Check 5: terminal combination rules
        terminal_names = [n for n in names if n in _ALL_TERMINAL_OR_SKIP]
        if len(terminal_names) > 1:
            return ValidationResult(
                kind=ValidationResultKind.TERMINAL_COMBINATION_INVALID,
                tool_calls=parsed,
                violation_detail=(
                    f"multiple terminal/skip tools in one call: {', '.join(terminal_names)}. "
                    "terminal tools must be called alone or only with send_text_message"
                ),
            )

        # Check 6: terminal + non-send tool in same call
        if terminal_names:
            terminal_name = terminal_names[0]
            non_send_non_terminal = [
                n for n in names
                if n not in _ALL_TERMINAL_OR_SKIP and n not in _SEND_TOOLS
            ]
            if non_send_non_terminal:
                return ValidationResult(
                    kind=ValidationResultKind.TERMINAL_COMBINATION_INVALID,
                    tool_calls=parsed,
                    violation_detail=(
                        f"terminal tool '{terminal_name}' combined with "
                        f"non-send tool(s): {', '.join(non_send_non_terminal)}"
                    ),
                )

        # Check 7: skip + any other tool (must be sole call)
        skip_names = [n for n in names if n in _SKIP_TOOLS]
        if skip_names and len(names) > 1:
            return ValidationResult(
                kind=ValidationResultKind.TERMINAL_COMBINATION_INVALID,
                tool_calls=parsed,
                violation_detail=(
                    f"skip tool '{skip_names[0]}' must be sole tool call, "
                    f"not combined with: {', '.join(n for n in names if n != skip_names[0])}"
                ),
            )

        # All checks passed
        return ValidationResult(
            kind=ValidationResultKind.VALID,
            tool_calls=parsed,
            assistant_content=assistant_content,
        )

    def record_retry(self) -> bool:
        """Return True if retry budget remains, False if exhausted."""
        self.retry_count += 1
        return self.retry_count <= self.max_retries

    @property
    def retries_exhausted(self) -> bool:
        return self.retry_count > self.max_retries

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _parse_tool_call(tc: Any) -> dict[str, Any] | None:
        if isinstance(tc, dict):
            fn = tc.get("function", {})
            if isinstance(fn, dict):
                return {
                    "id": str(tc.get("id", "")),
                    "name": str(fn.get("name", "")),
                    "arguments": str(fn.get("arguments", "")),
                }
            return {"id": "", "name": "", "arguments": ""}

        fn = getattr(tc, "function", None)
        if fn is not None:
            return {
                "id": str(getattr(tc, "id", "")),
                "name": str(getattr(fn, "name", "")),
                "arguments": str(getattr(fn, "arguments", "")),
            }
        return None

    @staticmethod
    def _validate_args_json(args_str: str) -> bool:
        if not args_str or not args_str.strip():
            return True  # empty args are valid
        try:
            _json.loads(args_str)
            return True
        except (_json.JSONDecodeError, TypeError):
            return False
