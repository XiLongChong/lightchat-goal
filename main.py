"""Goal text-chat agent and WeChat bridge entry point."""

import asyncio
import secrets
import uuid
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, time as datetime_time
from typing import Any
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from agent.chat_agent import ChatAgent
from agent.compressor import compress_conversation
from agent.context_builder import build_chat_system_prompt
from agent.llm_gateway import LLMGateway
from agent.output_guard import guard_messages, guard_single_message
from agent.runtime_context_builder import ContextBuilder
from agent.state_machine import ModelProfile, TurnResult, TurnScratchpad, TurnState, DeliveryStatus
from agent.tool_exposure_policy import ToolExposurePolicy
from agent.tools import configure_runtime, configure_tools
from bridge.wechat_poller import WeChatBridge
from memory.conversation import ConversationContext
from memory.store import MemoryStore
from runtime.chat_runtime import ChatRuntime, create_chat_runtime

# ================================================================
# Config loading
# ================================================================

load_dotenv()

def _env_replace(value: str) -> str:
    """Replace ${VAR:-default} patterns with env values."""
    import re
    def replacer(match):
        var_expr = match.group(1)
        if ":-" in var_expr:
            var, default = var_expr.split(":-", 1)
            return os.getenv(var.strip(), default.strip())
        return os.getenv(var_expr.strip(), "")
    return re.sub(r"\$\{([^}]+)\}", replacer, value)


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    raw = _env_replace(raw)
    return yaml.safe_load(raw)


cfg = load_config()

# GOAL_NATIVE_TOOL_CALLS=true overrides config.yaml without editing the file.
_native_override = os.getenv("GOAL_NATIVE_TOOL_CALLS", "").strip().lower()
if _native_override in ("true", "1", "yes", "on"):
    _ntc = cfg.setdefault("model", {}).setdefault("native_tool_calls", {})
    _ntc["chat_agent"] = True
    if not _ntc.get("allowed_tools"):
        _ntc["allowed_tools"] = [
            "send_text_message",
            "get_weather",
            "get_time",
        ]

# Development-only LLM tap/proxy override. This lets local tools such as
# claude-tap capture raw provider traffic without editing config.yaml/.env.
_llm_tap_base_url = (
    os.getenv("GOAL_LLM_BASE_URL", "").strip()
    or os.getenv("LLM_TAP_BASE_URL", "").strip()
)
if _llm_tap_base_url:
    cfg.setdefault("model", {})["base_url"] = _llm_tap_base_url
RUNTIME_SETTINGS_PATH = Path("./data/runtime_settings.json")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ADMIN_SESSIONS: set[str] = set()
ADMIN_COOKIE = "goal_admin_session"
PROCESS_STARTED_AT = time.time()
BOT_ID = "default"


