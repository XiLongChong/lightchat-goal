"""Phase 7: DeliveryService tests."""

from __future__ import annotations

from agent.delivery_service import DeliveryService
from runtime_contracts import DeliveryStatus


# ═══════════════════════════════════════════════════════════════════
# delivered returns receipt
# ═══════════════════════════════════════════════════════════════════


class TestDeliveredReceipt:
    def test_deliver_returns_delivery_receipt(self):
        ds = DeliveryService(turn_id="turn_1")
        receipt = ds.deliver("你好")
        assert receipt.status == DeliveryStatus.DELIVERED
        assert receipt.message_id.startswith("msg_")
        assert receipt.delivery_receipt_id.startswith("dr_")
        assert receipt.turn_id == "turn_1"

    def test_deliver_with_custom_send_fn_returns_receipt(self):
        ds = DeliveryService(turn_id="turn_1")
        sent = []

        def mock_send(text: str):
            sent.append(text)

        receipt = ds.deliver("hello", send_fn=mock_send)
        assert receipt.status == DeliveryStatus.DELIVERED
        assert sent == ["hello"]

    def test_deliver_with_failing_send_fn(self):
        ds = DeliveryService(turn_id="turn_1")

        def failing_send(text: str):
            raise Exception("send failed")

        receipt = ds.deliver("test", send_fn=failing_send)
        assert receipt.status == DeliveryStatus.FAILED
        assert receipt.retryable is True

    def test_deliver_failing_returns_failed_receipt(self):
        ds = DeliveryService(turn_id="turn_1")
        receipt = ds.deliver_failing("bad")
        assert receipt.status == DeliveryStatus.FAILED

    def test_deliver_unknown_returns_unknown_receipt(self):
        ds = DeliveryService(turn_id="turn_1")
        receipt = ds.deliver_unknown("maybe")
        assert receipt.status == DeliveryStatus.UNKNOWN
        assert receipt.retryable is False


# ═══════════════════════════════════════════════════════════════════
# Failed/unknown not in history writable
# ═══════════════════════════════════════════════════════════════════


class TestHistoryWritable:
    def test_delivered_messages_in_history_writable(self):
        ds = DeliveryService(turn_id="turn_1")
        ds.deliver("你好")
        ds.deliver("在吗")
        writable = ds.get_history_writable()
        assert len(writable) == 2
        assert writable[0]["content"] == "你好"
        assert writable[1]["content"] == "在吗"

    def test_failed_messages_not_in_history_writable(self):
        ds = DeliveryService(turn_id="turn_1")
        ds.deliver("ok")
        ds.deliver_failing("bad")
        ds.deliver("ok2")
        writable = ds.get_history_writable()
        assert len(writable) == 2
        texts = [w["content"] for w in writable]
        assert "ok" in texts
        assert "ok2" in texts
        assert "bad" not in texts

    def test_unknown_messages_not_in_history_writable(self):
        ds = DeliveryService(turn_id="turn_1")
        ds.deliver("ok")
        ds.deliver_unknown("maybe")
        writable = ds.get_history_writable()
        assert len(writable) == 1
        assert writable[0]["content"] == "ok"

    def test_all_failed_produces_empty_history(self):
        ds = DeliveryService(turn_id="turn_1")
        ds.deliver_failing("a")
        ds.deliver_failing("b")
        assert ds.get_history_writable() == []
        assert ds.get_delivered_texts() == []
        assert len(ds.get_failed_texts()) == 2


# ═══════════════════════════════════════════════════════════════════
# Count tracking
# ═══════════════════════════════════════════════════════════════════


class TestCountTracking:
    def test_delivered_count(self):
        ds = DeliveryService(turn_id="turn_1")
        ds.deliver("a")
        ds.deliver("b")
        assert ds.delivered_count == 2
        assert ds.failed_count == 0

    def test_mixed_counts(self):
        ds = DeliveryService(turn_id="turn_1")
        ds.deliver("a")
        ds.deliver_failing("b")
        ds.deliver("c")
        ds.deliver_unknown("d")
        assert ds.delivered_count == 2
        assert ds.failed_count == 2

    def test_send_attempt_count_tracks_all(self):
        ds = DeliveryService(turn_id="turn_1")
        ds.deliver("a")
        ds.deliver_failing("b")
        ds.deliver_unknown("c")
        assert ds.send_attempt_count == 3
