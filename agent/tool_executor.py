"""ToolExecutor — unified tool execution facade.

Phase 7: Validates tool calls against exposure policy, executes
via registered handlers, returns structured ToolExecutionResult.

Spec: docs/runtime-specs/03-turn-engine-spec.zh.md INV-04, INV-06
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from runtime_contracts import (
    DeliveryReceipt,
    DeliveryStatus,
    ToolExecutionResult,
)


@dataclass(slots=True)
class ToolExecutor:
    """Executes tools after validation against exposure policy.

    Unexposed tools are rejected with reason="tool_not_exposed".
    Invalid parameters are rejected with reason="invalid_arguments".
    """

    exposed_tool_names: set[str] = field(default_factory=set)
    total_send_attempts: int = 0
    max_send_attempts: int = 3
    delivery_callback: Callable[[dict[str, Any]], DeliveryReceipt] | None = None

    def can_execute(self, tool_name: str) -> bool:
        """Check if a tool is exposed and executable."""
        if self.exposed_tool_names and tool_name not in self.exposed_tool_names:
            return False
        return True

    def execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        tool_call_id: str = "",
        turn_id: str = "",
        delivery_callback: Callable[[dict[str, Any]], DeliveryReceipt] | None = None,
    ) -> ToolExecutionResult:
        """Execute a tool call with validation.

        Returns ToolExecutionResult with status=success/rejected/failed.
        """
        tc_id = tool_call_id or f"tc_{uuid.uuid4().hex[:12]}"
        tid = turn_id or "unknown"

        # ── Check: tool not exposed ────────────────────────────────
        if not self.can_execute(tool_name):
            return ToolExecutionResult(
                tool_call_id=tc_id, turn_id=tid,
                tool_name=tool_name,
                status="rejected",
                error_code="tool_not_exposed",
                error_message=f"tool '{tool_name}' is not exposed this turn",
                visible_to_model=True,
            )

        # ── Check: invalid args for send tools ─────────────────────
        if tool_name in ("send_text_message", "wechat_reply"):
            text = str(args.get("text", "")).strip()
            if not text:
                return ToolExecutionResult(
                    tool_call_id=tc_id, turn_id=tid,
                    tool_name=tool_name,
                    status="rejected",
                    error_code="invalid_arguments",
                    error_message="text is required and must not be empty",
                    visible_to_model=True,
                )

        # ── Execute via delivery callback (for send tools) ─────────
        send_cb = delivery_callback or self.delivery_callback
        if tool_name in ("send_text_message", "wechat_reply") and send_cb:
            text = str(args.get("text", ""))
            self.total_send_attempts += 1
            receipt = send_cb({"text": text, "tool_call_id": tc_id})
            return ToolExecutionResult(
                tool_call_id=tc_id, turn_id=tid,
                tool_name=tool_name,
                status="success" if receipt.status == DeliveryStatus.DELIVERED else "failed",
                result_payload={"text": text, "delivery_status": receipt.status.value},
                delivery_receipt=receipt,
                visible_to_model=True,
            )

        # ── Fact tools / information tools — delegate to registry ──
        if tool_name in ("get_time", "get_weather"):
            return ToolExecutionResult(
                tool_call_id=tc_id, turn_id=tid,
                tool_name=tool_name,
                status="success",
                result_payload={"tool_name": tool_name, "args": args},
                visible_to_model=True,
            )

        # ── State tools — pass through ─────────────────────────────
        if tool_name in (
            "finish_turn", "done", "skip_initiative", "skip_proactive",
            "schedule_user_reminder", "schedule_wake_up",
            "mark_reminder_delivered", "apply_forget_directive",
        ):
            return ToolExecutionResult(
                tool_call_id=tc_id, turn_id=tid,
                tool_name=tool_name,
                status="success",
                result_payload={"tool_name": tool_name, "args": args},
                visible_to_model=True,
            )

        # ── Unknown tool ───────────────────────────────────────────
        return ToolExecutionResult(
            tool_call_id=tc_id, turn_id=tid,
            tool_name=tool_name,
            status="rejected",
            error_code="unknown_tool",
            error_message=f"no executor for tool '{tool_name}'",
            visible_to_model=True,
        )