def _deep_update(base: dict, updates: dict) -> dict:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_runtime_settings() -> dict:
    if not RUNTIME_SETTINGS_PATH.exists():
        return {}
    try:
        with open(RUNTIME_SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logging.getLogger("goal").warning(
            "Failed to load runtime settings", exc_info=True
        )
        return {}


def save_runtime_settings(settings: dict):
    RUNTIME_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNTIME_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def read_persona_file() -> str:
    path = Path(cfg.get("agent", {}).get("persona_file", "./prompts/persona.md"))
    if path.exists():
        return path.read_text(encoding="utf-8")
    return cfg["agent"].get("personality", "")


def require_admin(request: Request):
    token = request.cookies.get(ADMIN_COOKIE, "")
    if not token or token not in ADMIN_SESSIONS:
        raise HTTPException(status_code=401, detail="请先登录")


runtime_settings = load_runtime_settings()
_deep_update(cfg, runtime_settings)

# ================================================================
# Logging
# ================================================================

# Ensure data directory exists before logging touches it
Path("./data").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, cfg.get("logging", {}).get("level", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            cfg.get("logging", {}).get("file", "./data/agent.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("goal")
logging.getLogger("httpx").setLevel(logging.WARNING)
configure_tools(cfg)


def _read_recent_log_lines(
    log_path: str | Path,
    limit: int = 200,
    level: str = "",
    contains: str = "",
) -> dict[str, Any]:
    """Return recent log lines without loading a large log file into memory."""
    path = Path(log_path)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    limit = max(1, min(int(limit or 200), 1000))
    level_filter = str(level or "").strip().upper()
    contains_filter = str(contains or "").strip().lower()

    if not path.exists():
        return {"path": str(path), "lines": [], "truncated": False, "exists": False}

    chunk_size = 8192
    max_bytes = 512 * 1024
    file_size = path.stat().st_size
    data = b""
    with path.open("rb") as f:
        pos = file_size
        while pos > 0 and len(data) < max_bytes:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size) + data
            if data.count(b"\n") > limit * 3:
                break

    text = data.decode("utf-8", errors="replace")
    raw_lines = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    if file_size > len(data) and raw_lines:
        raw_lines = raw_lines[1:]

    filtered = []
    for line in raw_lines:
        line_lower = line.lower()
        if level_filter and f"[{level_filter}]" not in line.upper():
            continue
        if contains_filter and contains_filter not in line_lower:
            continue
        filtered.append(line)

    return {
        "path": str(path),
        "lines": filtered[-limit:],
        "truncated": file_size > len(data),
        "exists": True,
    }

# ================================================================
# Clients and services
# ================================================================

llm_gateway = LLMGateway.from_model_config(cfg["model"])

model_name = cfg["model"].get("default_model") or cfg["model"]["name"]
temperature = cfg["model"].get("temperature", 0.7)

memory_store = MemoryStore(
    db_path=cfg.get("storage", {}).get("db_path", "./data/memory.db"),
)
configure_runtime(memory_store=memory_store)

conversation_ctx = ConversationContext(user_id="", bot_id=BOT_ID)

# LLM trace callback (simplified — no longer persisted separately)
async def _on_llm_trace(trace: dict):
    pass

llm_gateway.on_trace(_on_llm_trace)

def _read_persona() -> str:
    path = Path(cfg.get("agent", {}).get("persona_file", "./prompts/persona.md"))
    if path.exists():
        return path.read_text(encoding="utf-8")
    return cfg["agent"].get("persona", "")

def _read_personality() -> str:
    return cfg["agent"].get("personality", "")

_flash_cfg = cfg.get("model", {}).get("flash_profile", {})
_pro_cfg = cfg.get("model", {}).get("pro_profile", {})
flash_profile = ModelProfile(
    model_name=_flash_cfg.get("model_name", "deepseek-v4-flash"),
    temperature=float(_flash_cfg.get("temperature", 0.8) or 0.8),
    max_tokens=int(_flash_cfg.get("max_tokens", 800) or 800),
    timeout_seconds=float(_flash_cfg.get("timeout_seconds", 20) or 20),
)
pro_profile = ModelProfile(
    model_name=_pro_cfg.get("model_name", "deepseek-v4-pro"),
    temperature=float(_pro_cfg.get("temperature", 0.7) or 0.7),
    max_tokens=int(_pro_cfg.get("max_tokens", 1200) or 1200),
    timeout_seconds=float(_pro_cfg.get("timeout_seconds", 40) or 40),
)
_routing_mode = cfg.get("model", {}).get("routing_mode", "auto")
_manual_upgrade = cfg.get("model", {}).get("manual_flash_allow_upgrade", False)
_max_upgrade = int(cfg.get("model", {}).get("max_flash_upgrade_attempts", 1) or 1)

_tool_exposure_policy = ToolExposurePolicy()

_context_builder = ContextBuilder(
    persona_text=f"{_read_persona()}\n\n{_read_personality()}",
)

chat_agent = ChatAgent(
    model=model_name,
    persona=_read_persona(),
    personality=_read_personality(),
    temperature=temperature,
    defaults={
        "default_city": cfg.get("weather", {}).get(
            "default_city",
            cfg.get("fixed_messages", {}).get("morning", {}).get("city", ""),
        )
    },
    llm_gateway=llm_gateway,
    native_tool_calls=cfg.get("model", {}).get("native_tool_calls", {}).get("chat_agent", True),
    native_allowed_tools=cfg.get("model", {}).get("native_tool_calls", {}).get("allowed_tools", []),
    json_output_enabled=False,
    test_mode=cfg.get("wechat", {}).get("send_delay", {}).get("test_mode_zero_delay", False),
    flash_profile=flash_profile,
    pro_profile=pro_profile,
    routing_mode=_routing_mode,
    manual_flash_allow_upgrade=_manual_upgrade,
    max_flash_upgrade_attempts=_max_upgrade,
    tool_exposure_policy=_tool_exposure_policy,
    context_builder=_context_builder,
)

chat_runtime = create_chat_runtime(
    user_id="",
    bot_id=BOT_ID,
    persona=_read_persona(),
    personality=_read_personality(),
    chat_agent=chat_agent,
    context_builder=_context_builder,
    tool_exposure_policy=_tool_exposure_policy,
)

# Memory context is loaded separately via build_enriched_system_prompt().
def build_system_prompt_for_turn() -> str:
    """Build minimal system prompt for a chat turn."""
    persona = _read_persona()
    personality = _read_personality()
    default_city = cfg.get("weather", {}).get("default_city", "")
    tool_names = ["send_text_message", "get_time", "get_weather"]
    return build_chat_system_prompt(
        persona=persona,
        personality=personality,
        tool_names=tool_names,
    )


async def wechat_sender(user_id: str, context_token: str, messages: list[dict]) -> list[dict]:
    """Send messages through the WeChat bridge with delivery tracking.

    Args:
        user_id: WeChat user to send to
        context_token: iLink session token
        messages: [{"text": str, "delay": float}, ...] (already guarded upstream)

    Returns:
        Same messages with send_status added ("delivered" | "failed")
    """
    if _bridge is None or not _bridge.token or not context_token:
        for m in messages:
            m["send_status"] = "failed"
            m["send_error"] = "no_bridge"
        return messages

    wechat_cfg = cfg.get("wechat", {})
    min_gap = float(wechat_cfg.get("message_delay", 0.8) or 0.8)
    max_reply_delay = float(wechat_cfg.get("max_reply_delay", 3.0) or 3.0)

    for i, m in enumerate(messages):
        text = str(m.get("text", "")).strip()
        if not text:
            m["send_status"] = "blocked"
            m["send_error"] = "empty text"
            continue

        delay = max(0.0, float(m.get("delay", 0) or 0))
        if max_reply_delay:
            delay = min(delay, max_reply_delay)

        try:
            if i == 0:
                await asyncio.sleep(max(delay, 0))
            else:
                await asyncio.sleep(max(delay, min_gap))
            await _bridge.send_text(user_id, text, context_token)
            m["send_status"] = "delivered"
        except Exception as e:
            logger.error(f"wechat_sender: failed to send message[{i}]: {e}")
            err_msg = str(e)[:200]
            if "请稍后再试" in err_msg or "rate" in err_msg.lower():
                m["send_status"] = "rate_limited"
            else:
                m["send_status"] = "failed"
            m["send_error"] = err_msg

    return messages

# WeChat iLink bridge (lazy start if config.enabled)
_bridge: WeChatBridge | None = None
if cfg.get("wechat", {}).get("enabled"):
    wcfg = cfg["wechat"]
    _bridge = WeChatBridge(
        token=wcfg.get("token", ""),
        base_url=wcfg.get("base_url", ""),
        goal_url=wcfg.get("goal_url", "http://127.0.0.1:8899"),
        chat_api_key=os.getenv(
            (cfg.get("api") or {}).get("chat_api_key_env", "CHAT_API_KEY"),
            "",
        ),
        poll_timeout_ms=wcfg.get("poll_timeout_ms", 35_000),
        message_delay=wcfg.get("message_delay", 1.2),
        buffer_debounce_seconds=wcfg.get("buffer_debounce_seconds", 5.0),
        buffer_max_wait_seconds=wcfg.get("buffer_max_wait_seconds", 12.0),
        chat_request_timeout_seconds=wcfg.get("chat_request_timeout_seconds", 200.0),
    )

# ================================================================
# TODO: single-process only; multi-process / multi-replica needs a distributed lock
# ================================================================

_user_turn_locks: dict[str, asyncio.Lock] = {}
_user_turn_lock_access: dict[str, float] = {}
_MAX_USER_TURN_LOCKS = 512
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]+$")

# Maps user_id → latest inbound seq seen globally.
_user_latest_inbound_seq: dict[str, int] = {}


def _record_user_message_arrival(user_id: str, seq: int = 0) -> None:
    """Record the latest inbound sequence for a user."""
    if seq > 0:
        current = _user_latest_inbound_seq.get(user_id, 0)
        if seq > current:
            _user_latest_inbound_seq[user_id] = seq


def _make_interrupt_checker(user_id: str, input_latest_seq: int):
    """Return a checker that detects a newer inbound message."""
    def _check(_interrupt_token: str) -> bool:
        latest_seq = _user_latest_inbound_seq.get(user_id, 0)
        interrupted = latest_seq > input_latest_seq
        if interrupted:
            logging.getLogger("goal").info(
                "interrupt_check: latest_seq=%d > input_latest_seq=%d -> interrupted=True",
                latest_seq, input_latest_seq,
            )
        return interrupted

    return _check


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _api_config() -> dict[str, Any]:
    api_cfg = cfg.get("api") or {}
    return {
        "chat_auth_enabled": _as_bool(api_cfg.get("chat_auth_enabled"), True),
        "chat_api_key_env": str(api_cfg.get("chat_api_key_env") or "CHAT_API_KEY"),
        "max_chat_total_chars": int(api_cfg.get("max_chat_total_chars", 4000) or 4000),
        "max_user_id_len": int(api_cfg.get("max_user_id_len", 128) or 128),
        "max_message_chars": int(api_cfg.get("max_message_chars", 1000) or 1000),
        "max_messages_per_request": int(api_cfg.get("max_messages_per_request", 8) or 8),
    }


def _get_chat_api_key() -> str:
    """Return the configured chat API key, or empty string if not set."""
    api_cfg = _api_config()
    return os.getenv(api_cfg["chat_api_key_env"], "")


def _startup_validate() -> None:
    """Run startup self-checks. Log warnings/errors for misconfigurations."""
    errors: list[str] = []
    api_cfg = _api_config()

    # 1. If chat_auth_enabled, CHAT_API_KEY must be set
    if api_cfg["chat_auth_enabled"]:
        chat_key = _get_chat_api_key()
        if not chat_key:
            msg = (
                "api.chat_auth_enabled=true but CHAT_API_KEY env var is not set; "
                "/chat will reject all requests with 503"
            )
            logger.error(msg)
            errors.append("chat_auth_key_not_configured")
        else:
            logger.info("chat_auth: enabled, CHAT_API_KEY is set")

    # 2. If wechat_poller enabled + chat_auth_enabled, poller must have same key
    wcfg = cfg.get("wechat", {})
    wechat_enabled = wcfg.get("enabled", False)
    if wechat_enabled and api_cfg["chat_auth_enabled"]:
        chat_key = _get_chat_api_key()
        if not chat_key:
            msg = (
                "wechat.enabled=true AND chat_auth_enabled=true but "
                "CHAT_API_KEY is not set; internal bridge calls to /chat will fail"
            )
            logger.error(msg)
            errors.append("wechat_poller_missing_chat_api_key")

    # 3. Persona file must exist
    persona_path = Path(cfg.get("agent", {}).get("persona_file", "./prompts/persona.md"))
    if not persona_path.exists():
        logger.warning(f"Persona file not found at {persona_path}, using inline persona")
    else:
        logger.info(f"Persona loaded from: {persona_path}")

    # 4. LLM API key must exist
    llm_key = cfg.get("model", {}).get("api_key", "")
    if not llm_key:
        msg = "model.api_key is not set; LLM calls will fail"
        logger.error(msg)
        errors.append("llm_api_key_missing")
    else:
        logger.info("LLM API key: configured")

    # 5. data dir must be writable
    db_path = cfg.get("storage", {}).get("db_path", "./data/memory.db")
    data_dir = str(Path(db_path).parent)
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        test_file = Path(data_dir) / ".startup_write_test"
        test_file.write_text("ok")
        test_file.unlink()
    except Exception as e:
        msg = f"data directory {data_dir!r} is not writable: {e}"
        logger.error(msg)
        errors.append("data_dir_not_writable")

    if errors:
        logger.error(
            f"Startup validation found {len(errors)} issue(s): {', '.join(errors)}"
        )
    else:
        logger.info("Startup validation: all checks passed")


def _require_chat_auth(request: Request, api_cfg: dict[str, Any]) -> None:
    if not api_cfg["chat_auth_enabled"]:
        return

    expected = os.getenv(api_cfg["chat_api_key_env"], "")
    if not expected:
        logger.error(
            "chat_auth_enabled=true but CHAT_API_KEY env var is not set; "
            "all /chat requests will be rejected"
        )
        raise HTTPException(status_code=503, detail="chat_auth_key_not_configured")

    auth_header = request.headers.get("authorization", "")
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    token = token or request.headers.get("x-api-key", "").strip()

    if not token:
        raise HTTPException(status_code=401, detail="chat_auth_missing")
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="chat_auth_invalid")


