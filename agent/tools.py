"""Implementations for the current fact tools."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
import pytz

from agent.tool_registry import is_external_tool
from memory.long_term import LongTermMemoryError, LongTermMemoryService, MemoryOperation

TOOL_RUNTIME_CONFIG: dict[str, Any] = {}
LONG_TERM_MEMORY: LongTermMemoryService | None = None
_WEATHER_CACHE: dict[str, tuple[float, str]] = {}
logger = logging.getLogger(__name__)


def configure_tools(config: dict[str, Any]) -> None:
    global TOOL_RUNTIME_CONFIG
    TOOL_RUNTIME_CONFIG = config or {}


def configure_runtime(
    memory_store: Any = None,
    long_term_memory: LongTermMemoryService | None = None,
) -> None:
    global LONG_TERM_MEMORY
    del memory_store
    if long_term_memory is not None:
        LONG_TERM_MEMORY = long_term_memory


async def _get_time(timezone_name: str = "Asia/Shanghai") -> str:
    try:
        timezone = pytz.timezone(timezone_name)
    except pytz.UnknownTimeZoneError:
        timezone_name = "Asia/Shanghai"
        timezone = pytz.timezone(timezone_name)
    now = datetime.now(timezone)
    return f"{now:%Y-%m-%d %H:%M:%S} ({timezone_name})"


async def _get_weather(location: str = "") -> str:
    location = str(location or "").strip()[:80] or _default_weather_city()
    if not location:
        return "A location is required."

    weather_config = TOOL_RUNTIME_CONFIG.get("weather", {})
    ttl = float(weather_config.get("cache_ttl_seconds", 600))
    timeout = float(weather_config.get("timeout_seconds", 4))
    cache_key = location.casefold()
    now = time.time()
    cached = _WEATHER_CACHE.get(cache_key)
    if cached and now - cached[0] < ttl:
        return cached[1]

    url = f"https://wttr.in/{quote(location)}?format=%C+%t+%h+%w&lang=zh"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return f"Weather lookup failed for {location}."
        result = f"{location}: {response.text.strip()}"
        _WEATHER_CACHE[cache_key] = (now, result)
        return result
    except Exception as exc:
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
    return await _get_weather(str(args.get("location") or args.get("city") or ""))


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
