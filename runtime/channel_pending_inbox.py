"""In-memory pending inbox for claude_channel sessions.

When a user message targets a ``claude_channel`` session, the Router
stores it in this inbox so a channel server (e.g. an MCP ``claude/channel``
server) can poll it and push it to Claude Code via
``notifications/claude/channel``.

The inbox is purely in-memory — pending messages are lost on restart.
This is acceptable for the prototype stage: the App can re-send a
message with the same ``clientMessageId`` (idempotent at the DB layer)
and the channel will pick it up again.

Delivery semantics: at-least-once.  A message stays in the inbox until
``ack`` is called, at which point it is deleted.  If ``ack`` is missing,
``poll`` keeps returning the same message.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any


class ChannelPendingInbox:
    """Thread/async-safe in-memory pending message store.

    Keys are globally-unique ``deliveryId`` values.  Each message carries
    its ``sessionId`` so ``list_pending`` can filter by session.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # deliveryId -> message dict
        self._messages: dict[str, dict[str, Any]] = {}

    # ── public API ───────────────────────────────────────────────────

    async def add(
        self,
        *,
        remote_agent_id: str,
        session_id: str,
        client_message_id: str,
        content: str,
        created_at: str,
        delivery_id: str | None = None,
        attachments: list | None = None,
    ) -> dict[str, Any]:
        """Add a pending message.

        Idempotent by ``(sessionId, clientMessageId)``: if a pending
        message with the same session + clientMessageId already exists,
        the existing entry is returned unchanged and no new delivery is
        created.

        Returns the message dict (existing or newly created).
        """
        async with self._lock:
            # Idempotency: same session + clientMessageId → return existing
            for msg in self._messages.values():
                if (
                    msg["sessionId"] == session_id
                    and msg["clientMessageId"] == client_message_id
                ):
                    return dict(msg)

            delivery = delivery_id or f"delivery_{uuid.uuid4().hex[:16]}"
            msg: dict[str, Any] = {
                "deliveryId": delivery,
                "remoteAgentId": remote_agent_id,
                "sessionId": session_id,
                "clientMessageId": client_message_id,
                "content": content,
                "createdAt": created_at,
                "attachments": list(attachments) if attachments else [],
                "status": "pending",
            }
            self._messages[delivery] = msg
            return dict(msg)

    async def list_pending(
        self, session_id: str, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return up to *limit* pending messages for *session_id*.

        Only messages with ``status == "pending"`` are returned.  Acked
        messages are suppressed (kept in memory for tracing but not
        re-delivered).
        """
        limit = max(1, min(int(limit), 100))
        async with self._lock:
            result: list[dict[str, Any]] = []
            for msg in self._messages.values():
                if (
                    msg["sessionId"] == session_id
                    and msg["status"] == "pending"
                ):
                    result.append(dict(msg))
                    if len(result) >= limit:
                        break
            return result

    async def ack(self, delivery_id: str) -> dict[str, Any] | None:
        """Remove a pending message from the inbox and mark it as delivered.

        Returns the acked message dict (with ``status`` set to ``"acked"``),
        or ``None`` if *delivery_id* is unknown.  The message is deleted
        from the inbox so memory does not grow unboundedly.

        Acking an already-acked (deleted) message returns ``None``.
        """
        async with self._lock:
            msg = self._messages.pop(delivery_id, None)
            if msg is None:
                return None
            msg["status"] = "acked"
            return dict(msg)

    async def get(self, delivery_id: str) -> dict[str, Any] | None:
        """Return a copy of the message or None."""
        async with self._lock:
            msg = self._messages.get(delivery_id)
            return dict(msg) if msg is not None else None

    async def clear(self) -> None:
        """Remove all pending messages (test helper)."""
        async with self._lock:
            self._messages.clear()
