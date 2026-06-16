"""Timed proactive check-in service."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("runtime")

SHANGHAI = ZoneInfo("Asia/Shanghai")
TriggerCallback = Callable[[str, int], Awaitable[None]]


class ProactiveService:
    """Periodically asks the runtime to run a proactive check-in.

    This service intentionally does not post a fake user request to /chat. The
    caller provides an in-process callback that owns the background turn.
    """

    def __init__(
        self,
        user_id: str,
        settings: Any,
        store: Any,
        trigger_callback: TriggerCallback | None = None,
    ) -> None:
        self.user_id = user_id
        self.settings = settings
        self.store = store
        self.trigger_callback = trigger_callback
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def config(self) -> dict[str, Any]:
        from agent.tools import TOOL_RUNTIME_CONFIG

        return TOOL_RUNTIME_CONFIG.get("proactive", {})

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    @property
    def interval_seconds(self) -> int:
        return int(self.config.get("interval_minutes", 30)) * 60

    def _in_time_window(self) -> bool:
        now = datetime.now(SHANGHAI)
        start = int(self.config.get("time_window_start", 8))
        end = int(self.config.get("time_window_end", 22))
        return start <= now.hour < end

    def _cooldown_ok(self) -> bool:
        cooldown = int(self.config.get("cooldown_minutes", 5)) * 60
        last = float(getattr(self.settings, "last_proactive_time", 0.0) or 0.0)
        return (time.time() - last) >= cooldown

    async def start(self) -> None:
        if not self.enabled:
            logger.info("ProactiveService disabled")
            return
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("ProactiveService started interval=%ss", self.interval_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("ProactiveService stopped")

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
                break
            except asyncio.TimeoutError:
                pass
            if not self._stop_event.is_set():
                await self._check_and_trigger()

    async def _check_and_trigger(self) -> None:
        if not self._in_time_window():
            logger.debug("ProactiveService outside time window")
            return
        if not self._cooldown_ok():
            logger.debug("ProactiveService in cooldown")
            return
        await self._send_trigger()

    async def _send_trigger(self) -> None:
        self.settings.last_proactive_time = time.time()
        if hasattr(self.settings, "save"):
            self.settings.save()

        trigger_seq = int(time.time() * 1000)
        await self.store.append_raw_event(
            user_id=self.user_id,
            seq=trigger_seq,
            timestamp=datetime.now().isoformat(),
            direction="in",
            msg_type="proactive_trigger",
            content="__proactive_trigger__",
        )

        if self.trigger_callback is None:
            logger.warning("ProactiveService has no trigger callback configured")
            return
        await self.trigger_callback(self.user_id, trigger_seq)
