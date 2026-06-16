"""测试 Microsoft To Do Provider 和工具执行器。"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

CONFIG_PATCH = {"client_id": "test-id", "tenant_id": "common"}


class TestTodoProvider:
    def test_not_configured(self):
        from agent.todo_provider import MSTodoProvider
        with patch("agent.todo_provider._get_config", return_value={"client_id": ""}):
            p = MSTodoProvider()
            assert p.configured is False

    def test_configured(self):
        from agent.todo_provider import MSTodoProvider
        with patch("agent.todo_provider._get_config", return_value={"client_id": "test-id"}):
            p = MSTodoProvider()
            assert p.configured is True

    def test_device_flow_not_configured(self):
        from agent.todo_provider import MSTodoProvider
        with patch("agent.todo_provider._get_config", return_value={"client_id": ""}):
            p = MSTodoProvider()
            assert "未配置" in p.initiate_device_flow()

    def test_get_default_list(self):
        from agent.todo_provider import MSTodoProvider
        p = MSTodoProvider()
        p._current_token = "t"
        p._request = AsyncMock(return_value={"value": [
            {"displayName": "Custom", "id": "l1", "wellknownListName": ""},
            {"displayName": "Tasks", "id": "l2", "wellknownListName": "defaultList"},
        ]})
        r = asyncio.run(p.get_default_list())
        assert r["id"] == "l2"

    def test_get_default_list_not_found(self):
        from agent.todo_provider import MSTodoProvider, TodoError
        p = MSTodoProvider()
        p._current_token = "t"
        p._request = AsyncMock(return_value={"value": [{"displayName": "C", "id": "l1", "wellknownListName": ""}]})
        with patch("agent.todo_provider.logger"):
            try:
                asyncio.run(p.get_default_list())
                assert False
            except TodoError as e:
                assert e.code == "default_list_not_found"

    def test_find_list_by_name(self):
        from agent.todo_provider import MSTodoProvider
        p = MSTodoProvider()
        p._current_token = "t"
        p._request = AsyncMock(return_value={"value": [
            {"displayName": "Shopping", "id": "l1"}, {"displayName": "Work", "id": "l2"},
        ]})
        r = asyncio.run(p.find_list_by_name("shop"))
        assert len(r) == 1
        assert r[0]["displayName"] == "Shopping"

    def test_complete_task_unique(self):
        from agent.todo_provider import MSTodoProvider
        p = MSTodoProvider()
        p._current_token = "t"
        p._request = AsyncMock()
        p._request.side_effect = [
            {"value": [{"title": "Buy milk", "id": "t1", "status": "notStarted"}]},
            {"title": "Buy milk", "id": "t1", "status": "completed"},
        ]
        r = asyncio.run(p.complete_task(list_id="l1", task_title="Buy milk"))
        assert r["completed"] is True

    def test_complete_task_ambiguous(self):
        from agent.todo_provider import MSTodoProvider
        p = MSTodoProvider()
        p._current_token = "t"
        p._request = AsyncMock(return_value={"value": [
            {"title": "Buy milk", "id": "t1", "status": "notStarted"},
            {"title": "Buy milk", "id": "t2", "status": "notStarted"},
        ]})
        r = asyncio.run(p.complete_task(list_id="l1", task_title="Buy milk"))
        assert r["error"] == "ambiguous"
        assert len(r["candidates"]) == 2

    def test_complete_task_not_found(self):
        from agent.todo_provider import MSTodoProvider
        p = MSTodoProvider()
        p._current_token = "t"
        p._request = AsyncMock(return_value={"value": []})
        r = asyncio.run(p.complete_task(list_id="l1", task_title="Nothing"))
        assert r["error"] == "not_found"

    def test_complete_task_by_id(self):
        from agent.todo_provider import MSTodoProvider
        p = MSTodoProvider()
        p._current_token = "t"
        p._request = AsyncMock()
        p._request.side_effect = [
            {"title": "Task", "id": "t1", "status": "notStarted"},
            {"title": "Task", "id": "t1", "status": "completed"},
        ]
        r = asyncio.run(p.complete_task(list_id="l1", task_id="t1"))
        assert r["completed"] is True

    def test_create_task(self):
        from agent.todo_provider import MSTodoProvider
        p = MSTodoProvider()
        p._current_token = "t"
        p._request = AsyncMock(return_value={"title": "New", "id": "n1", "status": "notStarted"})
        r = asyncio.run(p.create_task(list_id="l1", title="New", due_date="2026-06-20T00:00:00", importance="high"))
        assert r["title"] == "New"


class TestDeviceFlowStateMachine:
    """测试设备码登录状态机的关键修复。"""

    def test_initiate_device_flow_reuses_pending_flow(self):
        """已有未过期 pending flow 时，initiate_device_flow 不应生成新码。"""
        from agent.todo_provider import MSTodoProvider
        with patch("agent.todo_provider._get_config", return_value=CONFIG_PATCH):
            p = MSTodoProvider()
            # 手动设置一个 pending flow
            p._device_flow_data = {
                "device_code": "DC123",
                "user_code": "ABCD-1234",
                "verification_uri": "https://microsoft.com/devicelogin",
                "interval": 5,
                "expires_in": 900,
            }
            p._flow_expires_at = time.time() + 900

            msg = p.initiate_device_flow()
            # 应返回已有 flow 的 user_code，不调用 MSAL
            assert "ABCD-1234" in msg
            # device_flow_data 不应被覆盖
            assert p._device_flow_data["device_code"] == "DC123"

    def test_initiate_device_flow_creates_new_when_expired(self):
        """pending flow 过期后，initiate_device_flow 应生成新码。"""
        from agent.todo_provider import MSTodoProvider
        with patch("agent.todo_provider._get_config", return_value=CONFIG_PATCH):
            p = MSTodoProvider()
            p._device_flow_data = {
                "device_code": "OLD",
                "user_code": "OLD-CODE",
                "verification_uri": "https://microsoft.com/devicelogin",
                "interval": 5,
            }
            p._flow_expires_at = time.time() - 10  # 已过期

            mock_app = MagicMock()
            mock_app.initiate_device_flow.return_value = {
                "device_code": "NEW",
                "user_code": "NEW-CODE",
                "verification_uri": "https://microsoft.com/devicelogin",
                "interval": 5,
                "expires_in": 900,
            }
            p._app = mock_app

            msg = p.initiate_device_flow()
            assert "NEW-CODE" in msg
            assert p._device_flow_data["device_code"] == "NEW"

    def test_ensure_auth_silent_token(self):
        """有缓存 token 时，ensure_auth 直接返回 token。"""
        from agent.todo_provider import MSTodoProvider
        with patch("agent.todo_provider._get_config", return_value=CONFIG_PATCH):
            p = MSTodoProvider()
            mock_app = MagicMock()
            mock_app.get_accounts.return_value = [{"home_account_id": "acc1"}]
            mock_app.acquire_token_silent.return_value = {"access_token": "silent-tok"}
            p._app = mock_app

            token, auth_msg = asyncio.run(p.ensure_auth())
            assert token == "silent-tok"
            assert auth_msg is None

    def test_ensure_auth_pending_flow_returns_same_message(self):
        """有 pending flow 且用户未完成授权时，返回同一设备码消息。"""
        from agent.todo_provider import MSTodoProvider
        with patch("agent.todo_provider._get_config", return_value=CONFIG_PATCH):
            p = MSTodoProvider()
            p._device_flow_data = {
                "device_code": "DC456",
                "user_code": "WXYZ-7890",
                "verification_uri": "https://microsoft.com/devicelogin",
                "interval": 5,
            }
            p._flow_expires_at = time.time() + 900

            mock_app = MagicMock()
            mock_app.get_accounts.return_value = []
            mock_app.acquire_token_by_device_flow.return_value = {
                "error": "authorization_pending",
            }
            p._app = mock_app

            token, auth_msg = asyncio.run(p.ensure_auth())
            assert token is None
            assert "WXYZ-7890" in auth_msg
            # flow 不应被清除
            assert p._device_flow_data is not None
            assert p._device_flow_data["device_code"] == "DC456"

    def test_ensure_auth_completes_flow(self):
        """用户完成授权后，ensure_auth 返回 token。"""
        from agent.todo_provider import MSTodoProvider
        with patch("agent.todo_provider._get_config", return_value=CONFIG_PATCH):
            p = MSTodoProvider()
            p._device_flow_data = {
                "device_code": "DC789",
                "user_code": "DONE-1234",
                "verification_uri": "https://microsoft.com/devicelogin",
                "interval": 5,
            }
            p._flow_expires_at = time.time() + 900

            mock_app = MagicMock()
            mock_app.get_accounts.return_value = []
            mock_app.acquire_token_by_device_flow.return_value = {
                "access_token": "fresh-tok",
                "token_type": "Bearer",
            }
            p._app = mock_app

            token, auth_msg = asyncio.run(p.ensure_auth())
            assert token == "fresh-tok"
            assert auth_msg is None
            # flow 应被清除
            assert p._device_flow_data is None
            assert p._flow_expires_at == 0

    def test_ensure_auth_creates_new_flow_when_none(self):
        """无 token、无 pending flow 时，ensure_auth 创建新 device flow。"""
        from agent.todo_provider import MSTodoProvider
        with patch("agent.todo_provider._get_config", return_value=CONFIG_PATCH):
            p = MSTodoProvider()

            mock_app = MagicMock()
            mock_app.get_accounts.return_value = []
            mock_app.initiate_device_flow.return_value = {
                "device_code": "DC_NEW",
                "user_code": "FRESH-5678",
                "verification_uri": "https://microsoft.com/devicelogin",
                "interval": 5,
                "expires_in": 900,
            }
            p._app = mock_app

            token, auth_msg = asyncio.run(p.ensure_auth())
            assert token is None
            assert "FRESH-5678" in auth_msg
            assert p._device_flow_data["device_code"] == "DC_NEW"

    def test_ensure_auth_expired_flow_cleared(self):
        """过期的 pending flow 应被清除，然后创建新的。"""
        from agent.todo_provider import MSTodoProvider
        with patch("agent.todo_provider._get_config", return_value=CONFIG_PATCH):
            p = MSTodoProvider()
            p._device_flow_data = {
                "device_code": "EXPIRED",
                "user_code": "OLD-1234",
                "verification_uri": "https://microsoft.com/devicelogin",
                "interval": 5,
            }
            p._flow_expires_at = time.time() - 10  # 已过期

            mock_app = MagicMock()
            mock_app.get_accounts.return_value = []
            mock_app.initiate_device_flow.return_value = {
                "device_code": "DC_NEW2",
                "user_code": "NEW2-5678",
                "verification_uri": "https://microsoft.com/devicelogin",
                "interval": 5,
                "expires_in": 900,
            }
            p._app = mock_app

            token, auth_msg = asyncio.run(p.ensure_auth())
            assert token is None
            assert "NEW2-5678" in auth_msg

    def test_ensure_auth_not_configured(self):
        """未配置时返回 (None, None)。"""
        from agent.todo_provider import MSTodoProvider
        with patch("agent.todo_provider._get_config", return_value={"client_id": ""}):
            p = MSTodoProvider()
            token, auth_msg = asyncio.run(p.ensure_auth())
            assert token is None
            assert auth_msg is None

    def test_auth_pending_property(self):
        """auth_pending 属性正确反映 pending 状态。"""
        from agent.todo_provider import MSTodoProvider
        with patch("agent.todo_provider._get_config", return_value=CONFIG_PATCH):
            p = MSTodoProvider()
            assert p.auth_pending is False

            p._device_flow_data = {"device_code": "X"}
            p._flow_expires_at = time.time() + 900
            assert p.auth_pending is True

            p._flow_expires_at = time.time() - 10
            assert p.auth_pending is False

    def test_concurrent_ensure_auth_same_flow(self):
        """并发调用 ensure_auth 不应生成多个不同的 device code。"""
        from agent.todo_provider import MSTodoProvider
        with patch("agent.todo_provider._get_config", return_value=CONFIG_PATCH):
            p = MSTodoProvider()

            flow_counter = [0]

            def mock_initiate(scopes):
                flow_counter[0] += 1
                return {
                    "device_code": f"DC_CONCURRENT_{flow_counter[0]}",
                    "user_code": f"CON-{flow_counter[0]:04d}",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "interval": 5,
                    "expires_in": 900,
                }

            mock_app = MagicMock()
            mock_app.get_accounts.return_value = []
            mock_app.acquire_token_by_device_flow.return_value = {
                "error": "authorization_pending",
            }
            mock_app.initiate_device_flow.side_effect = mock_initiate
            p._app = mock_app

            async def run_concurrent():
                results = await asyncio.gather(
                    p.ensure_auth(),
                    p.ensure_auth(),
                    p.ensure_auth(),
                )
                return results

            results = asyncio.run(run_concurrent())

            # 只有第一次调用应该创建 flow，后续应复用（被锁序列化）
            assert flow_counter[0] == 1, f"Expected 1 flow creation, got {flow_counter[0]}"

            # 所有结果应返回相同的 user_code
            codes = [r[1] for r in results if r[1]]
            assert all("CON-0001" in c for c in codes), f"Expected same code in all results: {codes}"


class TestTodoToolExecutors:
    def test_list_lists_not_configured(self):
        from agent.tools import _execute_todo_list_lists
        with patch("agent.tools._get_todo_provider") as mock_p:
            mock_p.return_value.configured = False
            result = asyncio.run(_execute_todo_list_lists({}))
            assert "not_connected" in result

    def test_create_task_empty_title(self):
        from agent.tools import _execute_todo_create_task
        result = asyncio.run(_execute_todo_create_task({"title": ""}))
        assert "invalid_args" in result

    def test_complete_task_no_args(self):
        from agent.tools import _execute_todo_complete_task
        with patch("agent.tools._get_todo_provider") as mock_p:
            mock_p.return_value.configured = True
            mock_p.return_value.ensure_auth = AsyncMock(return_value=("token", None))
            mock_p.return_value.get_default_list = AsyncMock(return_value={"id": "l1"})
            result = asyncio.run(_execute_todo_complete_task({}))
            assert "invalid_args" in result

    def test_list_lists_auth_pending(self):
        """认证 pending 时，工具应返回设备码消息而非报错。"""
        from agent.tools import _execute_todo_list_lists
        with patch("agent.tools._get_todo_provider") as mock_p:
            mock_p.return_value.configured = True
            mock_p.return_value.ensure_auth = AsyncMock(
                return_value=(None, "🔑 Microsoft To Do 登录\n\n请打开 https://microsoft.com/devicelogin 并输入以下代码：\n\nABCD-1234")
            )
            result = asyncio.run(_execute_todo_list_lists({}))
            assert "ABCD-1234" in result

    def test_list_lists_authenticated(self):
        """认证成功后，工具应正常调用 API。"""
        from agent.tools import _execute_todo_list_lists
        with patch("agent.tools._get_todo_provider") as mock_p:
            mock_p.return_value.configured = True
            mock_p.return_value.ensure_auth = AsyncMock(return_value=("token", None))
            mock_p.return_value.get_lists = AsyncMock(return_value=[
                {"displayName": "Tasks", "id": "l1"},
            ])
            result = asyncio.run(_execute_todo_list_lists({}))
            assert "Tasks" in result

    def test_format_ambiguous(self):
        from agent.tools import _format_todo_result
        r = _format_todo_result({"error": "ambiguous", "candidates": [
            {"task_id": "t1", "title": "Buy milk", "due": "2026-06-20"},
            {"task_id": "t2", "title": "Buy milk", "due": ""},
        ]})
        assert "ambiguous" in r
        assert "t1" in r
        assert "t2" in r

    def test_format_completed(self):
        from agent.tools import _format_todo_result
        assert "completed" in _format_todo_result({"completed": True, "title": "Done"})

    def test_format_already_completed(self):
        from agent.tools import _format_todo_result
        r = _format_todo_result({"completed": True, "title": "Done", "already_completed": True})
        assert "already completed" in r
