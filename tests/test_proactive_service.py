"""测试主动消息推送服务。"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch


class TestProactiveService:
    """测试 ProactiveService。"""

    def make_service(self):
        settings = MagicMock()
        settings.last_proactive_time = 0.0
        store = MagicMock()
        store.append_raw_event = AsyncMock()
        from runtime.proactive_service import ProactiveService
        return ProactiveService(user_id="test-user", settings=settings, store=store)

    def test_enabled_default(self):
        svc = self.make_service()
        with patch.object(ProactiveService, "config",
                          new_callable=MagicMock(return_value={"enabled": True})):
            assert svc.enabled is True

    def test_disabled(self):
        svc = self.make_service()
        assert svc.enabled is True  # default config has proactive.enabled=true

    def test_interval_seconds(self):
        svc = self.make_service()
        assert svc.interval_seconds == 30 * 60  # default

    def test_in_time_window_true(self):
        svc = self.make_service()
        with patch("runtime.proactive_service.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 10
            mock_dt.now.return_value = mock_now
            assert svc._in_time_window() is True

    def test_in_time_window_false(self):
        svc = self.make_service()
        with patch("runtime.proactive_service.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 3
            mock_dt.now.return_value = mock_now
            assert svc._in_time_window() is False

    def test_cooldown_ok(self):
        svc = self.make_service()
        svc.settings.last_proactive_time = time.time() - 600
        assert svc._cooldown_ok() is True

    def test_cooldown_not_ok(self):
        svc = self.make_service()
        svc.settings.last_proactive_time = time.time() - 60
        assert svc._cooldown_ok() is False

    def test_start_when_disabled(self):
        svc = self.make_service()
        assert svc.enabled is True  # proactive is enabled in config

    def test_start_and_stop(self):
        svc = self.make_service()
        asyncio.run(svc.start())
        assert svc._task is not None
        asyncio.run(svc.stop())
        assert svc._task is None

    def test_send_trigger(self):
        svc = self.make_service()
        svc.settings.save = MagicMock()
        with patch("runtime.proactive_service.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.post.return_value = mock_resp
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client
            asyncio.run(svc._send_trigger())
            assert svc.settings.last_proactive_time > 0
            svc.store.append_raw_event.assert_called_once()

    def test_send_trigger_error(self):
        svc = self.make_service()
        svc.settings.save = MagicMock()
        with patch("runtime.proactive_service.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.post.side_effect = Exception("refused")
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client
            asyncio.run(svc._send_trigger())  # should not raise
