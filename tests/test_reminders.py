"""测试提醒工具。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch


class TestReminderStore:
    def make_store(self):
        from memory.reminder_store import ReminderStore
        return ReminderStore(":memory:")

    def test_create(self):
        store = self.make_store()
        tz = timezone(timedelta(hours=8))
        future = (datetime.now(tz) + timedelta(hours=1)).isoformat()
        r = asyncio.run(store.create("u1", "开会", future))
        assert r["status"] == "pending"
        assert r["message"] == "开会"
        assert len(r["id"]) == 12

    def test_get_due(self):
        store = self.make_store()
        tz = timezone(timedelta(hours=8))
        past = (datetime.now(tz) - timedelta(hours=1)).isoformat()
        future = (datetime.now(tz) + timedelta(hours=2)).isoformat()
        asyncio.run(store.create("u1", "过期", past))
        asyncio.run(store.create("u1", "未来", future))
        due = asyncio.run(store.get_due("u1"))
        assert len(due) == 1
        assert due[0]["message"] == "过期"

    def test_mark_sent(self):
        store = self.make_store()
        tz = timezone(timedelta(hours=8))
        future = (datetime.now(tz) + timedelta(hours=1)).isoformat()
        r = asyncio.run(store.create("u1", "test", future))
        ok = asyncio.run(store.mark_sent(r["id"]))
        assert ok is True
        pending = asyncio.run(store.list_by_user("u1", status="pending"))
        assert len(pending) == 0

    def test_cancel(self):
        store = self.make_store()
        tz = timezone(timedelta(hours=8))
        future = (datetime.now(tz) + timedelta(hours=1)).isoformat()
        r = asyncio.run(store.create("u1", "test", future))
        ok = asyncio.run(store.cancel(r["id"], "u1"))
        assert ok is True
        pending = asyncio.run(store.list_by_user("u1", status="pending"))
        assert len(pending) == 0

    def test_cancel_wrong_user(self):
        store = self.make_store()
        tz = timezone(timedelta(hours=8))
        future = (datetime.now(tz) + timedelta(hours=1)).isoformat()
        r = asyncio.run(store.create("user-a", "test", future))
        ok = asyncio.run(store.cancel(r["id"], "user-b"))
        assert ok is False
        pending = asyncio.run(store.list_by_user("user-a", status="pending"))
        assert len(pending) == 1


class TestReminderTools:
    def test_create_no_store(self):
        from agent.tools import _execute_create_reminder
        with patch("agent.tools._reminder_store", None):
            result = asyncio.run(_execute_create_reminder({
                "trigger_at": "2026-06-20T14:00:00+08:00",
                "reminder_note": "test",
                "delivery_text": "test msg",
            }))
            assert "未初始化" in result

    def test_create_missing_params(self):
        from agent.tools import _execute_create_reminder
        result = asyncio.run(_execute_create_reminder({}))
        assert "缺少必要参数" in result

    def test_create_past_time(self):
        from agent.tools import _execute_create_reminder
        result = asyncio.run(_execute_create_reminder({
            "trigger_at": "2020-01-01T00:00:00+08:00",
            "reminder_note": "test",
            "delivery_text": "test",
        }))
        assert "至少 10 秒之后" in result

    def test_create_invalid_format(self):
        from agent.tools import _execute_create_reminder
        result = asyncio.run(_execute_create_reminder({
            "trigger_at": "not-a-date",
            "reminder_note": "test",
            "delivery_text": "test",
        }))
        assert "格式无效" in result

    def test_create_too_far(self):
        """超过 90 天的提醒被拒绝。"""
        from agent.tools import _execute_create_reminder
        result = asyncio.run(_execute_create_reminder({
            "trigger_at": "2026-09-30T00:00:00+08:00",
            "reminder_note": "test",
            "delivery_text": "x" * 600,
        }))
        assert "不能超过 90 天" in result

    def test_create_too_long(self):
        """delivery_text 超过 500 字符被拒绝（时间在范围内但文本过长）。"""
        from agent.tools import _execute_create_reminder
        # Use a date just barely within 90 days
        tz = timezone(timedelta(hours=8))
        future = (datetime.now(tz) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        result = asyncio.run(_execute_create_reminder({
            "trigger_at": future,
            "reminder_note": "test",
            "delivery_text": "x" * 600,
        }))
        assert "不能超过 500" in result

    def test_list_no_store(self):
        from agent.tools import _execute_list_reminders
        with patch("agent.tools._reminder_store", None):
            result = asyncio.run(_execute_list_reminders({}))
            assert "未初始化" in result

    def test_cancel_no_task_id(self):
        from agent.tools import _execute_cancel_reminder
        result = asyncio.run(_execute_cancel_reminder({}))
        assert "需要提供 task_id" in result

    def test_create_success(self):
        from agent.tools import _execute_create_reminder
        mock_store = MagicMock()
        mock_store.create = AsyncMock(return_value={
            "id": "abc123", "message": "test msg",
            "remind_at": "2026-12-31T00:00:00+08:00",
            "status": "pending", "created_at": "2026-01-01",
        })
        with patch("agent.tools._reminder_store", mock_store):
            result = asyncio.run(_execute_create_reminder({
                "trigger_at": "2026-12-31T00:00:00+08:00",
                "reminder_note": "test",
                "delivery_text": "test msg",
            }))
            assert "已创建" in result
            assert "abc123" in result
