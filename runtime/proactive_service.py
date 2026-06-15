"""主动消息推送服务。

后台定时检查是否应该触发模型主动联系用户。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("runtime")

SHANGHAI = ZoneInfo("Asia/Shanghai")


class ProactiveService:
    """主动消息推送后台服务。

    定时检查时间窗口和冷却时间，满足条件时通过内部 HTTP 触发 /chat。
    """

    def __init__(self, user_id: str, settings: Any, store: Any):
        self.user_id = user_id
        self.settings = settings
        self.store = store
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def config(self) -> dict:
        from runtime.config import get_config
        return get_config().get("proactive", {})

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    @property
    def interval_seconds(self) -> int:
        return int(self.config.get("interval_minutes", 30)) * 60

    def _in_time_window(self) -> bool:
        """检查当前时间是否在允许推送的窗口内。"""
        now = datetime.now(SHANGHAI)
        start = int(self.config.get("time_window_start", 8))
        end = int(self.config.get("time_window_end", 22))
        return start <= now.hour < end

    def _cooldown_ok(self) -> bool:
        """检查距上次推送是否已过冷却时间。"""
        cooldown = int(self.config.get("cooldown_minutes", 5)) * 60
        last = getattr(self.settings, "last_proactive_time", 0.0)
        return (time.time() - last) >= cooldown

    async def start(self):
        """启动后台任务。"""
        if not self.enabled:
            logger.info("ProactiveService: 主动推送已禁用")
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"ProactiveService: 已启动 (interval={self.interval_seconds}s, "
            f"window={self.config.get('time_window_start', 8)}:00-{self.config.get('time_window_end', 22)}:00)"
        )

    async def stop(self):
        """停止后台任务。"""
        if self._task:
            self._stop_event.set()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("ProactiveService: 已停止")

    async def _run_loop(self):
        """后台循环。"""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
                break
            except asyncio.TimeoutError:
                pass

            if self._stop_event.is_set():
                break

            await self._check_and_trigger()

    async def _check_and_trigger(self):
        """检查条件并触发主动消息。"""
        if not self._in_time_window():
            logger.debug("ProactiveService: 不在时间窗口内")
            return

        if not self._cooldown_ok():
            logger.debug("ProactiveService: 冷却中")
            return

        await self._send_trigger()

    async def _send_trigger(self):
        """发送主动触发到 /chat 端点。"""
        import uuid

        self.settings.last_proactive_time = time.time()
        if hasattr(self.settings, "save"):
            self.settings.save()

        trigger_seq = int(time.time() * 1000)

        # 记录触发事件
        await self.store.append_raw_event(
            user_id=self.user_id,
            seq=trigger_seq,
            timestamp=datetime.now().isoformat(),
            direction="in",
            msg_type="proactive_trigger",
            content="__proactive_trigger__",
        )

        logger.info(f"ProactiveService: 已触发主动检查 (seq={trigger_seq})")

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "http://127.0.0.1:8000/chat",
                    json={
                        "user_id": self.user_id,
                        "message": "__proactive_trigger__",
                        "source": "proactive",
                        "seq": trigger_seq,
                    },
                )
                if resp.status_code == 200:
                    logger.info("ProactiveService: /chat 响应成功")
                else:
                    logger.warning(f"ProactiveService: /chat 返回 {resp.status_code}")
        except Exception as e:
            logger.warning(f"ProactiveService: /chat 请求失败: {e}")
