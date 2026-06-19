"""测试提醒工具。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
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
        with patch("agent.tools._reminder_store", None), patch("agent.tools._task_scheduler", None):
            result = asyncio.run(_execute_create_reminder({
                "trigger_at": "2026-06-20T14:00:00+08:00",
                "reminder_note": "test",
                "delivery_text": "test msg",
            }))
            assert "reminder_unavailable" in result

    def test_create_missing_params(self):
        from agent.tools import _execute_create_reminder
        result = asyncio.run(_execute_create_reminder({}))
        assert "invalid_args" in result

    def test_create_past_time(self):
        from agent.tools import _execute_create_reminder
        result = asyncio.run(_execute_create_reminder({
            "trigger_at": "2020-01-01T00:00:00+08:00",
            "reminder_note": "test",
            "delivery_text": "test",
        }))
        assert "too_soon" in result

    def test_create_invalid_format(self):
        from agent.tools import _execute_create_reminder
        result = asyncio.run(_execute_create_reminder({
            "trigger_at": "not-a-date",
            "reminder_note": "test",
            "delivery_text": "test",
        }))
        assert "invalid_format" in result

    def test_create_too_far(self):
        """超过 90 天的提醒被拒绝。"""
        from agent.tools import _execute_create_reminder
        result = asyncio.run(_execute_create_reminder({
            "trigger_at": "2026-09-30T00:00:00+08:00",
            "reminder_note": "test",
            "delivery_text": "x" * 600,
        }))
        assert "too_far" in result

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
        assert "too_long" in result

    def test_list_no_store(self):
        from agent.tools import _execute_list_reminders
        with patch("agent.tools._reminder_store", None), patch("agent.tools._task_scheduler", None):
            result = asyncio.run(_execute_list_reminders({}))
            assert "reminder_unavailable" in result

    def test_cancel_no_task_id(self):
        from agent.tools import _execute_cancel_reminder
        result = asyncio.run(_execute_cancel_reminder({}))
        assert "invalid_args" in result

    def test_create_success(self):
        from agent.tools import _execute_create_reminder
        mock_store = MagicMock()
        mock_store.create = AsyncMock(return_value={
            "id": "abc123", "message": "test msg",
            "remind_at": "2026-07-01T00:00:00+08:00",
            "status": "pending", "created_at": "2026-01-01",
        })
        with patch("agent.tools._reminder_store", mock_store), patch("agent.tools._task_scheduler", None):
            result = asyncio.run(_execute_create_reminder({
                "trigger_at": "2026-07-01T00:00:00+08:00",
                "reminder_note": "test",
                "delivery_text": "test msg",
            }))
            assert "created" in result
            assert "abc123" in result

    def test_create_success_with_scheduler_shows_local_time(self):
        from agent.tools import _execute_create_reminder

        scheduler = MagicMock()
        scheduler.schedule_reminder = AsyncMock(return_value={
            "task_id": "rem_123",
            "trigger_at_utc": "2026-07-01T02:34:28Z",
            "payload_json": json.dumps({
                "original_trigger_at": "2026-07-01T10:34:28+08:00",
                "delivery_text": "test msg",
            }),
        })
        with patch("agent.tools._task_scheduler", scheduler):
            result = asyncio.run(_execute_create_reminder({
                "trigger_at": "2026-07-01T10:34:28+08:00",
                "reminder_note": "test",
                "delivery_text": "test msg",
            }))
            assert "rem_123" in result
            assert "2026-07-01T10:34:28+08:00" in result
            assert "2026-07-01T02:34:28Z" in result


class TestTaskScheduler:
    def test_delivers_reminder_and_writes_turn(self):
        from memory.store import MemoryStore
        from runtime.task_scheduler import TaskScheduler

        sent = []

        async def run():
            store = MemoryStore(":memory:")

            async def sender(user_id, context_token, messages):
                sent.append((user_id, context_token, messages))
                return [{"send_status": "delivered"}]

            scheduler = TaskScheduler(memory_store=store, send_text=sender)
            await store.ensure_conversation("u1", "default")
            await store.update_contact_state("u1", "ctx", "default")
            future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
            task = await scheduler.schedule_reminder(
                user_id="u1",
                trigger_at=future,
                delivery_text="time to stand up",
                reminder_note="stand",
            )
            await scheduler._fire_task(task["task_id"], task["generation"])
            stored = await store.get_scheduled_task(task["task_id"])
            events = await store.get_recent_events("u1:default")
            await store.close()
            return stored, events

        stored, events = asyncio.run(run())
        assert sent
        assert stored["status"] == "fired"
        assert [e["role"] for e in events] == ["user", "assistant"]
        assert "time to stand up" in str(events[1]["content"])

    def test_commit_proactive_success_creates_next_task(self):
        from memory.store import MemoryStore
        from runtime.task_scheduler import TaskScheduler

        async def run():
            store = MemoryStore(":memory:")

            async def sender(user_id, context_token, messages):
                return [{"send_status": "delivered"}]

            scheduler = TaskScheduler(
                memory_store=store,
                send_text=sender,
                proactive_config_getter=lambda: {
                    "enabled": True,
                    "default_frequency": "moderate",
                    "quiet_hours_start": 23,
                    "quiet_hours_end": 8,
                    "quiet_hours_timezone": "Asia/Shanghai",
                    "frequencies": {
                        "moderate": {
                            "daily_limit": 10,
                            "interval_range_seconds": [60, 3600],
                        }
                    },
                },
            )
            await store.ensure_conversation("u1", "default")
            task = await scheduler.schedule_proactive(user_id="u1", delay_seconds=120)
            claimed = await store.claim_scheduled_task(task["task_id"], task["generation"])
            turn = SimpleNamespace(
                visible_messages=[SimpleNamespace(delivery_status="sent_confirmed")],
                proactive_interval_candidate=300,
            )
            result = await scheduler.complete_proactive_turn(
                turn=turn,
                user_id="u1",
                bot_id="default",
                current_task_id=claimed["task_id"],
                current_generation=claimed["generation"],
            )
            rows = await store.list_scheduled_tasks(user_id="u1", task_type="proactive_speak")
            used = await store.get_proactive_quota_used(
                user_id="u1",
                date=scheduler._today_quota_date(),
            )
            await store.close()
            return result, rows, used

        result, rows, used = asyncio.run(run())
        assert result["next_task_created"] is True
        assert used == 1
        assert [row["status"] for row in rows] == ["fired", "scheduled"]

    def test_proactive_quota_exhausted_skips_next_task(self):
        from memory.store import MemoryStore
        from runtime.task_scheduler import TaskScheduler

        async def run():
            store = MemoryStore(":memory:")

            async def sender(user_id, context_token, messages):
                return [{"send_status": "delivered"}]

            scheduler = TaskScheduler(
                memory_store=store,
                send_text=sender,
                proactive_config_getter=lambda: {
                    "enabled": True,
                    "default_frequency": "minimal",
                    "frequencies": {
                        "minimal": {
                            "daily_limit": 1,
                            "interval_range_seconds": [60, 120],
                        }
                    },
                },
            )
            turn = SimpleNamespace(
                visible_messages=[SimpleNamespace(delivery_status="sent_confirmed")],
                proactive_interval_candidate=None,
            )
            result = await scheduler.complete_proactive_turn(
                turn=turn,
                user_id="u1",
                bot_id="default",
            )
            rows = await store.list_scheduled_tasks(user_id="u1", task_type="proactive_speak")
            used = await store.get_proactive_quota_used(
                user_id="u1",
                date=scheduler._today_quota_date(),
            )
            await store.close()
            return result, rows, used

        result, rows, used = asyncio.run(run())
        assert result["next_task_created"] is False
        assert rows == []
        assert used == 1

    def test_initial_proactive_delay_is_not_clamped_to_frequency_minimum(self):
        from memory.store import MemoryStore
        from runtime.task_scheduler import TaskScheduler, _parse_datetime

        async def run():
            store = MemoryStore(":memory:")

            async def sender(user_id, context_token, messages):
                return [{"send_status": "delivered"}]

            scheduler = TaskScheduler(
                memory_store=store,
                send_text=sender,
                proactive_config_getter=lambda: {
                    "enabled": True,
                    "first_check_delay_seconds": 120,
                    "quiet_hours_start": 99,
                    "quiet_hours_end": 0,
                    "quiet_hours_timezone": "Asia/Shanghai",
                    "frequencies": {
                        "moderate": {
                            "daily_limit": 10,
                            "interval_range_seconds": [1800, 7200],
                        }
                    },
                },
            )
            before = datetime.now(timezone.utc)
            task = await scheduler.schedule_initial_proactive("u1")
            after = datetime.now(timezone.utc)
            trigger_at = _parse_datetime(task["trigger_at_utc"])
            await store.close()
            return before, after, trigger_at

        before, after, trigger_at = asyncio.run(run())
        assert before + timedelta(seconds=90) <= trigger_at
        assert trigger_at <= after + timedelta(seconds=180)

    def test_daily_compression_jobs_are_registered_on_start(self):
        from memory.store import MemoryStore
        from runtime.task_scheduler import TaskScheduler

        async def run():
            store = MemoryStore(":memory:")

            async def sender(user_id, context_token, messages):
                return [{"send_status": "delivered"}]

            async def compression_callback():
                return {"status": "skipped"}

            scheduler = TaskScheduler(
                memory_store=store,
                send_text=sender,
                daily_compression_callback=compression_callback,
            )
            await scheduler.start()
            job_ids = {job.id for job in scheduler._scheduler.get_jobs()}
            await scheduler.stop()
            await store.close()
            return job_ids

        job_ids = asyncio.run(run())
        assert "daily_compression_0356" in job_ids
        assert "daily_compression_0401_0421" in job_ids
