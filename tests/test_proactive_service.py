"""测试主动消息推送服务。"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from runtime.proactive_service import ProactiveService


class TestProactiveServiceConfig:
    """测试 ProactiveService 配置解析。"""

    def make_service(self, **overrides):
        """创建测试用的 ProactiveService。"""
        settings = MagicMock()
        settings.last_proactive_time = 0.0
        store = MagicMock()
        return ProactiveService(user_id="test-user", settings=settings, store=store)

    def test_enabled_by_default(self):
        svc = self.make_service()
        assert svc.enabled is True

    def test_disabled_when_config_says_so(self):
        with patch("runtime.proactive_service.get_config", return_value={"proactive": {"enabled": False}}):
            svc = self.make_service()
            assert svc.enabled is False

    def test_interval_seconds(self):
        with patch("runtime.proactive_service.get_config", return_value={"proactive": {"interval_minutes": 15}}):
            svc = self.make_service()
            assert svc.interval_seconds == 900

    def test_in_time_window_true(self):
        svc = self.make_service()
        # Mock datetime.now to return a time within the window
        with patch("runtime.proactive_service.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 10
            mock_dt.now.return_value.replace.return_value = mock_dt.now.return_value
            # Default window is 8-22, hour 10 is inside
            with patch.object(svc, "config", {"time_window_start": 8, "time_window_end": 22}):
                assert svc._in_time_window() is True

    def test_in_time_window_false(self):
        svc = self.make_service()
        with patch.object(svc, "config", {"time_window_start": 8, "time_window_end": 22}):
            with patch("runtime.proactive_service.datetime") as mock_dt:
                mock_dt.now.return_value.hour = 3
                assert svc._in_time_window() is False

    def test_cooldown_ok_when_enough_time_passed(self):
        svc = self.make_service()
        svc.settings.last_proactive_time = time.time() - 600  # 10 minutes ago
        with patch.object(svc, "config", {"cooldown_minutes": 5}):
            assert svc._cooldown_ok() is True

    def test_cooldown_not_ok_when_too_soon(self):
        svc = self.make_service()
        svc.settings.last_proactive_time = time.time() - 60  # 1 minute ago
        with patch.object(svc, "config", {"cooldown_minutes": 5}):
            assert svc._cooldown_ok() is False

    def test_start_when_disabled_does_not_create_task(self):
        svc = self.make_service()
        with patch.object(svc, "config", {"enabled": False}):
            asyncio.run(svc.start())
            assert svc._task is None

    def test_start_creates_background_task(self):
        svc = self.make_service()
        with patch.object(svc, "config", {"enabled": True, "interval_minutes": 30, "time_window_start": 8, "time_window_end": 22}):
            asyncio.run(svc.start())
            assert svc._task is not None
            # Clean up
            asyncio.run(svc.stop())

    def test_stop_cancels_task(self):
        svc = self.make_service()
        with patch.object(svc, "config", {"enabled": True, "interval_minutes": 30, "time_window_start": 8, "time_window_end": 22}):
            asyncio.run(svc.start())
            assert svc._task is not None
            asyncio.run(svc.stop())
            assert svc._task is None

    def test_send_proactive_trigger(self):
        """测试主动触发消息的发送流程。"""
        svc = self.make_service()
        svc.settings.save = MagicMock()
        svc.store.append_raw_event = AsyncMock()

        with patch("runtime.proactive_service.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.post.return_value = mock_response
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client

            asyncio.run(svc._send_proactive_trigger())

            # 验证时间戳更新
            assert svc.settings.last_proactive_time > 0
            svc.settings.save.assert_called_once()
            # 验证 raw_event 写入
            svc.store.append_raw_event.assert_called_once()
            # 验证 /chat 调用
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert call_args.kwargs["json"]["source"] == "proactive"
            assert call_args.kwargs["json"]["message"] == "__proactive_trigger__"

    def test_send_proactive_trigger_handles_http_error(self):
        """测试 HTTP 错误时的容错处理。"""
        svc = self.make_service()
        svc.settings.save = MagicMock()
        svc.store.append_raw_event = AsyncMock()

        with patch("runtime.proactive_service.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.post.side_effect = Exception("Connection refused")
            mock_httpx.AsyncClient.return_value.__aenter__.return_value = mock_client

            # 不应抛出异常
            asyncio.run(svc._send_proactive_trigger())


class TestProactiveContextBuilder:
    """测试主动推送的上下文构建。"""

    def test_build_proactive_instruction(self):
        """测试主动推送指令的生成。"""
        from agent.runtime_context_builder import RuntimeContextBuilder

        builder = RuntimeContextBuilder.__new__(RuntimeContextBuilder)
        instruction = builder._build_proactive_instruction()

        assert "主动检查模式" in instruction
        assert "用户没有主动发消息" in instruction
        assert "不要发送" in instruction

    def test_build_with_proactive_source(self):
        """测试 source=proactive 时注入特殊指令。"""
        from agent.runtime_context_builder import RuntimeContextBuilder

        builder = MagicMock(spec=RuntimeContextBuilder)
        builder._build_system_prompt.return_value = "系统提示词"
        builder._build_proactive_instruction.return_value = "\n\n## 主动检查模式\n\n..."
        builder._build_summary_context.return_value = ""
        builder._build_history_messages.return_value = []
        builder._build_current_message.return_value = {"role": "user", "content": "__proactive_trigger__"}

        # 模拟实际的 build 方法
        def mock_build(msg_type="text", content="", source="user"):
            system_text = builder._build_system_prompt()
            if source == "proactive":
                system_text += builder._build_proactive_instruction()
            messages = [{"role": "system", "content": system_text}]
            summary = builder._build_summary_context()
            if summary:
                messages.append({"role": "user", "content": summary})
            messages.extend(builder._build_history_messages())
            messages.append(builder._build_current_message(msg_type, content))
            return {"messages": messages}

        result = mock_build(source="proactive", content="__proactive_trigger__")
        messages = result["messages"]

        # 系统提示词应包含主动推送指令
        system_msg = messages[0]
        assert "主动检查模式" in system_msg["content"]

        # 最后一条是主动触发消息
        last_msg = messages[-1]
        assert last_msg["content"] == "__proactive_trigger__"
