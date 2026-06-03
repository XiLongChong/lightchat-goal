"""Phase 7+12: DeliveryService tests — async deliver() with exception-based status."""

from __future__ import annotations

import asyncio

import pytest

from agent.delivery_service import (
    DeliveryService,
    DeliveryBlockedError,
    DeliveryRateLimitedError,
    DeliveryUnknownError,
)
from runtime_contracts import DeliveryStatus


# ═══════════════════════════════════════════════════════════════════
# delivered returns receipt
# ═══════════════════════════════════════════════════════════════════


class TestDeliveredReceipt:
    def test_deliver_returns_delivery_receipt(self):
        ds = DeliveryService(turn_id="turn_1")
        receipt = asyncio.run(ds.deliver("你好"))
        assert receipt.status == DeliveryStatus.DELIVERED
        assert receipt.message_id.startswith("msg_")
        assert receipt.delivery_receipt_id.startswith("dr_")
        assert receipt.turn_id == "turn_1"

    def test_deliver_with_custom_send_fn_returns_receipt(self):
        ds = DeliveryService(turn_id="turn_1")
        sent = []

        async def mock_send(text: str):
            sent.append(text)

        receipt = asyncio.run(ds.deliver("hello", send_fn=mock_send))
        assert receipt.status == DeliveryStatus.DELIVERED
        assert sent == ["hello"]

    def test_deliver_with_failing_send_fn(self):
        ds = DeliveryService(turn_id="turn_1")

        async def failing_send(text: str):
            raise Exception("send failed")

        receipt = asyncio.run(ds.deliver("test", send_fn=failing_send))
        assert receipt.status == DeliveryStatus.FAILED
        assert receipt.retryable is True

    def test_deliver_rate_limited(self):
        ds = DeliveryService(turn_id="turn_1")

        async def rate_limited_send(text: str):
            raise DeliveryRateLimitedError("rate limited")

        receipt = asyncio.run(ds.deliver("hi", send_fn=rate_limited_send))
        assert receipt.status == DeliveryStatus.RATE_LIMITED
        assert receipt.retryable is True
        assert receipt.error_code == "rate_limited"

    def test_deliver_blocked(self):
        ds = DeliveryService(turn_id="turn_1")

        async def blocked_send(text: str):
            raise DeliveryBlockedError("blocked by platform")

        receipt = asyncio.run(ds.deliver("hi", send_fn=blocked_send))
        assert receipt.status == DeliveryStatus.BLOCKED
        assert receipt.retryable is True
        assert receipt.error_code == "blocked_by_platform"

    def test_deliver_unknown_error(self):
        ds = DeliveryService(turn_id="turn_1")

        async def unknown_send(text: str):
            raise DeliveryUnknownError("cannot confirm")

        receipt = asyncio.run(ds.deliver("hi", send_fn=unknown_send))
        assert receipt.status == DeliveryStatus.UNKNOWN
        assert receipt.retryable is False
        assert receipt.error_code == "unknown_status"

    def test_deliver_unrecoverable_failure(self):
        ds = DeliveryService(turn_id="turn_1")

        async def no_bridge_send(text: str):
            raise Exception("no_bridge: iLink disconnected")

        receipt = asyncio.run(ds.deliver("hi", send_fn=no_bridge_send))
        assert receipt.status == DeliveryStatus.FAILED
        assert receipt.retryable is False
        assert receipt.error_code == "wechat_sender_unavailable"

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
        asyncio.run(ds.deliver("你好"))
        asyncio.run(ds.deliver("在吗"))
        writable = ds.get_history_writable()
        assert len(writable) == 2
        assert writable[0]["content"] == "你好"
        assert writable[1]["content"] == "在吗"

    def test_failed_messages_not_in_history_writable(self):
        ds = DeliveryService(turn_id="turn_1")
        asyncio.run(ds.deliver("ok"))
        ds.deliver_failing("bad")
        asyncio.run(ds.deliver("ok2"))
        writable = ds.get_history_writable()
        assert len(writable) == 2
        texts = [w["content"] for w in writable]
        assert "ok" in texts
        assert "ok2" in texts
        assert "bad" not in texts

    def test_unknown_messages_not_in_history_writable(self):
        ds = DeliveryService(turn_id="turn_1")
        asyncio.run(ds.deliver("ok"))
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
        asyncio.run(ds.deliver("a"))
        asyncio.run(ds.deliver("b"))
        assert ds.delivered_count == 2
        assert ds.failed_count == 0

    def test_mixed_counts(self):
        ds = DeliveryService(turn_id="turn_1")
        asyncio.run(ds.deliver("a"))
        ds.deliver_failing("b")
        asyncio.run(ds.deliver("c"))
        ds.deliver_unknown("d")
        assert ds.delivered_count == 2
        assert ds.failed_count == 2

    def test_send_attempt_count_tracks_all(self):
        ds = DeliveryService(turn_id="turn_1")
        asyncio.run(ds.deliver("a"))
        ds.deliver_failing("b")
        ds.deliver_unknown("c")
        assert ds.send_attempt_count == 3
