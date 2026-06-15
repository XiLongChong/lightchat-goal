"""Implementations for the current fact tools."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
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

TOOL_RUNTIME_CONFIG: dict[str, Any] = {}
LONG_TERM_MEMORY: LongTermMemoryService | None = None
_weather_provider: QWeatherProvider | None = None
_todo_provider: Any = None  # MSTodoProvider, lazy init
_reminder_store: Any = None  # ReminderStore, set by configure_runtime
logger = logging.getLogger(__name__)


def configure_tools(config: dict[str, Any]) -> None:
    global TOOL_RUNTIME_CONFIG, _weather_provider
    TOOL_RUNTIME_CONFIG = config or {}
    _init_weather_provider()


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

    api_host = os.getenv("QWEATHER_API_HOST", "")
    # Backward-compatible: old cache_ttl_seconds → observation_cache_ttl_seconds
    obs_ttl = float(
        weather_cfg.get("observation_cache_ttl_seconds")
        or weather_cfg.get("cache_ttl_seconds", 600)
    )

    _weather_provider = QWeatherProvider(
        api_key=api_key,
        api_host=api_host,
        timeout_seconds=float(weather_cfg.get("timeout_seconds", 5)),
        observation_cache_ttl=obs_ttl,
        forecast_cache_ttl=float(weather_cfg.get("forecast_cache_ttl_seconds", 1800)),
        location_cache_ttl=float(weather_cfg.get("location_cache_ttl_seconds", 86400)),
    )
    logger.info("QWeather provider initialized (host=%s)", api_host or "api.qweather.com")


def _get_todo_provider():
    """延迟初始化 MSTodoProvider。"""
    global _todo_provider
    if _todo_provider is None:
        from agent.todo_provider import MSTodoProvider
        _todo_provider = MSTodoProvider()
    return _todo_provider


def _format_todo_result(result: dict) -> str:
    """将 MSTodoProvider 返回的 dict 格式化为给模型的字符串。"""
    if "error" in result:
        code = result.get("error", "unknown")
        msg = result.get("message", "")
        if code == "not_connected":
            return f"❌ Microsoft To Do 未连接。{msg}"
        if code == "not_found":
            return f"🔍 未找到：{msg}"
        if code == "ambiguous":
            candidates = result.get("candidates", [])
            lines = [f"⚠️ 找到 {len(candidates)} 个同名任务，请指定 task_id：", ""]
            for c in candidates:
                lines.append(f"  - task_id: `{c['task_id']}` — {c.get('title', '')}")
                if c.get("due"):
                    lines.append(f"    截止: {c['due']}")
            return "\n".join(lines)
        if code == "rate_limited":
            return f"⏳ {msg}"
        return f"❌ {msg or code}"

    if result.get("completed"):
        if result.get("already_completed"):
            return f"✅「{result.get('title', '')}」已经是完成状态。"
        return f"✅ 已完成：{result.get('title', '')}"

    # list results
    if "value" in result:
        items = result["value"]
        if not items:
            return "📋 暂无内容。"
        lines = [f"📋 共 {len(items)} 项：", ""]
        for i, item in enumerate(items, 1):
            name = item.get("displayName") or item.get("title", "未命名")
            item_id = item.get("id", "")
            wl = item.get("wellknownListName", "")
            status = item.get("status", "")
            tag = " 📌" if wl == "defaultList" else ""
            status_icon = {"completed": "✅", "inProgress": "🔄"}.get(status, "⬜")
            lines.append(f"{i}. {status_icon} {name}{tag}")
            if item_id:
                lines.append(f"   ID: `{item_id}`")
            due = item.get("dueDateTime", {})
            if due.get("dateTime"):
                lines.append(f"   📅 {due['dateTime']}")
        return "\n".join(lines)

    if result.get("title") and result.get("id"):
        return f"✅ 已创建：{result['title']} (ID: `{result['id']}`)"

    return json.dumps(result, ensure_ascii=False, default=str)


async def _execute_todo_list_lists(args: dict[str, Any]) -> str:
    del args
    provider = _get_todo_provider()
    if not provider.configured:
        return "❌ Microsoft To Do 未配置（缺少 MS_TODO_CLIENT_ID）。"

    token = await provider.acquire_token()
    if token is None:
        return provider.initiate_device_flow()

    try:
        lists = await provider.get_lists()
    except Exception as e:
        return f"❌ 获取列表失败：{e}"

    return _format_todo_result({"value": lists})


async def _execute_todo_list_tasks(args: dict[str, Any]) -> str:
    provider = _get_todo_provider()
    if not provider.configured:
        return "❌ Microsoft To Do 未配置。"

    token = await provider.acquire_token()
    if token is None:
        return provider.initiate_device_flow()

    try:
        default_list = await provider.get_default_list()
        list_id = default_list["id"]
    except Exception:
        return "❌ 找不到默认任务列表。"

    list_name = str(args.get("list_name", "")).strip()
    if list_name:
        matches = await provider.find_list_by_name(list_name)
        if not matches:
            return f"🔍 未找到名为「{list_name}」的列表。"
        list_id = matches[0]["id"]

    include_completed = bool(args.get("include_completed", False))
    max_results = int(args.get("max_results", 20))

    try:
        tasks = await provider.get_tasks(list_id, max_results=max_results, include_completed=include_completed)
    except Exception as e:
        return f"❌ 获取任务失败：{e}"

    return _format_todo_result({"value": tasks})


async def _execute_todo_create_task(args: dict[str, Any]) -> str:
    title = str(args.get("title", "")).strip()
    if not title:
        return "❌ 任务标题不能为空。"

    provider = _get_todo_provider()
    if not provider.configured:
        return "❌ Microsoft To Do 未配置。"

    token = await provider.acquire_token()
    if token is None:
        return provider.initiate_device_flow()

    try:
        default_list = await provider.get_default_list()
        list_id = default_list["id"]
    except Exception:
        return "❌ 找不到默认任务列表。"

    list_name = str(args.get("list_name", "")).strip()
    if list_name:
        matches = await provider.find_list_by_name(list_name)
        if not matches:
            return f"🔍 未找到名为「{list_name}」的列表。"
        list_id = matches[0]["id"]

    try:
        result = await provider.create_task(
            list_id=list_id,
            title=title,
            due_date=args.get("due_date"),
            reminder_date=args.get("reminder_date"),
            note=args.get("note"),
            importance=str(args.get("importance", "normal")),
        )
    except Exception as e:
        return f"❌ 创建任务失败：{e}"

    return _format_todo_result(result)


async def _execute_todo_complete_task(args: dict[str, Any]) -> str:
    provider = _get_todo_provider()
    if not provider.configured:
        return "❌ Microsoft To Do 未配置。"

    token = await provider.acquire_token()
    if token is None:
        return provider.initiate_device_flow()

    try:
        default_list = await provider.get_default_list()
        list_id = default_list["id"]
    except Exception:
        return "❌ 找不到默认任务列表。"

    list_name = str(args.get("list_name", "")).strip()
    if list_name:
        matches = await provider.find_list_by_name(list_name)
        if not matches:
            return f"🔍 未找到名为「{list_name}」的列表。"
        list_id = matches[0]["id"]

    task_id = str(args.get("task_id", "")).strip() or None
    task_title = str(args.get("task_title", "")).strip() or None

    if not task_id and not task_title:
        return "❌ 需要提供 task_id 或 task_title。"

    try:
        result = await provider.complete_task(
            list_id=list_id,
            task_id=task_id,
            task_title=task_title,
        )
    except Exception as e:
        return f"❌ 完成任务失败：{e}"

    return _format_todo_result(result)


async def _execute_web_search(args: dict[str, Any]) -> str:
    """Execute web_search tool."""
    from agent.search_provider import web_search

    query = str(args.get("query", "")).strip()
    if not query:
        return "❌ 搜索查询不能为空。"

    max_results = args.get("max_results")
    if max_results is not None:
        max_results = int(max_results)

    include_answer = args.get("include_answer")
    if include_answer is not None:
        include_answer = bool(include_answer)

    include_domains = args.get("include_domains")
    exclude_domains = args.get("exclude_domains")

    return await web_search(
        query=query,
        max_results=max_results,
        include_answer=include_answer,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
    )


# ── Reminder tools ──────────────────────────────────────

async def _execute_create_reminder(args: dict[str, Any]) -> str:
    """创建定时提醒。"""
    from datetime import datetime, timedelta, timezone

    global _reminder_store
    if _reminder_store is None:
        return "❌ 提醒服务未初始化。"

    trigger_at = str(args.get("trigger_at", "")).strip()
    reminder_note = str(args.get("reminder_note", "")).strip()
    delivery_text = str(args.get("delivery_text", "")).strip()

    if not trigger_at or not reminder_note or not delivery_text:
        return "❌ 缺少必要参数：trigger_at, reminder_note, delivery_text。"

    # 校验 ISO 8601 格式
    try:
        # 尝试解析 ISO 8601
        dt = datetime.fromisoformat(trigger_at)
    except ValueError:
        return "❌ trigger_at 格式无效，需要 ISO 8601 格式（如 2026-06-16T14:40:00+08:00）。"

    # 校验未来时间
    tz = dt.tzinfo or timezone(timedelta(hours=8))
    now = datetime.now(tz)
    min_advance = timedelta(seconds=10)
    if dt <= now + min_advance:
        return "❌ 提醒时间必须在至少 10 秒之后。"

    # 校验最大提前量
    max_advance = timedelta(days=90)
    if dt > now + max_advance:
        return "❌ 提醒时间不能超过 90 天。"

    # 校验 delivery_text 长度
    if len(delivery_text) > 500:
        return f"❌ delivery_text 不能超过 500 字符（当前 {len(delivery_text)}）。"

    try:
        result = await _reminder_store.create(
            user_id="default",
            message=delivery_text,
            remind_at=trigger_at,
        )
    except Exception as e:
        return f"❌ 创建提醒失败：{e}"

    return (
        f"✅ 提醒已创建！\n"
        f"ID: `{result['id']}`\n"
        f"时间: {trigger_at}\n"
        f"内容: {delivery_text}"
    )


async def _execute_list_reminders(args: dict[str, Any]) -> str:
    """列出提醒。"""
    del args
    global _reminder_store
    if _reminder_store is None:
        return "❌ 提醒服务未初始化。"

    try:
        reminders = await _reminder_store.list_by_user("default", status="pending")
    except Exception as e:
        return f"❌ 获取提醒列表失败：{e}"

    if not reminders:
        return "📋 暂无待处理的提醒。"

    lines = [f"📋 待处理提醒（共 {len(reminders)} 个）：", ""]
    for i, r in enumerate(reminders, 1):
        lines.append(f"{i}. ID: `{r['id']}`")
        lines.append(f"   时间: {r['remind_at']}")
        lines.append(f"   内容: {r['message']}")
        lines.append("")

    return "\n".join(lines).strip()


async def _execute_cancel_reminder(args: dict[str, Any]) -> str:
    """取消提醒。"""
    global _reminder_store
    if _reminder_store is None:
        return "❌ 提醒服务未初始化。"

    task_id = str(args.get("task_id", "")).strip()
    if not task_id:
        return "❌ 需要提供 task_id。"

    try:
        ok = await _reminder_store.cancel(task_id, "default")
    except Exception as e:
        return f"❌ 取消提醒失败：{e}"

    if ok:
        return f"✅ 提醒 `{task_id}` 已取消。"
    return f"🔍 未找到待处理的提醒 `{task_id}`。"


def configure_runtime(
    memory_store: Any = None,
    long_term_memory: LongTermMemoryService | None = None,
) -> None:
    global LONG_TERM_MEMORY, _reminder_store
    del memory_store
    if long_term_memory is not None:
        LONG_TERM_MEMORY = long_term_memory

    # 初始化 ReminderStore
    if _reminder_store is None:
        from memory.reminder_store import ReminderStore
        from runtime.config import get_config
        cfg = get_config()
        db_path = cfg.get("storage", {}).get("db_path", "./data/memory.db")
        _reminder_store = ReminderStore(db_path)


async def _get_time(timezone_name: str = "Asia/Shanghai") -> str:
    try:
        timezone = pytz.timezone(timezone_name)
    except pytz.UnknownTimeZoneError:
        timezone_name = "Asia/Shanghai"
        timezone = pytz.timezone(timezone_name)
    now = datetime.now(timezone)
    return f"{now:%Y-%m-%d %H:%M:%S} ({timezone_name})"


async def _execute_get_weather(args: dict[str, Any]) -> str:
    location = str(args.get("location") or "").strip()[:80]
    if not location:
        location = _default_weather_city()
    if not location:
        return "A location is required."
    if _weather_provider is None:
        return "Weather service is not configured. Set QWEATHER_API_KEY environment variable."

    raw_date = str(args.get("date") or "").strip()
    try:
        date = normalize_date(raw_date) if raw_date else None
    except WeatherError as exc:
        return format_weather_error(exc)

    try:
        result = await _weather_provider.get_weather(location, date)
        return format_weather_result(result)
    except WeatherError as exc:
        return format_weather_error(exc)
    except Exception as exc:
        logger.exception("weather tool failed for location=%s date=%s", location, raw_date)
        return f"Weather lookup failed: {exc}"


async def execute_tool(
    name: str,
    args: dict[str, Any] | None,
    bot_id: str = "",
    *,
    user_id: str = "",
    turn_id: str = "",
    call_id: str = "",
    source: str = "ordinary_chat",
) -> str:
    del bot_id
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
    if name == "get_time":
        return await _get_time(str(args.get("timezone") or "Asia/Shanghai"))
    if name == "get_weather":
        return await _execute_get_weather(args)
    if name == "web_search":
        return await _execute_web_search(args)
    if name == "todo_list_lists":
        return await _execute_todo_list_lists(args)
    if name == "todo_list_tasks":
        return await _execute_todo_list_tasks(args)
    if name == "todo_create_task":
        return await _execute_todo_create_task(args)
    if name == "todo_complete_task":
        return await _execute_todo_complete_task(args)
    if name == "create_reminder":
        return await _execute_create_reminder(args)
    if name == "list_reminders":
        return await _execute_list_reminders(args)
    if name == "cancel_reminder":
        return await _execute_cancel_reminder(args)
    return f"Tool is unavailable: {name}"


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
        "memory operation source=%s user=%s command=%s path=%s ok=%s code=%s bytes=%s turn=%s call=%s",
        source,
        (
            LONG_TERM_MEMORY.user_key(user_id)[:12]
            if LONG_TERM_MEMORY is not None and user_id
            else ""
        ),
        command,
        result.get("path", path),
        result.get("ok"),
        result.get("code"),
        result.get("bytes", 0),
        turn_id,
        call_id,
    )
    return json.dumps(result, ensure_ascii=False)


def _default_weather_city() -> str:
    return str(TOOL_RUNTIME_CONFIG.get("weather", {}).get("default_city", "") or "").strip()[:80]