def _validate_chat_user_id(user_id: str, api_cfg: dict[str, Any]) -> str:
    user_id = str(user_id or "default").strip()
    max_len = api_cfg["max_user_id_len"]
    if not user_id or len(user_id) > max_len or not _USER_ID_RE.fullmatch(user_id):
        raise HTTPException(status_code=400, detail="Invalid user_id")
    return user_id


def _normalize_chat_messages(body: dict[str, Any], api_cfg: dict[str, Any]) -> list[str]:
    raw_messages = body.get("messages")
    if raw_messages is None:
        raw_messages = [body.get("message", "")]
    if isinstance(raw_messages, str):
        raw_messages = [raw_messages]
    if not isinstance(raw_messages, list) or not raw_messages:
        raise HTTPException(status_code=400, detail="messages required")
    max_count = api_cfg["max_messages_per_request"]
    if len(raw_messages) > max_count:
        raise HTTPException(status_code=400, detail=f"Too many messages; max {max_count}")

    messages: list[str] = []
    total = 0
    max_message_chars = api_cfg["max_message_chars"]
    max_total_chars = api_cfg["max_chat_total_chars"]
    for item in raw_messages:
        if isinstance(item, (dict, list)):
            raise HTTPException(status_code=400, detail="messages must be strings")
        text = str(item)
        if len(text) > max_message_chars:
            raise HTTPException(status_code=400, detail=f"Message too long; max {max_message_chars}")
        total += len(text)
        if total > max_total_chars:
            raise HTTPException(status_code=400, detail=f"Message batch too long; max {max_total_chars}")
        messages.append(text)
    return messages


