"""Tool executors used by the chat agent."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytz

from agent.tool_registry import is_external_tool
from agent.weather_provider import (
    QWeatherProvider,
    WeatherError,
    format_weather_error,
    format_weather_result,
    normalize_date,
)
from memory.long_term import LongTermMemoryError, LongTermMemoryService, MemoryOperation

logger = logging.getLogger(__name__)

TOOL_RUNTIME_CONFIG: dict[str, Any] = {}
LONG_TERM_MEMORY: LongTermMemoryService | None = None
_weather_provider: QWeatherProvider | None = None
_todo_provider: Any = None
_reminder_store: Any = None
_task_scheduler: Any = None


@dataclass(slots=True)
class ToolExecutionContext:
    user_id: str = ""
    bot_id: str = ""
    source: str = "user_message"
    turn_type: str = "user_chat"
    turn: Any = None


ToolExecutor = Callable[[dict[str, Any], ToolExecutionContext], Awaitable[str]]
_EXECUTORS: dict[str, ToolExecutor] = {}


def _register_executor(name: str, fn: ToolExecutor) -> None:
    _EXECUTORS[name] = fn


def registered_executor_names() -> frozenset[str]:
    return frozenset(_EXECUTORS)


def configure_tools(config: dict[str, Any]) -> None:
    global TOOL_RUNTIME_CONFIG
    TOOL_RUNTIME_CONFIG = config or {}
    _init_weather_provider()


def configure_runtime(
    memory_store: Any = None,
    long_term_memory: LongTermMemoryService | None = None,
    task_scheduler: Any = None,
) -> None:
    global LONG_TERM_MEMORY, _reminder_store, _task_scheduler
    del memory_store
    if long_term_memory is not None:
        LONG_TERM_MEMORY = long_term_memory
    if task_scheduler is not None:
        _task_scheduler = task_scheduler
    if _reminder_store is None:
        from memory.reminder_store import ReminderStore

        db_path = str(TOOL_RUNTIME_CONFIG.get("storage", {}).get("db_path", "./data/memory.db"))
        _reminder_store = ReminderStore(db_path)


def _init_weather_provider() -> None:
    global _weather_provider
    weather_cfg = TOOL_RUNTIME_CONFIG.get("weather", {})
    provider_name = str(weather_cfg.get("provider", "qweather")).strip()
    if provider_name != "qweather":
        logger.warning("Unknown weather provider: %s", provider_name)
        _weather_provider = None
        return

    api_key = os.getenv("QWEATHER_API_KEY", "")
    if not api_key:
        logger.warning("QWEATHER_API_KEY not set; weather tool will fail on use")
        _weather_provider = None
        return

    obs_ttl = float(
        weather_cfg.get("observation_cache_ttl_seconds")
        or weather_cfg.get("cache_ttl_seconds", 600)
    )
    _weather_provider = QWeatherProvider(
        api_key=api_key,
        api_host=os.getenv("QWEATHER_API_HOST", ""),
        timeout_seconds=float(weather_cfg.get("timeout_seconds", 5)),
        observation_cache_ttl=obs_ttl,
        forecast_cache_ttl=float(weather_cfg.get("forecast_cache_ttl_seconds", 1800)),
        location_cache_ttl=float(weather_cfg.get("location_cache_ttl_seconds", 86400)),
    )


def _get_todo_provider() -> Any:
    global _todo_provider
    if _todo_provider is None:
        from agent.todo_provider import MSTodoProvider

        _todo_provider = MSTodoProvider()
    return _todo_provider


def _format_todo_result(result: dict[str, Any]) -> str:
    if "error" in result:
        code = str(result.get("error", "unknown"))
        msg = str(result.get("message", "") or code)
        if code == "ambiguous":
            candidates = result.get("candidates", [])
            lines = [f"ambiguous: found {len(candidates)} matching tasks", ""]
            for item in candidates:
                lines.append(f"- task_id: `{item.get('task_id')}` - {item.get('title', '')}")
            return "\n".join(lines)
        return f"{code}: {msg}"

    if result.get("completed"):
        suffix = "already completed" if result.get("already_completed") else "completed"
        return f"{suffix}: {result.get('title', '')}"

    if "value" in result:
        items = result["value"]
        if not items:
            return "No items."
        lines = [f"Items ({len(items)}):", ""]
        for index, item in enumerate(items, 1):
            name = item.get("displayName") or item.get("title") or "Untitled"
            lines.append(f"{index}. {name}")
            if item.get("id"):
                lines.append(f"   ID: `{item['id']}`")
            if item.get("status"):
                lines.append(f"   Status: {item['status']}")
            due = item.get("dueDateTime") or {}
            if due.get("dateTime"):
                lines.append(f"   Due: {due['dateTime']}")
        return "\n".join(lines)

    if result.get("title") and result.get("id"):
        return f"created: {result['title']} (ID: `{result['id']}`)"
    return json.dumps(result, ensure_ascii=False, default=str)


async def _get_time(timezone_name: str = "Asia/Shanghai") -> str:
    try:
        tz = pytz.timezone(timezone_name)
    except pytz.UnknownTimeZoneError:
        timezone_name = "Asia/Shanghai"
        tz = pytz.timezone(timezone_name)
    now = datetime.now(tz)
    return f"{now:%Y-%m-%d %H:%M:%S} ({timezone_name})"


def _default_weather_city() -> str:
    return str(TOOL_RUNTIME_CONFIG.get("weather", {}).get("default_city", "") or "").strip()[:80]


async def _execute_get_weather(args: dict[str, Any]) -> str:
    location = str(args.get("location") or "").strip()[:80] or _default_weather_city()
    if not location:
        return "A location is required."
    if _weather_provider is None:
        return "Weather service is not configured. Set QWEATHER_API_KEY environment variable."

    raw_date = str(args.get("date") or "").strip()
    try:
        date = normalize_date(raw_date) if raw_date else None
        return format_weather_result(await _weather_provider.get_weather(location, date))
    except WeatherError as exc:
        return format_weather_error(exc)
    except Exception as exc:
        logger.exception("weather tool failed")
        return f"Weather lookup failed: {exc}"


def _execute_web_search(args: dict[str, Any]) -> str:
    from agent.search_provider import web_search

    query = str(args.get("query", "")).strip()
    if not query:
        return "invalid_query: query is required"
    max_results = args.get("max_results")
    include_answer = args.get("include_answer")
    return web_search(
        query=query,
        max_results=int(max_results) if max_results is not None else None,
        include_answer=bool(include_answer) if include_answer is not None else None,
        include_domains=args.get("include_domains"),
        exclude_domains=args.get("exclude_domains"),
    )


async def _execute_web_fetch(args: dict[str, Any], context: ToolExecutionContext | None = None) -> str:
    del context
    from agent.search_provider import web_fetch

    url = str(args.get("url", "")).strip()
    if not url:
        return "invalid_url: URL is required"
    return web_fetch(url)


async def _ensure_todo_auth(provider: Any) -> str:
    """确保 To Do 认证完成。返回空字符串表示已认证，否则返回设备码/错误消息。"""
    if not provider.configured:
        return "not_connected: Microsoft To Do is not configured"
    _token, auth_msg = await provider.ensure_auth()
    return auth_msg or ""


async def _execute_todo_list_lists(args: dict[str, Any]) -> str:
    del args
    provider = _get_todo_provider()
    auth_msg = await _ensure_todo_auth(provider)
    if auth_msg:
        return auth_msg
    return _format_todo_result({"value": await provider.get_lists()})


async def _execute_todo_list_tasks(args: dict[str, Any]) -> str:
    provider = _get_todo_provider()
    auth_msg = await _ensure_todo_auth(provider)
    if auth_msg:
        return auth_msg

    try:
        target = await provider.get_default_list()
        list_name = str(args.get("list_name", "")).strip()
        if list_name:
            matches = await provider.find_list_by_name(list_name)
            if not matches:
                return f'list_not_found: no list matching "{list_name}"'
            if len(matches) > 1:
                return "ambiguous: multiple matching task lists"
            target = matches[0]
        tasks = await provider.get_tasks(
            target["id"],
            max_results=int(args.get("max_results", 20) or 20),
            include_completed=bool(args.get("include_completed", False)),
        )
        return _format_todo_result({"value": tasks})
    except Exception as exc:
        return f"provider_error: {exc}"


async def _execute_todo_create_task(args: dict[str, Any]) -> str:
    title = str(args.get("title", "")).strip()
    if not title:
        return "invalid_args: title is required"
    provider = _get_todo_provider()
    auth_msg = await _ensure_todo_auth(provider)
    if auth_msg:
        return auth_msg
    try:
        target = await provider.get_default_list()
        list_name = str(args.get("list_name", "")).strip()
        if list_name:
            matches = await provider.find_list_by_name(list_name)
            if not matches:
                return f'list_not_found: no list matching "{list_name}"'
            if len(matches) > 1:
                return "ambiguous: multiple matching task lists"
            target = matches[0]
        result = await provider.create_task(
            list_id=target["id"],
            title=title,
            due_date=args.get("due_date"),
            reminder_date=args.get("reminder_date"),
            note=args.get("note"),
            importance=str(args.get("importance", "normal")),
        )
        return _format_todo_result(result)
    except Exception as exc:
        return f"provider_error: {exc}"


async def _execute_todo_complete_task(args: dict[str, Any]) -> str:
    provider = _get_todo_provider()
    auth_msg = await _ensure_todo_auth(provider)
    if auth_msg:
        return auth_msg
    task_id = str(args.get("task_id", "")).strip() or None
    task_title = str(args.get("task_title", "")).strip() or None
    if not task_id and not task_title:
        return "invalid_args: task_id or task_title is required"
    try:
        target = await provider.get_default_list()
        list_name = str(args.get("list_name", "")).strip()
        if list_name:
            matches = await provider.find_list_by_name(list_name)
            if not matches:
                return f'list_not_found: no list matching "{list_name}"'
            if len(matches) > 1:
                return "ambiguous: multiple matching task lists"
            target = matches[0]
        return _format_todo_result(await provider.complete_task(
            list_id=target["id"],
            task_id=task_id,
            task_title=task_title,
        ))
    except Exception as exc:
        return f"provider_error: {exc}"


def _parse_trigger_at(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt


def _format_reminder_time(stored: dict[str, Any]) -> str:
    payload = json.loads(stored.get("payload_json") or "{}") if stored.get("payload_json") else {}
    local_time = str(payload.get("original_trigger_at") or stored.get("remind_at") or "").strip()
    utc_time = str(stored.get("trigger_at_utc") or "").strip()
    if local_time and utc_time and local_time != utc_time:
        return f"{local_time} ({utc_time} UTC)"
    return utc_time or local_time


async def _execute_create_reminder(args: dict[str, Any], context: ToolExecutionContext | None = None) -> str:
    trigger_at = str(args.get("trigger_at", "")).strip()
    reminder_note = str(args.get("reminder_note", "")).strip()
    delivery_text = str(args.get("delivery_text", "")).strip()
    if not trigger_at or not reminder_note or not delivery_text:
        return "invalid_args: trigger_at, reminder_note and delivery_text are required"
    try:
        dt = _parse_trigger_at(trigger_at)
    except ValueError:
        return "invalid_format: trigger_at must be ISO 8601 with timezone offset"

    now = datetime.now(dt.tzinfo or timezone(timedelta(hours=8)))
    if dt <= now + timedelta(seconds=10):
        return "too_soon: reminder must be at least 10 seconds in the future"
    if dt > now + timedelta(days=90):
        return "too_far: reminders can be at most 90 days in advance"
    if len(delivery_text) > 500:
        return f"too_long: delivery_text must be <= 500 characters ({len(delivery_text)})"
    if _reminder_store is None and _task_scheduler is None:
        return "reminder_unavailable: reminder service is not initialized"

    user_id = context.user_id if context and context.user_id else "default"
    if _task_scheduler is not None:
        result = await _task_scheduler.schedule_reminder(
            user_id=user_id,
            trigger_at=trigger_at,
            delivery_text=delivery_text,
            reminder_note=reminder_note,
        )
        reminder_id = result["task_id"]
        remind_at = _format_reminder_time(result)
    else:
        result = await _reminder_store.create(user_id=user_id, message=delivery_text, remind_at=trigger_at)
        reminder_id = result["id"]
        remind_at = result["remind_at"]
    return f"created: reminder scheduled\nID: `{reminder_id}`\nTime: {remind_at}\nMessage: {delivery_text}"


async def _execute_list_reminders(args: dict[str, Any], context: ToolExecutionContext | None = None) -> str:
    del args
    if _reminder_store is None and _task_scheduler is None:
        return "reminder_unavailable: reminder service is not initialized"
    user_id = context.user_id if context and context.user_id else "default"
    if _task_scheduler is not None:
        reminders = await _task_scheduler.list_reminders(user_id)
    else:
        reminders = await _reminder_store.list_by_user(user_id, status="pending")
    if not reminders:
        return "No pending reminders."
    lines = [f"Pending reminders ({len(reminders)}):", ""]
    for index, item in enumerate(reminders, 1):
        payload = json.loads(item.get("payload_json") or "{}") if item.get("payload_json") else {}
        lines.append(f"{index}. ID: `{item.get('task_id') or item.get('id')}`")
        lines.append(f"   Time: {_format_reminder_time(item)}")
        lines.append(f"   Message: {payload.get('delivery_text') or item.get('message', '')}")
    return "\n".join(lines)


async def _execute_cancel_reminder(args: dict[str, Any], context: ToolExecutionContext | None = None) -> str:
    task_id = str(args.get("task_id", "")).strip()
    if not task_id:
        return "invalid_args: task_id is required"
    if _reminder_store is None and _task_scheduler is None:
        return "reminder_unavailable: reminder service is not initialized"
    user_id = context.user_id if context and context.user_id else "default"
    if _task_scheduler is not None:
        ok = await _task_scheduler.cancel_reminder(user_id, task_id)
    else:
        ok = await _reminder_store.cancel(task_id, user_id)
    return f"cancelled: reminder `{task_id}`" if ok else f"not_found: pending reminder `{task_id}` was not found"


async def _memory_tool(
    args: dict[str, Any],
    *,
    user_id: str,
    turn_id: str,
    call_id: str,
    source: str,
) -> str:
    command = str(args.get("command") or "")
    path = str(args.get("path") or "")
    if LONG_TERM_MEMORY is None:
        result = {
            "ok": False,
            "code": "memory_unavailable",
            "command": command,
            "path": path,
            "bytes": 0,
            "message": "long-term memory service is unavailable",
        }
    else:
        try:
            result = await LONG_TERM_MEMORY.execute(user_id, MemoryOperation(
                command=command,
                path=path,
                file_text=str(args.get("file_text") or ""),
                old_str=str(args.get("old_str") or ""),
                new_str=str(args.get("new_str") or ""),
            ))
        except LongTermMemoryError as exc:
            result = {
                "ok": False,
                "code": exc.code,
                "command": command,
                "path": exc.path or path,
                "bytes": 0,
                "message": exc.message,
            }
        except Exception:
            logger.exception("memory tool failed")
            result = {
                "ok": False,
                "code": "memory_io_error",
                "command": command,
                "path": path,
                "bytes": 0,
                "message": "memory operation failed",
            }
    logger.info(
        "memory operation source=%s command=%s path=%s ok=%s code=%s turn=%s call=%s",
        source,
        command,
        result.get("path", path),
        result.get("ok"),
        result.get("code"),
        turn_id,
        call_id,
    )
    return json.dumps(result, ensure_ascii=False)


async def execute_tool(
    name: str,
    args: dict[str, Any] | None,
    bot_id: str = "",
    *,
    user_id: str = "",
    turn_id: str = "",
    call_id: str = "",
    source: str = "ordinary_chat",
    turn: Any = None,
) -> str:
    args = args or {}
    if name == "memory":
        return await _memory_tool(
            args,
            user_id=user_id,
            turn_id=turn_id,
            call_id=call_id,
            source=source,
        )
    if not is_external_tool(name):
        return f"Tool is unavailable: {name}"
    executor = _EXECUTORS.get(name)
    if executor is None:
        return f"Tool is unavailable: {name}"
    context = ToolExecutionContext(
        user_id=user_id,
        bot_id=bot_id,
        source=source,
        turn_type=source,
        turn=turn,
    )
    return await executor(args, context)


async def _execute_get_time_adapter(args: dict[str, Any], context: ToolExecutionContext) -> str:
    del context
    return await _get_time(str(args.get("timezone") or "Asia/Shanghai"))


async def _execute_get_weather_adapter(args: dict[str, Any], context: ToolExecutionContext) -> str:
    del context
    return await _execute_get_weather(args)


async def _execute_web_search_adapter(args: dict[str, Any], context: ToolExecutionContext) -> str:
    del context
    return _execute_web_search(args)


async def _execute_todo_list_lists_adapter(args: dict[str, Any], context: ToolExecutionContext) -> str:
    del context
    return await _execute_todo_list_lists(args)


async def _execute_todo_list_tasks_adapter(args: dict[str, Any], context: ToolExecutionContext) -> str:
    del context
    return await _execute_todo_list_tasks(args)


async def _execute_todo_create_task_adapter(args: dict[str, Any], context: ToolExecutionContext) -> str:
    del context
    return await _execute_todo_create_task(args)


async def _execute_todo_complete_task_adapter(args: dict[str, Any], context: ToolExecutionContext) -> str:
    del context
    return await _execute_todo_complete_task(args)


async def _execute_schedule_next_proactive(args: dict[str, Any], context: ToolExecutionContext) -> str:
    if context.source not in {"proactive", "proactive_speak"}:
        return "not_available: schedule_next_proactive is only available during proactive check-ins"
    try:
        delay = int(args.get("trigger_after_seconds"))
    except (TypeError, ValueError):
        return "invalid_args: trigger_after_seconds must be an integer"
    if delay <= 0:
        return "out_of_range: trigger_after_seconds must be positive"
    if context.turn is not None:
        setattr(context.turn, "proactive_interval_candidate", delay)
    return f"scheduled_candidate: next proactive check-in after {delay} seconds"


_register_executor("get_time", _execute_get_time_adapter)
_register_executor("get_weather", _execute_get_weather_adapter)
_register_executor("web_search", _execute_web_search_adapter)
_register_executor("web_fetch", _execute_web_fetch)
_register_executor("todo_list_lists", _execute_todo_list_lists_adapter)
_register_executor("todo_list_tasks", _execute_todo_list_tasks_adapter)
_register_executor("todo_create_task", _execute_todo_create_task_adapter)
_register_executor("todo_complete_task", _execute_todo_complete_task_adapter)
_register_executor("create_reminder", _execute_create_reminder)
_register_executor("list_reminders", _execute_list_reminders)
_register_executor("cancel_reminder", _execute_cancel_reminder)
_register_executor("schedule_next_proactive", _execute_schedule_next_proactive)
