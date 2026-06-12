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
