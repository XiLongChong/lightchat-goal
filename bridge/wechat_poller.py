"""iLink polling loop — bridges WeChat messages to Goal Chat Agent.

Polls iLink for new messages → forwards to /chat → sends replies back.

Runs as a background task inside the FastAPI process.
Single process, no TypeScript dependency.
"""

import asyncio
import logging
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
        poll_timeout_ms: int = 35_000,
        message_delay: float = 1.2,
    ):
        self.token = token
        self.base_url = base_url
        self.goal_url = goal_url.rstrip("/")
        self.poll_timeout_ms = poll_timeout_ms
        self.message_delay = message_delay
        self.buf = ""
        self.running = False
        self._task: asyncio.Task | None = None

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

        logger.info(f"WeChat [{from_user}]: {text[:60]}")

        # Forward to Goal Chat Agent
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self.goal_url}/chat",
                    json={
                        "messages": [text],
                        "user_id": from_user,
                        "context_token": context_token,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.exception("Goal chat failed")
            await self._send_reply(from_user, "（信号不好，稍等）", context_token)
            return

        # Send each reply message back via iLink
        reply_msgs = data.get("messages", [])
        for i, m in enumerate(reply_msgs):
            reply_text = m.get("text", "")
            delay = m.get("delay", 0)
            if reply_text:
                if i == 0:
                    await asyncio.sleep(max(float(delay or 0), 0))
                else:
                    await asyncio.sleep(max(float(delay or 0), self.message_delay))
                await self._send_reply(from_user, reply_text, context_token)

        logger.info(
            f"WeChat [{from_user}]: replied {len(reply_msgs)} msgs, "
            f"{data.get('iterations', 0)} iters"
        )

    async def _send_reply(self, to: str, text: str, context_token: str):
        """Send a single message via iLink."""
        try:
            await send_message(
                self.token, to, text, context_token, base_url=self.base_url
            )
        except Exception:
            logger.exception("iLink send failed")

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