def _cleanup_user_locks(now: float | None = None) -> None:
    if len(_user_turn_locks) <= _MAX_USER_TURN_LOCKS:
        return
    now = now or time.time()
    stale_ids = sorted(
        _user_turn_lock_access,
        key=lambda uid: _user_turn_lock_access.get(uid, 0),
    )
    target = _MAX_USER_TURN_LOCKS // 2
    for uid in stale_ids:
        lock = _user_turn_locks.get(uid)
        if lock is not None and lock.locked():
            continue
        _user_turn_locks.pop(uid, None)
        _user_turn_lock_access.pop(uid, None)
        if len(_user_turn_locks) <= target:
            break


def _get_user_lock(user_id: str) -> asyncio.Lock:
    """Get or create a per-user async lock for turn ordering."""
    now = time.time()
    _cleanup_user_locks(now)
    if user_id not in _user_turn_locks:
        _user_turn_locks[user_id] = asyncio.Lock()
    _user_turn_lock_access[user_id] = now
    return _user_turn_locks[user_id]


MAX_HISTORY_TURNS = cfg["conversation"].get("max_history_turns", 20)


def public_settings() -> dict:
    """Settings safe to show/edit in the local admin UI."""
    email_cfg = cfg.get("email", {})
    return {
        "wechat": {
            "message_delay": cfg.get("wechat", {}).get("message_delay", 1.2),
            "max_reply_delay": cfg.get("wechat", {}).get("max_reply_delay", 3.0),
            "buffer_debounce_seconds": cfg.get("wechat", {}).get("buffer_debounce_seconds", 5.0),
            "buffer_max_wait_seconds": cfg.get("wechat", {}).get("buffer_max_wait_seconds", 12.0),
            "chat_request_timeout_seconds": cfg.get("wechat", {}).get(
                "chat_request_timeout_seconds", 200.0
            ),
        },
        "weather": {
            "default_city": cfg.get("weather", {}).get("default_city", ""),
            "timeout_seconds": cfg.get("weather", {}).get("timeout_seconds", 4),
            "cache_ttl_seconds": cfg.get("weather", {}).get("cache_ttl_seconds", 600),
        },
        "email": {
            "enabled": email_cfg.get("enabled", False),
            "smtp_host": email_cfg.get("smtp_host", ""),
            "smtp_port": email_cfg.get("smtp_port", 465),
            "from_addr": email_cfg.get("from_addr", ""),
            "default_to": email_cfg.get("default_to", ""),
            "username": email_cfg.get("username", ""),
            "use_ssl": email_cfg.get("use_ssl", True),
            "use_starttls": email_cfg.get("use_starttls", False),
        },
    }


def _safe_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _admin_time(ts: Any) -> str:
    try:
        value = float(ts or 0)
    except (TypeError, ValueError):
        value = 0
    if not value:
        return ""
    return time.strftime("%H:%M", time.localtime(value))


def _route_label(route: str) -> str:
    return {
        "ordinary_chat": "普通聊天",
    }.get(route or "", route or "未知")


def _policy_label(policy: str) -> str:
    return {
        "short_term_history": "短期对话历史",
    }.get(policy or "", policy or "无")


def _send_status_label(status: str) -> str:
    return {
        "created": "已生成",
        "generated": "已生成",
        "sending": "发送中",
        "delivered": "已送达",
        "failed": "发送失败",
    }.get(status or "", status or "未知")


def _source_label(source: str) -> str:
    return {
        "wechat": "微信",
        "user_reply": "用户回复",
        "admin": "后台手动",
    }.get(source or "", source or "未知")


def apply_runtime_settings(updates: dict):
    """Persist and apply runtime-editable settings."""
    _deep_update(runtime_settings, updates)
    save_runtime_settings(runtime_settings)
    _deep_update(cfg, updates)
    configure_tools(cfg)
    configure_runtime(memory_store=memory_store)
    chat_agent.defaults = {
        "default_city": cfg.get("weather", {}).get("default_city", "")
    }

    if _bridge is not None:
        wcfg = cfg.get("wechat", {})
        _bridge.message_delay = float(wcfg.get("message_delay", 1.2))
        _bridge.max_reply_delay = float(wcfg.get("max_reply_delay", 3.0))
        _bridge.buffer_debounce_seconds = float(wcfg.get("buffer_debounce_seconds", 5.0))
        _bridge.buffer_max_wait_seconds = float(wcfg.get("buffer_max_wait_seconds", 12.0))
        _bridge.chat_request_timeout_seconds = float(
            wcfg.get("chat_request_timeout_seconds", 200.0)
        )



async def start_internal_wechat_bridge():
    """Start or restart the built-in iLink bridge from current config."""
    global _bridge
    wcfg = cfg.get("wechat", {})
    token = wcfg.get("token", "")
    if not token:
        raise HTTPException(400, "No wechat token configured. Run QR deployment first.")

    if _bridge is not None:
        await _bridge.stop()

    _bridge = WeChatBridge(
        token=token,
        base_url=wcfg.get("base_url", ""),
        goal_url=wcfg.get("goal_url", "http://127.0.0.1:8899"),
        chat_api_key=_get_chat_api_key(),
        poll_timeout_ms=wcfg.get("poll_timeout_ms", 35_000),
        message_delay=wcfg.get("message_delay", 1.2),
        max_reply_delay=wcfg.get("max_reply_delay", 3.0),
        buffer_debounce_seconds=wcfg.get("buffer_debounce_seconds", 5.0),
        buffer_max_wait_seconds=wcfg.get("buffer_max_wait_seconds", 12.0),
        chat_request_timeout_seconds=wcfg.get("chat_request_timeout_seconds", 200.0),
    )
    await _bridge.start()
    return _bridge


def persist_wechat_deployment(token: str, base_url: str = ""):
    """Persist iLink credentials so restart can reuse the deployment."""
    updates = {
        "wechat": {
            "enabled": True,
            "token": token,
            "base_url": base_url or cfg.get("wechat", {}).get("base_url", ""),
        }
    }
    apply_runtime_settings(updates)


