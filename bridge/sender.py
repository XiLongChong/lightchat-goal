"""WeChat message sender — batch sending with delivery tracking.

Extracted from main.py — batch E of main.py responsibility splitting.

Two sending modes are supported:

* ``send_batch`` — the primary runtime path (chat turns, proactive, reminders).
  Tracks per-message delivery status, enforces a per-user send lock, and
  rate-limits via a timestamp-based gap.
* ``send_admin_batch`` — the admin manual-send path.  Pre-guarded messages
  are sent without status tracking; the caller is responsible for guard
  application.  Raises on missing bridge/token.

Neither mode reads ``cfg`` directly; all configuration is injected via
``config_getter``.  The bridge instance is obtained via ``bridge_getter``
so that start/stop/redeploy cycles in ``main.py`` are reflected without
re-injection.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from bridge.wechat_poller import WeChatBridge

logger = logging.getLogger("goal")


class WeChatSender:
    """Send batches of text messages through the WeChat bridge."""

    def __init__(
        self,
        *,
        bridge_getter: Callable[[], WeChatBridge | None],
        config_getter: Callable[[], dict[str, Any]],
    ) -> None:
        self._bridge_getter = bridge_getter
        self._config_getter = config_getter
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._last_send_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Primary runtime path — delivery-status tracking + gap enforcement
    # ------------------------------------------------------------------

    async def send_batch(
        self,
        user_id: str,
        context_token: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Send messages through the WeChat bridge with delivery tracking.

        Args:
            user_id: WeChat user to send to.
            context_token: iLink session token.
            messages: ``[{"text": str, "delay": float}, ...]`` (already
                guarded upstream).

        Returns:
            The same ``messages`` list with ``send_status`` (and
            ``send_error`` on failure) added per message.
        """
        bridge = self._bridge_getter()
        if bridge is None or not bridge.token or not context_token:
            for m in messages:
                m["send_status"] = "failed"
                m["send_error"] = "no_bridge"
            return messages

        wechat_cfg = self._config_getter()
        min_gap = float(wechat_cfg.get("message_delay", 0.8) or 0.8)
        max_reply_delay = float(wechat_cfg.get("max_reply_delay", 3.0) or 3.0)

        lock = self._send_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            for i, m in enumerate(messages):
                text = str(m.get("text", "")).strip()
                if not text:
                    m["send_status"] = "blocked"
                    m["send_error"] = "empty text"
                    continue

                delay = max(0.0, float(m.get("delay", 0) or 0))
                if max_reply_delay:
                    delay = min(delay, max_reply_delay)

                try:
                    elapsed = time.monotonic() - self._last_send_at.get(user_id, 0.0)
                    gap_delay = max(min_gap - elapsed, 0.0)
                    await asyncio.sleep(max(delay, gap_delay))
                    await bridge.send_text(user_id, text, context_token)
                    self._last_send_at[user_id] = time.monotonic()
                    m["send_status"] = "delivered"
                except Exception as e:
                    logger.error(f"wechat_sender: failed to send message[{i}]: {e}")
                    err_msg = str(e)[:200]
                    if "请稍后再试" in err_msg or "rate" in err_msg.lower():
                        m["send_status"] = "rate_limited"
                    else:
                        m["send_status"] = "failed"
                    m["send_error"] = err_msg

        return messages

    # ------------------------------------------------------------------
    # Admin manual-send path — no status tracking, caller applies guard
    # ------------------------------------------------------------------

    async def send_admin_batch(
        self,
        user_id: str,
        context_token: str,
        messages: list[dict[str, Any]],
        *,
        min_gap: float,
        max_reply_delay: float,
        text_guard: Callable[[str], str] | None = None,
    ) -> None:
        """Send pre-guarded messages without per-message status tracking.

        Used by the admin/app manual-send path where ``guard_messages`` is
        applied upstream and per-message delivery status is not tracked.

        Args:
            user_id: WeChat user to send to.
            context_token: iLink session token.
            messages: Pre-guarded message list (output of ``guard_messages``).
            min_gap: Minimum gap between consecutive sends (seconds).
            max_reply_delay: Upper clamp for per-message delay (seconds;
                ``0`` disables clamping).
            text_guard: Optional per-message text guard callable (e.g.
                ``guard_single_message``).  When provided, applied inside
                the loop so that index-based delay semantics are preserved
                even if a message becomes empty after guarding.

        Raises:
            RuntimeError: If the bridge is not configured or
                ``context_token`` is empty.
        """
        bridge = self._bridge_getter()
        if bridge is None:
            raise RuntimeError("WeChat bridge is not configured")
        if not context_token:
            raise RuntimeError("missing iLink context_token")

        for i, m in enumerate(messages):
            raw_text = str(m.get("text", ""))
            text = text_guard(raw_text) if text_guard else raw_text.strip()
            if not text:
                continue

            delay = max(float(m.get("delay", 0) or 0), 0.0)
            if max_reply_delay:
                delay = min(delay, max_reply_delay)
            if i == 0:
                await asyncio.sleep(max(delay, 0))
            else:
                await asyncio.sleep(max(delay, min_gap))
            await bridge.send_text(user_id, text, context_token)
