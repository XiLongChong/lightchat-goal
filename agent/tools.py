"""Tool implementations — v0.2 Phase 1.

Fact tools: get_time, get_weather.
Terminal tools (send_wechat_messages, skip_reply) are handled by the state machine.
Dead tools removed: send_email, search_memory, read_persona_file, propose_persona_change.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from typing import Any

import httpx
import pytz

from agent.tool_registry import (
    TOOL_REGISTRY,
    get_all_tool_names,
    is_action,
    is_external_tool,
)


# ================================================================
# Whitelist
# ================================================================

TOOL_WHITELIST = get_all_tool_names()

# Backward-compat: rebuild TOOL_DEFINITIONS from registries
def _build_legacy_definitions() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for t in TOOL_REGISTRY.iter_defs():
        result[t.name] = {"description": t.description, "args": list(t.args_schema)}
    return result

TOOL_DEFINITIONS = _build_legacy_definitions()
TOOL_RUNTIME_CONFIG: dict[str, Any] = {}
TOOL_RUNTIME_CONFIG["_current_bot_id"] = ""
_WEATHER_CACHE: dict[str, tuple[float, str]] = {}
logger = logging.getLogger(__name__)


def configure_tools(config: dict[str, Any]):
    """Inject runtime config for tools without coupling tools to FastAPI."""
    global TOOL_RUNTIME_CONFIG
    TOOL_RUNTIME_CONFIG = config or {}


def configure_runtime(memory_store: Any = None):
    """Inject runtime objects that tools may use without importing main.py."""
    TOOL_RUNTIME_CONFIG["_memory_store"] = memory_store


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


# ================================================================
# Tool implementations
# ================================================================

async def _get_time(timezone: str = "Asia/Shanghai") -> str:
    """Get current time in the given timezone."""
    allowed_zones = {
        "Asia/Shanghai", "Asia/Tokyo", "Asia/Seoul",
        "Asia/Singapore", "Asia/Hong_Kong",
        "America/New_York", "America/Los_Angeles",
        "America/Chicago", "Europe/London", "Europe/Paris",
        "Australia/Sydney", "Pacific/Auckland",
    }

    tz_name = str(timezone).strip()
    if tz_name not in allowed_zones:
        tz_name = "Asia/Shanghai"

    try:
        tz = pytz.timezone(tz_name)
        now = datetime.now(tz)
        return f"{now.strftime('%Y年%m月%d日 %H:%M')} ({tz_name})"
    except Exception:
        now = datetime.now()
        return now.strftime("%Y年%m月%d日 %H:%M")


async def _get_weather(city: str = "") -> str:
    """Get weather from wttr.in (free, no API key)."""
    city = str(city).strip()[:50]
    if not city:
        city = _default_weather_city()
    if not city:
        return "请指定城市"

    weather_cfg = TOOL_RUNTIME_CONFIG.get("weather", {})
    ttl = float(weather_cfg.get("cache_ttl_seconds", 600))
    timeout = float(weather_cfg.get("timeout_seconds", 4))

    cache_key = city.lower()
    cached = _WEATHER_CACHE.get(cache_key)
    now = time.time()
    if cached and now - cached[0] < ttl:
        return cached[1]

    url = f"https://wttr.in/{quote(city)}?format=%C+%t+%h+%w&lang=zh"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                result = f"{city}天气：{resp.text.strip()}"
                _WEATHER_CACHE[cache_key] = (now, result)
                return result
            return f"查询{city}天气失败"
    except Exception as e:
        return f"天气查询出错：{e}"


# ================================================================
# send_wechat_messages -- delay computation
# ================================================================

def compute_message_delay(index: int, text: str, total: int, test_mode: bool = False) -> float:
    """Compute natural-feeling delay between WeChat messages.

    Args:
        index: 0-based position in the message batch
        text: the message content
        total: total number of messages in the batch
        test_mode: if True, return 0 delay for fast tests

    Rules:
        - First message (index 0): 0.3-0.7s
        - Subsequent short messages (<60 chars): 0.8-1.6s
        - Subsequent longer messages: 1.5-3.0s
        - Light jitter applied in production mode
    """
    import random as _random

    if test_mode:
        return 0.0

    text_len = len(str(text or ""))

    if index == 0:
        base = 0.3 + _random.random() * 0.4
    elif text_len < 60:
        base = 0.8 + _random.random() * 0.8
    else:
        base = 1.5 + _random.random() * 1.5

    return round(base, 2)


# ================================================================
# Executor
# ================================================================

async def execute_tool(name: str, args: dict[str, Any] | None, bot_id: str = "") -> str:
    """Execute a tool by name. Returns result string.

    v0.2 Phase 1: only fact tools (get_time, get_weather) are dispatched here.
    Terminal tools (send_wechat_messages, skip_reply) are handled by the state machine.
    """
    TOOL_RUNTIME_CONFIG["_current_bot_id"] = bot_id

    args = args or {}

    if is_action(name):
        logger.debug("execute_tool: internal action %s reached executor (no-op)", name)
        return "已处理"

    if name not in TOOL_WHITELIST:
        return f"[工具 '{name}' 不在可用列表中]"

    if not is_external_tool(name):
        logger.warning("execute_tool: unknown tool %s rejected", name)
        return f"[未知工具: {name}]"

    if name == "get_time":
        tz = str(args.get("timezone", "Asia/Shanghai"))[:30]
        return await _get_time(tz)

    if name == "get_weather":
        city = str(args.get("city", ""))[:50]
        return await _get_weather(city)

    return f"[未知工具: {name}]"


def _default_weather_city() -> str:
    weather_cfg = TOOL_RUNTIME_CONFIG.get("weather", {})
    city = str(weather_cfg.get("default_city", "") or "").strip()
    if city:
        return city[:50]

    morning_cfg = TOOL_RUNTIME_CONFIG.get("fixed_messages", {}).get("morning", {})
    city = str(morning_cfg.get("city", "") or "").strip()
    return city[:50]
