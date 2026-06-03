"""Phase 7: ToolExecutor tests."""

from __future__ import annotations

import pytest
from runtime_contracts import DeliveryReceipt, DeliveryStatus
from agent.tool_executor import ToolExecutor


def make_delivery_callback(status: DeliveryStatus = DeliveryStatus.DELIVERED):
    def _cb(payload: dict) -> DeliveryReceipt:
        return DeliveryReceipt(
            delivery_receipt_id="dr_1",
            message_id="msg_1",
            turn_id="turn_1",
            status=status,
        )
    return _cb


# ═══════════════════════════════════════════════════════════════════
# Unexposed tools rejected
# ═══════════════════════════════════════════════════════════════════


class TestUnexposedTools:
    def test_unexposed_tool_rejected(self):
        executor = ToolExecutor(exposed_tool_names={"send_text_message", "finish_turn"})
        result = executor.execute("generate_image_message", {"prompt": "cat"})
        assert result.status == "rejected"
        assert result.error_code == "tool_not_exposed"

    def test_exposed_tool_accepted(self):
        executor = ToolExecutor(exposed_tool_names={"send_text_message", "finish_turn"})
        result = executor.execute("send_text_message", {"text": "hello"},
                                  delivery_callback=make_delivery_callback())
        assert result.status != "rejected"

    def test_empty_exposed_set_allows_all(self):
        executor = ToolExecutor()
        result = executor.execute("any_tool", {})
        assert result.status != "rejected" or result.error_code != "tool_not_exposed"

    def test_can_execute_returns_false_for_unexposed(self):
        executor = ToolExecutor(exposed_tool_names={"send_text_message"})
        assert executor.can_execute("finish_turn") is False
        assert executor.can_execute("send_text_message") is True


# ═══════════════════════════════════════════════════════════════════
# Invalid params rejected
# ═══════════════════════════════════════════════════════════════════


class TestInvalidParams:
    def test_empty_text_rejected_for_send(self):
        executor = ToolExecutor(exposed_tool_names={"send_text_message"})
        result = executor.execute("send_text_message", {"text": ""})
        assert result.status == "rejected"
        assert result.error_code == "invalid_arguments"

    def test_whitespace_only_text_rejected(self):
        executor = ToolExecutor(exposed_tool_names={"send_text_message"})
        result = executor.execute("send_text_message", {"text": "   "})
        assert result.status == "rejected"

    def test_valid_text_accepted(self):
        executor = ToolExecutor(exposed_tool_names={"send_text_message"})
        result = executor.execute("send_text_message", {"text": "hello"},
                                  delivery_callback=make_delivery_callback())
        assert result.status == "success"


# ═══════════════════════════════════════════════════════════════════
# Delivered returns receipt
# ═══════════════════════════════════════════════════════════════════


class TestDeliveredReceipt:
    def test_send_text_returns_delivery_receipt(self):
        executor = ToolExecutor(
            exposed_tool_names={"send_text_message"},
            delivery_callback=make_delivery_callback(DeliveryStatus.DELIVERED),
        )
        result = executor.execute("send_text_message", {"text": "hi"})
        assert result.delivery_receipt is not None
        assert result.delivery_receipt.status == DeliveryStatus.DELIVERED

    def test_failed_delivery_updates_status(self):
        executor = ToolExecutor(
            exposed_tool_names={"send_text_message"},
            delivery_callback=make_delivery_callback(DeliveryStatus.FAILED),
        )
        result = executor.execute("send_text_message", {"text": "hi"})
        assert result.status == "failed"

    def test_finish_turn_returns_success_without_receipt(self):
        executor = ToolExecutor(exposed_tool_names={"finish_turn"})
        result = executor.execute("finish_turn", {"status": "replied"})
        assert result.status == "success"
        assert result.delivery_receipt is None

    def test_tool_call_id_preserved(self):
        executor = ToolExecutor(exposed_tool_names={"get_time"})
        result = executor.execute("get_time", {}, tool_call_id="tc_42")
        assert result.tool_call_id == "tc_42"


# ═══════════════════════════════════════════════════════════════════
# Failed/unknown don't write history
# ═══════════════════════════════════════════════════════════════════


class TestHistoryWritable:
    def test_delivered_messages_in_result_payload(self):
        executor = ToolExecutor(
            exposed_tool_names={"send_text_message"},
            delivery_callback=make_delivery_callback(DeliveryStatus.DELIVERED),
        )
        result = executor.execute("send_text_message", {"text": "你好"})
        assert result.result_payload.get("delivery_status") == "delivered"

    def test_failed_messages_marked_in_result(self):
        executor = ToolExecutor(
            exposed_tool_names={"send_text_message"},
            delivery_callback=make_delivery_callback(DeliveryStatus.FAILED),
        )
        result = executor.execute("send_text_message", {"text": "失败"})
        assert result.status == "failed"


# ═══════════════════════════════════════════════════════════════════
# Fact / state tools
# ═══════════════════════════════════════════════════════════════════


class TestFactAndStateTools:
    def test_get_time_returns_success(self):
        executor = ToolExecutor(exposed_tool_names={"get_time"})
        result = executor.execute("get_time", {"timezone": "Asia/Shanghai"})
        assert result.status == "success"

    def test_schedule_reminder_returns_success(self):
        executor = ToolExecutor(exposed_tool_names={"schedule_user_reminder"})
        result = executor.execute("schedule_user_reminder",
                                   {"message_hint": "喝水", "delay_minutes": 10})
        assert result.status == "success"

    def test_unknown_tool_name_rejected(self):
        executor = ToolExecutor()
        result = executor.execute("nonexistent_tool", {})
        assert result.status == "rejected"
        assert result.error_code == "unknown_tool"
