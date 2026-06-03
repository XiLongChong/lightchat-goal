"""DeliveryService — unified send/delivery tracking facade.

Phase 7: Abstract send operations, produce DeliveryReceipt,
track per-turn send counts, enforce send budget.

Spec: docs/runtime-specs/03-turn-engine-spec.zh.md §5.4 Delivery Status state machine
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from runtime_contracts import DeliveryReceipt, DeliveryStatus


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

    def deliver(
        self,
        text: str,
        tool_call_id: str = "",
        send_fn=None,
    ) -> DeliveryReceipt:
        """Attempt to deliver a text message.

        If send_fn is provided, calls it to do actual delivery.
        Otherwise, simulates success for testing.

        Returns DeliveryReceipt indicating the outcome.
        """
        self.send_attempt_count += 1
        message_id = f"msg_{self.turn_id}_{self.send_attempt_count}"
        receipt_id = f"dr_{uuid.uuid4().hex[:12]}"

        if send_fn is not None:
            try:
                send_fn(text)
                status = DeliveryStatus.DELIVERED
                delivered_at = datetime.utcnow()
            except Exception:
                status = DeliveryStatus.FAILED
                delivered_at = None
        else:
            # Simulated delivery — successful by default
            status = DeliveryStatus.DELIVERED
            delivered_at = datetime.utcnow()

        receipt = DeliveryReceipt(
            delivery_receipt_id=receipt_id,
            message_id=message_id,
            turn_id=self.turn_id,
            status=status,
            delivered_at=delivered_at,
            retryable=(status == DeliveryStatus.FAILED),
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
