"""Tool whitelist and execution — Safeguard #4.

Only tools listed in TOOL_WHITELIST can be invoked.
All arguments are validated and sanitized before execution.
"""

import asyncio
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import quote
from typing import Any

import httpx
import pytz


# ================================================================
# Whitelist — add tools here
# ================================================================

TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "wechat_reply": {
        "description": "回复微信消息。每次调用发一条消息给辣椒。可以多次调用来分段回复。text 是回复内容，delay 是发送前等待秒数",
        "args": ["text", "delay"],
    },
    "schedule_wechat_message": {
        "description": "安排未来发送的微信消息。代码会持久化任务并在到期后逐条发送，重启后仍然有效",
        "args": ["delay_minutes", "messages"],
    },
    "get_time": {
        "description": "获取指定时区的当前时间",
        "args": ["timezone"],
    },
    "get_weather": {
        "description": "查询城市的实时天气",
        "args": ["city"],
    },
    "send_email": {
        "description": "发送邮件。需要系统已配置 SMTP。args: to, subject, body",
        "args": ["to", "subject", "body"],
    },
}

TOOL_WHITELIST = set(TOOL_DEFINITIONS.keys())
TOOL_RUNTIME_CONFIG: dict[str, Any] = {}
_WEATHER_CACHE: dict[str, tuple[float, str]] = {}


def configure_tools(config: dict[str, Any]):
    """Inject runtime config for tools without coupling tools to FastAPI."""
    global TOOL_RUNTIME_CONFIG
    TOOL_RUNTIME_CONFIG = config or {}


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


async def _send_email(
    to: str = "",
    subject: str = "",
    body: str = "",
) -> str:
    """Send email through configured SMTP."""
    email_cfg = TOOL_RUNTIME_CONFIG.get("email", {})
    if not _as_bool(email_cfg.get("enabled", False)):
        return "邮件功能未启用"

    host = str(email_cfg.get("smtp_host", "")).strip()
    port = int(email_cfg.get("smtp_port", 465) or 465)
    username = str(email_cfg.get("username", "")).strip()
    password = str(email_cfg.get("password", "")).strip()
    from_addr = str(email_cfg.get("from_addr", username)).strip()
    default_to = str(email_cfg.get("default_to", "")).strip()
    use_ssl = _as_bool(email_cfg.get("use_ssl", True))
    use_starttls = _as_bool(email_cfg.get("use_starttls", False))

    to_addr = str(to or default_to).strip()[:200]
    subject = str(subject or "来自 Yu 的邮件").strip()[:200]
    body = str(body or "").strip()[:5000]

    if not host or not from_addr or not to_addr:
        return "邮件配置不完整"
    if not body:
        return "邮件正文为空，未发送"

    def send_blocking():
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)

        smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_cls(host, port, timeout=15) as smtp:
            if use_starttls and not use_ssl:
                smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)

    try:
        await asyncio.to_thread(send_blocking)
        return f"邮件已发送给 {to_addr}"
    except Exception as e:
        return f"邮件发送失败：{e}"


# ================================================================
# Executor
# ================================================================

async def execute_tool(name: str, args: dict[str, Any] | None) -> str:
    """Execute a tool by name. Returns result string.

    Safeguard #4: only whitelisted tools can run.
    All arguments are sanitized before execution.

    wechat_reply and schedule_wechat_message are intercepted by chat_agent
    before reaching here.
    """
    if name not in TOOL_WHITELIST:
        return f"[工具 '{name}' 不在可用列表中]"

    args = args or {}

    # wechat_reply is handled by chat_agent as a message action.
    # If it reaches here, it's a no-op (shouldn't happen in normal flow).
    if name == "wechat_reply":
        return "已发送"

    if name == "schedule_wechat_message":
        return "已安排"

    if name == "get_time":
        tz = str(args.get("timezone", "Asia/Shanghai"))[:30]
        return await _get_time(tz)

    if name == "get_weather":
        city = str(args.get("city", ""))[:50]
        return await _get_weather(city)

    if name == "send_email":
        return await _send_email(
            to=str(args.get("to", "")),
            subject=str(args.get("subject", "")),
            body=str(args.get("body", "")),
        )

    return f"[未知工具: {name}]"
