"""测试主动消息推送服务。"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from runtime.proactive_service import ProactiveService


class TestProactiveService:
    def make_service(self, trigger_callback=None):
        settings = MagicMock()
        settings.last_proactive_time = 0.0
        store = MagicMock()
        store.append_raw_event = AsyncMock()
        return ProactiveService(
            user_id="test-user",
            settings=settings,
            store=store,
            trigger_callback=trigger_callback,
        )

    def test_enabled_default(self):
        svc = self.make_service()
        with patch.object(ProactiveService, "config", new={"enabled": True, "interval_minutes": 30, "time_window_start": 8, "time_window_end": 22}):
            assert svc.enabled is True

    def test_disabled(self):
        svc = self.make_service()
        with patch.object(ProactiveService, "config", new={"enabled": False}):
            assert svc.enabled is False

    def test_interval_seconds(self):
        svc = self.make_service()
        with patch.object(ProactiveService, "config", new={"interval_minutes": 15, "enabled": True}):
            assert svc.interval_seconds == 900

    def test_in_time_window_true(self):
        svc = self.make_service()
        with patch.object(ProactiveService, "config", new={"time_window_start": 8, "time_window_end": 22, "enabled": True}):
            with patch("runtime.proactive_service.datetime") as mock_dt:
                mock_now = MagicMock()
                mock_now.hour = 10
                mock_dt.now.return_value = mock_now
                assert svc._in_time_window() is True

    def test_in_time_window_false(self):
        svc = self.make_service()
        with patch.object(ProactiveService, "config", new={"time_window_start": 8, "time_window_end": 22, "enabled": True}):
            with patch("runtime.proactive_service.datetime") as mock_dt:
                mock_now = MagicMock()
                mock_now.hour = 3
                mock_dt.now.return_value = mock_now
                assert svc._in_time_window() is False

    def test_cooldown_ok(self):
        svc = self.make_service()
        with patch.object(ProactiveService, "config", new={"cooldown_minutes": 5, "enabled": True}):
            svc.settings.last_proactive_time = time.time() - 600
            assert svc._cooldown_ok() is True

    def test_cooldown_not_ok(self):
        svc = self.make_service()
        with patch.object(ProactiveService, "config", new={"cooldown_minutes": 5, "enabled": True}):
            svc.settings.last_proactive_time = time.time() - 60
            assert svc._cooldown_ok() is False

    def test_start_when_enabled(self):
        svc = self.make_service()
        with patch.object(ProactiveService, "config", new={"enabled": True, "interval_minutes": 30, "time_window_start": 8, "time_window_end": 22}):
            asyncio.run(svc.start())
            assert svc._task is not None
            asyncio.run(svc.stop())

    def test_start_when_disabled(self):
        svc = self.make_service()
        with patch.object(ProactiveService, "config", new={"enabled": False}):
            asyncio.run(svc.start())
            assert svc._task is None

    def test_send_trigger(self):
        callback = AsyncMock()
        svc = self.make_service(trigger_callback=callback)
        svc.settings.save = MagicMock()
        asyncio.run(svc._send_trigger())
        assert svc.settings.last_proactive_time > 0
        svc.store.append_raw_event.assert_called_once()
        callback.assert_awaited_once()

    def test_send_trigger_error(self):
        svc = self.make_service()
        svc.settings.save = MagicMock()
        asyncio.run(svc._send_trigger())  # should not raise without a callback
        svc.store.append_raw_event.assert_called_once()
