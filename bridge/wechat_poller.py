"""iLink polling loop — bridges WeChat messages to Goal Chat Agent.

Polls iLink for new messages → forwards to /chat → sends replies back.

Runs as a background task inside the FastAPI process.
Single process, no TypeScript dependency.
"""

import asyncio
import logging
import time
import uuid
from typing import Any

import httpx
from bridge.ilink_client import get_updates, send_message, get_config

logger = logging.getLogger("goal.bridge")


class WeChatBridge:
    """Long-polls iLink for a single bot, forwarding to Goal."""

    def __init__(
        self,
        token: str,
        base_url: str = "",
        goal_url: str = "http://127.0.0.1:8899",
        chat_api_key: str = "",
        poll_timeout_ms: int = 35_000,
        message_delay: float = 1.2,
        max_reply_delay: float = 3.0,
        buffer_debounce_seconds: float = 5.0,
        buffer_max_wait_seconds: float = 12.0,
    ):
        self.token = token
        self.base_url = base_url
        self.goal_url = goal_url.rstrip("/")
        self.chat_api_key = chat_api_key
        self.poll_timeout_ms = poll_timeout_ms
        self.message_delay = message_delay
        self.max_reply_delay = max_reply_delay
        self.buffer_debounce_seconds = buffer_debounce_seconds
        self.buffer_max_wait_seconds = buffer_max_wait_seconds
        self.buf = ""
        self.running = False
        self._task: asyncio.Task | None = None
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._buffer_started: dict[str, float] = {}
        self._buffer_tasks: dict[str, asyncio.Task] = {}
        self._inbound_seq: dict[str, int] = {}  # v1.2: user_id → next seq

    async def start(self):
        """Begin polling in the background."""
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"Bridge started (goal={self.goal_url})")

    async def stop(self):
        """Stop polling."""
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for task in list(self._buffer_tasks.values()):
            task.cancel()
        if self._buffer_tasks:
            await asyncio.gather(*self._buffer_tasks.values(), return_exceptions=True)
        logger.info("Bridge stopped")

    async def _poll_loop(self):
        while self.running:
            try:
                resp = await get_updates(
                    self.token,
                    self.buf,
                    base_url=self.base_url,
                    timeout_ms=self.poll_timeout_ms,
                )
                self.buf = resp.get("get_updates_buf", self.buf)

                msgs = resp.get("msgs", [])
                if msgs:
                    for msg in msgs:
                        await self._handle_message(msg)
                else:
                    await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Bridge poll error")
                await asyncio.sleep(5)

    async def _handle_message(self, msg: dict[str, Any]):
        """Process one WeChat message through Goal and reply."""
        from_user = msg.get("from_user_id", "")
        context_token = msg.get("context_token", "")
        items = msg.get("item_list", [])
        raw_id = (
            msg.get("msg_id")
            or msg.get("id")
            or msg.get("message_id")
            or f"ilink_{uuid.uuid4().hex[:12]}"
        )
        msg_id = str(raw_id) if raw_id is not None else f"ilink_{uuid.uuid4().hex[:12]}"

        # Extract text from message items
        text_parts = []
        for item in items:
            if item.get("type") == 1:  # text
                ti = item.get("text_item", {})
                t = ti.get("text", "")
                if t:
                    text_parts.append(t)

        text = " ".join(text_parts).strip()
        if not text or not from_user:
            return

        logger.info(f"WeChat [{from_user}]: {text[:60]} msg_id={msg_id[:16]} (buffered)")
        await self._push_buffer(from_user, text, context_token, msg_id)

    async def _push_buffer(self, from_user: str, text: str, context_token: str,
                          msg_id: str = ""):
        now = time.time()
        # v1.2: per-user inbound seq
        seq = self._inbound_seq.get(from_user, 0) + 1
        self._inbound_seq[from_user] = seq

        if from_user not in self._buffers:
            self._buffers[from_user] = []
            self._buffer_started[from_user] = now
        self._buffers[from_user].append({
            "text": text,
            "context_token": context_token,
            "msg_id": msg_id,
            "seq": seq,
            "received_at": now,
        })

        logger.info(f"WeChat [{from_user}]: buffered msg_id={msg_id[:16]} seq={seq}")

        existing = self._buffer_tasks.get(from_user)
        if existing and not existing.done():
            existing.cancel()

        elapsed = now - self._buffer_started.get(from_user, now)
        remaining = self.buffer_max_wait_seconds - elapsed
        delay = 0 if remaining <= 0 else min(self.buffer_debounce_seconds, remaining)
        self._buffer_tasks[from_user] = asyncio.create_task(
            self._flush_later(from_user, delay)
        )

    async def _flush_later(self, from_user: str, delay: float):
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._flush_buffer(from_user)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Buffered WeChat flush failed")

    async def _flush_buffer(self, from_user: str):
        batch = self._buffers.pop(from_user, [])
        self._buffer_started.pop(from_user, None)
        self._buffer_tasks.pop(from_user, None)
        if not batch:
            return

        messages = [item["text"] for item in batch if item.get("text")]
        context_token = next(
            (item.get("context_token", "") for item in reversed(batch) if item.get("context_token")),
            "",
        )
        if not messages:
            return

        # v1.2: input boundary fields
        input_message_ids = [item["msg_id"] for item in batch if item.get("msg_id")]
        input_first_seq = batch[0].get("seq", 0) if batch else 0
        input_latest_seq = batch[-1].get("seq", 0) if batch else 0
        input_cutoff_time = max(
            (item.get("received_at", 0) for item in batch), default=0
        )

        logger.info(
            f"WeChat [{from_user}]: flushing {len(messages)} message(s) "
            f"first_seq={input_first_seq} latest_seq={input_latest_seq}"
        )

        # Forward to Goal Chat Agent
        try:
            headers = {}
            if self.chat_api_key:
                headers["Authorization"] = f"Bearer {self.chat_api_key}"

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self.goal_url}/chat",
                    json={
                        "messages": messages,
                        "user_id": from_user,
                        "context_token": context_token,
                        "input_message_ids": input_message_ids,
                        "input_first_seq": input_first_seq,
                        "input_latest_seq": input_latest_seq,
                        "input_cutoff_time": input_cutoff_time,
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Goal chat HTTP {e.response.status_code}: {e.response.text[:500]}"
            )
            await self._send_reply(from_user, "（信号不好，稍等）", context_token)
            return
        except Exception:
            logger.exception("Goal chat failed")
            await self._send_reply(from_user, "（信号不好，稍等）", context_token)
            return

        # v1.2: messages already sent via wechat_sender callback inside ChatAgent.
        # delivered_count reflects actual deliveries; messages is always [] to prevent double-send.
        delivered = data.get("delivered_count", 0)
        terminal = data.get("terminal_state", "?")
        logger.info(
            f"WeChat [{from_user}]: delivered={delivered} msgs, "
            f"{data.get('iterations', 0)} iters, terminal={terminal}"
        )

    async def _send_reply(self, to: str, text: str, context_token: str):
        """Send a single message via iLink."""
        try:
            await send_message(
                self.token, to, text, context_token, base_url=self.base_url
            )
        except Exception:
            logger.exception("iLink send failed")

    def _bounded_reply_delay(self, delay: Any) -> float:
        """Clamp model-proposed per-message delays to keep chat responsive."""
        try:
            value = float(delay or 0)
        except (TypeError, ValueError):
            value = 0.0
        value = max(value, 0.0)
        max_delay = max(float(self.max_reply_delay or 0), 0.0)
        if max_delay:
            value = min(value, max_delay)
        return value

    async def send_text(self, to: str, text: str, context_token: str = ""):
        """Public send helper for scheduled/background messages."""
        await send_message(
            self.token, to, text, context_token, base_url=self.base_url
        )


# ─── QR Login helpers ────────────────────────────────────────


async def login_qr(base_url: str = "") -> dict | None:
    """Get QR code, poll until scanned. Returns bot credentials or None."""
    from bridge.ilink_client import get_qr_code, poll_qr_status

    qr = await get_qr_code(base_url)
    qrcode = qr.get("qrcode", "")
    qrcode_img = qr.get("qrcode_img_content", "")
    logger.info(f"QR code ready. Scan with WeChat. (len={len(qrcode_img)})")

    for _ in range(120):  # 2 min timeout
        status = await poll_qr_status(qrcode, base_url)
        st = status.get("status", "wait")

        if st == "confirmed":
            return {
                "token": status.get("bot_token", ""),
                "base_url": status.get("baseurl", base_url),
                "bot_id": status.get("ilink_bot_id", ""),
                "user_id": status.get("ilink_user_id", ""),
            }
        elif st == "expired":
            logger.warning("QR code expired")
            return None

        await asyncio.sleep(1)

    logger.warning("QR login timed out")
    return None
