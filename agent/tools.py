"""Tool whitelist and execution — Safeguard #4.

Only tools listed in TOOL_WHITELIST can be invoked.
All arguments are validated and sanitized before execution.
"""

import asyncio
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote
from typing import Any

import httpx
import pytz


# ================================================================
# Whitelist — add tools here
# ================================================================

TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "wechat_reply": {
        "description": "回复微信消息。每次调用发一条消息给用户。可以多次调用来分段回复。text 是回复内容，delay 是发送前等待秒数",
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
    "search_memory": {
        "description": "按需搜索长期记忆。只有需要过去事实、偏好、项目进展时调用；没有结果就不要编造",
        "args": ["query", "top_k"],
    },
    "read_persona_file": {
        "description": "读取当前人设文件原文。只能读配置允许的人设文件，不能读任意项目文件",
        "args": [],
    },
    "propose_persona_change": {
        "description": "提出人设文件修改草案。不会直接写入，系统会发微信让用户确认后再改",
        "args": ["content", "reason"],
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
    subject = str(subject or "来自 bot 的邮件").strip()[:200]
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

    if name == "search_memory":
        store = TOOL_RUNTIME_CONFIG.get("_memory_store")
        if store is None:
            return "记忆系统未连接"
        query = str(args.get("query", "")).strip()[:500]
        top_k = int(args.get("top_k", 5) or 5)
        top_k = max(1, min(8, top_k))
        if not query:
            return "请提供要搜索的记忆关键词"
        memories = await store.search(query, top_k=top_k)
        if not memories:
            return "没有找到相关记忆"
        lines = []
        for i, memory in enumerate(memories, 1):
            lines.append(f"{i}. [{memory.get('category', 'other')}] {memory.get('content', '')}")
        return "\n".join(lines)[:2000]

    if name == "read_persona_file":
        path = _persona_path()
        try:
            return path.read_text(encoding="utf-8")[:4000]
        except FileNotFoundError:
            return "人设文件不存在"
        except Exception as e:
            return f"读取人设文件失败：{e}"

    if name == "propose_persona_change":
        store = TOOL_RUNTIME_CONFIG.get("_memory_store")
        if store is None:
            return "确认系统未连接，无法提交修改草案"
        content = str(args.get("content", "")).strip()
        reason = str(args.get("reason", "")).strip()[:300]
        if not content:
            return "修改草案为空，未提交"
        await store.set_pending_persona_change(content=content, reason=reason)
        return "已提交人设修改草案，等待用户在微信回复“确认/保存/可以”后写入"

    if name == "send_email":
        return await _send_email(
            to=str(args.get("to", "")),
            subject=str(args.get("subject", "")),
            body=str(args.get("body", "")),
        )

    return f"[未知工具: {name}]"


def _persona_path() -> Path:
    agent_cfg = TOOL_RUNTIME_CONFIG.get("agent", {})
    configured = Path(agent_cfg.get("persona_file", "./prompts/persona.md"))
    root = Path.cwd().resolve()
    path = (root / configured).resolve() if not configured.is_absolute() else configured.resolve()
    if root not in path.parents and path != root:
        raise ValueError("persona_file must stay inside project root")
    return path


def _default_weather_city() -> str:
    weather_cfg = TOOL_RUNTIME_CONFIG.get("weather", {})
    city = str(weather_cfg.get("default_city", "") or "").strip()
    if city:
        return city[:50]

    morning_cfg = TOOL_RUNTIME_CONFIG.get("fixed_messages", {}).get("morning", {})
    city = str(morning_cfg.get("city", "") or "").strip()
    return city[:50]