def normalize_qr_payload(raw: dict) -> dict:
    """Normalize the iLink QR response for the admin frontend."""
    qrcode = (
        raw.get("qrcode")
        or raw.get("qr_code")
        or raw.get("code")
        or ""
    )
    image = (
        raw.get("qrcode_img_content")
        or raw.get("qrcode_img")
        or raw.get("qrcode_url")
        or raw.get("qr_code_url")
        or raw.get("url")
        or ""
    )
    qr_url = image or qrcode
    svg = make_qr_svg(qr_url)
    return {
        "qrcode": qrcode,
        "qrcode_content": qr_url,
        "qrcode_img": image,
        "qrcode_url": qr_url,
        "qrcode_svg": svg,
        "can_render_qr": bool(svg),
        "status": raw.get("status", "wait"),
        "raw_keys": sorted(raw.keys()),
    }


def make_qr_svg(content: str) -> str:
    """Render a QR code SVG for the deployment URL.

    iLink returns a WeChat liteapp URL, not an image. The browser needs an
    actual QR image to scan, matching emoRebot's Next implementation.
    """
    if not content:
        return ""
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except Exception:
        return ""

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    return img.to_string(encoding="unicode")


def make_qr_png(content: str) -> bytes:
    """Render QR content to PNG bytes for browsers that cannot use raw SVG."""
    if not content:
        return b""
    import qrcode

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ================================================================
# FastAPI app
# ================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown."""
    del app
    logger.info(f"Starting Goal Chat Agent (model={model_name})")
    _startup_validate()
    if _bridge is not None and _bridge.token:
        await _bridge.start()
    yield
    logger.info("Shutting down...")
    if _bridge is not None:
        await _bridge.stop()
    await memory_store.close()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Goal Chat Agent",
    version="1.0.0",
    lifespan=lifespan,
)

STATIC_DIR = Path("./static")
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ================================================================
# API Routes
# ================================================================


@app.get("/health")
async def health():
    return {"status": "ok", "model": model_name}


@app.get("/")
async def landing_page():
    """Only the admin console is kept as a UI entry point."""
    return RedirectResponse(url="/admin", status_code=307)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """Serve the administration console."""
    html_path = STATIC_DIR / "admin.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="admin console not found")
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/admin/auth/status")
async def admin_auth_status(request: Request):
    token = request.cookies.get(ADMIN_COOKIE, "")
    return {
        "authenticated": bool(token and token in ADMIN_SESSIONS),
        "default_password": ADMIN_PASSWORD == "admin",
    }


@app.post("/admin/auth/login")
async def admin_auth_login(request: Request):
    body = await request.json()
    password = str(body.get("password", ""))
    if not secrets.compare_digest(password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="密码不对")

    token = secrets.token_urlsafe(32)
    ADMIN_SESSIONS.add(token)
    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        ADMIN_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
    )
    return response


@app.post("/admin/auth/logout")
async def admin_auth_logout(request: Request):
    token = request.cookies.get(ADMIN_COOKIE, "")
    if token:
        ADMIN_SESSIONS.discard(token)
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(ADMIN_COOKIE)
    return response


@app.get("/admin/config")
async def admin_config(request: Request):
    require_admin(request)
    contact = await memory_store.get_contact_state()
    outbound_available = _bridge is not None and bool(_bridge.token)
    token = str(cfg.get("wechat", {}).get("token", ""))
    current_bot_id = BOT_ID
    return {
        "persona": read_persona_file(),
        "settings": public_settings(),
        "wechat": {
            "running": _bridge is not None and _bridge.running,
            "configured": bool(cfg.get("wechat", {}).get("token", "")),
            "outbound_available": outbound_available,
            "inbound_seen": bool(contact.get("user_id")),
            "mode": "internal_bridge" if outbound_available else "external_or_chat_only",
            "token_masked": f"...{token[-6:]}" if token else "",
            "base_url": cfg.get("wechat", {}).get("base_url", ""),
        },
        "contact": contact,
        "send_ready": bool(
            outbound_available
            and _contact_can_send_for_bot(contact, current_bot_id)
        ),
    }


