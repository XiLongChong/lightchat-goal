"""Persistent scheduled task runner for reminders and proactive jobs."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agent.output_guard import guard_single_message

logger = logging.getLogger("runtime")

Sender = Callable[[str, str, list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]
ProactiveCallback = Callable[[str, int, str | None, int | None], Awaitable[Any]]

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _parse_datetime(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError("datetime must include timezone")
    return dt


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class TaskScheduler:
    """Run persisted scheduled_tasks records through APScheduler."""

    def __init__(
        self,
        *,
        memory_store: Any,
        send_text: Sender,
        bot_id: str = "default",
        max_attempts: int = 3,
        proactive_callback: ProactiveCallback | None = None,
        proactive_config_getter: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._store = memory_store
        self._send_text = send_text
        self._bot_id = bot_id
        self._max_attempts = max_attempts
        self._proactive_callback = proactive_callback
        self._proactive_config_getter = proactive_config_getter
        self._scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            job_defaults={
                "coalesce": True,
                "misfire_grace_time": 60,
            },
        )

    async def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
        await self._recover_tasks()
        logger.info("TaskScheduler started")

    async def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        logger.info("TaskScheduler stopped")

    async def schedule_reminder(
        self,
        *,
        user_id: str,
        trigger_at: str,
        delivery_text: str,
        reminder_note: str,
        timezone_name: str = "Asia/Shanghai",
    ) -> dict[str, Any]:
        trigger_dt = _parse_datetime(trigger_at)
        task_id = f"rem_{uuid.uuid4().hex[:12]}"
        task = await self._store.create_scheduled_task(
            task_id=task_id,
            user_id=user_id,
            bot_id=self._bot_id,
            task_type="user_reminder",
            trigger_at_utc=_utc_iso(trigger_dt),
            timezone_name=timezone_name,
            payload={
                "delivery_text": delivery_text,
                "reminder_note": reminder_note,
                "original_trigger_at": trigger_at,
            },
            delivery_key=f"{task_id}:v1",
            max_attempts=self._max_attempts,
        )
        self._schedule_job(task)
        return task

    async def list_reminders(self, user_id: str) -> list[dict[str, Any]]:
        rows = await self._store.list_scheduled_tasks(
            user_id=user_id,
            task_type="user_reminder",
            statuses=["scheduled", "firing", "sending", "unknown_after_attempt"],
        )
        for row in rows:
            payload = json.loads(row.get("payload_json") or "{}")
            row.setdefault("id", row.get("task_id", ""))
            row.setdefault("remind_at", row.get("trigger_at_utc", ""))
            row.setdefault("message", payload.get("delivery_text", ""))
        return rows

    async def cancel_reminder(self, user_id: str, task_id: str) -> bool:
        ok = await self._store.cancel_scheduled_task(task_id, user_id)
        if ok:
            try:
                self._scheduler.remove_job(task_id)
            except Exception:
                pass
        return ok

    async def _recover_tasks(self) -> None:
        tasks = await self._store.list_scheduled_tasks(
            statuses=["scheduled", "firing", "sending", "unknown_after_attempt"],
        )
        for task in tasks:
            status = task["status"]
            if status == "scheduled":
                self._schedule_job(task)
            elif status == "firing":
                self._schedule_job(task)
            elif status == "sending":
                await self._store.mark_task_unknown(
                    task["task_id"],
                    int(task["generation"]),
                    "process_restarted_while_sending",
                )
            elif status == "unknown_after_attempt":
                logger.warning("Task %s needs manual review", task["task_id"])

    def set_proactive_callback(self, callback: ProactiveCallback | None) -> None:
        self._proactive_callback = callback

    def _proactive_config(self) -> dict[str, Any]:
        if self._proactive_config_getter is None:
            return {}
        config = self._proactive_config_getter()
        return config if isinstance(config, dict) else {}

    def _frequency_settings(self) -> tuple[str, int, tuple[int, int]]:
        config = self._proactive_config()
        tier = str(config.get("default_frequency") or "moderate")
        frequencies = config.get("frequencies")
        if not isinstance(frequencies, dict):
            frequencies = {}
        selected = frequencies.get(tier)
        if not isinstance(selected, dict):
            selected = {}
        daily_limit = int(selected.get("daily_limit", config.get("daily_limit", 10)) or 10)
        interval_range = selected.get("interval_range_seconds", [1800, 7200])
        if (
            not isinstance(interval_range, list)
            or len(interval_range) != 2
        ):
            interval_range = [1800, 7200]
        low = max(1, int(interval_range[0]))
        high = max(low, int(interval_range[1]))
        return tier, daily_limit, (low, high)

    def _today_quota_date(self) -> str:
        timezone_name = str(
            self._proactive_config().get("quiet_hours_timezone") or "Asia/Shanghai"
        )
        return datetime.now(ZoneInfo(timezone_name)).strftime("%Y-%m-%d")

    def _resolve_proactive_trigger_at(self, delay_seconds: int) -> datetime:
        config = self._proactive_config()
        timezone_name = str(config.get("quiet_hours_timezone") or "Asia/Shanghai")
        zone = ZoneInfo(timezone_name)
        quiet_start = int(config.get("quiet_hours_start", config.get("time_window_end", 23)) or 23)
        quiet_end = int(config.get("quiet_hours_end", config.get("time_window_start", 8)) or 8)
        target = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        local = target.astimezone(zone)

        in_quiet = local.hour >= quiet_start or local.hour < quiet_end
        if in_quiet:
            next_open = local.replace(
                hour=quiet_end,
                minute=0,
                second=0,
                microsecond=0,
            )
            if local.hour >= quiet_start:
                next_open += timedelta(days=1)
            target = next_open.astimezone(timezone.utc)
        return target

    async def schedule_initial_proactive(self, user_id: str) -> dict[str, Any] | None:
        config = self._proactive_config()
        if not bool(config.get("enabled", True)):
            await self.cancel_user_proactive_tasks(user_id, reason="proactive_disabled")
            return None
        delay = int(config.get("first_check_delay_seconds", 120) or 120)
        return await self.schedule_proactive(
            user_id=user_id,
            delay_seconds=delay,
            fallback_reason="initial_after_user_turn",
            clamp_to_frequency=False,
        )

    async def schedule_proactive(
        self,
        *,
        user_id: str,
        delay_seconds: int,
        fallback_reason: str = "",
        clamp_to_frequency: bool = True,
    ) -> dict[str, Any] | None:
        tier, daily_limit, interval_range = self._frequency_settings()
        used = await self._store.get_proactive_quota_used(
            user_id=user_id,
            date=self._today_quota_date(),
        )
        if used >= daily_limit:
            logger.info("proactive quota exhausted user=%s used=%s limit=%s", user_id, used, daily_limit)
            return None

        if clamp_to_frequency:
            low, high = interval_range
            delay_seconds = min(max(int(delay_seconds), low), high)
        else:
            delay_seconds = max(1, int(delay_seconds))
        await self.cancel_user_proactive_tasks(user_id, reason="replace_proactive_schedule")
        task_id = f"pro_{uuid.uuid4().hex[:12]}"
        trigger_at = self._resolve_proactive_trigger_at(delay_seconds)
        task = await self._store.create_scheduled_task(
            task_id=task_id,
            user_id=user_id,
            bot_id=self._bot_id,
            task_type="proactive_speak",
            trigger_at_utc=_utc_iso(trigger_at),
            timezone_name=str(self._proactive_config().get("quiet_hours_timezone") or "Asia/Shanghai"),
            payload={
                "frequency_tier": tier,
                "fallback_reason": fallback_reason,
                "delay_seconds": delay_seconds,
            },
            delivery_key=f"{task_id}:v1",
            max_attempts=1,
        )
        self._schedule_job(task)
        return task

    async def cancel_user_proactive_tasks(self, user_id: str, reason: str = "") -> int:
        tasks = await self._store.list_scheduled_tasks(
            user_id=user_id,
            task_type="proactive_speak",
            statuses=["scheduled", "firing"],
        )
        count = await self._store.cancel_scheduled_tasks(
            user_id=user_id,
            task_type="proactive_speak",
            reason=reason,
        )
        for task in tasks:
            try:
                self._scheduler.remove_job(task["task_id"])
            except Exception:
                pass
        return count

    async def complete_proactive_turn(
        self,
        *,
        turn: Any,
        user_id: str,
        bot_id: str,
        current_task_id: str | None = None,
        current_generation: int | None = None,
    ) -> dict[str, Any]:
        delivered = 0
        for item in getattr(turn, "visible_messages", []):
            status = str(getattr(item, "delivery_status", ""))
            if status in {"sent_confirmed", "delivered"}:
                delivered += 1
        if delivered <= 0:
            return {"status": "skipped", "reason": "no_delivered_messages"}

        config = self._proactive_config()
        if not bool(config.get("enabled", True)):
            return {"status": "skipped", "reason": "proactive_disabled"}

        tier, daily_limit, interval_range = self._frequency_settings()
        date = self._today_quota_date()
        used = await self._store.get_proactive_quota_used(user_id=user_id, date=date)
        next_task: dict[str, Any] | None = None
        if used + 1 < daily_limit:
            candidate = getattr(turn, "proactive_interval_candidate", None)
            fallback_reason = ""
            if not candidate:
                low, high = interval_range
                candidate = int((low + high) / 2)
                fallback_reason = "missing_schedule_next_proactive"
            low, high = interval_range
            candidate = min(max(int(candidate), low), high)
            task_id = f"pro_{uuid.uuid4().hex[:12]}"
            next_task = {
                "task_id": task_id,
                "trigger_at_utc": _utc_iso(self._resolve_proactive_trigger_at(candidate)),
                "timezone": str(config.get("quiet_hours_timezone") or "Asia/Shanghai"),
                "payload": {
                    "frequency_tier": tier,
                    "delay_seconds": candidate,
                    "fallback_reason": fallback_reason,
                },
                "delivery_key": f"{task_id}:v1",
                "max_attempts": 1,
            }

        result = await self._store.commit_proactive_success(
            user_id=user_id,
            bot_id=bot_id,
            date=date,
            frequency_tier=tier,
            current_task_id=current_task_id,
            current_generation=current_generation,
            next_task=next_task,
        )
        if next_task is not None:
            stored = await self._store.get_scheduled_task(next_task["task_id"])
            if stored is not None:
                self._schedule_job(stored)
        return result

    def _schedule_job(self, task: dict[str, Any]) -> None:
        run_date = _parse_datetime(task["trigger_at_utc"])
        if run_date < datetime.now(timezone.utc):
            run_date = datetime.now(timezone.utc)
        self._scheduler.add_job(
            self._fire_task,
            "date",
            run_date=run_date,
            id=task["task_id"],
            args=[task["task_id"], int(task["generation"])],
            replace_existing=True,
            coalesce=True,
        )

    async def _fire_task(self, task_id: str, expected_generation: int) -> None:
        task = await self._store.claim_scheduled_task(task_id, expected_generation)
        if task is None:
            return
        if task["task_type"] == "user_reminder":
            await self._deliver_reminder(task)
            return
        if task["task_type"] == "proactive_speak":
            await self._trigger_proactive(task)
            return
        logger.warning("Unsupported scheduled task type: %s", task["task_type"])
        await self._store.mark_task_failed_or_retry(
            task_id,
            expected_generation,
            f"unsupported task type {task['task_type']}",
        )

    async def _deliver_reminder(self, task: dict[str, Any]) -> None:
        generation = int(task["generation"])
        try:
            payload = json.loads(task.get("payload_json") or "{}")
            text = str(payload.get("delivery_text", "")).strip()
            guarded = guard_single_message(text)
            if not guarded:
                await self._store.mark_task_failed_or_retry(
                    task["task_id"], generation, "blocked_by_guard"
                )
                return

            contact = await self._store.get_contact_state()
            context_token = str(contact.get("context_token", "") or "")
            contact_user = str(contact.get("user_id", "") or "")
            if not context_token or contact_user != task["user_id"]:
                await self._store.mark_task_failed_or_retry(
                    task["task_id"], generation, "no_contact"
                )
                return

            if not await self._store.mark_task_sending(task["task_id"], generation):
                return

            result = await self._send_text(
                task["user_id"],
                context_token,
                [{"text": guarded, "delay": 0.0}],
            )
            status = str((result[0] if result else {}).get("send_status", "unknown_after_attempt"))
            if status in {"delivered", "sent_confirmed"}:
                await self._store.commit_delivered_reminder(
                    task=task,
                    anchor=(
                        "<system-reminder>\n"
                        "A scheduled reminder was delivered by the system. "
                        "This is historical context, not a new user message.\n"
                        "</system-reminder>"
                    ),
                    delivered_text=guarded,
                )
                return
            if status == "unknown_after_attempt":
                await self._store.mark_task_unknown(
                    task["task_id"], generation, str(result)[:500]
                )
                return
            await self._store.mark_task_failed_or_retry(
                task["task_id"], generation, str(result)[:500]
            )
        except Exception as exc:
            logger.exception("Reminder delivery failed for task=%s", task.get("task_id"))
            await self._store.mark_task_unknown(task["task_id"], generation, str(exc))

    async def _trigger_proactive(self, task: dict[str, Any]) -> None:
        generation = int(task["generation"])
        if self._proactive_callback is None:
            await self._store.mark_task_failed_or_retry(
                task["task_id"], generation, "missing_proactive_callback"
            )
            return
        try:
            trigger_seq = int(datetime.now(timezone.utc).timestamp() * 1000)
            await self._proactive_callback(
                task["user_id"],
                trigger_seq,
                task["task_id"],
                generation,
            )
            current = await self._store.get_scheduled_task(task["task_id"])
            if current is not None and current.get("status") == "firing":
                await self._store.mark_task_failed_or_retry(
                    task["task_id"],
                    generation,
                    "proactive_turn_not_committed",
                )
        except Exception as exc:
            logger.exception("Proactive task failed task=%s", task.get("task_id"))
            await self._store.mark_task_unknown(task["task_id"], generation, str(exc))
