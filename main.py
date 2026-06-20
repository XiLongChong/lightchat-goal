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
from datetime import datetime, time as datetime_time, timezone
from typing import Any
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from agent.chat_agent import ChatAgent
from agent.compressor import compress_conversation, maybe_compress_daily, compute_summary_date
from agent.context_builder import build_chat_system_prompt
from agent.llm_gateway import LLMGateway
from agent.output_guard import guard_messages, guard_single_message
from agent.runtime_context_builder import ContextBuilder
from agent.state_machine import ModelProfile, TurnResult, TurnScratchpad, TurnState, DeliveryStatus
from agent.tool_exposure_policy import ToolExposurePolicy
from agent.tool_registry import TOOL_REGISTRY
from agent.tools import configure_runtime, configure_tools
from bridge.wechat_poller import WeChatBridge
from bridge.qr_utils import normalize_qr_payload, make_qr_svg, make_qr_png
from bridge.sender import WeChatSender
from memory.conversation import ConversationContext
from memory.store import MemoryStore
from memory.long_term import LongTermMemoryService
from runtime.admin_labels import (
    route_label,
    policy_label,
    send_status_label,
    source_label,
)
from runtime.chat_runtime import ChatRuntime, create_chat_runtime
from runtime.config import RuntimeConfig, load_config
from runtime.contact import contact_send_block_reason, contact_can_send_for_bot
from runtime.daily_compression import DailyCompressionRunner
from runtime.protocol_writer import ProtocolEventWriter
from runtime.turn_hooks import build_observability_hooks
from runtime.user_turns import UserTurnRegistry
from runtime.log_reader import read_recent_log_lines
from runtime.task_scheduler import TaskScheduler
from runtime.app_agent_session_registry import AppAgentSessionRegistry
from runtime.channel_pending_inbox import ChannelPendingInbox

# ================================================================
# Config loading
# ================================================================

load_dotenv()

RUNTIME_SETTINGS_PATH = Path("./data/runtime_settings.json")

# RuntimeConfig loads config.yaml (with ${VAR:-default} env substitution) and
# data/runtime_settings.json, then deep-merges runtime settings into cfg —
# matching the original load sequence.  Env-only dev overrides
# (GOAL_NATIVE_TOOL_CALLS, GOAL_LLM_BASE_URL) are applied below.
runtime_config = RuntimeConfig(
    config_path="config.yaml",
    runtime_settings_path=RUNTIME_SETTINGS_PATH,
)
cfg = runtime_config.cfg
runtime_settings = runtime_config.runtime_settings

# GOAL_NATIVE_TOOL_CALLS=true overrides config.yaml without editing the file.
_native_override = os.getenv("GOAL_NATIVE_TOOL_CALLS", "").strip().lower()
if _native_override in ("true", "1", "yes", "on"):
    _ntc = cfg.setdefault("model", {}).setdefault("native_tool_calls", {})
    _ntc["chat_agent"] = True
    if not _ntc.get("allowed_tools"):
        _ntc["allowed_tools"] = [
            definition.name for definition in TOOL_REGISTRY.iter_defs()
        ]

# Development-only LLM tap/proxy override. This lets local tools such as
# claude-tap capture raw provider traffic without editing config.yaml/.env.
_llm_tap_base_url = (
    os.getenv("GOAL_LLM_BASE_URL", "").strip()
    or os.getenv("LLM_TAP_BASE_URL", "").strip()
)
if _llm_tap_base_url:
    cfg.setdefault("model", {})["base_url"] = _llm_tap_base_url

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ADMIN_SESSIONS: set[str] = set()
ADMIN_COOKIE = "goal_admin_session"
PROCESS_STARTED_AT = time.time()
BOT_ID = "default"


def read_persona_file() -> str:
    path = Path(cfg.get("agent", {}).get("persona_file", "./prompts/persona.md"))
    if path.exists():
        return path.read_text(encoding="utf-8")
    return cfg["agent"].get("personality", "")


def require_admin(request: Request):
    token = request.cookies.get(ADMIN_COOKIE, "")
    if not token or token not in ADMIN_SESSIONS:
        raise HTTPException(status_code=401, detail="请先登录")

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

# ================================================================
# Clients and services
# ================================================================

llm_gateway = LLMGateway.from_model_config(cfg["model"])

model_name = cfg["model"].get("default_model") or cfg["model"]["name"]
temperature = cfg["model"].get("temperature", 0.7)

memory_store = MemoryStore(
    db_path=cfg.get("storage", {}).get("db_path", "./data/memory.db"),
)
long_term_memory = LongTermMemoryService(
    cfg.get("storage", {}).get(
        "long_term_memory_dir", "./data/long_term_memory"
    )
)
configure_runtime(memory_store=memory_store, long_term_memory=long_term_memory)

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
    long_term_memory=long_term_memory,
    idle_time_reminder_minutes=float(
        cfg.get("conversation", {}).get("idle_time_reminder_minutes", 10) or 10
    ),
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
    context_window_tokens=int(
        cfg.get("model", {}).get("context_window_tokens", 1_000_000) or 1_000_000
    ),
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
    tool_names = [definition.name for definition in TOOL_REGISTRY.iter_defs()]
    return build_chat_system_prompt(
        persona=persona,
        personality=personality,
        tool_names=tool_names,
    )


async def wechat_sender(user_id: str, context_token: str, messages: list[dict]) -> list[dict]:
    """Send messages through the WeChat bridge with delivery tracking.

    Thin wrapper around ``wechat_sender_service.send_batch`` — the actual
    sending logic lives in :class:`bridge.sender.WeChatSender`.

    Args:
        user_id: WeChat user to send to
        context_token: iLink session token
        messages: [{"text": str, "delay": float}, ...] (already guarded upstream)

    Returns:
        Same messages with send_status added ("delivered" | "failed")
    """
    return await wechat_sender_service.send_batch(user_id, context_token, messages)

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

# WeChat sender service — encapsulates batch sending logic (batch E).
# bridge_getter returns the current _bridge so start/stop/redeploy cycles
# are reflected without re-injection; config_getter returns the live
# wechat config so runtime_settings updates take effect immediately.
wechat_sender_service = WeChatSender(
    bridge_getter=lambda: _bridge,
    config_getter=lambda: cfg.get("wechat", {}),
)

task_scheduler = TaskScheduler(
    memory_store=memory_store,
    send_text=wechat_sender,
    bot_id=BOT_ID,
    max_attempts=int(cfg.get("reminders", {}).get("max_retry_attempts", 3) or 3),
    proactive_config_getter=lambda: cfg.get("proactive", {}),
)
configure_runtime(task_scheduler=task_scheduler)

# ================================================================
# TODO: single-process only; multi-process / multi-replica needs a distributed lock
# ================================================================

user_turns = UserTurnRegistry(max_locks=512)

_USER_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]+$")


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

    # 6. Proactive runtime override visibility
    config_proactive = load_config().get("proactive", {})
    effective_proactive = cfg.get("proactive", {})
    effective_enabled = bool(effective_proactive.get("enabled", True))
    default_enabled = bool(config_proactive.get("enabled", True))
    logger.info(
        "proactive config: enabled=%s default_enabled=%s runtime_override_present=%s",
        effective_enabled,
        default_enabled,
        "enabled" in (runtime_settings.get("proactive") or {}),
    )
    if default_enabled and not effective_enabled:
        logger.warning(
            "proactive.enabled is disabled by runtime settings; no proactive_speak "
            "tasks will be scheduled"
        )

    # 7. Weather provider credentials
    weather_provider = str(cfg.get("weather", {}).get("provider", "")).strip()
    if weather_provider == "qweather":
        qw_key = os.getenv("QWEATHER_API_KEY", "")
        if not qw_key:
            logger.warning(
                "weather.provider=qweather but QWEATHER_API_KEY is not set; "
                "weather tool will not work"
            )
        else:
            logger.info("QWeather API key: configured")


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
            "enabled": _as_bool(cfg.get("weather", {}).get("enabled", True)),
            "provider": cfg.get("weather", {}).get("provider", "qweather"),
            "default_city": cfg.get("weather", {}).get("default_city", ""),
            "timeout_seconds": cfg.get("weather", {}).get("timeout_seconds", 5),
            "observation_cache_ttl_seconds": cfg.get("weather", {}).get("observation_cache_ttl_seconds", 600),
            "forecast_cache_ttl_seconds": cfg.get("weather", {}).get("forecast_cache_ttl_seconds", 1800),
            "location_cache_ttl_seconds": cfg.get("weather", {}).get("location_cache_ttl_seconds", 86400),
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


def apply_runtime_settings(updates: dict):
    """Persist and apply runtime-editable settings."""
    runtime_config.apply_updates(updates)
    configure_tools(cfg)
    configure_runtime(memory_store=memory_store, long_term_memory=long_term_memory)
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