@app.post("/admin/persona")
async def admin_save_persona(request: Request):
    require_admin(request)
    body = await request.json()
    content = str(body.get("content", "")).strip()
    if not content:
        raise HTTPException(status_code=400, detail="content required")

    path = Path(cfg.get("agent", {}).get("persona_file", "./prompts/persona.md"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")
    chat_agent.personality = content
    return {"status": "saved", "path": str(path)}


@app.post("/admin/settings")
async def admin_save_settings(request: Request):
    require_admin(request)
    body = await request.json()
    allowed = {}
    for section in ["wechat", "weather", "email"]:
        if isinstance(body.get(section), dict):
            allowed[section] = body[section]
    if not allowed:
        raise HTTPException(status_code=400, detail="no settings provided")
    apply_runtime_settings(allowed)
    return {"status": "saved", "settings": public_settings()}


@app.post("/admin/send")
async def admin_send_message(request: Request):
    """Manually send a message to the latest known single-user contact."""
    require_admin(request)
    body = await request.json()
    text = str(body.get("text", "")).strip()
    raw_messages = body.get("messages")
    messages = []

    if isinstance(raw_messages, list):
        for item in raw_messages:
            if isinstance(item, dict):
                item_text = str(item.get("text", "")).strip()
                delay = item.get("delay", 0)
            else:
                item_text = str(item).strip()
                delay = 0
            if item_text:
                messages.append({"text": item_text, "delay": delay})
    elif text:
        messages.append({"text": text, "delay": 0})

    if not messages:
        raise HTTPException(status_code=400, detail="message text required")

    contact = await memory_store.get_contact_state()
    if not contact.get("user_id"):
        raise HTTPException(status_code=400, detail="还没有记录到微信联系人。先从手机给她发一条消息。")
    if not contact.get("context_token"):
        raise HTTPException(
            status_code=400,
            detail="还没有真实 iLink 会话。请部署后从手机给这个内置 bot 发一条消息。",
        )
    block_reason = _contact_send_block_reason(contact, BOT_ID)
    if block_reason:
        raise HTTPException(
            status_code=400,
            detail=f"当前微信联系人未绑定到这个 bot：{block_reason}。请先从微信给当前 bot 发一条消息。",
        )
    if _bridge is None or not _bridge.token:
        raise HTTPException(
            status_code=400,
            detail=(
                "内置微信发送通道未启动。当前只看到 /chat 收到了消息，"
                "但后台没有可用于主动发送的 iLink token。"
            ),
        )

    await _send_message_batch(
        user_id=contact["user_id"],
        context_token=contact.get("context_token", ""),
        messages=messages,
    )
    return {"status": "sent", "count": len(messages)}


# ================================================================
# ================================================================

_IS_DELIVERED = {"delivered", "sent_confirmed"}


# ================================================================
# ================================================================

def _visible_messages_as_dicts(turn: TurnScratchpad) -> list[dict[str, Any]]:
    """Convert VisibleMessage list to backward-compat dict list."""
    return [
        {
            "text": vm.guarded_text or vm.original_text,
            "delay": vm.delay,
            "send_status": vm.delivery_status,
            "delivery_status": vm.delivery_status,
        }
        for vm in turn.visible_messages
    ]


async def _maybe_compress(user_id: str, bot_id: str) -> None:
    """Daily compression: if past 4am and not compressed today, compress now."""
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    today_4am = now.replace(hour=4, minute=0, second=0, microsecond=0)

    if now < today_4am:
        return  # Before 4am — check later

    conv = await memory_store.get_conversation(user_id, bot_id)
    last_compressed = float(conv.get("last_compressed_at", 0)) if conv else 0

    if last_compressed >= today_4am.timestamp():
        return  # Already compressed today

    logger.info("[%s] Starting daily compression (last=%s, today_4am=%s)",
                user_id, last_compressed, today_4am)
    cid = f"{user_id}:{bot_id}"

    model = cfg["model"].get("default_model") or cfg["model"]["name"]
    ok = await compress_conversation(
        store=memory_store,
        gateway=llm_gateway,
        conversation_id=cid,
        model_name=model,
    )
    if ok:
        await conversation_ctx.rebuild(memory_store)
        logger.info("[%s] Compression complete, context rebuilt", user_id)
    else:
        logger.info("[%s] Compression skipped or failed", user_id)


def _build_protocol_event_writer(user_id: str, bot_id: str):
    """Persist raw events incrementally and update ConversationContext."""
    async def _writer(
        turn: TurnScratchpad,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        cid = f"{user_id}:{bot_id}"
        if event_type == "turn_started":
            user_message = payload.get("user_message")
            await memory_store.begin_turn(
                turn_id=turn.turn_id,
                conversation_id=cid,
                user_message=None if user_message is None else str(user_message),
            )
            return

        if event_type == "message":
            message = payload.get("message") or {}
            role = str(message.get("role", ""))
            content = message.get("content")
            await memory_store.append_event(
                turn_id=turn.turn_id,
                role=role,
                content=content,
                call_id=str(payload.get("call_id", "")),
            )
            # Also append to in-memory context
            conversation_ctx.add_event(role, content)
            return

        if event_type == "turn_finished":
            terminal = str(payload.get("terminal_state", ""))
            status = "completed" if terminal in {TurnState.END_REPLIED.value} else terminal
            await memory_store.finish_turn(
                turn_id=turn.turn_id,
                status=status,
                token_count=turn.token_usage.get("total_tokens", 0),
                elapsed_ms=turn.elapsed_ms,
            )

    return _writer


def _build_chatagent_post_turn_hooks(
    user_id: str,
    bot_id: str,
    route: str,
    context_policy: str,
    raw_messages: list[str],
    memory_store: Any,
) -> list:
    """Return a list of PostTerminalCallback hooks for ChatAgent.

    Each hook receives (turn: TurnScratchpad, terminal_state: TurnState).
    Individual failures are caught inside each hook and logged.
    send_status is computed from terminal_state inside _hook_dev_logs.
    """

    async def _hook_dev_logs(turn: TurnScratchpad, terminal_state: TurnState) -> None:
        visible = _visible_messages_as_dicts(turn)
        delivered_count = sum(
            1 for m in visible
            if (m.get("send_status") in _IS_DELIVERED
                or m.get("delivery_status") in _IS_DELIVERED)
        )
        token_count = turn.token_usage.get("total_tokens", 0)
        logger.info(
            "[%s] turn=%s terminal=%s delivered=%d "
            "tool_results=%d iterations=%d elapsed_ms=%d tokens=%d",
            user_id, turn.turn_id, terminal_state.value,
            delivered_count, len(turn.tool_results),
            turn.llm_call_count, turn.elapsed_ms, token_count,
        )

    async def _hook_model_attempt_log(turn: TurnScratchpad, terminal_state: TurnState) -> None:
        for attempt in turn.model_attempts:
            logger.info(
                "model_attempt: index=%d model=%s/%s mode=%s reason=%s "
                "latency=%dms result=%s error=%s upgraded_from=%s",
                attempt.attempt_index,
                attempt.selected_model,
                attempt.model_name,
                attempt.routing_mode,
                attempt.routing_reason,
                attempt.latency_ms,
                attempt.result_type,
                attempt.error_type,
                attempt.upgraded_from,
            )

    return [
        _hook_dev_logs,
        _hook_model_attempt_log,
    ]


@app.post("/chat")
async def chat(request: Request):
    """Handle one ordinary text-chat turn."""
    api_cfg = _api_config()
    bot_id = "default"

    # ── readiness / auth gate logging ──────────────────────────
    chat_api_key_set = bool(os.getenv(api_cfg["chat_api_key_env"], ""))
    has_auth_header = bool(
        request.headers.get("authorization", "")
        or request.headers.get("x-api-key", "")
    )
    remote_addr = request.client.host if request.client else "unknown"

    logger.info(
        f"/chat entry: remote={remote_addr} "
        f"chat_auth_enabled={api_cfg['chat_auth_enabled']} "
        f"has_auth_header={has_auth_header} "
        f"chat_api_key_set={chat_api_key_set} "
        f"memory_store_ok={memory_store is not None} "
        f"llm_gateway_ok={llm_gateway is not None}"
    )

    if memory_store is None:
        logger.error("/chat rejected: memory_store_not_ready")
        raise HTTPException(status_code=503, detail="memory_store_not_ready")
    if llm_gateway is None:
        logger.error("/chat rejected: llm_gateway_not_ready")
        raise HTTPException(status_code=503, detail="llm_gateway_not_ready")

    _require_chat_auth(request, api_cfg)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    raw_messages = _normalize_chat_messages(body, api_cfg)
    user_message = "\n".join(raw_messages)
    user_id = _validate_chat_user_id(str(body.get("user_id", "default")), api_cfg)
    context_token = str(body.get("context_token", ""))
    inbound_trigger = str(body.get("trigger") or "user")
    input_message_ids = body.get("input_message_ids")
    if not isinstance(input_message_ids, list):
        input_message_ids = []
    try:
        input_first_seq = int(body.get("input_first_seq", 0) or 0)
        input_latest_seq = int(body.get("input_latest_seq", 0) or 0)
        input_cutoff_time = float(body.get("input_cutoff_time", 0) or 0)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid input boundary field: {e}")
    if input_latest_seq == 0 and inbound_trigger == "user":
        logger.warning(
            "[%s] missing input_latest_seq in /chat payload — "
            "interrupt checker will be degraded (seq=0)", user_id,
        )

    logger.info(
        f"[{user_id}] Received {len(str(user_message))} chars "
        f"seq={input_first_seq}-{input_latest_seq} count={len(input_message_ids)}"
    )

    _record_user_message_arrival(user_id, seq=input_latest_seq)
    lock = _get_user_lock(user_id)
    turn_id = ""
    send_status = ""
    try:
        async with lock:
            route = "ordinary_chat"
            context_policy = "short_term_history"

            # Rebuild context if user changed or first use
            if conversation_ctx.user_id != user_id:
                conversation_ctx.user_id = user_id
                conversation_ctx.bot_id = bot_id
                await memory_store.ensure_conversation(user_id, bot_id)
                await conversation_ctx.rebuild(memory_store)

            await _maybe_compress(user_id, bot_id)

            prebuilt_messages = conversation_ctx.messages

            system_prompt = build_system_prompt_for_turn()

            async def _bound_sender(messages: list[dict]) -> list[dict]:
                return await wechat_sender(user_id, context_token, messages)

            interrupt_checker = _make_interrupt_checker(user_id, input_latest_seq)

            _hook_cbs = _build_chatagent_post_turn_hooks(
                user_id=user_id, bot_id=bot_id,
                route=route, context_policy=context_policy,
                raw_messages=raw_messages,
                memory_store=memory_store,
            )
            result: TurnResult = await chat_runtime.handle_chat(
                user_message=user_message,
                history=prebuilt_messages,
                system_prompt=system_prompt,
                prebuilt_messages=prebuilt_messages,
                trigger="user",
                source="user_message",
                wechat_sender=_bound_sender,
                input_message_ids=input_message_ids,
                input_first_seq=input_first_seq,
                input_latest_seq=input_latest_seq,
                input_cutoff_time=input_cutoff_time,
                interrupt_checker=interrupt_checker,
                post_turn_hooks=_hook_cbs,
                protocol_event_writer=_build_protocol_event_writer(user_id, bot_id),
            )

            turn_id = result.turn_id

            match result.terminal_state:
                case TurnState.END_REPLIED:
                    send_status = "delivered"
                case TurnState.END_FAILED:
                    send_status = "failed" if result.sent_message_count == 0 else "failed_after_partial_reply"
                case TurnState.END_CANCELLED:
                    send_status = "cancelled"
                case TurnState.END_SUPERSEDED_BEFORE_SEND:
                    send_status = "superseded_before_send"
                case TurnState.END_INTERRUPTED_DURING_REPLY_SEQUENCE:
                    send_status = "interrupted_during_reply"
                case TurnState.END_MAX_LIMIT:
                    send_status = "max_limit"
                case TurnState.END_PROTOCOL_ERROR:
                    send_status = "protocol_error"
                case _:
                    send_status = "generated"

            # Remember the latest reachable contact
            if user_id and user_id != "default" and context_token:
                await memory_store.update_contact_state(
                    user_id, context_token, bot_id=bot_id,
                )

            logger.info(
                f"[{user_id}] terminal_state={result.terminal_state} "
                f"delivered={result.delivered_count} "
                f"tool_results={len(result.tool_results)} "
                f"iterations={result.iterations} "
                f"elapsed_ms={result.elapsed_ms} "
                f"route={route}"
            )

        # Lock released — return response.
        # Messages were already sent via wechat_sender callback.
        # Return empty messages so the bridge poller doesn't double-send.
        # delivered_count gives the bridge accurate stats for logging.
        return {
            "messages": [],
            "delivered_count": result.delivered_count,
            "tool_calls": result.tool_results,
            "iterations": result.iterations,
            "elapsed_ms": result.elapsed_ms,
            "turn_id": turn_id,
            "terminal_state": str(result.terminal_state),
            "route": route,
        }

    except Exception as exc:
        logger.exception(f"[{user_id}] Chat handler internal error: {exc}")
        error_turn_id = turn_id or f"error_{uuid.uuid4().hex[:8]}"
        return {
            "messages": [],
            "tool_calls": [],
            "iterations": 0,
            "elapsed_ms": 0,
            "turn_id": error_turn_id,
            "route": "error",
            "error": "internal_error",
        }


@app.post("/admin/dev/compress")
async def developer_compress_now(request: Request):
    """Manually trigger conversation compression."""
    require_admin(request)
    contact = await memory_store.get_contact_state()
    user_id = str(contact.get("user_id", "") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="还没有微信联系人记录")

    bot_id = "default"
    cid = f"{user_id}:{bot_id}"
    model = cfg["model"].get("default_model") or cfg["model"]["name"]

    logger.info("Manual compression triggered for %s", user_id)
    ok = await compress_conversation(
        store=memory_store,
        gateway=llm_gateway,
        conversation_id=cid,
        model_name=model,
    )
    if ok:
        await conversation_ctx.rebuild(memory_store)
        return {"status": "compressed", "user_id": user_id}
    else:
        return {"status": "skipped", "reason": "no events or no tool_result found", "user_id": user_id}


@app.post("/admin/dev/forget")
async def developer_forget_all(request: Request):
    """Clear test conversation state while preserving the WeChat contact."""
    return await developer_reset_context(request)


@app.post("/admin/dev/reset-context")
async def developer_reset_context(request: Request):
    """Developer reset: clear context/test state while keeping WeChat contact state."""
    require_admin(request)
    counts = await memory_store.clear_all()
    conversation_ctx._messages = []
    return {
        "status": "reset",
        "mode": "context_only_keep_connection",
        "cleared": counts,
        "kept": ["bot_contact_state"],
    }


# ================================================================
# WeChat iLink Bridge routes
# ================================================================


@app.get("/wechat/qr")
async def wechat_qr_login(request: Request):
    """Get a QR code for WeChat iLink login. Poll /wechat/qr/status next."""
    require_admin(request)
    from bridge.ilink_client import get_qr_code
    wcfg = cfg.get("wechat", {})
    base_url = wcfg.get("base_url", "")
    qr = await get_qr_code(base_url)
    return normalize_qr_payload(qr)


@app.get("/wechat/qr/status")
async def wechat_qr_status(request: Request, qrcode: str):
    """Poll QR code scan status. Returns bot credentials on confirmed."""
    require_admin(request)
    from bridge.ilink_client import poll_qr_status
    wcfg = cfg.get("wechat", {})
    base_url = wcfg.get("base_url", "")
    status = await poll_qr_status(qrcode, base_url)
    return status


@app.post("/wechat/deploy/start")
async def wechat_deploy_start(request: Request):
    """Start QR deployment flow for the built-in iLink bridge."""
    require_admin(request)
    from bridge.ilink_client import get_qr_code

    wcfg = cfg.get("wechat", {})
    qr = await get_qr_code(wcfg.get("base_url", ""))
    return normalize_qr_payload(qr)


@app.get("/wechat/deploy/qr-image")
async def wechat_deploy_qr_image(request: Request, data: str = ""):
    """Render iLink QR content as a PNG image for the admin frontend."""
    require_admin(request)
    if not data:
        raise HTTPException(status_code=400, detail="Missing data parameter")
    png = make_qr_png(data)
    if not png:
        raise HTTPException(status_code=400, detail="Invalid QR content")
    return Response(content=png, media_type="image/png")


@app.get("/wechat/deploy/status")
async def wechat_deploy_status(request: Request, qrcode: str):
    """Poll QR deployment status; persist credentials when confirmed."""
    require_admin(request)
    from bridge.ilink_client import poll_qr_status

    wcfg = cfg.get("wechat", {})
    status = await poll_qr_status(qrcode, wcfg.get("base_url", ""))
    st = status.get("status", "wait")

    if st == "confirmed":
        token = status.get("bot_token") or status.get("token") or ""
        base_url = (
            status.get("baseurl")
            or status.get("base_url")
            or wcfg.get("base_url", "")
        )
        if not token:
            raise HTTPException(502, "QR confirmed but no bot token returned")
        persist_wechat_deployment(token=token, base_url=base_url)
        await start_internal_wechat_bridge()
        return {
            "status": "confirmed",
            "running": True,
            "configured": True,
            "token_masked": f"...{token[-6:]}",
        }

    return {
        "status": st,
        "running": _bridge is not None and _bridge.running,
        "configured": bool(cfg.get("wechat", {}).get("token", "")),
    }


@app.post("/wechat/start")
async def wechat_start(request: Request):
    """Start or restart the WeChat bridge."""
    require_admin(request)
    await start_internal_wechat_bridge()
    return {"status": "started"}


@app.post("/wechat/stop")
async def wechat_stop(request: Request):
    """Stop the WeChat bridge."""
    require_admin(request)
    global _bridge
    if _bridge is not None:
        await _bridge.stop()
    return {"status": "stopped"}


@app.get("/wechat/status")
async def wechat_status(request: Request):
    """Get WeChat bridge status."""
    require_admin(request)
    return {
        "running": _bridge is not None and _bridge.running,
        "configured": bool(cfg.get("wechat", {}).get("token", "")),
        "base_url": cfg.get("wechat", {}).get("base_url", ""),
    }


# ================================================================
# ================================================================


@app.get("/admin/logs")
async def admin_logs(
    request: Request,
    limit: int = 200,
    level: str = "",
    contains: str = "",
):
    """Read the tail of the runtime log for the admin debug console."""
    require_admin(request)
    logging_cfg = cfg.get("logging", {})
    log_path = logging_cfg.get("file", "./data/agent.log")
    return _read_recent_log_lines(
        log_path=log_path,
        limit=limit,
        level=level,
        contains=contains,
    )


# ================================================================
# Background tasks
# ================================================================


async def _send_message_batch(
    user_id: str,
    context_token: str,
    messages: list[dict],
):
    """Send a normalized message list through the active WeChat bridge."""
    if _bridge is None:
        raise RuntimeError("WeChat bridge is not configured")
    if not context_token:
        raise RuntimeError("missing iLink context_token")

    non_empty = [
        m for m in messages
        if isinstance(m, dict) and str(m.get("text", "")).strip()
    ]
    if not non_empty:
        logger.warning("Empty message batch, skipping WeChat send entirely")
        return

    guarded = guard_messages(non_empty)

    if not guarded:
        logger.warning("All messages filtered by guard, skipping WeChat send")
        return

    min_gap = float(cfg.get("wechat", {}).get("message_delay", 1.2) or 1.2)
    max_reply_delay = float(cfg.get("wechat", {}).get("max_reply_delay", 3.0) or 3.0)
    for i, m in enumerate(guarded):
        text = guard_single_message(m.get("text", ""))
        if not text:
            continue
        delay = max(float(m.get("delay", 0) or 0), 0.0)
        if max_reply_delay:
            delay = min(delay, max_reply_delay)
        if i == 0:
            await asyncio.sleep(max(delay, 0))
        else:
            await asyncio.sleep(max(delay, min_gap))
        await _bridge.send_text(user_id, text, context_token)


# ================================================================
# Contact send helpers
# ================================================================


def _contact_send_block_reason(contact: dict[str, Any], bot_id: str) -> str:
    """Return why a stored WeChat contact cannot receive current-bot sends."""
    if not contact.get("user_id"):
        return "no contact"
    if not contact.get("context_token"):
        return "missing context_token"
    contact_bot = str(contact.get("bot_id", "") or "")
    if contact_bot != bot_id:
        return "bot not bound"
    updated_at = float(contact.get("updated_at") or 0)
    if updated_at < PROCESS_STARTED_AT:
        return "stale contact binding"
    return ""


def _contact_can_send_for_bot(contact: dict[str, Any], bot_id: str) -> bool:
    return _contact_send_block_reason(contact, bot_id) == ""


# ================================================================
# Entry point
# ================================================================

if __name__ == "__main__":
    import uvicorn

    host = cfg.get("server", {}).get("host", "0.0.0.0")
    port = int(cfg.get("server", {}).get("port", 8899))

    logger.info(f"Starting server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


