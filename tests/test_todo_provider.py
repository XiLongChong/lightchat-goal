"""测试 Microsoft To Do Provider 和工具执行器。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


class TestTodoProvider:
    """测试 MSTodoProvider 核心方法。"""

    def test_not_configured(self):
        """未配置 client_id 时返回 false。"""
        from agent.todo_provider import MSTodoProvider

        with patch("agent.todo_provider._get_config", return_value={"client_id": ""}):
            provider = MSTodoProvider()
            assert provider.configured is False

    def test_configured(self):
        """已配置时返回 true。"""
        from agent.todo_provider import MSTodoProvider

        with patch("agent.todo_provider._get_config", return_value={"client_id": "test-id"}):
            provider = MSTodoProvider()
            assert provider.configured is True

    def test_initiate_device_flow_not_configured(self):
        """未配置时设备码流程返回错误。"""
        from agent.todo_provider import MSTodoProvider

        with patch("agent.todo_provider._get_config", return_value={"client_id": ""}):
            provider = MSTodoProvider()
            result = provider.initiate_device_flow()
            assert "未配置" in result

    def test_get_default_list_found(self):
        """找到默认列表。"""
        import asyncio
        from agent.todo_provider import MSTodoProvider

        provider = MSTodoProvider()
        provider._current_token = "test-token"
        provider._request = AsyncMock(return_value={
            "value": [
                {"displayName": "Custom", "id": "l1", "wellknownListName": ""},
                {"displayName": "Tasks", "id": "l2", "wellknownListName": "defaultList"},
            ]
        })

        result = asyncio.run(provider.get_default_list())
        assert result["id"] == "l2"
        assert result["wellknownListName"] == "defaultList"

    def test_get_default_list_not_found(self):
        """没有默认列表时抛异常。"""
        import asyncio
        from agent.todo_provider import MSTodoProvider, TodoError

        provider = MSTodoProvider()
        provider._current_token = "test-token"
        provider._request = AsyncMock(return_value={
            "value": [
                {"displayName": "Custom", "id": "l1", "wellknownListName": ""},
            ]
        })

        with patch("agent.todo_provider.logger"):
            try:
                asyncio.run(provider.get_default_list())
                assert False, "Expected TodoError"
            except TodoError as e:
                assert e.code == "default_list_not_found"

    def test_find_list_by_name_match(self):
        """按名称模糊匹配列表。"""
        import asyncio
        from agent.todo_provider import MSTodoProvider

        provider = MSTodoProvider()
        provider._current_token = "test-token"
        provider._request = AsyncMock(return_value={
            "value": [
                {"displayName": "Shopping", "id": "l1"},
                {"displayName": "Work Tasks", "id": "l2"},
            ]
        })

        result = asyncio.run(provider.find_list_by_name("shop"))
        assert len(result) == 1
        assert result[0]["displayName"] == "Shopping"

    def test_complete_task_by_title_unique_match(self):
        """唯一精确匹配时完成任务。"""
        import asyncio
        from agent.todo_provider import MSTodoProvider

        provider = MSTodoProvider()
        provider._current_token = "test-token"
        provider._request = AsyncMock(return_value={
            "value": [
                {"title": "Buy milk", "id": "t1", "status": "notStarted"},
            ]
        })
        # Second call for PATCH
        provider._request.side_effect = [
            {"value": [{"title": "Buy milk", "id": "t1", "status": "notStarted"}]},
            {"title": "Buy milk", "id": "t1", "status": "completed"},
        ]

        result = asyncio.run(provider.complete_task(list_id="l1", task_title="Buy milk"))
        assert result["completed"] is True
        assert result["title"] == "Buy milk"

    def test_complete_task_ambiguous(self):
        """多个同名任务时返回 ambiguous。"""
        import asyncio
        from agent.todo_provider import MSTodoProvider

        provider = MSTodoProvider()
        provider._current_token = "test-token"
        provider._request = AsyncMock(return_value={
            "value": [
                {"title": "Buy milk", "id": "t1", "status": "notStarted"},
                {"title": "Buy milk", "id": "t2", "status": "notStarted"},
            ]
        })

        result = asyncio.run(provider.complete_task(list_id="l1", task_title="Buy milk"))
        assert "error" in result
        assert result["error"] == "ambiguous"
        assert len(result["candidates"]) == 2

    def test_complete_task_not_found(self):
        """无匹配时返回 not_found。"""
        import asyncio
        from agent.todo_provider import MSTodoProvider

        provider = MSTodoProvider()
        provider._current_token = "test-token"
        provider._request = AsyncMock(return_value={"value": []})

        result = asyncio.run(provider.complete_task(list_id="l1", task_title="Nothing"))
        assert result["error"] == "not_found"

    def test_complete_task_by_id(self):
        """按 task_id 完成任务。"""
        import asyncio
        from agent.todo_provider import MSTodoProvider

        provider = MSTodoProvider()
        provider._current_token = "test-token"
        provider._request = AsyncMock()
        provider._request.side_effect = [
            {"title": "Task", "id": "t1", "status": "notStarted"},
            {"title": "Task", "id": "t1", "status": "completed"},
        ]

        result = asyncio.run(provider.complete_task(list_id="l1", task_id="t1"))
        assert result["completed"] is True

    def test_create_task(self):
        """创建任务。"""
        import asyncio
        from agent.todo_provider import MSTodoProvider

        provider = MSTodoProvider()
        provider._current_token = "test-token"
        provider._request = AsyncMock(return_value={
            "title": "New Task", "id": "new-id", "status": "notStarted",
        })

        result = asyncio.run(provider.create_task(
            list_id="l1", title="New Task",
            due_date="2026-06-20T00:00:00",
            importance="high",
        ))
        assert result["title"] == "New Task"
        assert result["id"] == "new-id"


class TestTodoToolExecutors:
    """测试 todo 工具执行器。"""

    def test_todo_list_lists_not_configured(self):
        from agent.tools import _execute_todo_list_lists

        with patch("agent.tools._get_todo_provider") as mock_p:
            mock_p.return_value.configured = False
            result = _execute_todo_list_lists({})
            assert "未配置" in result

    def test_todo_create_task_empty_title(self):
        from agent.tools import _execute_todo_create_task

        result = _execute_todo_create_task({"title": ""})
        assert "不能为空" in result

    def test_todo_complete_task_no_args(self):
        from agent.tools import _execute_todo_complete_task

        result = _execute_todo_complete_task({})
        assert "需要提供" in result

    def test_format_todo_result_ambiguous(self):
        from agent.tools import _format_todo_result

        result = _format_todo_result({
            "error": "ambiguous",
            "candidates": [
                {"task_id": "t1", "title": "Buy milk", "due": "2026-06-20"},
                {"task_id": "t2", "title": "Buy milk", "due": ""},
            ],
        })
        assert "同名任务" in result
        assert "t1" in result
        assert "t2" in result

    def test_format_todo_result_completed(self):
        from agent.tools import _format_todo_result

        result = _format_todo_result({"completed": True, "title": "Done"})
        assert "已完成" in result
        assert "Done" in result

    def test_format_todo_result_already_completed(self):
        from agent.tools import _format_todo_result

        result = _format_todo_result({"completed": True, "title": "Done", "already_completed": True})
        assert "已经" in result
