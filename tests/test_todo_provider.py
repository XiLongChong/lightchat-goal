"""测试 Microsoft To Do Provider 和工具执行器。

todo 工具执行器是 async 函数，测试需用 asyncio.run() 调用。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


class TestTodoProvider:
    """测试 MSTodoProvider 核心方法。"""

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
        p._request = AsyncMock(return_value={
            "value": [
                {"displayName": "Custom", "id": "l1", "wellknownListName": ""},
                {"displayName": "Tasks", "id": "l2", "wellknownListName": "defaultList"},
            ]
        })
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
            {"displayName": "Shopping", "id": "l1"},
            {"displayName": "Work", "id": "l2"},
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


class TestTodoToolExecutors:
    """测试 todo 工具执行器（都是 async）。"""

    def test_list_lists_not_configured(self):
        from agent.tools import _execute_todo_list_lists
        with patch("agent.tools._get_todo_provider") as mock_p:
            mock_p.return_value.configured = False
            result = asyncio.run(_execute_todo_list_lists({}))
            assert "未配置" in result

    def test_create_task_empty_title(self):
        from agent.tools import _execute_todo_create_task
        result = asyncio.run(_execute_todo_create_task({"title": ""}))
        assert "不能为空" in result

    def test_complete_task_no_args(self):
        from agent.tools import _execute_todo_complete_task
        result = asyncio.run(_execute_todo_complete_task({}))
        assert "需要提供" in result

    def test_format_ambiguous(self):
        from agent.tools import _format_todo_result
        r = _format_todo_result({"error": "ambiguous", "candidates": [
            {"task_id": "t1", "title": "Buy milk", "due": "2026-06-20"},
            {"task_id": "t2", "title": "Buy milk", "due": ""},
        ]})
        assert "同名任务" in r
        assert "t1" in r
        assert "t2" in r

    def test_format_completed(self):
        from agent.tools import _format_todo_result
        assert "已完成" in _format_todo_result({"completed": True, "title": "Done"})

    def test_format_already_completed(self):
        from agent.tools import _format_todo_result
        r = _format_todo_result({"completed": True, "title": "Done", "already_completed": True})
        assert "已经" in r
