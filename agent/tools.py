"""Implementations for the current fact tools."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
import pytz

from agent.tool_registry import is_external_tool

TOOL_RUNTIME_CONFIG: dict[str, Any] = {}
_WEATHER_CACHE: dict[str, tuple[float, str]] = {}


def configure_tools(config: dict[str, Any]) -> None:
    global TOOL_RUNTIME_CONFIG
    TOOL_RUNTIME_CONFIG = config or {}


def configure_runtime(memory_store: Any = None) -> None:
    del memory_store


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


async def execute_tool(name: str, args: dict[str, Any] | None, bot_id: str = "") -> str:
    del bot_id
    args = args or {}
    if not is_external_tool(name):
        return f"Tool is unavailable: {name}"
    if name == "get_time":
        return await _get_time(str(args.get("timezone") or "Asia/Shanghai"))
    return await _get_weather(str(args.get("location") or args.get("city") or ""))


def _default_weather_city() -> str:
    return str(TOOL_RUNTIME_CONFIG.get("weather", {}).get("default_city", "") or "").strip()[:80]