# ================================================================
# FastAPI app
# ================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown."""
    del app
    logger.info(f"Starting Goal Chat Agent (model={model_name})")
    _startup_validate()
    contact = await memory_store.get_contact_state()
    startup_user_id = str(contact.get("user_id", "") or "").strip()
    if startup_user_id:
        diagnostic = long_term_memory.diagnostics(startup_user_id)
        for warning in diagnostic["warnings"]:
            logger.warning(
                "long-term memory diagnostic code=%s message=%s",
                warning.get("code"),
                warning.get("message"),
            )
    if _bridge is not None and _bridge.token:
        await _bridge.start()
    await task_scheduler.start()

    yield

    logger.info("Shutting down...")
    await task_scheduler.stop()
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
            and contact_can_send_for_bot(contact, current_bot_id, PROCESS_STARTED_AT)
        ),
    }


@app.get("/admin/context-usage")
async def admin_context_usage(request: Request):
    require_admin(request)
    contact = await memory_store.get_contact_state()
    user_id = str(contact.get("user_id", "") or "").strip()
    bot_id = str(contact.get("bot_id", "") or BOT_ID).strip() or BOT_ID
    if not user_id:
        return {"available": False}

    usage = await memory_store.get_latest_context_usage(user_id, bot_id)
    if not usage:
        return {"available": False}

    prompt_tokens_peak = int(usage.get("prompt_tokens_peak", 0) or 0)
    context_window_tokens = int(usage.get("context_window_tokens", 0) or 0)
    if prompt_tokens_peak <= 0 or context_window_tokens <= 0:
        return {"available": False}

    usage_ratio = prompt_tokens_peak / context_window_tokens
    return {
        "available": True,
        "turn_id": str(usage.get("turn_id", "")),
        "model": str(usage.get("model_name", "")),
        "prompt_tokens_peak": prompt_tokens_peak,
        "context_window_tokens": context_window_tokens,
        "usage_ratio": usage_ratio,
        "usage_percent": usage_ratio * 100,
        "estimated": bool(usage.get("usage_estimated", False)),
        "measured_at": float(usage.get("completed_at", 0) or 0),
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
    block_reason = contact_send_block_reason(contact, BOT_ID, PROCESS_STARTED_AT)
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

async def _handle_proactive_trigger(
    user_id: str,
    trigger_seq: int,
    task_id: str | None = None,
    task_generation: int | None = None,
) -> TurnResult | None:
    """Run a proactive check-in without posting a fake user request to /chat."""
    if not user_id:
        return None
    contact = await memory_store.get_contact_state()
    context_token = str(contact.get("context_token", "") or "")
    if not context_token or str(contact.get("user_id", "")) != user_id:
        logger.warning("proactive trigger skipped: no current contact for user=%s", user_id)
        return None

    lock = user_turns.get_lock(user_id)
    async with lock:
        if conversation_ctx.user_id != user_id:
            conversation_ctx.user_id = user_id
            conversation_ctx.bot_id = BOT_ID
            await memory_store.ensure_conversation(user_id, BOT_ID)
            await conversation_ctx.rebuild(memory_store)

        async def _bound_sender(messages: list[dict]) -> list[dict]:
            return await wechat_sender(user_id, context_token, messages)

        system_reminder = (
            "<system-reminder>\n"
            "This is a proactive check-in. The user has been inactive. "
            "You must send one natural message now to reconnect. After sending, "
            "use schedule_next_proactive to suggest your next check-in.\n"
            "</system-reminder>"
        )
        result = await chat_runtime.handle_chat(
            user_message=system_reminder,
            user_id=user_id,
            bot_id=BOT_ID,
            history=conversation_ctx.messages,
            system_prompt=build_system_prompt_for_turn(),
            prebuilt_messages=conversation_ctx.messages,
            trigger="proactive_speak",
            source="proactive_speak",
            wechat_sender=_bound_sender,
            input_latest_seq=trigger_seq,
            protocol_event_writer=ProtocolEventWriter(
                store=memory_store,
                context=conversation_ctx,
                user_id=user_id,
                bot_id=BOT_ID,
            ).write,
            post_turn_hooks=_build_chatagent_post_turn_hooks(
                user_id=user_id,
                bot_id=BOT_ID,
                route="proactive",
                context_policy="short_term_history",
                raw_messages=[system_reminder],
                memory_store=memory_store,
                proactive_task_id=task_id,
                proactive_task_generation=task_generation,
            ),
        )
        logger.info(
            "proactive trigger completed user=%s turn=%s delivered=%d terminal=%s",
            user_id,
            result.turn_id,
            result.delivered_count,
            result.terminal_state,
        )
        return result


task_scheduler.set_proactive_callback(_handle_proactive_trigger)


# Compression logic lives in agent/compressor.py (maybe_compress_daily, compress_conversation).


async def _run_compression(
    user_id: str,
    bot_id: str,
    *,
    force: bool,
) -> dict[str, Any]:
    cid = f"{user_id}:{bot_id}"
    model = cfg["model"].get("default_model") or cfg["model"]["name"]
    memory_context = long_term_memory.all_indexed_content(user_id)
    if force:
        result = await compress_conversation(
            messages=conversation_ctx.messages,
            gateway=llm_gateway,
            model_name=model,
            memory_context=memory_context,
        )
    else:
        conv = await memory_store.get_conversation(user_id, bot_id)
        last_compressed = float(conv.get("last_compressed_at", 0)) if conv else 0
        result = await maybe_compress_daily(
            messages=conversation_ctx.messages,
            last_compressed_at=last_compressed,
            gateway=llm_gateway,
            model_name=model,
            memory_context=memory_context,
        )
    if result is None:
        return {"status": "skipped", "reason": "no valid compression result"}

    receipt = None
    try:
        receipt = await long_term_memory.apply_batch(
            user_id, result.memory_operations
        )
        compressed_messages = [{
            "role": "user",
            "content": [{
                "type": "text",
                "text": f"<summary>\n{result.summary}\n</summary>",
            }],
        }]
        summary_date = compute_summary_date()
        summary_text = f"<summary>\n{result.summary}\n</summary>"
        await memory_store.commit_compression(
            cid,
            compressed_messages,
            summary_date,
            summary_text,
        )
    except Exception:
        if receipt is not None:
            await receipt.rollback()
        logger.exception(
            "Compression commit failed user=%s memory_operations=%d",
            long_term_memory.user_key(user_id)[:12],
            len(result.memory_operations),
        )
        raise
    receipt.finalize()
    conversation_ctx.replace_with_compressed(result.summary)
    safe_user = long_term_memory.user_key(user_id)[:12]
    for operation in result.memory_operations:
        logger.info(
            "memory operation source=compact user=%s command=%s path=%s ok=true code=memory_ok",
            safe_user,
            operation.command,
            operation.path,
        )
    logger.info(
        "[%s] Compression complete memory_operations=%d",
        safe_user,
        len(result.memory_operations),
    )
    return {
        "status": "compressed",
        "user_id": user_id,
        "summary_date": summary_date,
        "memory_operations": len(result.memory_operations),
    }


# Daily compression runner — orchestrates background compression for the
# current bot contact (batch F).  The compression algorithm stays in
# agent/compressor.py; this runner only handles contact lookup, per-user
# locking, and context rebuild.
daily_compression_runner = DailyCompressionRunner(
    store=memory_store,
    context=conversation_ctx,
    user_turns=user_turns,
    run_compression=lambda user_id, bot_id: _run_compression(user_id, bot_id, force=False),
    default_bot_id=BOT_ID,
)

task_scheduler.set_daily_compression_callback(daily_compression_runner.run_current_contact)


def _build_chatagent_post_turn_hooks(
    user_id: str,
    bot_id: str,
    route: str,
    context_policy: str,
    raw_messages: list[str],
    memory_store: Any,
    proactive_task_id: str | None = None,
    proactive_task_generation: int | None = None,
) -> list:
    """Return a list of PostTerminalCallback hooks for ChatAgent.

    Composed of:
      - generic observability hooks (dev-logs + model-attempt-log) from runtime.turn_hooks
      - a local proactive-scheduling hook (captures task_scheduler)
    """

    async def _hook_proactive_schedule(turn: TurnScratchpad, terminal_state: TurnState) -> None:
        delivered = sum(
            1 for item in turn.visible_messages
            if str(item.delivery_status) in _IS_DELIVERED
        )
        if terminal_state != TurnState.END_REPLIED or delivered <= 0:
            return

        if route == "ordinary_chat":
            await task_scheduler.cancel_user_proactive_tasks(
                user_id,
                reason="user_message_arrived",
            )
            await task_scheduler.schedule_initial_proactive(user_id)
            return

        if route == "proactive":
            result = await task_scheduler.complete_proactive_turn(
                turn=turn,
                user_id=user_id,
                bot_id=bot_id,
                current_task_id=proactive_task_id,
                current_generation=proactive_task_generation,
            )
            logger.info("proactive schedule commit user=%s result=%s", user_id, result)

    return build_observability_hooks(
        user_id=user_id,
        bot_id=bot_id,
        route=route,
        context_policy=context_policy,
        raw_messages=raw_messages,
        memory_store=memory_store,
    ) + [_hook_proactive_schedule]


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
    inbound_source = str(body.get("source", "user_message"))
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

    user_turns.record_arrival(user_id, seq=input_latest_seq)
    lock = user_turns.get_lock(user_id)
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

            # ── daily compression ──────────────────────────────────
            await _run_compression(user_id, bot_id, force=False)

            prebuilt_messages = conversation_ctx.messages

            system_prompt = build_system_prompt_for_turn()
            previous_user_turn_at = await memory_store.get_latest_user_turn_created_at(
                user_id,
                bot_id,
            )
            idle_seconds = (
                max(time.time() - previous_user_turn_at, 0.0)
                if previous_user_turn_at is not None
                else None
            )

            async def _bound_sender(messages: list[dict]) -> list[dict]:
                return await wechat_sender(user_id, context_token, messages)

            interrupt_checker = user_turns.make_interrupt_checker(user_id, input_latest_seq)

            _hook_cbs = _build_chatagent_post_turn_hooks(
                user_id=user_id, bot_id=bot_id,
                route=route, context_policy=context_policy,
                raw_messages=raw_messages,
                memory_store=memory_store,
            )
            result: TurnResult = await chat_runtime.handle_chat(
                user_message=user_message,
                user_id=user_id,
                bot_id=bot_id,
                history=prebuilt_messages,
                system_prompt=system_prompt,
                prebuilt_messages=prebuilt_messages,
                trigger="user" if inbound_source == "user_message" else "proactive",
                source=inbound_source,
                wechat_sender=_bound_sender,
                input_message_ids=input_message_ids,
                input_first_seq=input_first_seq,
                input_latest_seq=input_latest_seq,
                input_cutoff_time=input_cutoff_time,
                interrupt_checker=interrupt_checker,
                post_turn_hooks=_hook_cbs,
                protocol_event_writer=ProtocolEventWriter(
                    store=memory_store,
                    context=conversation_ctx,
                    user_id=user_id,
                    bot_id=bot_id,
                ).write,
                idle_seconds=idle_seconds,
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


# ================================================================
# App adapter endpoints (AsPocket Android client)
# ================================================================

_APP_USER_ID = os.getenv("APP_USER_ID", "app:user")
_APP_AGENT_TIMEOUT_S = 55.0

# In-memory registry for attached agent sessions (Claude Code etc.).
# Sessions are lost on restart — acceptable for this prototype stage.
app_agent_session_registry = AppAgentSessionRegistry()

# In-memory pending inbox for claude_channel sessions.  A channel server
# polls this to pull user messages and pushes them to Claude Code via
# ``notifications/claude/channel``.  Lost on restart — acceptable.
channel_pending_inbox = ChannelPendingInbox()


# ── attachment helpers ─────────────────────────────────────────────

import os as _os
import re as _re

_ATTACHMENTS_DIR = Path("./data/app_attachments")
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_BODY_CONTENT_LENGTH = _MAX_ATTACHMENT_BYTES + 1 * 1024 * 1024  # 11 MB fudge for multipart overhead
_ATTACHMENT_ID_RE = _re.compile(r"^app_att_[a-f0-9]+$")


def _sanitize_attachment_filename(filename: str) -> str:
    """Return a safe basename; never empty.  Strips path separators."""
    name = _os.path.basename(filename).strip() or "image"
    name = name.replace("\\", "").replace("/", "").replace("\x00", "")
    return name if name else "image"


def _attachment_ext(mime_type: str, filename: str = "") -> str:
    """Derive a safe file extension (no leading dot)."""
    if filename:
        _, dot_ext = _os.path.splitext(filename)
        if dot_ext and 1 < len(dot_ext) <= 6:
            return dot_ext.lower().lstrip(".")
    mime_map = {
        "image/jpeg": "jpg", "image/png": "png",
        "image/gif": "gif", "image/webp": "webp",
        "image/svg+xml": "svg", "image/bmp": "bmp",
    }
    return mime_map.get(mime_type, "bin")


def parse_multipart_form(content_type: str, body: bytes) -> list[dict]:
    """Parse a ``multipart/form-data`` body into [{name, filename?, content_type, data}].

    Returns an empty list when *content_type* isn't multipart or has no boundary.
    """
    if not content_type or "multipart/form-data" not in content_type.lower():
        return []
    boundary: str | None = None
    for param in content_type.split(";"):
        param = param.strip()
        if param.lower().startswith("boundary="):
            boundary = param[len("boundary="):].strip().strip('"')
            break
    if not boundary:
        return []
    delim = b"--" + boundary.encode("ascii")
    parts: list[dict] = []
    for seg in body.split(delim):
        if seg in (b"--", b"--\r\n", b"\r\n", b""):
            continue
        if seg.startswith(b"--"):
            continue  # closing boundary
        if seg.startswith(b"\r\n"):
            seg = seg[2:]
        if seg.endswith(b"\r\n"):
            seg = seg[:-2]
        if b"\r\n\r\n" in seg:
            header_blob, data = seg.split(b"\r\n\r\n", 1)
        else:
            header_blob, data = seg, b""
        name = None
        filename = None
        ctype = ""
        for line in header_blob.split(b"\r\n"):
            try:
                line_s = line.decode("utf-8", "replace")
            except Exception:
                continue
            low = line_s.lower()
            if low.startswith("content-disposition:"):
                for p in line_s.split(";"):
                    p = p.strip()
                    if p.startswith("name="):
                        name = p[5:].strip().strip('"')
                    elif p.startswith("filename="):
                        filename = p[9:].strip().strip('"')
            elif low.startswith("content-type:"):
                ctype = line_s.split(":", 1)[1].strip()
        if name:
            parts.append({
                "name": name,
                "filename": filename,
                "content_type": ctype,
                "data": data,
            })
    return parts


def _parse_attachments_json(value: Any) -> list[dict]:
    """Parse an attachments_json column value into a Python list."""
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


# ── app contact-invitation / remote-agent helpers ──────────────────


def normalize_remote_agent_id(value: str | None) -> str:
    """Normalize an incoming remoteAgentId to a non-empty string.

    ``None``, empty string, and whitespace-only all map to ``"default"``
    so that old App clients that don't send the field continue to work.
    """
    if not value:
        return "default"
    stripped = str(value).strip()
    return stripped if stripped else "default"


def validate_attached_session_callback_url(callback_url: str) -> str:
    """Validate and normalise an attached-session ``callbackUrl``.

    Returns the normalised URL (trailing slash stripped) on success.

    An empty/whitespace-only input is allowed and returns ``""`` — a
    session without a callback simply returns 501 when messaged.

    Only ``http://`` URLs pointing at ``localhost``, ``127.0.0.1``, or
    ``::1`` (IPv6 loopback) are accepted.  This closes the SSRF surface
    on the callback forwarding path: the Router only ever calls back to
    the same machine it runs on.

    Rejected: non-http schemes, ``0.0.0.0``, private/LAN ranges
    (``192.168.*``, ``10.*``, ``172.16-31.*``), public hostnames,
    ``file:`` / ``javascript:``, URLs with credentials, and URLs with
    no hostname.
    """
    text = str(callback_url or "").strip()
    if not text:
        return ""

    parsed = urlparse(text)
    if parsed.scheme != "http":
        raise ValueError("callbackUrl must be an http URL")
    if parsed.username or parsed.password:
        raise ValueError("callbackUrl must not contain credentials")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("callbackUrl must have a hostname")

    # Normalise hostname: lowercase, strip IPv6 brackets already removed
    # by urlparse.hostname.
    host = hostname.lower()
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("callbackUrl must point at localhost / 127.0.0.1 / ::1")

    # Rebuild a clean URL without trailing slash / path / query / fragment.
    # We keep scheme + host (+ port).  IPv6 ::1 is written back in bracket
    # form for HTTP semantics.
    port = parsed.port
    if host == "::1":
        netloc = f"[::1]:{port}" if port is not None else "[::1]"
    else:
        netloc = host if port is None else f"{host}:{port}"
    return f"http://{netloc}"


def get_known_app_remote_agent_ids() -> set[str]:
    """Return the set of remoteAgentId values the server recognises.

    Derived from ``app.contact_invitations`` config and the in-memory
    :class:`AppAgentSessionRegistry`.  Always includes ``"default"``.
    """
    ids: set[str] = {"default"}
    configured = cfg.get("app", {}).get("contact_invitations")
    if configured:
        for entry in configured:
            rid = str(entry.get("remoteAgentId", "default")).strip()
            if rid:
                ids.add(rid)
    ids |= app_agent_session_registry.known_remote_agent_ids()
    return ids


def resolve_app_remote_agent(remote_agent_id: str) -> dict:
    """Validate *remote_agent_id* and return routing metadata.

    Returns a dict with keys:

    * ``remoteAgentId`` — the canonical id (str)
    * ``target`` — ``"default"`` for internal, ``"attached_session"`` for
      registered callback sessions, ``"claude_channel"`` for channel
      sessions polled by an MCP channel server
    * ``sessionId`` — session id or None
    * ``status`` — ``"online"`` / ``"offline"`` or None
    * ``callbackUrl`` — callback URL or None
    * ``kind`` — session kind (``"attached_session"`` / ``"claude_channel"``) or None

    Raises ``HTTPException(400)`` for unknown ids.
    """
    known = get_known_app_remote_agent_ids()
    if remote_agent_id not in known:
        raise HTTPException(
            status_code=400,
            detail=f"unknown remoteAgentId: {remote_agent_id}",
        )

    # Check registry for a registered session with this remoteAgentId
    session = app_agent_session_registry.find_by_remote_agent_id(remote_agent_id)
    if session is not None:
        kind = session.get("kind") or "attached_session"
        target = "claude_channel" if kind == "claude_channel" else "attached_session"
        return {
            "remoteAgentId": remote_agent_id,
            "target": target,
            "kind": kind,
            "sessionId": session["sessionId"],
            "status": session["status"],
            "callbackUrl": session.get("callbackUrl"),
        }

    # Config / default
    return {
        "remoteAgentId": remote_agent_id,
        "target": "default",
        "kind": None,
        "sessionId": None,
        "status": None,
        "callbackUrl": None,
    }


# ── app attachments ──────────────────────────────────────────────


@app.post("/api/attachments")
async def api_upload_attachment(request: Request):
    """Upload an image attachment (multipart/form-data).

    Auth-first: returns 401 before parsing the file body.
    Phase 1: only ``image/*``, max 10 MB.
    """
    api_cfg = _api_config()
    _require_chat_auth(request, api_cfg)  # 401 before body

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type.lower():
        raise HTTPException(status_code=400, detail="content-type must be multipart/form-data")

    # Early size guard via Content-Length (optional — body-size check catches the rest)
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > _MAX_BODY_CONTENT_LENGTH:
                raise HTTPException(status_code=413, detail="attachment exceeds 10 MB limit")
        except (ValueError, TypeError):
            pass

    body = await request.body()
    if len(body) > _MAX_BODY_CONTENT_LENGTH:
        raise HTTPException(status_code=413, detail="attachment exceeds 10 MB limit")

    parts = parse_multipart_form(content_type, body)
    file_part = next((p for p in parts if p["name"] == "file"), None)
    if file_part is None or not file_part.get("filename"):
        raise HTTPException(status_code=400, detail="file part is required")
    if not file_part.get("content_type", "").startswith("image/"):
        raise HTTPException(status_code=400, detail="only image attachments are supported")
    if not file_part["data"]:
        raise HTTPException(status_code=400, detail="empty file")
    if len(file_part["data"]) > _MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="attachment exceeds 10 MB limit")

    mime_type = file_part["content_type"].strip()
    original_filename = file_part["filename"]
    sanitized = _sanitize_attachment_filename(original_filename)
    ext = _attachment_ext(mime_type, original_filename)
    attachment_id = f"app_att_{uuid.uuid4().hex}"
    storage_filename = f"{attachment_id}.{ext}"
    _ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    storage_path = _ATTACHMENTS_DIR / storage_filename

    storage_path.write_bytes(file_part["data"])

    size_bytes = len(file_part["data"])
    now_ts = time.time()
    await memory_store.insert_app_attachment(
        attachment_id=attachment_id,
        type="image",
        mime_type=mime_type,
        file_name=sanitized,
        size_bytes=size_bytes,
        storage_path=str(storage_path),
        created_at=now_ts,
    )

    base = str(request.base_url).rstrip("/")
    url = f"{base}/api/attachments/{attachment_id}"

    logger.info("[app] attachment uploaded id=%s mime=%s size=%d", attachment_id, mime_type, size_bytes)

    return {
        "attachmentId": attachment_id,
        "type": "image",
        "mimeType": mime_type,
        "fileName": sanitized,
        "sizeBytes": size_bytes,
        "url": url,
        "createdAt": _iso_from_ts(now_ts),
    }


@app.get("/api/attachments/{attachment_id}")
async def api_get_attachment(attachment_id: str, request: Request):
    """Download a previously uploaded attachment."""
    api_cfg = _api_config()
    _require_chat_auth(request, api_cfg)

    if not _ATTACHMENT_ID_RE.fullmatch(attachment_id):
        raise HTTPException(status_code=404, detail="attachment not found")

    meta = await memory_store.get_app_attachment(attachment_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="attachment not found")

    storage_path = str(meta["storage_path"])
    # Defensive: refuse paths that don't resolve under the attachments root
    resolved = _os.path.realpath(_os.path.normpath(storage_path))
    expected_root = _os.path.realpath(_os.path.normpath(str(_ATTACHMENTS_DIR)))
    if not resolved.startswith(expected_root + _os.path.sep) and resolved != expected_root:
        logger.warning("[app] attachment path traversal attempt id=%s resolved=%s", attachment_id, resolved)
        raise HTTPException(status_code=404, detail="attachment not found")

    if not _os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="attachment file missing on disk")

    return FileResponse(resolved, media_type=str(meta["mime_type"]))


