"""DeliveryService — unified send/delivery tracking facade.

Phase 7+12: Abstract send operations, produce DeliveryReceipt,
track per-turn send counts, enforce send budget.

Spec: docs/runtime-specs/03-turn-engine-spec.zh.md §5.4 Delivery Status state machine
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from runtime_contracts import DeliveryReceipt, DeliveryStatus


# ── Delivery-specific exceptions ──────────────────────────────────────


class DeliveryException(Exception):
    """Base exception for delivery failures."""


class DeliveryRateLimitedError(DeliveryException):
    """WeChat rate limit — retryable after delay."""


class DeliveryBlockedError(DeliveryException):
    """Message blocked by WeChat platform (empty/sensitive content)."""


class DeliveryUnknownError(DeliveryException):
    """Delivery status could not be determined."""


@dataclass
class DeliveryService:
    """Tracks message delivery attempts and produces receipts.

    Caller (TurnEngine) calls deliver() for each send attempt.
    get_history_writable() returns only delivered messages after turn ends.
    """

    turn_id: str = ""
    max_send_attempts: int = 3
    sent_messages: list[dict[str, Any]] = field(default_factory=list)
    send_attempt_count: int = 0
    failed_attempt_count: int = 0

    async def deliver(
        self,
        text: str,
        tool_call_id: str = "",
        send_fn=None,
    ) -> DeliveryReceipt:
        """Attempt to deliver a text message (async).

        If send_fn is provided, awaits it with the text.
        send_fn should raise DeliveryRateLimitedError, DeliveryBlockedError,
        or DeliveryUnknownError for non-success outcomes.
        Any other Exception is treated as FAILED (checked for unrecoverable).

        Returns DeliveryReceipt indicating the outcome.
        """
        self.send_attempt_count += 1
        message_id = f"msg_{self.turn_id}_{self.send_attempt_count}"
        receipt_id = f"dr_{uuid.uuid4().hex[:12]}"

        if send_fn is not None:
            try:
                await send_fn(text)
                status = DeliveryStatus.DELIVERED
                delivered_at = datetime.utcnow()
                error_code = None
                error_message = None
                retryable = False
            except DeliveryRateLimitedError as e:
                status = DeliveryStatus.RATE_LIMITED
                delivered_at = None
                error_code = "rate_limited"
                error_message = str(e) or "rate limited by WeChat"
                retryable = True
            except DeliveryBlockedError as e:
                status = DeliveryStatus.BLOCKED
                delivered_at = None
                error_code = "blocked_by_platform"
                error_message = str(e) or "message blocked by platform"
                retryable = True
            except DeliveryUnknownError as e:
                status = DeliveryStatus.UNKNOWN
                delivered_at = None
                error_code = "unknown_status"
                error_message = str(e) or "could not confirm delivery"
                retryable = False
            except Exception as e:
                is_unrecoverable = "no_bridge" in str(e).lower()
                status = DeliveryStatus.FAILED
                delivered_at = None
                error_code = "wechat_sender_unavailable" if is_unrecoverable else "send_error"
                error_message = str(e)[:200]
                retryable = not is_unrecoverable
        else:
            status = DeliveryStatus.DELIVERED
            delivered_at = datetime.utcnow()
            error_code = None
            error_message = None
            retryable = False

        receipt = DeliveryReceipt(
            delivery_receipt_id=receipt_id,
            message_id=message_id,
            turn_id=self.turn_id,
            status=status,
            delivered_at=delivered_at,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
        )

        self.sent_messages.append({
            "message_id": message_id,
            "tool_call_id": tool_call_id,
            "text": text,
            "status": status,
            "receipt": receipt,
        })

        if status != DeliveryStatus.DELIVERED:
            self.failed_attempt_count += 1

        return receipt

    def deliver_failing(self, text: str, tool_call_id: str = "") -> DeliveryReceipt:
        """Force a failed delivery (for testing)."""
        self.send_attempt_count += 1
        self.failed_attempt_count += 1
        message_id = f"msg_{self.turn_id}_{self.send_attempt_count}"
        receipt_id = f"dr_{uuid.uuid4().hex[:12]}"

        receipt = DeliveryReceipt(
            delivery_receipt_id=receipt_id,
            message_id=message_id,
            turn_id=self.turn_id,
            status=DeliveryStatus.FAILED,
            error_code="send_error",
            error_message="simulated failure",
            retryable=True,
        )

        self.sent_messages.append({
            "message_id": message_id,
            "tool_call_id": tool_call_id,
            "text": text,
            "status": DeliveryStatus.FAILED,
            "receipt": receipt,
        })

        return receipt

    def deliver_unknown(self, text: str, tool_call_id: str = "") -> DeliveryReceipt:
        """Force a delivery with unknown status (for testing)."""
        self.send_attempt_count += 1
        message_id = f"msg_{self.turn_id}_{self.send_attempt_count}"
        receipt_id = f"dr_{uuid.uuid4().hex[:12]}"

        receipt = DeliveryReceipt(
            delivery_receipt_id=receipt_id,
            message_id=message_id,
            turn_id=self.turn_id,
            status=DeliveryStatus.UNKNOWN,
            error_code="no_confirmation",
            error_message="could not confirm delivery",
            retryable=False,
        )

        self.sent_messages.append({
            "message_id": message_id,
            "tool_call_id": tool_call_id,
            "text": text,
            "status": DeliveryStatus.UNKNOWN,
            "receipt": receipt,
        })

        return receipt

    def get_delivered_texts(self) -> list[str]:
        """Return texts of only delivered messages."""
        return [
            m["text"] for m in self.sent_messages
            if m["status"] == DeliveryStatus.DELIVERED
        ]

    def get_failed_texts(self) -> list[str]:
        """Return texts of failed/unknown messages."""
        return [
            m["text"] for m in self.sent_messages
            if m["status"] in (DeliveryStatus.FAILED, DeliveryStatus.UNKNOWN)
        ]

    def get_history_writable(self) -> list[dict[str, str]]:
        """Return only delivered messages for history writing.

        INV-09: Only delivered messages enter conversation history.
        """
        return [
            {"role": "assistant", "content": m["text"]}
            for m in self.sent_messages
            if m["status"] == DeliveryStatus.DELIVERED
        ]

    @property
    def delivered_count(self) -> int:
        return sum(
            1 for m in self.sent_messages
            if m["status"] == DeliveryStatus.DELIVERED
        )

    @property
    def failed_count(self) -> int:
        return sum(
            1 for m in self.sent_messages
            if m["status"] in (DeliveryStatus.FAILED, DeliveryStatus.UNKNOWN)
        )
