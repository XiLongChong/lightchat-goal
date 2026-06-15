"""主动消息推送服务。

后台定时检查是否应该触发模型主动联系用户。
通过构造 __proactive_trigger__ 伪消息复用现有 /chat 流程。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from runtime.config import get_config

logger = logging.getLogger("runtime")


class ProactiveService:
    """主动消息推送后台服务。

    定时检查时间窗口和冷却时间，满足条件时触发主动消息。
    """

    def __init__(self, user_id: str, settings, store):
        self.user_id = user_id
        self.settings = settings
        self.store = store
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def config(self) -> dict:
        cfg = get_config()
        return cfg.get("proactive", {})

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled", True)

    @property
    def interval_seconds(self) -> int:
        return self.config.get("interval_minutes", 30) * 60

    def _in_time_window(self) -> bool:
        """检查当前时间是否在允许推送的窗口内。"""
        now = datetime.now()
        start = self.config.get("time_window_start", 8)
        end = self.config.get("time_window_end", 22)
        return start <= now.hour < end

    def _cooldown_ok(self) -> bool:
        """检查距上次推送是否已过冷却时间。"""
        cooldown = self.config.get("cooldown_minutes", 5) * 60
        last = self.settings.last_proactive_time
        return (time.time() - last) >= cooldown

    async def start(self):
        """启动后台任务。"""
        if not self.enabled:
            logger.info("ProactiveService: 主动推送已禁用")
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"ProactiveService: 已启动（间隔 {self.interval_seconds}s, "
            f"窗口 {self.config.get('time_window_start', 8)}:00-{self.config.get('time_window_end', 22)}:00）"
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
        """后台循环：定期检查并触发主动消息。"""
        while not self._stop_event.is_set():
            try:
                # 等待一个间隔周期，支持提前退出
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval_seconds,
                )
                # 收到停止信号
                break
            except asyncio.TimeoutError:
                # 超时 = 到了检查时间
                pass

            if self._stop_event.is_set():
                break

            await self._check_and_trigger()

    async def _check_and_trigger(self):
        """检查条件并触发主动消息。"""
        if not self._in_time_window():
            logger.debug("ProactiveService: 不在时间窗口内，跳过")
            return

        if not self._cooldown_ok():
            logger.debug("ProactiveService: 冷却中，跳过")
            return

        try:
            await self._send_proactive_trigger()
        except Exception:
            logger.exception("ProactiveService: 触发主动消息失败")

    async def _send_proactive_trigger(self):
        """构造并发送主动触发消息。

        在 raw_events 中插入一条 __proactive_trigger__ 事件，
        然后通过内部 HTTP 调用触发 /chat 端点。
        """
        import uuid

        # 记录触发时间
        self.settings.last_proactive_time = time.time()
        self.settings.save()

        # 写入 trigger 事件到 raw_events
        trigger_seq = int(time.time() * 1000)
        await self.store.append_raw_event(
            user_id=self.user_id,
            seq=trigger_seq,
            timestamp=datetime.now().isoformat(),
            direction="in",
            msg_type="proactive_trigger",
            content="__proactive_trigger__",
        )

        logger.info(f"ProactiveService: 已触发主动检查 (seq={trigger_seq})")

        # 调用 /chat 端点
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
                    logger.warning(f"ProactiveService: /chat 返回 {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"ProactiveService: /chat 请求失败: {e}")