@app.get("/api/profile")
async def api_profile(request: Request):
    """Validate auth and return agent name — same auth as /chat."""
    api_cfg = _api_config()
    _require_chat_auth(request, api_cfg)
    return {
        "agentName": cfg.get("agent", {}).get("name", "bot"),
        "connectedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@app.post("/api/messages")
async def api_send_message(request: Request):
    """Accept one user message, call Agent, return all delivered replies.

    Idempotent by clientMessageId.  Only send_text_message tool calls
    become visible bubbles — assistant final text, thinking, tool_result,
    and non-send tools are ignored.  Server-side timeout: 55 s.

    Optional *remoteAgentId* selects the target agent (defaults to
    ``"default"``).  Routing is a placeholder — every known id currently
    resolves to the single-agent runtime.
    """
    api_cfg = _api_config()
    _require_chat_auth(request, api_cfg)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    client_message_id = str(body.get("clientMessageId", "")).strip()
    content = str(body.get("content", "")).strip()
    raw_attachments = body.get("attachments")

    if not client_message_id:
        raise HTTPException(status_code=400, detail="clientMessageId is required")
    if not content and not raw_attachments:
        raise HTTPException(status_code=400, detail="content or attachments are required")

    # ── validate and canonicalise attachments ──────────────────────
    incoming_attachments: list[dict] = []
    if raw_attachments:
        if not isinstance(raw_attachments, list):
            raise HTTPException(status_code=400, detail="attachments must be an array")
        if len(raw_attachments) > 1:
            raise HTTPException(
                status_code=400,
                detail="Phase 1 supports at most 1 attachment per message",
            )
        # Validate each attachmentId exists in the store
        incoming_ids: list[str] = []
        for att in raw_attachments:
            aid = str(att.get("attachmentId", "")).strip()
            if not aid:
                raise HTTPException(status_code=400, detail="attachmentId is required")
            incoming_ids.append(aid)

        stored_atts = await memory_store.get_app_attachments(incoming_ids)
        stored_by_id = {s["attachment_id"]: s for s in stored_atts}
        for aid in incoming_ids:
            if aid not in stored_by_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown attachmentId: {aid}",
                )
        base = str(request.base_url).rstrip("/")
        # Build canonical attachment dicts from stored metadata (not from client)
        for aid in incoming_ids:
            s = stored_by_id[aid]
            incoming_attachments.append({
                "attachmentId": s["attachment_id"],
                "type": s["type"],
                "mimeType": s["mime_type"],
                "fileName": s["file_name"],
                "sizeBytes": int(s["size_bytes"]),
                "url": f"{base}/api/attachments/{s['attachment_id']}",
            })

    # ── idempotency key for attachments (stable JSON) ──────────────
    _attachments_sort_key = json.dumps(
        [tuple(sorted(a.items())) for a in incoming_attachments],
        ensure_ascii=False,
    )

    # ── resolve remote agent id ────────────────────────────────────
    raw_remote = body.get("remoteAgentId")
    remote_agent_id = normalize_remote_agent_id(raw_remote)
    resolved = resolve_app_remote_agent(remote_agent_id)
    logger.info(
        "[app] POST /api/messages remoteAgentId=%s resolved_target=%s",
        remote_agent_id, resolved["target"],
    )

    # ── attached-session routing guards ─────────────────────────────
    if resolved["target"] == "attached_session":
        if resolved.get("status") != "online":
            raise HTTPException(
                status_code=503,
                detail=json.dumps({
                    "error": {
                        "code": "agent_session_offline",
                        "message": "Agent session is offline",
                    },
                }),
            )
        if not resolved.get("callbackUrl"):
            raise HTTPException(
                status_code=501,
                detail=json.dumps({
                    "error": {
                        "code": "agent_session_callback_not_configured",
                        "message": "Agent session has no callback URL configured",
                    },
                }),
            )

    # Note: claude_channel sessions queue user messages into a pending
    # inbox that the channel server polls.  A message is only queued when
    # the session is online (effective status, incl. stale-TTL check) —
    # an offline/stale channel returns 503 below instead of enqueuing.

    # ── idempotency ──────────────────────────────────────────────
    existing_user = await memory_store.find_app_message_by_client_message_id(
        client_message_id,
        remote_agent_id=remote_agent_id,
    )
    if existing_user is not None:
        # Compare content
        if str(existing_user.get("content", "")) != content:
            raise HTTPException(
                status_code=409,
                detail="clientMessageId already used with different content",
            )
        # Compare attachments (canonical JSON)
        existing_attach = _parse_attachments_json(existing_user.get("attachments_json"))
        existing_attach_key = json.dumps(
            [tuple(sorted(a.items())) for a in existing_attach],
            ensure_ascii=False,
        )
        if existing_attach_key != _attachments_sort_key:
            raise HTTPException(
                status_code=409,
                detail="clientMessageId already used with different attachments",
            )
        # Return the existing result
        source_turn_id = str(existing_user.get("source_turn_id", ""))
        agent_replies = await memory_store.find_app_agent_replies_by_turn_id(
            source_turn_id,
            remote_agent_id=remote_agent_id,
        )
        return {
            "userMessage": {
                "remoteId": existing_user["id"],
                "clientMessageId": client_message_id,
                "status": "sent",
                "createdAt": _iso_from_ts(existing_user.get("created_at", 0)),
                "attachments": _parse_attachments_json(
                    existing_user.get("attachments_json")
                ),
            },
            "agentReplies": [
                {
                    "remoteId": r["id"],
                    "content": r["content"],
                    "createdAt": _iso_from_ts(r.get("created_at", 0)),
                    "sourceToolCallId": r.get("source_tool_call_id") or None,
                }
                for r in agent_replies
            ],
        }

    # ── claude_channel inbox path ───────────────────────────────────
    # Channel sessions don't call a callback or the internal agent.
    # The user message is persisted to app_messages and enqueued in the
    # in-memory pending inbox.  A channel server polls the inbox and
    # pushes messages to Claude Code via ``notifications/claude/channel``.
    # Replies come back via ``POST /api/app-agent-sessions/{id}/replies``.
    if resolved["target"] == "claude_channel":
        # Stale/offline channel: the channel process is not polling, so a
        # queued message would sit in the inbox indefinitely.  Reject with
        # 503 instead of writing a pending delivery.  A retried
        # clientMessageId that was already accepted while online is handled
        # by the idempotency block above and returns its existing result.
        if resolved.get("status") != "online":
            raise HTTPException(
                status_code=503,
                detail=json.dumps({
                    "error": {
                        "code": "agent_session_offline",
                        "message": "Agent session is offline",
                    },
                }),
            )
        turn_id = f"app_turn_{uuid.uuid4().hex[:12]}"
        user_remote_id = f"app_rm_user_{uuid.uuid4().hex[:12]}"
        now_ts = time.time()

        await memory_store.insert_app_message(
            remote_id=user_remote_id,
            client_message_id=client_message_id,
            role="user",
            content=content,
            source_turn_id=turn_id,
            sequence=0,
            remote_agent_id=remote_agent_id,
            created_at=now_ts,
            attachments=incoming_attachments,
        )

        # Enqueue in the pending inbox for channel polling.  The inbox
        # is idempotent by (sessionId, clientMessageId) so a retry after
        # a transient failure would not create a duplicate delivery.
        await channel_pending_inbox.add(
            remote_agent_id=remote_agent_id,
            session_id=resolved["sessionId"],
            client_message_id=client_message_id,
            content=content,
            created_at=_iso_from_ts(now_ts),
            attachments=incoming_attachments,
        )

        logger.info(
            "[app] claude_channel turn=%s user_remote=%s session=%s queued",
            turn_id, user_remote_id, resolved["sessionId"],
        )

        return {
            "userMessage": {
                "remoteId": user_remote_id,
                "clientMessageId": client_message_id,
                "status": "sent",
                "createdAt": _iso_from_ts(now_ts),
                "attachments": incoming_attachments,
            },
            "agentReplies": [],
        }

    # ── attached-session callback path ───────────────────────────────
    if resolved["target"] == "attached_session":
        callback_replies = await _call_attached_session_callback(
            resolved, client_message_id, content, attachments=incoming_attachments,
        )

        # Persist user message + replies after successful callback.
        #
        # Router generates its own local app_messages.id for every agent
        # reply (``app_rm_agent_<uuid>``); the session callback's
        # ``remoteId`` is preserved in ``source_tool_call_id`` for
        # debugging/tracing.  This avoids cross-session id collisions:
        # different sessions may return the same ``remoteId`` (e.g.
        # "msg-1") without conflicting on the Router's global primary key.
        #
        # All rows (user + replies) are written in a single transaction
        # via ``insert_app_turn_messages`` so that a failure in any reply
        # insert rolls back the user message too — no half-success state.
        turn_id = f"app_turn_{uuid.uuid4().hex[:12]}"
        user_remote_id = f"app_rm_user_{uuid.uuid4().hex[:12]}"
        now_ts = time.time()

        agent_replies: list[dict[str, Any]] = []
        reply_rows: list[dict[str, Any]] = []
        for seq, reply in enumerate(callback_replies, start=1):
            router_reply_id = f"app_rm_agent_{uuid.uuid4().hex[:12]}"
            reply_text = reply["content"]
            session_remote_id = reply.get("remoteId") or ""
            cb_created_at = reply.get("createdAt")
            reply_ts = _parse_iso_to_ts(cb_created_at)
            if reply_ts is None:
                reply_ts = now_ts
                cb_created_at = _iso_from_ts(now_ts)
            agent_replies.append({
                "remoteId": router_reply_id,
                "content": reply_text,
                "createdAt": cb_created_at,
                "sourceToolCallId": session_remote_id or None,
            })
            reply_rows.append({
                "remote_id": router_reply_id,
                "content": reply_text,
                "source_turn_id": turn_id,
                "sequence": seq,
                "source_tool_call_id": session_remote_id,
                "created_at": reply_ts,
                "remote_agent_id": remote_agent_id,
            })

        await memory_store.insert_app_turn_messages(
            user_message={
                "remote_id": user_remote_id,
                "client_message_id": client_message_id,
                "content": content,
                "source_turn_id": turn_id,
                "remote_agent_id": remote_agent_id,
                "created_at": now_ts,
                "attachments": incoming_attachments,
            },
            agent_replies=reply_rows,
        )

        logger.info(
            "[app] callback turn=%s user_remote=%s agent_replies=%d",
            turn_id, user_remote_id, len(agent_replies),
        )

        return {
            "userMessage": {
                "remoteId": user_remote_id,
                "clientMessageId": client_message_id,
                "status": "sent",
                "createdAt": _iso_from_ts(now_ts),
                "attachments": incoming_attachments,
            },
            "agentReplies": agent_replies,
        }

    # ── persist user message (internal agent) ───────────────────────
    turn_id = f"app_turn_{uuid.uuid4().hex[:12]}"
    user_remote_id = f"app_rm_user_{uuid.uuid4().hex[:12]}"
    now_ts = time.time()

    await memory_store.insert_app_message(
        remote_id=user_remote_id,
        client_message_id=client_message_id,
        role="user",
        content=content,
        source_turn_id=turn_id,
        sequence=0,
        remote_agent_id=remote_agent_id,
        attachments=incoming_attachments,
    )

    # ── collector sender (replaces wechat_sender) ─────────────────
    collected_texts: list[str] = []

    async def _app_collector(messages: list[dict]) -> list[dict]:
        for m in messages:
            text = str(m.get("text", "")).strip()
            if text:
                collected_texts.append(text)
            m["send_status"] = "delivered"
        return messages

    # ── call Agent with 55 s timeout ──────────────────────────────
    agent_user_message = content if content else "用户发送了一张图片。"
    try:
        result: TurnResult = await asyncio.wait_for(
            chat_runtime.handle_chat(
                user_message=agent_user_message,
                user_id=_APP_USER_ID,
                bot_id=BOT_ID,
                history=[],
                system_prompt=build_system_prompt_for_turn(),
                trigger="user",
                source="user_message",
                wechat_sender=_app_collector,
                prebuilt_messages=[],
                post_turn_hooks=[],
                protocol_event_writer=None,
            ),
            timeout=_APP_AGENT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[app] Agent turn timed out after %.1fs clientMessageId=%s",
            _APP_AGENT_TIMEOUT_S, client_message_id,
        )
        raise HTTPException(
            status_code=503,
            detail=json.dumps({
                "error": {
                    "code": "agent_timeout",
                    "message": "Agent 响应超时，请稍后重试",
                },
            }),
        )

    # ── extract delivered replies ─────────────────────────────────
    # Only SENT_CONFIRMED / delivered send_text_message outputs become
    # agentReplies.  Assistant final text, thinking, tool_result, and
    # non-send tools are ignored.
    agent_replies_internal: list[dict[str, Any]] = []
    seq = 1
    for vm in result.visible_messages:
        is_delivered = (
            vm.get("delivery_status") == DeliveryStatus.SENT_CONFIRMED
            or vm.get("send_status") == "delivered"
        )
        if is_delivered:
            reply_id = f"app_rm_agent_{uuid.uuid4().hex[:12]}"
            reply_text = str(vm.get("text", ""))
            agent_replies_internal.append({
                "remoteId": reply_id,
                "content": reply_text,
                "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
            await memory_store.insert_app_message(
                remote_id=reply_id,
                client_message_id=None,
                role="agent",
                content=reply_text,
                source_turn_id=turn_id,
                sequence=seq,
                remote_agent_id=remote_agent_id,
            )
            seq += 1

    logger.info(
        "[app] turn=%s user_remote=%s agent_replies=%d elapsed_ms=%d",
        turn_id, user_remote_id, len(agent_replies_internal), result.elapsed_ms,
    )

    return {
        "userMessage": {
            "remoteId": user_remote_id,
            "clientMessageId": client_message_id,
            "status": "sent",
            "createdAt": _iso_from_ts(now_ts),
            "attachments": incoming_attachments,
        },
        "agentReplies": agent_replies_internal,
    }


@app.get("/api/messages")
async def api_get_messages(
    request: Request,
    cursor: str | None = None,
    limit: int = 50,
    remoteAgentId: str | None = None,
):
    """Return app_messages ordered by created_at.  Paginated by id cursor.

    Optional *remoteAgentId* selects the target agent (defaults to
    ``"default"``).  Routing is a placeholder — every known id currently
    returns the same message history.
    """
    api_cfg = _api_config()
    _require_chat_auth(request, api_cfg)

    # ── resolve remote agent id ────────────────────────────────────
    resolved_id = normalize_remote_agent_id(remoteAgentId)
    resolved = resolve_app_remote_agent(resolved_id)
    logger.info(
        "[app] GET /api/messages remoteAgentId=%s resolved_target=%s",
        resolved_id, resolved["target"],
    )

    messages, next_cursor, has_more = (
        await memory_store.get_app_messages_after_cursor(
            cursor=cursor,
            limit=limit,
            remote_agent_id=resolved_id,
        )
    )
    return {
        "messages": [
            {
                "remoteId": m["id"],
                "clientMessageId": m.get("client_message_id") or None,
                "role": m["role"],
                "content": m["content"],
                "createdAt": _iso_from_ts(m.get("created_at", 0)),
                "attachments": _parse_attachments_json(m.get("attachments_json")),
            }
            for m in messages
        ],
        "nextCursor": next_cursor,
        "hasMore": has_more,
    }


@app.get("/api/contact-invitations")
async def api_contact_invitations(request: Request):
    """Return contact invitations for the AsPocket app.

    Driven by ``app.contact_invitations`` config when present; otherwise
    falls back to the single pc-agent-default.  Same auth as /api/profile
    and /api/messages.  No relay, no multi-agent runtime.
    """
    api_cfg = _api_config()
    _require_chat_auth(request, api_cfg)

    return {
        "invitations": build_app_contact_invitations(request),
    }


def build_app_contact_invitation(request: Request) -> dict:
    """Build the default single PC agent contact invitation dict.

    Helper so the invitation shape can be reused or tested independently
    without invoking the full endpoint.  Returns one invitation dict (not
    wrapped in the ``invitations`` envelope).
    """
    return {
        "id": "pc-agent-default",
        "remoteAgentId": "default",
        "displayName": cfg.get("agent", {}).get("name", "PC Agent"),
        "avatarUri": None,
        "description": "Personal assistant on this PC",
        "agentType": "pc-agent",
        "sourceLabel": os.getenv("COMPUTERNAME", "PC Agent"),
        "endpoint": str(request.base_url).rstrip("/"),
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expiresAt": None,
    }


def build_app_contact_invitations(request: Request) -> list[dict]:
    """Build the list of contact invitation dicts.

    Merges three sources:

    1. Config-driven invitations from ``app.contact_invitations``.
    2. Online attached sessions from :class:`AppAgentSessionRegistry`.
    3. Default fallback (pc-agent-default) when neither source exists.

    When a config invitation's ``remoteAgentId`` collides with a session,
    the session takes precedence and the config entry is dropped.
    """
    endpoint = str(request.base_url).rstrip("/")
    source_label = os.getenv("COMPUTERNAME", "PC Agent")
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    default_display_name = cfg.get("agent", {}).get("name", "PC Agent")

    invitations: list[dict] = []
    session_ids = app_agent_session_registry.known_remote_agent_ids()

    # 1. Config-driven invitations
    configured = cfg.get("app", {}).get("contact_invitations")
    if configured:
        for entry in configured:
            rid = str(entry.get("remoteAgentId", "default")).strip() or "default"
            if rid in session_ids:
                # Session takes precedence — skip config entry
                continue
            invitations.append({
                "id": str(entry.get("id", "pc-agent-default")),
                "remoteAgentId": rid,
                "displayName": str(entry.get("displayName") or default_display_name),
                "avatarUri": None,
                "description": str(entry.get("description") or "Personal assistant on this PC"),
                "agentType": "pc-agent",
                "sourceLabel": source_label,
                "endpoint": endpoint,
                "createdAt": created_at,
                "expiresAt": None,
            })

    # 2. Online attached sessions
    # list_online() is async — but this function is sync.  We access the
    # internal _sessions dict directly, which is safe for a read-only
    # snapshot in the synchronous context.  effective_status() applies the
    # stale-TTL so a dead channel (no heartbeat within the TTL) is excluded
    # from new invitations even though it never called /offline.
    online_sessions = [
        s for s in app_agent_session_registry._sessions.values()
        if app_agent_session_registry.effective_status(s) == "online"
    ]
    for sess in online_sessions:
        kind = sess.get("kind") or "attached_session"
        agent_type = "claude-code-channel" if kind == "claude_channel" else "attached-session"
        invitations.append({
            "id": f"session-{sess['sessionId']}",
            "remoteAgentId": sess["remoteAgentId"],
            "displayName": sess["displayName"],
            "avatarUri": None,
            "description": sess["description"],
            "agentType": agent_type,
            "sourceLabel": sess.get("sourceLabel") or source_label,
            "endpoint": endpoint,
            "createdAt": sess["createdAt"],
            "expiresAt": sess.get("expiresAt"),
        })

    # 3. Default fallback — always include unless its remoteAgentId
    #    ("default") is already covered by a session or config entry.
    existing_rids = {inv["remoteAgentId"] for inv in invitations}
    if "default" not in session_ids and "default" not in existing_rids:
        invitations.append(build_app_contact_invitation(request))

    return invitations


def _iso_from_ts(ts: float) -> str:
    """Convert a Unix timestamp (seconds) to ISO 8601 UTC string."""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OSError, ValueError, OverflowError):
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_to_ts(iso_str: str | None) -> float | None:
    """Parse an ISO 8601 UTC string (e.g. ``2026-06-19T12:00:02Z``) to a Unix timestamp.

    Returns ``None`` when *iso_str* is missing or cannot be parsed.
    Used to persist attached-session callback reply ``createdAt`` values.
    """
    if not iso_str or not isinstance(iso_str, str):
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


# ── attached session callback forwarding (v1) ──────────────────────

import httpx

_CALLBACK_TIMEOUT_S = 55.0  # matches _APP_AGENT_TIMEOUT_S


def _build_session_callback_url(callback_url: str) -> str:
    """Append ``/api/app-session/messages`` to *callback_url*, stripping trailing slash."""
    return f"{callback_url.rstrip('/')}/api/app-session/messages"


async def _call_attached_session_callback(
    resolved: dict,
    client_message_id: str,
    content: str,
    *,
    attachments: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """POST to the attached session's callback endpoint and return validated replies.

    Raises ``HTTPException`` with appropriate status/detail on any failure.
    Returns a list of reply dicts (may be empty).
    """
    callback_url = _build_session_callback_url(resolved["callbackUrl"])
    payload = {
        "remoteAgentId": resolved["remoteAgentId"],
        "sessionId": resolved["sessionId"],
        "clientMessageId": client_message_id,
        "content": content,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attachments": list(attachments) if attachments else [],
    }

    try:
        async with httpx.AsyncClient(timeout=_CALLBACK_TIMEOUT_S) as client:
            resp = await client.post(callback_url, json=payload)
    except httpx.TimeoutException:
        logger.warning(
            "[app] callback timeout sessionId=%s url=%s",
            resolved["sessionId"], callback_url,
        )
        raise HTTPException(
            status_code=504,
            detail=json.dumps({
                "error": {
                    "code": "agent_session_callback_timeout",
                    "message": "Agent session callback timed out",
                },
            }),
        )
    except Exception as exc:
        logger.warning(
            "[app] callback network error sessionId=%s url=%s err=%s",
            resolved["sessionId"], callback_url, exc,
        )
        raise HTTPException(
            status_code=502,
            detail=json.dumps({
                "error": {
                    "code": "agent_session_callback_error",
                    "message": "Agent session callback network error",
                },
            }),
        )

    if resp.status_code < 200 or resp.status_code >= 300:
        logger.warning(
            "[app] callback non-2xx sessionId=%s status=%d body=%s",
            resolved["sessionId"], resp.status_code,
            resp.text[:500] if resp.text else "",
        )
        raise HTTPException(
            status_code=502,
            detail=json.dumps({
                "error": {
                    "code": "agent_session_callback_failed",
                    "message": f"Agent session returned HTTP {resp.status_code}",
                },
            }),
        )

    # Parse and validate response body
    try:
        body = resp.json()
    except Exception:
        logger.warning(
            "[app] callback invalid JSON sessionId=%s body=%s",
            resolved["sessionId"],
            resp.text[:500] if resp.text else "",
        )
        raise HTTPException(
            status_code=502,
            detail=json.dumps({
                "error": {
                    "code": "agent_session_callback_invalid_response",
                    "message": "Agent session returned invalid JSON",
                },
            }),
        )

    validated = validate_callback_response(body)
    if not validated["ok"]:
        logger.warning(
            "[app] callback invalid response shape sessionId=%s error=%s",
            resolved["sessionId"], validated["error"],
        )
        raise HTTPException(
            status_code=502,
            detail=json.dumps({
                "error": {
                    "code": "agent_session_callback_invalid_response",
                    "message": "Agent session response does not match v1 contract",
                },
            }),
        )

    return validated["replies"]


# ── attached session callback validation (v1 spec) ──────────────────


def validate_callback_response(body: Any) -> dict[str, Any]:
    """Validate an attached-session callback response against the v1 contract.

    Returns ``{"ok": True, "replies": [...]}`` when valid, or
    ``{"ok": False, "error": str}`` when the envelope is malformed.

    Individual reply entries with missing / non-string / empty content are
    silently skipped (not treated as an error).

    This is a pure validator — it does NOT insert messages or modify state.
    It is not yet wired into any production code path.
    """
    if not isinstance(body, dict):
        return {"ok": False, "error": "callback response must be a JSON object"}
    if "agentReplies" not in body:
        return {"ok": False, "error": "callback response missing agentReplies field"}
    raw = body["agentReplies"]
    if not isinstance(raw, list):
        return {"ok": False, "error": "agentReplies must be an array"}

    replies: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        reply: dict[str, Any] = {"content": content.strip()}
        if isinstance(entry.get("remoteId"), str):
            reply["remoteId"] = entry["remoteId"]
        if isinstance(entry.get("createdAt"), str):
            reply["createdAt"] = entry["createdAt"]
        replies.append(reply)

    return {"ok": True, "replies": replies}


# ================================================================
# App agent session registry endpoints
# ================================================================


@app.post("/api/app-agent-sessions/register")
async def api_register_app_agent_session(request: Request):
    """Register or update an attached agent session.

    Same auth as the other /api/* endpoints.  Body: ``sessionId``
    (required), ``remoteAgentId`` (defaults to sessionId),
    ``displayName`` (defaults to sessionId), ``description``,
    ``callbackUrl``, ``sourceLabel``, ``kind``.

    ``kind`` selects the session target type:

    * ``"attached_session"`` (default) — Router forwards messages to
      ``callbackUrl`` via HTTP POST.
    * ``"claude_channel"`` — Router stores messages in an in-memory
      pending inbox polled by a channel server.  ``callbackUrl`` is
      not required and ignored.

    409 if *remoteAgentId* is already claimed by a different session.
    """
    api_cfg = _api_config()
    _require_chat_auth(request, api_cfg)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    session_id = str(body.get("sessionId", "")).strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="sessionId is required")

    remote_agent_id = str(body.get("remoteAgentId") or session_id).strip() or session_id
    display_name = str(body.get("displayName") or session_id).strip() or session_id
    description = str(body.get("description") or "Attached agent session").strip() or "Attached agent session"
    raw_kind = str(body.get("kind", "")).strip()
    kind = raw_kind if raw_kind in ("attached_session", "claude_channel") else "attached_session"
    raw_callback_url = str(body.get("callbackUrl", "")).strip()
    try:
        callback_url = validate_attached_session_callback_url(raw_callback_url) or None
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"callbackUrl must be a localhost http URL ({exc})",
        )
    source_label = str(body.get("sourceLabel", "")).strip() or None

    try:
        session = await app_agent_session_registry.register(
            session_id=session_id,
            remote_agent_id=remote_agent_id,
            display_name=display_name,
            description=description,
            callback_url=callback_url,
            source_label=source_label,
            kind=kind,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    logger.info(
        "[app] session registered sessionId=%s remoteAgentId=%s kind=%s",
        session_id, remote_agent_id, kind,
    )
    return {"session": session}


@app.post("/api/app-agent-sessions/{session_id}/heartbeat")
async def api_app_agent_session_heartbeat(session_id: str, request: Request):
    """Update lastSeenAt and set status to online."""
    api_cfg = _api_config()
    _require_chat_auth(request, api_cfg)

    session = await app_agent_session_registry.heartbeat(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session": session}


@app.post("/api/app-agent-sessions/{session_id}/offline")
async def api_app_agent_session_offline(session_id: str, request: Request):
    """Mark a session offline."""
    api_cfg = _api_config()
    _require_chat_auth(request, api_cfg)

    session = await app_agent_session_registry.offline(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    logger.info("[app] session offline sessionId=%s", session_id)
    return {"session": session}


@app.get("/api/app-agent-sessions")
async def api_list_app_agent_sessions(request: Request):
    """Return all registered sessions (debug / inspection)."""
    api_cfg = _api_config()
    _require_chat_auth(request, api_cfg)

    sessions = await app_agent_session_registry.list_all()
    return {"sessions": sessions}


# ================================================================
# Claude channel inbox / outbox endpoints
# ================================================================
#
# These endpoints serve MCP ``claude/channel`` servers (e.g. an
# AsPocket channel server that bridges App messages to Claude Code).
# A channel server polls pending user messages, pushes them to Claude
# Code via ``notifications/claude/channel``, acks delivery, and posts
# Claude Code's replies back so the App can fetch them via
# ``GET /api/messages``.


@app.get("/api/app-agent-sessions/{session_id}/pending-messages")
async def api_channel_poll_pending(
    session_id: str, request: Request, limit: int = 10,
):
    """Poll undelivered user messages for a claude_channel session.

    Returns up to *limit* pending messages (default 10).  Each message
    stays in the inbox until ``ack`` is called — at-least-once delivery.
    """
    api_cfg = _api_config()
    _require_chat_auth(request, api_cfg)

    session = await app_agent_session_registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if (session.get("kind") or "attached_session") != "claude_channel":
        raise HTTPException(
            status_code=400,
            detail="session is not a claude_channel session",
        )

    messages = await channel_pending_inbox.list_pending(session_id, limit=limit)
    return {
        "messages": [
            {
                "deliveryId": m["deliveryId"],
                "remoteAgentId": m["remoteAgentId"],
                "sessionId": m["sessionId"],
                "clientMessageId": m["clientMessageId"],
                "content": m["content"],
                "createdAt": m["createdAt"],
                "attachments": m.get("attachments", []),
            }
            for m in messages
        ],
    }


@app.post("/api/app-agent-sessions/{session_id}/pending-messages/{delivery_id}/ack")
async def api_channel_ack_pending(
    session_id: str, delivery_id: str, request: Request,
):
    """Acknowledge that a pending message was delivered to the channel.

    Ack is not a reply; it only suppresses the message from later polls
    so the channel doesn't re-deliver it to Claude Code.
    """
    api_cfg = _api_config()
    _require_chat_auth(request, api_cfg)

    session = await app_agent_session_registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if (session.get("kind") or "attached_session") != "claude_channel":
        raise HTTPException(
            status_code=400,
            detail="session is not a claude_channel session",
        )

    msg = await channel_pending_inbox.get(delivery_id)
    if msg is None or msg["sessionId"] != session_id:
        raise HTTPException(status_code=404, detail="delivery not found")

    acked = await channel_pending_inbox.ack(delivery_id)
    return {"acked": acked}


@app.post("/api/app-agent-sessions/{session_id}/replies")
async def api_channel_post_reply(session_id: str, request: Request):
    """Post a Claude Code reply back to the App's message history.

    The reply is persisted as an agent ``app_message`` under the same
    ``remoteAgentId`` and ``source_turn_id`` as the original user
    message, so ``GET /api/messages`` returns it as a normal reply.

    Body: ``clientMessageId`` (the original App message id),
    ``content`` (non-empty), ``sourceToolCallId`` (optional).
    """
    api_cfg = _api_config()
    _require_chat_auth(request, api_cfg)

    session = await app_agent_session_registry.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if (session.get("kind") or "attached_session") != "claude_channel":
        raise HTTPException(
            status_code=400,
            detail="session is not a claude_channel session",
        )
    if session.get("status") != "online":
        raise HTTPException(
            status_code=503,
            detail=json.dumps({
                "error": {
                    "code": "agent_session_offline",
                    "message": "Agent session is offline",
                },
            }),
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    client_message_id = str(body.get("clientMessageId", "")).strip()
    if not client_message_id:
        raise HTTPException(status_code=400, detail="clientMessageId is required")
    content = str(body.get("content", "")).strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    source_tool_call_id = str(body.get("sourceToolCallId", "")).strip() or ""

    remote_agent_id = session["remoteAgentId"]
    user_msg = await memory_store.find_app_message_by_client_message_id(
        client_message_id, remote_agent_id=remote_agent_id,
    )
    if user_msg is None:
        raise HTTPException(
            status_code=404,
            detail="unknown clientMessageId for this session",
        )

    turn_id = str(user_msg.get("source_turn_id", ""))
    # Sequence = max existing agent sequence + 1 for this turn
    existing_replies = await memory_store.find_app_agent_replies_by_turn_id(
        turn_id, remote_agent_id=remote_agent_id,
    )
    next_seq = max((r.get("sequence", 0) for r in existing_replies), default=0) + 1
    reply_remote_id = f"app_rm_agent_{uuid.uuid4().hex[:12]}"
    now_ts = time.time()

    await memory_store.insert_app_message(
        remote_id=reply_remote_id,
        client_message_id=None,
        role="agent",
        content=content,
        source_turn_id=turn_id,
        source_tool_call_id=source_tool_call_id,
        sequence=next_seq,
        remote_agent_id=remote_agent_id,
        created_at=now_ts,
    )

    logger.info(
        "[app] claude_channel reply session=%s turn=%s reply_remote=%s",
        session_id, turn_id, reply_remote_id,
    )

    return {
        "reply": {
            "remoteId": reply_remote_id,
            "clientMessageId": client_message_id,
            "content": content,
            "createdAt": _iso_from_ts(now_ts),
            "sourceToolCallId": source_tool_call_id or None,
        },
    }


@app.post("/admin/dev/compress")
async def developer_compress_now(request: Request):
    """Manually trigger conversation compression."""
    require_admin(request)
    contact = await memory_store.get_contact_state()
    user_id = str(contact.get("user_id", "") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="还没有微信联系人记录")

    logger.info("Manual compression triggered for %s", user_id)
    lock = user_turns.get_lock(user_id)
    async with lock:
        return await _run_compression(user_id, "default", force=True)


@app.post("/admin/dev/forget")
async def developer_forget_all(request: Request):
    """Clear test conversation state while preserving the WeChat contact."""
    return await developer_reset_context(request)


@app.post("/admin/dev/reset-context")
async def developer_reset_context(request: Request):
    """Clear conversation and long-term memory while keeping WeChat contact."""
    require_admin(request)
    contact = await memory_store.get_contact_state()
    user_id = str(contact.get("user_id", "") or "").strip()
    if not user_id:
        counts = await conversation_ctx.complete(memory_store)
        return {
            "status": "reset",
            "mode": "all_memory_keep_connection",
            "cleared": counts,
            "long_term_memory": {"files": 0},
            "kept": ["bot_contact_state"],
        }
    lock = user_turns.get_lock(user_id)
    async with lock:
        clear_receipt = await long_term_memory.stage_clear(user_id)
        try:
            counts = await conversation_ctx.complete(memory_store)
        except Exception:
            await clear_receipt.rollback()
            raise
        try:
            clear_receipt.finalize()
        except Exception:
            logger.exception("Failed to finalize long-term memory clear for %s", user_id)
            raise HTTPException(
                status_code=500,
                detail="对话已清除，但长期记忆备份清理失败，请检查日志",
            )
        logger.info(
            "memory clear source=admin user=%s files=%d ok=true",
            long_term_memory.user_key(user_id)[:12],
            clear_receipt.removed_files,
        )
    return {
        "status": "reset",
        "mode": "all_memory_keep_connection",
        "cleared": counts,
        "long_term_memory": {"files": clear_receipt.removed_files},
        "kept": ["bot_contact_state"],
    }


@app.get("/admin/memories")
async def admin_list_memories(request: Request):
    require_admin(request)
    contact = await memory_store.get_contact_state()
    user_id = str(contact.get("user_id", "") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="还没有微信联系人记录")
    lock = user_turns.get_lock(user_id)
    async with lock:
        result = long_term_memory.diagnostics(user_id)
    return {
        "user_id": user_id,
        "entries": result["entries"],
        "warnings": result["warnings"],
    }


@app.get("/admin/memories/{filename}")
async def admin_get_memory(filename: str, request: Request):
    require_admin(request)
    contact = await memory_store.get_contact_state()
    user_id = str(contact.get("user_id", "") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="还没有微信联系人记录")
    lock = user_turns.get_lock(user_id)
    async with lock:
        try:
            result = long_term_memory.view(user_id, filename)
        except Exception as exc:
            from memory.long_term import LongTermMemoryError
            if isinstance(exc, LongTermMemoryError):
                raise HTTPException(status_code=404, detail=exc.message) from exc
            raise
    return result


@app.delete("/admin/memories/{filename}")
async def admin_delete_memory(filename: str, request: Request):
    require_admin(request)
    contact = await memory_store.get_contact_state()
    user_id = str(contact.get("user_id", "") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="还没有微信联系人记录")
    lock = user_turns.get_lock(user_id)
    async with lock:
        try:
            result = await long_term_memory.delete_for_admin(user_id, filename)
        except Exception as exc:
            from memory.long_term import LongTermMemoryError
            if isinstance(exc, LongTermMemoryError):
                status = 404 if exc.code == "memory_not_found" else 400
                raise HTTPException(status_code=status, detail=exc.message) from exc
            raise
    logger.info(
        "memory admin delete user=%s path=%s code=%s bytes=%s",
        long_term_memory.user_key(user_id)[:12],
        filename,
        result.get("code"),
        result.get("bytes", 0),
    )
    return result


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
    return read_recent_log_lines(
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
    """Send a normalized message list through the active WeChat bridge.

    Thin wrapper around ``wechat_sender_service.send_admin_batch`` — guard
    application stays here (main.py owns the agent-layer guard functions),
    while the core sending loop lives in :class:`bridge.sender.WeChatSender`.
    """
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

    wcfg = cfg.get("wechat", {})
    min_gap = float(wcfg.get("message_delay", 1.2) or 1.2)
    max_reply_delay = float(wcfg.get("max_reply_delay", 3.0) or 3.0)
    await wechat_sender_service.send_admin_batch(
        user_id,
        context_token,
        guarded,
        min_gap=min_gap,
        max_reply_delay=max_reply_delay,
        text_guard=guard_single_message,
    )


# ================================================================
# Contact send helpers
# ================================================================


# ================================================================
# Entry point
# ================================================================

if __name__ == "__main__":
    import uvicorn

    host = cfg.get("server", {}).get("host", "0.0.0.0")
    port = int(cfg.get("server", {}).get("port", 8899))

    logger.info(f"Starting server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


