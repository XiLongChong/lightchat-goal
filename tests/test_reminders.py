"""测试提醒工具。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


class TestReminderStore:
    """测试 ReminderStore CRUD。"""

    def make_store(self):
        from memory.reminder_store import ReminderStore
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        store = ReminderStore(path)
        return store, path

    def test_create_reminder(self):
        store, path = self.make_store()
        try:
            tz = timezone(timedelta(hours=8))
            future = (datetime.now(tz) + timedelta(hours=1)).isoformat()

            result = asyncio.run(store.create("test-user", "开会提醒", future))
            assert result["status"] == "pending"
            assert result["message"] == "开会提醒"
            assert len(result["id"]) == 12

            # Verify in DB
            reminders = asyncio.run(store.list_by_user("test-user"))
            assert len(reminders) == 1
            assert reminders[0]["message"] == "开会提醒"
        finally:
            os.unlink(path)

    def test_get_due(self):
        store, path = self.make_store()
        try:
            tz = timezone(timedelta(hours=8))
            past = (datetime.now(tz) - timedelta(hours=1)).isoformat()
            future = (datetime.now(tz) + timedelta(hours=2)).isoformat()

            asyncio.run(store.create("test-user", "过期提醒", past))
            asyncio.run(store.create("test-user", "未来提醒", future))

            due = asyncio.run(store.get_due("test-user"))
            assert len(due) == 1
            assert due[0]["message"] == "过期提醒"
        finally:
            os.unlink(path)

    def test_mark_sent(self):
        store, path = self.make_store()
        try:
            tz = timezone(timedelta(hours=8))
            future = (datetime.now(tz) + timedelta(hours=1)).isoformat()

            result = asyncio.run(store.create("test-user", "test", future))
            ok = asyncio.run(store.mark_sent(result["id"]))
            assert ok is True

            # Should not be in pending anymore
            pending = asyncio.run(store.list_by_user("test-user", status="pending"))
            assert len(pending) == 0

            sent = asyncio.run(store.list_by_user("test-user", status="sent"))
            assert len(sent) == 1
        finally:
            os.unlink(path)

    def test_cancel(self):
        store, path = self.make_store()
        try:
            tz = timezone(timedelta(hours=8))
            future = (datetime.now(tz) + timedelta(hours=1)).isoformat()

            result = asyncio.run(store.create("test-user", "test", future))
            ok = asyncio.run(store.cancel(result["id"], "test-user"))
            assert ok is True

            pending = asyncio.run(store.list_by_user("test-user", status="pending"))
            assert len(pending) == 0
        finally:
            os.unlink(path)

    def test_cancel_wrong_user(self):
        store, path = self.make_store()
        try:
            tz = timezone(timedelta(hours=8))
            future = (datetime.now(tz) + timedelta(hours=1)).isoformat()

            result = asyncio.run(store.create("user-a", "test", future))
            ok = asyncio.run(store.cancel(result["id"], "user-b"))
            assert ok is False

            # Still pending for user-a
            pending = asyncio.run(store.list_by_user("user-a", status="pending"))
            assert len(pending) == 1
        finally:
            os.unlink(path)


class TestReminderTools:
    """测试提醒工具执行器。"""

    def test_create_reminder_no_store(self):
        from agent.tools import _execute_create_reminder

        with patch("agent.tools._reminder_store", None):
            result = _execute_create_reminder({
                "trigger_at": "2026-06-20T14:00:00+08:00",
                "reminder_note": "test",
                "delivery_text": "test msg",
            })
            assert "未初始化" in result

    def test_create_reminder_missing_params(self):
        from agent.tools import _execute_create_reminder

        result = _execute_create_reminder({})
        assert "缺少必要参数" in result

    def test_create_reminder_past_time(self):
        from agent.tools import _execute_create_reminder

        result = _execute_create_reminder({
            "trigger_at": "2020-01-01T00:00:00+08:00",
            "reminder_note": "test",
            "delivery_text": "test",
        })
        assert "至少 10 秒之后" in result

    def test_create_reminder_invalid_format(self):
        from agent.tools import _execute_create_reminder

        result = _execute_create_reminder({
            "trigger_at": "not-a-date",
            "reminder_note": "test",
            "delivery_text": "test",
        })
        assert "格式无效" in result

    def test_create_reminder_too_long_delivery(self):
        from agent.tools import _execute_create_reminder

        result = _execute_create_reminder({
            "trigger_at": "2026-12-31T00:00:00+08:00",
            "reminder_note": "test",
            "delivery_text": "x" * 600,
        })
        assert "不能超过 500" in result

    def test_list_reminders_no_store(self):
        from agent.tools import _execute_list_reminders

        with patch("agent.tools._reminder_store", None):
            result = _execute_list_reminders({})
            assert "未初始化" in result

    def test_cancel_reminder_no_task_id(self):
        from agent.tools import _execute_cancel_reminder

        result = _execute_cancel_reminder({})
        assert "需要提供 task_id" in result

    def test_create_reminder_success(self):
        from agent.tools import _execute_create_reminder

        mock_store = MagicMock()
        mock_store.create = MagicMock()
        mock_store.create.return_value = asyncio.Future()
        mock_store.create.return_value.set_result({
            "id": "abc123", "message": "test msg",
            "remind_at": "2026-12-31T00:00:00+08:00",
            "status": "pending", "created_at": "2026-01-01",
        })

        with patch("agent.tools._reminder_store", mock_store):
            result = _execute_create_reminder({
                "trigger_at": "2026-12-31T00:00:00+08:00",
                "reminder_note": "test",
                "delivery_text": "test msg",
            })
            assert "已创建" in result
            assert "abc123" in result
