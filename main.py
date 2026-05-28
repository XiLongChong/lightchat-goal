"""Goal Chat Agent — main entry point.

A lightweight, privacy-preserving chat agent with:
  - Multi-message output (JSON format, DeepSeek-compatible)
  - Simple tool calling (weather, time)
  - Long-term memory (SQLite + CJK bigram retrieval)
  - Proactive messaging (hard rules + LLM scheduler)
  - WeChat integration (emoRebot iLink bridge)

All 10 production safeguards built in from day one.
Single process, single SQLite file, zero cloud dependencies.
"""

import asyncio
import io
import random
import secrets
import uuid
import zipfile
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
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from agent.bot_profile import BotProfileManager, DEFAULT_BOT_ID
from agent.chat_agent import ChatAgent, ROUTE_SYSTEM_FAST, ROUTE_CHAT_LITE, ROUTE_CHAT_FULL
from agent.chat_agent import CTX_HOT_ONLY, CTX_HOT_CHEAP, CTX_HOT_FULL
from agent.context_builder import compact_history
from agent.llm_gateway import LLMGateway
from agent.llm_types import LLMRequest
from agent.memory_worker import MemoryWorker
from agent.output_guard import guard_messages, guard_single_message
from agent.scheduler import SchedulerAgent, CST as SCHEDULER_CST
from agent.tools import configure_runtime, configure_tools, execute_tool
from bridge.wechat_poller import WeChatBridge, login_qr
from memory.store import MemoryStore

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

# Phase 6A: Env-var override for native_tool_calls gray-release.
# GOAL_NATIVE_TOOL_CALLS=true overrides config.yaml without editing the file.
_native_override = os.getenv("GOAL_NATIVE_TOOL_CALLS", "").strip().lower()
if _native_override in ("true", "1", "yes", "on"):
    _ntc = cfg.setdefault("model", {}).setdefault("native_tool_calls", {})
    _ntc["chat_agent"] = True
    if not _ntc.get("allowed_tools"):
        _ntc["allowed_tools"] = ["get_time", "get_weather"]
RUNTIME_SETTINGS_PATH = Path("./data/runtime_settings.json")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ADMIN_SESSIONS: set[str] = set()
ADMIN_COOKIE = "goal_admin_session"
PROCESS_STARTED_AT = time.time()


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


CONFIRM_WORDS = {"确认", "可以", "保存", "写入", "改吧", "同意", "好", "行", "yes", "ok", "okay"}
CANCEL_WORDS = {"取消", "不要", "不用", "别改", "先别", "不保存", "no", "cancel"}


def _has_any_word(text: str, words: set[str]) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in words)


async def handle_pending_persona_confirmation(user_message: str) -> dict | None:
    pending = await memory_store.get_pending_persona_change()
    if not pending:
        return None

    if _has_any_word(user_message, CANCEL_WORDS):
        await memory_store.clear_pending_persona_change()
        return {
            "messages": [{"text": "好 我先不改", "delay": 0}],
            "tool_calls": [{"name": "pending_persona_change", "result": "cancelled"}],
            "scheduled_task_ids": [],
            "iterations": 0,
            "elapsed_ms": 0,
        }

    if not _has_any_word(user_message, CONFIRM_WORDS):
        return None

    content = str(pending.get("content", "")).strip()
    if not content:
        await memory_store.clear_pending_persona_change()
        return {
            "messages": [{"text": "草案是空的 我丢掉了", "delay": 0}],
            "tool_calls": [{"name": "pending_persona_change", "result": "empty"}],
            "scheduled_task_ids": [],
            "iterations": 0,
            "elapsed_ms": 0,
        }

    path = Path(cfg.get("agent", {}).get("persona_file", "./prompts/persona.md"))
    root = Path.cwd().resolve()
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if root not in path.parents and path != root:
        await memory_store.clear_pending_persona_change()
        return {
            "messages": [{"text": "这个路径不安全 我没改", "delay": 0}],
            "tool_calls": [{"name": "pending_persona_change", "result": "unsafe_path"}],
            "scheduled_task_ids": [],
            "iterations": 0,
            "elapsed_ms": 0,
        }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")
    chat_agent.personality = content
    bot_manager.invalidate_prompt_pack()  # V2.1.1: force re-read .md on next turn
    await memory_store.clear_pending_persona_change()
    return {
        "messages": [{"text": "好 已经写进人设了", "delay": 0}],
        "tool_calls": [{"name": "pending_persona_change", "result": "committed"}],
        "scheduled_task_ids": [],
        "iterations": 0,
        "elapsed_ms": 0,
    }


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
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
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
    db_path=cfg["memory"].get("db_path", "./data/memory.db"),
)
configure_runtime(memory_store=memory_store)

# Register LLM trace callback — fires after every gateway.complete() call.
# Only token counts, latency, and error kinds are persisted; never raw_response
# or provider raw error strings.
async def _on_llm_trace(trace: dict):
    try:
        await memory_store.save_llm_trace(trace)
    except Exception:
        pass  # Trace loss must never affect the caller.

llm_gateway.on_trace(_on_llm_trace)

# V2.1: Bot profile manager
bot_manager = BotProfileManager(
    config=cfg,
    prompts_dir=cfg.get("prompt_pack", {}).get("directory", "./prompts"),
    persona_file=cfg.get("agent", {}).get("persona_file", "./prompts/persona.md"),
)

chat_agent = ChatAgent(
    model=model_name,
    persona=cfg["agent"]["persona"],
    personality=read_persona_file(),
    temperature=temperature,
    defaults={
        "default_city": cfg.get("weather", {}).get(
            "default_city",
            cfg.get("fixed_messages", {}).get("morning", {}).get("city", ""),
        )
    },
    bot_manager=bot_manager,
    llm_gateway=llm_gateway,
    json_output_enabled=cfg.get("model", {}).get("json_output", {}).get("chat_agent", True),
    # Phase 5A: native tool calls feature flag (default: false)
    native_tool_calls=cfg.get("model", {}).get("native_tool_calls", {}).get("chat_agent", False),
    native_allowed_tools=cfg.get("model", {}).get("native_tool_calls", {}).get("allowed_tools", []),
)

memory_worker = MemoryWorker(
    store=memory_store,
    model=model_name,
    min_importance=cfg["memory"].get("min_importance_save", 0.4),
    prompts_dir=cfg.get("prompt_pack", {}).get("directory", "./prompts"),
    llm_gateway=llm_gateway,
)

scheduler_agent = SchedulerAgent(
    store=memory_store,
    model=model_name,
    llm_gateway=llm_gateway,
    config={
        **(cfg.get("scheduler") or {}),
        "prompts_dir": cfg.get("prompt_pack", {}).get("directory", "./prompts"),
        "json_output": cfg.get("model", {}).get("json_output", {}),
    },
)

# V2.1.1: Inject proactive timing config
_pro_cfg = cfg.get("proactive", {})
_timing = _pro_cfg.get("timing", {})
if _timing:
    scheduler_agent.min_interval = _timing.get("min_interval_minutes", 30)
    scheduler_agent.max_interval = _timing.get("max_interval_minutes", 60)
_gen_cfg = _pro_cfg.get("generation", {})
if _gen_cfg:
    scheduler_agent.enabled_types = _gen_cfg.get("enabled_types",
        ["check_in", "latest_topic_followup", "self_share", "light_ping"])
    scheduler_agent.latest_topic_only = _gen_cfg.get("latest_topic_only", True)
    scheduler_agent.allow_llm_skip = _gen_cfg.get("allow_llm_skip", False)
    scheduler_agent.clingy_mode_enabled = bool(_gen_cfg.get("clingy_mode_enabled", False))
    scheduler_agent.clingy_after_minutes = int(_gen_cfg.get("clingy_after_minutes", 20) or 20)
    scheduler_agent.max_proactive_messages = max(1, min(int(_gen_cfg.get("max_messages", 1) or 1), 3))
_topic_cfg = _pro_cfg.get("topic", {})
if _topic_cfg:
    scheduler_agent.followup_max = _topic_cfg.get("followup_max_count", 2)
    scheduler_agent.topic_expire_minutes = _topic_cfg.get("default_expire_minutes", 240)
_clingy_cfg = _pro_cfg.get("clingy", {})
if _clingy_cfg:
    scheduler_agent.max_followups_per_unanswered = int(_clingy_cfg.get("max_followups_per_unanswered", 3) or 3)

# V2.2: Emotion engine
from agent.emotion.engine import EmotionEngine
from agent.emotion.models import BotEmotionState
emotion_engine = EmotionEngine(config=cfg.get("emotion", {}))

# V2.2: Dialogue state engine
from agent.dialogue_state.engine import DialogueStateEngine
from agent.dialogue_state.models import DialogueState
dialogue_state_engine = DialogueStateEngine(
    config={
        **(cfg.get("dialogue_state", {})),
        "silence": (cfg.get("emotion", {}).get("silence", {})),
    }
)

scheduler = AsyncIOScheduler()

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
    )

# ================================================================
# V2: Per-user turn lock (Phase 4)
# TODO: single-process only; multi-process / multi-replica needs a distributed lock
# ================================================================

_user_turn_locks: dict[str, asyncio.Lock] = {}
_user_turn_lock_access: dict[str, float] = {}
_MAX_USER_TURN_LOCKS = 512
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

    # 3. current_bot_id must load a bot profile
    current_bot_id = bot_manager.current_bot_id
    active_bot = bot_manager.get_bot(current_bot_id)
    if active_bot is None:
        msg = (
            f"current_bot_id={current_bot_id!r} does not resolve to a bot profile; "
            "check bots section in config.yaml"
        )
        logger.error(msg)
        errors.append("bot_profile_not_found")
    else:
        logger.info(f"Active bot loaded: {current_bot_id} ({active_bot.display_name})")

    # 4. LLM API key must exist
    llm_key = cfg.get("model", {}).get("api_key", "")
    if not llm_key:
        msg = "model.api_key is not set; LLM calls will fail"
        logger.error(msg)
        errors.append("llm_api_key_missing")
    else:
        logger.info("LLM API key: configured")

    # 5. data dir must be writable
    db_path = cfg.get("memory", {}).get("db_path", "./data/memory.db")
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


# ================================================================
# V2: Route selection (Phase 5 — Conservative Routing)
# ================================================================

def _select_route(user_message: str, trigger: str, tool_likely: bool = False) -> str:
    """Select the conversation route for this turn.

    Conservative rules per V2 design:
    - SystemFastPath: empty input, duplicate confirmation, pure system-level
    - ChatLite: short user message that depends on context
    - ChatFull: complex input, proactive/scheduled trigger, or tool likely
    """
    if trigger != "user":
        return ROUTE_CHAT_FULL

    stripped = user_message.strip()

    # SystemFastPath: empty or trivial system-level
    if not stripped:
        return ROUTE_SYSTEM_FAST

    # ChatLite: short message, no obvious tool/complexity signal
    # (includes continuation confirmations like "继续", "嗯", "好" —
    #  these depend on context so they must not be routed to SystemFastPath)
    if len(stripped) <= 30 and not tool_likely:
        return ROUTE_CHAT_LITE

    # ChatFull: everything else
    return ROUTE_CHAT_FULL


# ================================================================
# V2: Context policy selection (Phase 6)
# ================================================================

def _select_context_policy(route: str, memories_count: int) -> str:
    """Select context loading policy.

    - hot_only: relationship, current time, recent messages
    - hot+cheap_memory: lightweight keyword/category search
    - hot+full_memory: broader SQLite search
    """
    if route == ROUTE_SYSTEM_FAST:
        return CTX_HOT_ONLY
    if route == ROUTE_CHAT_LITE:
        return CTX_HOT_CHEAP if memories_count > 0 else CTX_HOT_ONLY
    return CTX_HOT_FULL

# ================================================================
# Conversation history (in-memory, per session)
# ================================================================

# Simple in-memory history store. Keyed by "user_id:bot_id".
conversation_histories: dict[str, list[dict]] = {}
MAX_HISTORY_TURNS = cfg["conversation"].get("max_history_turns", 20)
COMPACT_AT_TURNS = cfg["conversation"].get("compact_at_turns", 40)


def _history_key(user_id: str, bot_id: str) -> str:
    return f"{user_id}:{bot_id}"


def get_history(user_id: str, bot_id: str = "") -> list[dict]:
    key = _history_key(user_id, bot_id)
    if key not in conversation_histories:
        conversation_histories[key] = []
    return conversation_histories[key]


async def get_history_async(user_id: str, bot_id: str = "") -> list[dict]:
    """Return in-memory history, restoring recent raw history after restart."""
    key = _history_key(user_id, bot_id)
    history = get_history(user_id, bot_id)
    if history:
        return history
    restored = await memory_store.get_recent_conversation_history(
        user_id,
        limit=MAX_HISTORY_TURNS * 2,
        bot_id=bot_id,
    )
    if restored:
        conversation_histories[key] = restored
    return conversation_histories[key]


def append_history(user_id: str, role: str, content: str, source: str = "user", bot_id: str = ""):
    key = _history_key(user_id, bot_id)
    history = get_history(user_id, bot_id)
    history.append({
        "role": role,
        "content": content,
        "ts": time.time(),
        "source": source,
    })

    if len(history) > COMPACT_AT_TURNS * 2:
        compacted, _ = compact_history(history, MAX_HISTORY_TURNS, COMPACT_AT_TURNS)
        conversation_histories[key] = compacted


async def append_history_async(user_id: str, role: str, content: str, source: str = "user", bot_id: str = ""):
    ts = time.time()
    key = _history_key(user_id, bot_id)
    history = get_history(user_id, bot_id)
    history.append({
        "role": role,
        "content": content,
        "ts": ts,
        "source": source,
    })
    await memory_store.append_conversation_history(
        user_id=user_id,
        role=role,
        content=content,
        source=source,
        ts=ts,
        bot_id=bot_id,
    )

    if len(history) > COMPACT_AT_TURNS * 2:
        compacted, summary = compact_history(history, MAX_HISTORY_TURNS, COMPACT_AT_TURNS)
        previous = await memory_store.get_history_summary(user_id, bot_id=bot_id)
        merged = "\n".join(part for part in [previous, summary] if part).strip()
        if merged:
            await memory_store.save_history_summary(user_id, merged[-4000:], bot_id=bot_id)
        conversation_histories[key] = compacted


def result_token_count(result: dict) -> int:
    usage = result.get("token_usage") or {}
    try:
        return max(0, int(usage.get("total_tokens", 0) or 0))
    except (TypeError, ValueError):
        return 0


def result_fallback_type(result: dict) -> str:
    events = result.get("fallback_events") or []
    types = []
    for item in events:
        if isinstance(item, dict) and item.get("type"):
            types.append(str(item["type"]))
    return ",".join(types[:3])


def result_fallback_detail(result: dict) -> str:
    events = result.get("fallback_events") or []
    if not events or not isinstance(events[0], dict):
        return ""
    return str(events[0].get("detail", ""))[:200]


def public_settings() -> dict:
    """Settings safe to show/edit in the local admin UI."""
    email_cfg = cfg.get("email", {})
    return {
        "scheduler": {
            "enabled": cfg.get("scheduler", {}).get("enabled", True),
            "interval_minutes": cfg.get("scheduler", {}).get("interval_minutes", 3),
            "scheduled_check_seconds": cfg.get("scheduler", {}).get("scheduled_check_seconds", 10),
            "quiet_hours_enabled": cfg.get("scheduler", {}).get("quiet_hours_enabled", True),
            "sleep_start": cfg.get("scheduler", {}).get("sleep_start", 0),
            "sleep_end": cfg.get("scheduler", {}).get("sleep_end", 7),
            "max_daily_proactive": cfg.get("scheduler", {}).get("max_daily_proactive", 5),
            "max_ignored_streak": cfg.get("scheduler", {}).get("max_ignored_streak", 3),
        },
        "proactive": {
            "timing": cfg.get("proactive", {}).get("timing", {}),
            "generation": cfg.get("proactive", {}).get("generation", {}),
            "topic": cfg.get("proactive", {}).get("topic", {}),
        },
        "clingy": cfg.get("proactive", {}).get("clingy", {}) or {
            "max_followups_per_unanswered": 3,
        },
        "wechat": {
            "message_delay": cfg.get("wechat", {}).get("message_delay", 1.2),
            "max_reply_delay": cfg.get("wechat", {}).get("max_reply_delay", 3.0),
            "buffer_debounce_seconds": cfg.get("wechat", {}).get("buffer_debounce_seconds", 5.0),
            "buffer_max_wait_seconds": cfg.get("wechat", {}).get("buffer_max_wait_seconds", 12.0),
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
        "SystemFast": "快速系统",
        "ChatLite": "轻量聊天",
        "ChatFull": "完整聊天",
    }.get(route or "", route or "未知")


def _policy_label(policy: str) -> str:
    return {
        "hot_only": "短上下文",
        "hot+cheap_memory": "短上下文 + 轻量记忆",
        "hot+full_memory": "短上下文 + 完整记忆",
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
        "scheduler": "主动消息",
        "proactive": "主动消息",
        "admin": "后台手动",
        "scheduled_reminder": "定时提醒",
        "fixed_morning": "早安消息",
    }.get(source or "", source or "未知")


async def build_admin_event_dashboard(limit: int = 20) -> dict[str, Any]:
    """Build a compact Chinese admin dashboard for the browser UI."""
    limit = max(5, min(int(limit or 20), 80))
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")
    bot_id = bot_manager.current_bot_id

    turns = await memory_store.get_recent_turns(user_id, bot_id, limit=limit) if user_id else []
    diagnostics = await memory_store.get_recent_turn_diagnostics(limit=limit, user_id=user_id) if user_id else []
    diag_by_turn = {d.get("turn_id"): d for d in diagnostics}
    candidates = await memory_store.get_recent_proactive_candidates(limit=limit)
    proactive_state = await admin_proactive_state_data(user_id, bot_id)
    topic = await memory_store.get_active_topic(user_id, bot_id) if user_id else None
    recaps = await memory_store.get_recaps(user_id, bot_id, limit=5) if user_id else []
    shared_facts = await memory_store.get_shared_facts(limit=8)
    outbound = await memory_store.get_outbound_messages(user_id=user_id, limit=limit) if user_id else []
    outbound_by_turn: dict[str, list[dict[str, Any]]] = {}
    for item in outbound:
        outbound_by_turn.setdefault(str(item.get("turn_id", "")), []).append(item)

    events: list[dict[str, Any]] = []
    for turn in turns:
        turn_id = turn.get("id", "")
        diag = diag_by_turn.get(turn_id, {})
        diag_meta = diag.get("metadata") if isinstance(diag.get("metadata"), dict) else {}
        turn_source = turn.get("source", "")
        turn_trigger = turn.get("trigger", "")
        turn_outbound = outbound_by_turn.get(turn_id, [])
        actions = _safe_json_list(turn.get("assistant_actions"))
        tools = _safe_json_list(turn.get("tool_calls"))
        replies = [
            str(a.get("text", "")).strip()
            for a in actions
            if isinstance(a, dict) and str(a.get("text", "")).strip()
        ]
        # Resolve intent label for proactive turns
        turn_intent = ""
        if turn_source == "proactive":
            turn_intent = diag_meta.get("intent", "")
        events.append({
            "id": turn_id,
            "kind": "turn",
            "time": _admin_time(turn.get("created_at")),
            "created_at": turn.get("created_at", 0),
            "title": "主动消息已送达" if turn_source == "proactive" else "收到用户消息",
            "user_text": turn.get("user_messages", ""),
            "intent": turn_intent,
            "is_proactive": turn_source == "proactive",
            "reply_texts": replies,
            "tool_calls": tools,
            "source": _source_label(turn_source),
            "raw_source": turn_source,
            "trigger": turn_trigger,
            "batch_id": turn_id,
            "candidate_id": diag_meta.get("candidate_id", ""),
            "skip_reply": bool(diag_meta.get("skip_reply")),
            "skip_reason": diag_meta.get("skip_reason", ""),
            "outbound_messages": turn_outbound,
            "route": _route_label(diag.get("route", "")),
            "context_policy": _policy_label(diag.get("context_policy", "")),
            "elapsed_ms": diag.get("elapsed_ms", 0),
            "token_count": diag.get("token_count", 0),
            "send_status": _send_status_label(diag.get("send_status") or turn.get("send_status", "")),
            "memory_enqueued": bool(diag.get("memory_enqueued", 0)),
        })

    for candidate in candidates[:8]:
        sent = bool(candidate.get("sent"))
        cand_send_status = candidate.get("send_status", "")
        if cand_send_status == "delivered":
            cand_title = "主动消息已送达"
        elif cand_send_status == "failed":
            cand_title = "主动消息发送失败"
        elif cand_send_status == "sending":
            cand_title = "主动消息发送中"
        else:
            cand_title = "主动消息" + ("已发送" if sent else "未发送")
        events.append({
            "id": candidate.get("id", ""),
            "kind": "proactive",
            "time": _admin_time(candidate.get("created_at")),
            "created_at": candidate.get("created_at", 0),
            "title": cand_title,
            "reason_type": candidate.get("reason_type", ""),
            "reason_detail": candidate.get("reason_detail", ""),
            "gate_detail": candidate.get("gate_detail", ""),
            "generated_message": candidate.get("generated_message", ""),
            "sent": sent,
            "source": candidate.get("source", "proactive"),
            "trigger": candidate.get("trigger", "proactive_reason"),
            "send_status": candidate.get("send_status", "delivered" if sent else ""),
            "candidate_id": candidate.get("id", ""),
            "batch_id": candidate.get("batch_id", "") or candidate.get("turn_id", ""),
        })

    events.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)

    return {
        "contact": contact,
        "bot": {
            "current_bot_id": bot_id,
            "display_name": bot_manager.current_bot.display_name,
        },
        "events": events[:limit],
        "turns": turns[-limit:],
        "outbound_messages": outbound,
        "proactive": {
            "state": proactive_state,
            "active_topic": dict(topic) if topic else None,
            "candidates": candidates,
        },
        "memory": {
            "recaps": recaps,
            "shared_facts": shared_facts,
        },
    }


async def admin_proactive_state_data(user_id: str, bot_id: str) -> dict[str, Any]:
    sched_state = await memory_store.get_proactive_schedule_state(user_id, bot_id)
    scheduler_state = await memory_store.get_scheduler_state()
    clingy_state = await memory_store.get_clingy_state(user_id, bot_id) if user_id else {}
    now = time.time()
    next_due = sched_state.get("next_due_ts", 0)
    last_sent = sched_state.get("last_sent_ts", 0)
    last_attempt = sched_state.get("last_attempt_ts", 0)
    return {
        "proactive_schedule": {
            "next_due_ts": next_due,
            "next_due_in_seconds": max(0, int(next_due - now)) if next_due else None,
            "next_due_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(next_due)) if next_due else None,
            "last_attempt_ts": last_attempt,
            "last_sent_ts": last_sent,
            "is_due": next_due > 0 and now >= next_due,
        },
        "scheduler_state": scheduler_state,
        "timing_config": {
            "quiet_hours_enabled": scheduler_agent.quiet_hours_enabled,
            "sleep_start": scheduler_agent.sleep_start,
            "sleep_end": scheduler_agent.sleep_end,
            "min_interval_minutes": scheduler_agent.min_interval,
            "max_interval_minutes": scheduler_agent.max_interval,
        },
        "clingy_state": {
            "awaiting_user_reply": bool(clingy_state.get("awaiting_user_reply")),
            "last_proactive_sent_at": clingy_state.get("last_proactive_sent_at", 0),
            "last_proactive_intent": clingy_state.get("last_proactive_intent", ""),
            "next_clingy_due_at": clingy_state.get("next_clingy_due_at", 0),
            "next_clingy_due_in_seconds": max(0, int(clingy_state.get("next_clingy_due_at", 0) - now)) if clingy_state.get("next_clingy_due_at") else None,
            "clingy_followup_count": clingy_state.get("clingy_followup_count", 0),
            "last_clingy_sent_at": clingy_state.get("last_clingy_sent_at", 0),
            "max_followups_per_unanswered": scheduler_agent.max_followups_per_unanswered,
            "clingy_after_minutes": scheduler_agent.clingy_after_minutes,
            "clingy_mode_enabled": scheduler_agent.clingy_mode_enabled,
        },
    }


def apply_runtime_settings(updates: dict):
    """Persist and apply runtime-editable settings."""
    _deep_update(runtime_settings, updates)
    save_runtime_settings(runtime_settings)
    _deep_update(cfg, updates)
    configure_tools(cfg)
    configure_runtime(memory_store=memory_store)

    scfg = cfg.get("scheduler", {})
    scheduler_agent.quiet_hours_enabled = scfg.get("quiet_hours_enabled", True)
    scheduler_agent.sleep_start = scfg.get("sleep_start", 0)
    scheduler_agent.sleep_end = scfg.get("sleep_end", 7)
    scheduler_agent.max_ignored_streak = scfg.get("max_ignored_streak", 3)
    scheduler_agent.max_daily = scfg.get("max_daily_proactive", 5)
    # V2.1.1: proactive timing
    pro_cfg = cfg.get("proactive", {})
    timing = pro_cfg.get("timing", {})
    if timing:
        scheduler_agent.min_interval = timing.get("min_interval_minutes", 30)
        scheduler_agent.max_interval = timing.get("max_interval_minutes", 60)
    gen_cfg = pro_cfg.get("generation", {})
    if gen_cfg:
        scheduler_agent.enabled_types = gen_cfg.get("enabled_types",
            ["check_in", "latest_topic_followup", "self_share", "light_ping"])
        if gen_cfg.get("clingy_mode_enabled", False) and "clingy_followup" not in scheduler_agent.enabled_types:
            scheduler_agent.enabled_types = ["clingy_followup", *scheduler_agent.enabled_types]
        scheduler_agent.clingy_mode_enabled = bool(gen_cfg.get("clingy_mode_enabled", False))
        scheduler_agent.clingy_after_minutes = int(gen_cfg.get("clingy_after_minutes", 20) or 20)
        scheduler_agent.max_proactive_messages = max(1, min(int(gen_cfg.get("max_messages", 1) or 1), 3))
        scheduler_agent.latest_topic_only = gen_cfg.get("latest_topic_only", True)
        scheduler_agent.allow_llm_skip = gen_cfg.get("allow_llm_skip", False)
    topic_cfg = pro_cfg.get("topic", {})
    if topic_cfg:
        scheduler_agent.followup_max = topic_cfg.get("followup_max_count", 2)
        scheduler_agent.topic_expire_minutes = topic_cfg.get("default_expire_minutes", 240)
    clingy_cfg = pro_cfg.get("clingy", {})
    if clingy_cfg:
        scheduler_agent.max_followups_per_unanswered = int(clingy_cfg.get("max_followups_per_unanswered", 3) or 3)
    chat_agent.defaults = {
        "default_city": cfg.get("weather", {}).get(
            "default_city",
            cfg.get("fixed_messages", {}).get("morning", {}).get("city", ""),
        )
    }

    if _bridge is not None:
        wcfg = cfg.get("wechat", {})
        _bridge.message_delay = float(wcfg.get("message_delay", 1.2))
        _bridge.max_reply_delay = float(wcfg.get("max_reply_delay", 3.0))
        _bridge.buffer_debounce_seconds = float(wcfg.get("buffer_debounce_seconds", 5.0))
        _bridge.buffer_max_wait_seconds = float(wcfg.get("buffer_max_wait_seconds", 12.0))

    if scheduler.running:
        check_job = scheduler.get_job("deliver_scheduled_messages")
        if check_job:
            check_job.reschedule(
                trigger="interval",
                seconds=scfg.get("scheduled_check_seconds", 10),
            )

        proactive_job = scheduler.get_job("scheduler_tick")
        if proactive_job:
            if scfg.get("enabled", True):
                proactive_job.resume()
                proactive_job.reschedule(
                    trigger="interval",
                    minutes=scfg.get("interval_minutes", 30),
                )
            else:
                proactive_job.pause()
        elif scfg.get("enabled", True):
            scheduler.add_job(
                scheduler_tick,
                "interval",
                minutes=scfg.get("interval_minutes", 30),
                id="scheduler_tick",
                replace_existing=True,
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


def parse_memory_upload(filename: str, data: bytes) -> list[dict]:
    """Parse zip/json/md/txt memory exports into normalized entries."""
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".zip" or zipfile.is_zipfile(io.BytesIO(data)):
        return parse_memory_zip(data)

    text = decode_text(data)
    if suffix == ".json":
        return parse_memory_json(text)
    return parse_memory_text(filename or "memory.txt", text)


def parse_memory_zip(data: bytes) -> list[dict]:
    entries: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            lower = name.lower()
            if not lower.endswith((".md", ".txt", ".json")):
                continue
            if lower.endswith("/memory.md") or lower == "memory/memory.md":
                # MEMORY.md is usually an index pointing to the real memories.
                continue
            raw = zf.read(info)
            text = decode_text(raw)
            if lower.endswith(".json"):
                entries.extend(parse_memory_json(text, source=name))
            else:
                entries.extend(parse_memory_text(name, text))
    return entries


def decode_text(data: bytes) -> str:
    for enc in ["utf-8-sig", "utf-8", "gb18030"]:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_memory_json(text: str, source: str = "memory.json") -> list[dict]:
    payload = json.loads(text)
    if isinstance(payload, dict):
        items = payload.get("memories", payload.get("items", []))
    else:
        items = payload
    if not isinstance(items, list):
        return []

    entries = []
    for item in items:
        if isinstance(item, str):
            entries.extend(parse_memory_text(source, item))
        elif isinstance(item, dict):
            content = str(item.get("content", item.get("text", ""))).strip()
            if content:
                entries.append({
                    "content": content,
                    "category": item.get("category", "imported"),
                    "importance": item.get("importance", 0.72),
                    "keywords": item.get("keywords", ""),
                    "source_turn_id": item.get("source_turn_id", source),
                })
    return entries


def parse_memory_text(source: str, text: str) -> list[dict]:
    meta, body = split_front_matter(text)
    body = body.strip()
    if not body:
        return []

    title = str(meta.get("name") or Path(source).stem)
    desc = str(meta.get("description") or "")
    category = str(meta.get("type") or infer_memory_category(source))
    session_id = str(meta.get("originSessionId") or meta.get("origin_session_id") or source)

    header = f"{title}\n{desc}".strip()
    base = f"{header}\n\n{body}" if header else body
    chunks = chunk_memory_text(base)
    return [
        {
            "content": chunk,
            "category": category,
            "importance": 0.78 if category in {"feedback", "user"} else 0.70,
            "keywords": title,
            "source_turn_id": session_id,
        }
        for chunk in chunks
    ]


def split_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not match:
        return {}, text
    raw_meta, body = match.groups()
    try:
        meta = yaml.safe_load(raw_meta) or {}
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    return meta, body


def infer_memory_category(source: str) -> str:
    lower = source.lower()
    if "feedback" in lower or "style" in lower or "persona" in lower:
        return "preference"
    if "summary" in lower or "chat" in lower:
        return "summary"
    return "imported"


def chunk_memory_text(text: str, max_chars: int = 1800) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    parts = re.split(r"\n(?=##? |\d+\. |- )", text)
    chunks: list[str] = []
    current = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if current and len(current) + len(part) + 2 > max_chars:
            chunks.append(current)
            current = part
        else:
            current = f"{current}\n\n{part}".strip() if current else part
    if current:
        chunks.append(current)

    if not chunks:
        chunks = [text[i:i + max_chars] for i in range(0, len(text), max_chars)]
    return chunks


def build_memory_export_zip(memories: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        payload = {
            "exported_at": time.time(),
            "format": "goal-memory-v1",
            "memories": memories,
        }
        zf.writestr(
            "memory/memories.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        lines = ["# Goal Memory Export", ""]
        for m in memories:
            lines.append(f"## {m.get('id', '')}")
            lines.append(f"- category: {m.get('category', '')}")
            lines.append(f"- importance: {m.get('importance', '')}")
            lines.append("")
            lines.append(str(m.get("content", "")))
            lines.append("")
        zf.writestr("memory/memories.md", "\n".join(lines))
    return buf.getvalue()


# ================================================================
# FastAPI app
# ================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown."""
    logger.info(f"Starting Goal Chat Agent (model={model_name})")

    # ── Startup validation ───────────────────────────────────────
    _startup_validate()

    # Start background jobs. One-shot reminders are always checked because they
    # are user-requested tasks; proactive messaging is separately configurable.
    scfg = cfg.get("scheduler", {})
    scheduler.add_job(
        deliver_scheduled_messages,
        "interval",
        seconds=scfg.get("scheduled_check_seconds", 10),
        id="deliver_scheduled_messages",
        replace_existing=True,
        max_instances=1,
    )

    if scfg.get("enabled", True):
        interval = scfg.get("interval_minutes", 30)
        scheduler.add_job(
            scheduler_tick,
            "interval",
            minutes=interval,
            id="scheduler_tick",
            replace_existing=True,
        )
        # Daily counter reset at midnight CST
        scheduler.add_job(
            daily_reset,
            "cron",
            hour=0,
            minute=1,
            timezone="Asia/Shanghai",
            id="daily_reset",
            replace_existing=True,
        )
        # Memory decay at 3am daily
        scheduler.add_job(
            memory_decay,
            "cron",
            hour=3,
            minute=0,
            timezone="Asia/Shanghai",
            id="memory_decay",
            replace_existing=True,
        )

        fixed_cfg = cfg.get("fixed_messages", {})
        morning = fixed_cfg.get("morning", {})
        if morning.get("enabled", True):
            scheduler.add_job(
                fixed_morning_message,
                "cron",
                hour=int(morning.get("hour", 8)),
                minute=int(morning.get("minute", 0)),
                timezone="Asia/Shanghai",
                id="fixed_morning_message",
                replace_existing=True,
            )
        night = fixed_cfg.get("night_recap", {})
        if night.get("enabled", True):
            scheduler.add_job(
                fixed_night_recap,
                "cron",
                hour=int(night.get("hour", 23)),
                minute=int(night.get("minute", 0)),
                timezone="Asia/Shanghai",
                id="fixed_night_recap",
                replace_existing=True,
            )

    scheduler.start()
    logger.info("Background scheduler started")

    # Start WeChat bridge if configured
    if _bridge is not None and _bridge.token:
        await _bridge.start()

    yield

    # Shutdown
    logger.info("Shutting down...")
    if _bridge is not None:
        await _bridge.stop()
    if scheduler.running:
        scheduler.shutdown(wait=False)
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


@app.get("/", response_class=HTMLResponse)
async def landing_page():
    """Public landing page."""
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="landing page not found")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """Mobile-first local admin UI."""
    html_path = STATIC_DIR / "admin.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="admin UI not found")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


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
    usage = await build_usage_dashboard()
    outbound_available = _bridge is not None and bool(_bridge.token)
    token = str(cfg.get("wechat", {}).get("token", ""))
    current_bot_id = bot_manager.current_bot_id
    return {
        "persona": read_persona_file(),
        "settings": public_settings(),
        "usage": usage,
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


async def build_usage_dashboard() -> dict:
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)
    today_start = datetime.combine(
        now.date(),
        datetime_time.min,
        tzinfo=tz,
    ).timestamp()
    summary = await memory_store.get_usage_summary(today_start)
    summary["runtime"] = {
        "started_at": PROCESS_STARTED_AT,
        "uptime_seconds": max(0, int(time.time() - PROCESS_STARTED_AT)),
        "bridge_running": _bridge is not None and _bridge.running,
    }
    return summary


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
    bot_manager.invalidate_prompt_pack()  # V2.1.1: force re-read .md on next turn
    return {"status": "saved", "path": str(path)}


@app.post("/admin/settings")
async def admin_save_settings(request: Request):
    require_admin(request)
    body = await request.json()
    allowed = {}
    for section in ["scheduler", "proactive", "wechat", "weather", "email"]:
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
    block_reason = _contact_send_block_reason(contact, bot_manager.current_bot_id)
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
    await memory_store.record_usage_event(
        event_type="manual_send",
        user_id=contact["user_id"],
        source="admin",
        message_count=len(messages),
    )
    return {"status": "sent", "count": len(messages)}


@app.post("/chat")
async def chat(request: Request):
    """Main chat endpoint — V2 with routing, context policy, turn lock, drafts.

    Compatible with emoRebot bridge:
      POST /chat {"messages": ["你好", "今天天气怎么样"], "user_id": "wxid_xxx"}

    Returns:
      {"messages": [{"text": "...", "delay": 1.0}, ...], "done": true}
    """
    api_cfg = _api_config()

    # ── readiness / auth gate logging ──────────────────────────
    chat_api_key_set = bool(os.getenv(api_cfg["chat_api_key_env"], ""))
    has_auth_header = bool(
        request.headers.get("authorization", "")
        or request.headers.get("x-api-key", "")
    )
    remote_addr = request.client.host if request.client else "unknown"
    current_bot_id = bot_manager.current_bot_id
    active_bot = bot_manager.get_bot(current_bot_id)

    logger.info(
        f"/chat entry: remote={remote_addr} "
        f"chat_auth_enabled={api_cfg['chat_auth_enabled']} "
        f"has_auth_header={has_auth_header} "
        f"chat_api_key_set={chat_api_key_set} "
        f"current_bot_id={current_bot_id} "
        f"active_bot_exists={active_bot is not None} "
        f"memory_store_ok={memory_store is not None} "
        f"llm_gateway_ok={llm_gateway is not None}"
    )

    # Fail early with clear diagnostics if service is not ready
    if memory_store is None:
        logger.error("/chat rejected: memory_store_not_ready")
        raise HTTPException(status_code=503, detail="memory_store_not_ready")
    if llm_gateway is None:
        logger.error("/chat rejected: llm_gateway_not_ready")
        raise HTTPException(status_code=503, detail="llm_gateway_not_ready")
    if active_bot is None:
        logger.error(f"/chat rejected: bot_not_ready (current_bot_id={current_bot_id})")
        raise HTTPException(status_code=503, detail="bot_not_ready")

    _require_chat_auth(request, api_cfg)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    # Parse input (compatible with emoRebot format: messages array or single message)
    raw_messages = _normalize_chat_messages(body, api_cfg)
    user_message = "\n".join(raw_messages)
    user_id = _validate_chat_user_id(str(body.get("user_id", "default")), api_cfg)
    context_token = str(body.get("context_token", ""))
    inbound_trigger = str(body.get("trigger") or "user")
    inbound_source = str(body.get("source") or "wechat")

    logger.info(f"[{user_id}] Received {len(str(user_message))} chars")

    # V2: Acquire per-user turn lock (Phase 4)
    lock = _get_user_lock(user_id)
    turn_id = ""
    send_status = ""
    memory_enqueued = 0

    try:
        async with lock:
            # V2.1: Resolve current bot once for the entire turn
            current_bot_id = bot_manager.current_bot_id
    
            confirmation_result = await handle_pending_persona_confirmation(user_message)
            if confirmation_result:
                await append_history_async(user_id, "user", user_message, source="wechat", bot_id=current_bot_id)
                await append_history_async(
                    user_id,
                    "assistant",
                    _assistant_history_json(confirmation_result),
                    source="assistant",
                    bot_id=current_bot_id,
                )
                await memory_store.record_usage_event(
                    event_type="chat_turn",
                    user_id=user_id,
                    source="wechat",
                    message_count=len(raw_messages) + len(confirmation_result.get("messages", [])),
                    elapsed_ms=int(confirmation_result.get("elapsed_ms", 0) or 0),
                    metadata={"handled_by": "pending_persona_confirmation"},
                )
                return confirmation_result
    
            # V2: Select route and context policy (Phase 5 + 6)
            route = _select_route(user_message, trigger="user")
    
            # Retrieve memories scoped to current bot (or shared)
            memories = await memory_store.search_bot(user_message, bot_id=current_bot_id, top_k=5)
    
            context_policy = _select_context_policy(route, len(memories))
    
            # Get relationship state scoped to current bot
            relationship = await memory_store.get_relationship_bot(current_bot_id)
    
            # Get conversation history scoped to current bot
            history = await get_history_async(user_id, bot_id=current_bot_id)
            history_summary = await memory_store.get_history_summary(user_id, bot_id=current_bot_id)
    
            # V2.1: Get recap for current bot
            latest_recap = await memory_store.get_latest_recap(
                user_id, current_bot_id
            )
            recap_text = latest_recap["recap_text"] if latest_recap else ""

            # V2.2: Load emotion + dialogue state, build inject
            emotion_cfg = cfg.get("emotion", {})
            emotion_enabled = bool(emotion_cfg.get("enabled", True))
            emotion_inject = ""
            must_reply = False
            if emotion_enabled:
                emo_defaults = {
                    "bot_mood": emotion_engine.baseline_mood,
                    "affection": emotion_engine.initial_affection,
                    "trust": emotion_engine.initial_trust,
                    "attachment": emotion_engine.initial_attachment,
                }
                emo_raw = await memory_store.get_bot_emotion_state(user_id, current_bot_id, defaults=emo_defaults)
                ds_raw = await memory_store.get_dialogue_state(user_id, current_bot_id)

                emo_state = BotEmotionState.from_dict(emo_raw)
                ds_state = DialogueState.from_dict(ds_raw)

                allow_silence = emo_state.bot_mood in (
                    "irritated", "angry", "jealous", "hurt"
                )
                must_reply = _check_must_reply(ds_state, emo_state, emotion_cfg)

                from agent.emotion.prompt_context import (
                    build_emotion_inject, build_silence_rules_inject,
                )
                emotion_inject = build_emotion_inject(
                    emo_raw, ds_raw,
                    allow_silence=allow_silence, must_reply=must_reply,
                )
                if emotion_cfg.get("inject_prompt"):
                    silence_rules = build_silence_rules_inject(
                        emo_raw, ds_raw,
                        allow_silence=allow_silence, must_reply=must_reply,
                        max_silent_count=_max_silent_for_mood(
                            emo_state.bot_mood, emotion_cfg
                        ),
                        silent_count=ds_state.user_messages_since_silent,
                        silence_config=emotion_cfg.get("silence", {}),
                    )
                    if silence_rules:
                        emotion_inject = emotion_inject + "\n" + silence_rules
            else:
                emo_raw = None
                ds_raw = None
                emo_state = None
                ds_state = None

            # Run Chat Agent (V2.2: with emotion_inject and must_reply)
            result = await chat_agent.chat(
                user_message=user_message,
                memories=memories,
                history=history,
                relationship=relationship,
                history_summary=history_summary,
                trigger="user",
                route=route,
                context_policy=context_policy,
                bot_id=current_bot_id,
                recap=recap_text,
                emotion_inject=emotion_inject,
                must_reply=must_reply,
            )

            turn_id = result["turn_id"]
            skipped_reply = bool(result.get("skip_reply")) and not result.get("messages") and not result.get("scheduled_messages")
            initial_send_status = "skipped" if skipped_reply else "generated"
    
            # V2.1: Record turn (L2) and outbound messages
            asyncio.create_task(
                memory_store.create_turn(
                    turn_id=turn_id,
                    user_id=user_id,
                    bot_id=current_bot_id,
                    user_messages=user_message,
                    assistant_actions=json.dumps(
                        result.get("messages", []),
                        ensure_ascii=False,
                    ),
                    tool_calls=json.dumps(
                        result.get("tool_calls", []),
                        ensure_ascii=False,
                    ),
                    source=result.get("source", "user_reply"),
                    trigger=result.get("trigger", "user_message"),
                    send_status=initial_send_status,
                )
            )
            # V2.1: Record outbound messages with full semantics.
            # Must await so that mark_outbound_batch_status below doesn't race.
            if result.get("messages"):
                await memory_store.record_outbound_messages(
                    turn_id=turn_id,
                    messages=result["messages"],
                    user_id=user_id,
                    bot_id=current_bot_id,
                    source=result.get("source", "user_reply"),
                    author=result.get("author", "assistant"),
                    trigger=result.get("trigger", "user_message"),
                )
    
            # V2.1: Trigger recap check in background
            recap_cfg = cfg.get("recap", {})
            if recap_cfg.get("enabled", True):
                unsummarized = await memory_store.count_unsummarized_turns(
                    user_id, current_bot_id
                )
                every_n = int(recap_cfg.get("trigger", {}).get("every_n_turns", 8))
                if unsummarized >= every_n:
                    asyncio.create_task(
                        _generate_recap(user_id, current_bot_id, recap_cfg)
                    )
    
            # Remember the latest reachable contact
            if user_id and user_id != "default" and context_token:
                await memory_store.update_contact_state(
                    user_id,
                    context_token,
                    bot_id=current_bot_id,
                )
    
            # V2: Persist turn draft BEFORE sending (Phase 3)
            # TODO: turn_drafts currently track send status only, not full
            # outbound idempotency. /chat does not check existing draft status
            # before sending — a crash-and-retry or bridge timeout replay could
            # still produce duplicates across process boundaries.
            await memory_store.create_turn_draft(
                turn_id=turn_id,
                user_id=user_id,
                messages=result["messages"],
                source="wechat",
                route=route,
            )
    
            scheduled_task_ids = []
            contact = await memory_store.get_contact_state()
            for task in result.get("scheduled_messages", []):
                due_at = time.time() + float(task["delay_minutes"]) * 60
                target_user_id = user_id
                target_context_token = context_token
                if not target_context_token and contact.get("user_id") and contact.get("context_token"):
                    target_user_id = contact["user_id"]
                    target_context_token = contact["context_token"]
                task_id = await memory_store.create_scheduled_message(
                    user_id=target_user_id,
                    context_token=target_context_token,
                    messages=task["messages"],
                    due_at=due_at,
                )
                scheduled_task_ids.append(task_id)
    
            # Save assistant history
            await append_history_async(user_id, "user", user_message, source="wechat", bot_id=current_bot_id)
            assistant_text = "\n".join(
                m["text"] for m in result["messages"] if m.get("text")
            )
            if result["messages"] or result.get("scheduled_messages"):
                await append_history_async(
                    user_id,
                    "assistant",
                    _assistant_history_json(result),
                    source="assistant",
                    bot_id=current_bot_id,
                )
    
            # Fire-and-forget memory extraction
            if len(user_message.strip()) >= 10 and assistant_text.strip():
                asyncio.create_task(
                    memory_worker.extract(user_message, assistant_text, memories, bot_id=current_bot_id)
                )
                memory_enqueued = 1
    
            # V2.1.1: Topic state analysis (fire-and-forget)
            if len(user_message.strip()) >= 4:
                asyncio.create_task(
                    _analyze_and_update_topic(
                        user_id, current_bot_id, user_message,
                        assistant_text, trigger="user",
                    )
                )
    
            # Update scheduler state
            await scheduler_agent.record_chat_activity()

            # V2.1.3: Only clear clingy awaiting on real user inbound.
            if _is_real_user_inbound(user_id, user_message, inbound_trigger, inbound_source):
                await memory_store.clear_clingy_awaiting(user_id, current_bot_id)

            # V2.3: Dialogue state MUST be committed synchronously before lock release
            if emotion_enabled and ds_raw is not None:
                ds_raw = await _commit_dialogue_state_after_turn(
                    user_id, current_bot_id,
                    user_message, result,
                    ds_raw, emotion_cfg,
                )

            # Emotion state update is fire-and-forget (non-critical for consistency)
            if emotion_enabled and emo_raw is not None:
                asyncio.create_task(
                    _update_emotion_state_only(
                        user_id, current_bot_id,
                        user_message, result,
                        emo_raw, emotion_cfg,
                    )
                )

            # V2: Record turn diagnostics (Phase 2)
            # Resolve fallback type: surface user_silent_request when the model
            # chose to skip because the user asked for silence.
            diag_fallback_type = result_fallback_type(result)
            if skipped_reply and "user_silent_request" not in diag_fallback_type:
                diag_fallback_type = ",".join(
                    filter(None, ["user_silent_request", diag_fallback_type])
                )
            await memory_store.record_turn_diagnostics(
                turn_id=turn_id,
                user_id=user_id,
                source="wechat",
                route=route,
                context_policy=context_policy,
                llm_status=result.get("llm_status", "ok"),
                token_count=result_token_count(result),
                elapsed_ms=int(result.get("elapsed_ms", 0) or 0),
                fallback_type=diag_fallback_type,
                send_status=initial_send_status,
                memory_enqueued=memory_enqueued,
                input_chars=len(user_message),
                output_messages=len(result.get("messages", [])),
                tool_calls=len(result.get("tool_calls", [])),
                metadata={
                    "skip_reply": skipped_reply,
                    "skip_reason": result.get("skip_reason", "") if skipped_reply else "",
                },
            )
    
            # Also record usage event (keep backward compat)
            await memory_store.record_usage_event(
                event_type="chat_turn",
                user_id=user_id,
                source="wechat",
                message_count=len(raw_messages) + len(result.get("messages", [])),
                token_count=result_token_count(result),
                elapsed_ms=int(result.get("elapsed_ms", 0) or 0),
                fallback_type=result_fallback_type(result),
                metadata={
                    "detail": result_fallback_detail(result),
                    "input_chars": len(user_message),
                    "output_messages": len(result.get("messages", [])),
                    "tool_calls": len(result.get("tool_calls", [])),
                    "token_estimated": bool((result.get("token_usage") or {}).get("estimated")),
                    "turn_id": turn_id,
                    "route": route,
                    "context_policy": context_policy,
                },
            )
    
            logger.info(
                f"[{user_id}] Reply: {len(result['messages'])} messages, "
                f"{len(result.get('tool_calls', []))} tool calls, "
                f"{result['iterations']} iterations, "
                f"{result['elapsed_ms']}ms, "
                f"route={route}, policy={context_policy}"
            )
    
            # V2: Send via built-in bridge inside the lock (Phase 4)
            # Sending under the lock prevents scheduler/proactive sends from
            # interleaving before the user reply finishes.
            bridge_sent = False
            send_error = ""
            if skipped_reply:
                send_status = "skipped"
                await memory_store.mark_turn_draft_status(turn_id, "skipped")
            elif _bridge is not None and _bridge.token and context_token:
                await memory_store.mark_turn_draft_status(turn_id, "sending")
                await memory_store.mark_outbound_batch_status(turn_id, "sending")
                try:
                    await _send_message_batch(
                        user_id=user_id,
                        context_token=context_token,
                        messages=result["messages"],
                    )
                    send_status = "delivered"
                    bridge_sent = True
                    await memory_store.mark_turn_draft_status(turn_id, "delivered")
                    await memory_store.mark_outbound_batch_status(turn_id, "delivered")
                except Exception as e:
                    logger.error(f"[{user_id}] Send failed for turn {turn_id}: {e}")
                    send_status = "failed"
                    send_error = str(e)[:500]
                    await memory_store.mark_turn_draft_status(turn_id, "failed", send_error)
                    await memory_store.mark_outbound_batch_status(turn_id, "failed", send_error)
                    # On failure, return empty messages — the poller must not
                    # re-send a partially-delivered batch, which would duplicate
                    # messages that already went out before the failure.
                    bridge_sent = True
            else:
                # No bridge — messages will be picked up by poller's reply path
                send_status = "delivered"
                await memory_store.mark_turn_draft_status(turn_id, "delivered")
                await memory_store.mark_outbound_batch_status(turn_id, "delivered")
    
            # V2: Update turn diagnostics send status (preserves prior fields, merges error)
            await memory_store.update_turn_send_status(turn_id, send_status, error=send_error)

            # V2.1.2: Bot sent a reply that wasn't skipped — begin awaiting user reply.
            # Any bot message (proactive or normal reply) enters clingy waiting state.
            if send_status == "delivered" and result.get("messages") and user_id and user_id != "default":
                await memory_store.update_clingy_state(
                    user_id=user_id, bot_id=current_bot_id,
                    awaiting_user_reply=1,
                    last_proactive_sent_at=time.time(),
                    last_proactive_intent="",  # normal chat reply, not a proactive intent
                    next_clingy_due_at=time.time() + scheduler_agent.clingy_after_minutes * 60,
                    clingy_followup_count=0,
                )

        # Lock released — return response.
        # When the built-in bridge already sent (or failed trying), return empty
        # messages so the bridge poller's reply loop doesn't double-send.
        return {
            "messages": [] if bridge_sent else [
                {"text": m["text"], "delay": m.get("delay", 0)}
                for m in result["messages"]
            ],
            "tool_calls": result.get("tool_calls", []),
            "scheduled_task_ids": scheduled_task_ids,
            "iterations": result["iterations"],
            "elapsed_ms": result["elapsed_ms"],
            "turn_id": turn_id,
            "route": route,
        }

    except Exception as exc:
        logger.exception(f"[{user_id}] Chat handler internal error: {exc}")
        # Mark turn as failed so admin UI doesn't show "已生成"
        if turn_id:
            try:
                await memory_store.update_turn_send_status(turn_id, "failed", error=str(exc)[:500])
            except Exception:
                pass
        error_turn_id = turn_id or f"error_{uuid.uuid4().hex[:8]}"
        return {
            "messages": [],
            "tool_calls": [],
            "scheduled_task_ids": [],
            "iterations": 0,
            "elapsed_ms": 0,
            "turn_id": error_turn_id,
            "route": "error",
            "error": "internal_error",
        }


@app.post("/scheduler/trigger")
async def scheduler_trigger(request: Request):
    """Manual trigger for scheduler check. Called by internal timer or admin."""
    require_admin(request)
    result = await scheduler_tick()
    return {"status": "ok", "result": result}


@app.get("/scheduler/state")
async def scheduler_state(request: Request):
    """Get current scheduler state."""
    require_admin(request)
    state = await memory_store.get_scheduler_state()
    return state


@app.get("/scheduler/messages")
async def scheduled_messages(request: Request, status: str = "", limit: int = 50):
    """Inspect scheduled one-shot message tasks."""
    require_admin(request)
    tasks = await memory_store.list_scheduled_messages(
        status=status or None,
        limit=limit,
    )
    return {"tasks": tasks}


@app.get("/memories")
async def list_memories(request: Request, category: str = "", limit: int = 20):
    """List memories, optionally filtered by category."""
    require_admin(request)
    memories = await memory_store.list_all()
    if category:
        memories = [m for m in memories if m.get("category") == category]
    return {"memories": memories[:limit]}


@app.get("/memories/export")
async def export_memories(request: Request):
    """Export current memories as a portable zip."""
    require_admin(request)
    memories = await memory_store.list_all()
    data = build_memory_export_zip(memories)
    filename = f"goal-memory-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/memories/import")
async def import_memories(request: Request, clear_existing: bool = False):
    """Import memories from zip/json/md/txt uploaded by the admin UI."""
    require_admin(request)
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    filename = request.headers.get("X-Filename", "memory.zip")
    try:
        entries = parse_memory_upload(filename, data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"解析失败：{e}")
    if not entries:
        raise HTTPException(status_code=400, detail="没有解析到可导入的记忆")
    result = await memory_store.import_memories(
        entries,
        clear_existing=clear_existing,
    )
    return {"status": "ok", **result, "parsed": len(entries)}


@app.delete("/memories/{mem_id}")
async def delete_memory(mem_id: str, request: Request):
    """Delete a single memory entry."""
    require_admin(request)
    deleted = await memory_store.delete_memory(mem_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "deleted", "id": mem_id}


@app.post("/admin/dev/forget")
async def developer_forget_all(request: Request):
    """Developer reset: forget model-visible memory/history state."""
    require_admin(request)
    conversation_histories.clear()
    counts = await memory_store.clear_developer_memory_state()
    return {"status": "forgotten", "cleared": counts}


@app.get("/relationship")
async def get_relationship(request: Request):
    """Get current relationship state."""
    require_admin(request)
    return await memory_store.get_relationship()


@app.post("/relationship")
async def update_relationship(request: Request):
    """Update relationship state fields."""
    require_admin(request)
    body = await request.json()
    await memory_store.update_relationship(body)
    return {"status": "updated"}


@app.get("/history/{user_id}")
async def get_history_route(user_id: str, request: Request):
    """Get conversation history for a user and current bot."""
    require_admin(request)
    history = await get_history_async(user_id, bot_id=bot_manager.current_bot_id)
    return {"history": history[-20:]}  # Last 20 messages


@app.delete("/history/{user_id}")
async def clear_history(user_id: str, request: Request):
    """Clear conversation history for a user and current bot."""
    require_admin(request)
    key = _history_key(user_id, bot_manager.current_bot_id)
    conversation_histories[key] = []
    deleted = await memory_store.clear_conversation_history(user_id, bot_id=bot_manager.current_bot_id)
    return {"status": "cleared", "deleted": deleted}


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
# V2: Turn diagnostics and drafts (Phase 2+3 admin routes)
# ================================================================


@app.get("/admin/diagnostics")
async def admin_diagnostics(request: Request, limit: int = 20, fallback_only: bool = False):
    """View recent turn diagnostics. V2 observability endpoint."""
    require_admin(request)
    rows = await memory_store.get_recent_turn_diagnostics(
        limit=limit,
        fallback_only=fallback_only,
    )
    return {"diagnostics": rows}


@app.get("/admin/drafts")
async def admin_drafts(request: Request, status: str = "", limit: int = 20):
    """View turn drafts. V2 delivery state endpoint."""
    require_admin(request)
    drafts = await memory_store.list_turn_drafts(
        status=status or None,
        limit=limit,
    )
    return {"drafts": drafts}


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


@app.get("/admin/events")
async def admin_events(request: Request, limit: int = 30):
    """Aggregated Chinese admin dashboard events for the browser UI."""
    require_admin(request)
    return await build_admin_event_dashboard(limit=limit)


# ================================================================
# Background tasks
# ================================================================


async def scheduler_tick() -> dict:
    """Periodic scheduler check. Called by APScheduler or manual trigger.

    V2.1.1: uses due-time + intent pipeline.
    Broad memory/recap are NOT loaded for proactive generation.
    Only active topic state is loaded.
    """
    try:
        # ---- LIGHTWEIGHT PRE-CHECK (no state read) ----
        try:
            contact_pre = await memory_store.get_contact_state()
            user_id = contact_pre.get("user_id", "")
        except Exception:
            user_id = ""

        if not user_id:
            return {"sent": False, "reason": "no contact", "stage": "pre_check"}

        emotion_cfg = cfg.get("emotion", {})

        # ---- ACQUIRE LOCK ----
        lock = _get_user_lock(user_id)
        async with lock:
            # V2.3: Re-read everything inside the lock to avoid stale snapshots
            # that could occur if bot/contact/bridge state changed while
            # waiting for the lock.

            # Bot identity
            bot_id = bot_manager.current_bot_id
            current_bot = bot_manager.current_bot

            # Contact
            contact = await memory_store.get_contact_state()
            user_id = contact.get("user_id", "")
            if not user_id:
                return {"sent": False, "reason": "no contact (re-read)", "stage": "pre_check"}

            # Prerequisites
            contact_block_reason = _contact_send_block_reason(contact, bot_id)
            prereqs = {
                "bot_active": bot_id == bot_manager.current_bot_id,
                "proactive_enabled": current_bot.proactive_enabled if current_bot else False,
                "bridge_available": _bridge is not None and bool(_bridge.token),
                "valid_contact": contact_block_reason == "",
                "contact_block_reason": contact_block_reason,
                "user_id": user_id,
                "bot_id": bot_id,
            }

            # Shared context
            relationship = await memory_store.get_relationship_bot(bot_id)
            stable_memories = await memory_store.get_shared_facts(limit=5)
            bot_identity = current_bot.prompt_pack.get("bot_identity", "") if current_bot else read_persona_file()

            # Emotion + dialogue state
            emo_defaults = {
                "bot_mood": emotion_engine.baseline_mood,
                "affection": emotion_engine.initial_affection,
                "trust": emotion_engine.initial_trust,
                "attachment": emotion_engine.initial_attachment,
            }
            emo_raw = await memory_store.get_bot_emotion_state(user_id, bot_id, defaults=emo_defaults)
            ds_raw = await memory_store.get_dialogue_state(user_id, bot_id)

            # V2.1.2: Clingy check first
            now_ts = time.time()
            now_cst = datetime.now(SCHEDULER_CST)
            decision = await scheduler_agent.run_clingy_check(
                now_ts=now_ts,
                now_cst=now_cst,
                prerequisites=prereqs,
                bot_identity=bot_identity,
                relationship=relationship,
                stable_memories=stable_memories,
                dialogue_state=ds_raw,
                emotion_state=emo_raw,
            )

            if decision.get("block_proactive"):
                logger.info("Scheduler: clingy awaiting but not due — blocking proactive")
                return {"sent": False, "reason": decision.get("reason", "clingy awaiting, proactive blocked"), "stage": "clingy"}
            if not decision.get("sent"):
                decision = await scheduler_agent.run_proactive_pipeline(
                    recent_memories=None,
                    recap_text="",
                    bot_id=bot_id,
                    prerequisites=prereqs,
                    bot_identity=bot_identity,
                    relationship=relationship,
                    stable_memories=stable_memories,
                    dialogue_state=ds_raw,
                    emotion_state=emo_raw,
                )

            if decision.get("sent"):
                decision_messages = decision.get("messages") or [{"text": decision.get("message", ""), "delay": 0}]
                emotion = decision.get("emotion", "neutral")
                reason = decision.get("reason", "")
                candidate_id = decision.get("candidate_id", "")
                intent = decision.get("intent", "")

                # V2.3: Comprehensive final pre-send check
                ds_final = await memory_store.get_dialogue_state(user_id, bot_id)
                wait_state_final = str(ds_final.get("wait_state", "none"))
                proactive_paused = float(ds_final.get("proactive_paused_until", 0))

                # Re-verify bot + contact + bridge
                current_bot_final = bot_manager.current_bot
                contact_final = await memory_store.get_contact_state()
                contact_block_final = _contact_send_block_reason(
                    contact_final, bot_manager.current_bot_id
                )
                bridge_ok = _bridge is not None and bool(_bridge.token)

                block_reason = ""
                if bot_manager.current_bot_id != bot_id:
                    block_reason = "bot changed during lock"
                elif not bridge_ok:
                    block_reason = "bridge lost"
                elif contact_block_final:
                    block_reason = f"contact invalid: {contact_block_final}"
                elif wait_state_final in ("bot_silent", "user_waiting_assistant", "paused_by_user"):
                    block_reason = f"wait_state={wait_state_final}"
                elif intent == "clingy_followup" and wait_state_final != "assistant_waiting_user":
                    block_reason = f"clingy but wait_state={wait_state_final}"
                elif proactive_paused > now_ts:
                    block_reason = f"proactive paused until {proactive_paused}"
                elif not prereqs.get("proactive_enabled"):
                    block_reason = "proactive disabled (re-read)"

                # Bug A fix: pre-send active-chat guard.
                # If conversation became active since candidate generation,
                # cancel the send to avoid interrupting the user mid-chat.
                if not block_reason and intent not in ("clingy_followup",):
                    proactive_cfg = cfg.get("proactive", {})
                    cooldown_s = int(proactive_cfg.get("active_chat_cooldown_seconds", 300) or 300)
                    if cooldown_s > 0:
                        active, active_reason = await scheduler_agent._is_conversation_active(
                            user_id, bot_id, now_ts, dialogue_state=ds_final,
                        )
                        if active:
                            block_reason = f"active_chat_cooldown: {active_reason}"
                            # Mark candidate as cancelled
                            if candidate_id:
                                await memory_store.update_proactive_candidate(
                                    candidate_id,
                                    passed_gate=False,
                                    gate_detail=f"pre-send cancelled: {block_reason}",
                                    send_status="cancelled",
                                )

                if block_reason:
                    logger.warning(
                        f"Scheduler: final check blocked for {user_id}: {block_reason}"
                    )
                    return {"sent": False, "reason": f"final check: {block_reason}"}

                if not any(str(m.get("text", "")).strip() for m in decision_messages if isinstance(m, dict)):
                    logger.warning("Proactive message empty, skipping send")
                    return {"sent": False, "reason": "empty message", "stage": decision.get("stage", "")}

                turn_id = f"turn_{uuid.uuid4().hex[:12]}"
                send_status = "delivered"
                send_error = ""
                guarded = []
                try:
                    guarded = guard_messages(
                        decision_messages,
                        max_messages=scheduler_agent.max_proactive_messages,
                    )
                    if not guarded:
                        logger.warning("Proactive message empty after guard, skipping send")
                        return {"sent": False, "reason": "empty after guard"}
                    await memory_store.record_outbound_messages(
                        turn_id=turn_id,
                        messages=guarded,
                        user_id=user_id,
                        bot_id=bot_id,
                        source="proactive",
                        author="assistant",
                        trigger="proactive_reason",
                        send_status="sending",
                        candidate_id=candidate_id,
                        batch_id=turn_id,
                    )
                    await _send_message_batch(
                        user_id=user_id,
                        context_token=contact.get("context_token", ""),
                        messages=guarded,
                    )
                    await memory_store.mark_outbound_batch_status(turn_id, "delivered")
                except Exception as e:
                    logger.error(f"Proactive send failed: {e}")
                    send_status = "failed"
                    send_error = str(e)
                    await memory_store.mark_outbound_batch_status(
                        turn_id,
                        "failed",
                        send_error,
                    )

                await memory_store.create_turn(
                    turn_id=turn_id,
                    user_id=user_id,
                    bot_id=bot_id,
                    user_messages="",
                    assistant_actions=json.dumps(guarded, ensure_ascii=False),
                    tool_calls=json.dumps([], ensure_ascii=False),
                    source="proactive",
                    trigger="proactive_reason",
                    send_status=send_status,
                )

                # V2: Record turn diagnostics
                await memory_store.record_turn_diagnostics(
                    turn_id=turn_id,
                    user_id=user_id,
                    source="proactive",
                    route=ROUTE_CHAT_FULL,
                    context_policy=CTX_HOT_CHEAP,
                    llm_status="ok",
                    elapsed_ms=0,
                    send_status=send_status,
                    output_messages=len(guarded),
                    metadata={
                        "intent": intent,
                        "send_error": send_error,
                        "candidate_id": candidate_id,
                        "batch_id": turn_id,
                        "source": "proactive",
                        "trigger": "proactive_reason",
                        "send_status": send_status,
                    },
                )
                if candidate_id:
                    await memory_store.update_proactive_candidate(
                        candidate_id,
                        sent=(send_status == "delivered"),
                        turn_id=turn_id,
                        batch_id=turn_id,
                        send_status=send_status,
                    )

                if send_status != "delivered":
                    logger.error(f"Scheduler: send failed, skipping post-send bookkeeping")
                    return {"sent": False, "reason": "send failed"}

                # V2.1.1: Commit state mutations only after confirmed delivery
                await scheduler_agent.commit_proactive_send(
                    user_id=user_id,
                    bot_id=bot_id,
                    topic_id=decision.get("topic_id", ""),
                    intent=decision.get("intent", ""),
                )

                await memory_store.record_usage_event(
                    event_type="proactive_send",
                    user_id=user_id,
                    source="proactive",
                    message_count=len(guarded),
                    metadata={
                        "reason": reason,
                        "emotion": emotion,
                        "intent": intent,
                        "candidate_id": candidate_id,
                        "batch_id": turn_id,
                        "source": "proactive",
                        "trigger": "proactive_reason",
                        "send_status": send_status,
                    },
                )
                await append_history_async(
                    user_id,
                    "assistant",
                    _assistant_history_json({"messages": guarded}),
                    source="proactive",
                    bot_id=bot_id,
                )
                if candidate_id:
                    await scheduler_agent.record_proactive_sent(candidate_id)
                else:
                    await scheduler_agent.record_sent()
                logger.info(f"Scheduler: SENT — {intent}: {reason} (stage={decision.get('stage', '')})")

                return {
                    "sent": True,
                    "message": "\n".join(m["text"] for m in guarded if m.get("text")),
                    "messages": guarded,
                    "emotion": emotion,
                    "reason": reason,
                    "intent": intent,
                    "stage": decision.get("stage", ""),
                }
            else:
                reason = decision.get("reason", "unknown")
                logger.info(f"Scheduler: skip — {reason} (stage={decision.get('stage', '')})")
                return {
                    "sent": False,
                    "reason": reason,
                    "stage": decision.get("stage", ""),
                }
    except Exception as e:
        logger.error(f"Scheduler tick failed: {e}", exc_info=True)
        return {"sent": False, "reason": f"error: {e}"}


async def fixed_morning_message() -> dict:
    fixed_cfg = cfg.get("fixed_messages", {}).get("morning", {})
    city = (
        cfg.get("weather", {}).get("default_city")
        or fixed_cfg.get("city")
        or "成都"
    )
    weather_result = await execute_tool("get_weather", {"city": city})
    prompt = (
        f"现在是早上7点。主动给用户发早安消息。"
        f"天气工具结果：{weather_result}。"
        "基于这个真实结果，用几条短微信说早安、天气、今天可以做的一两件小事。"
        "不要再调用 get_weather，不要编造工具没有返回的天气细节。"
    )
    return await _run_fixed_message_prompt(
        prompt,
        save_diary=False,
        fallback_messages=_morning_fallback_messages(city, weather_result),
        trigger="fixed_morning",
    )


async def fixed_night_recap() -> dict:
    prompt = (
        "现在是晚上11点。请从AI的角度写一小段今天的陪伴日记并发给用户。"
        "基于今天真实聊过的内容，说我今天和你聊到了什么、我看见了什么状态。"
        "我想轻轻替你记住什么。不要冒充用户本人，不要编造没聊过的现实事实。"
        "200字以内，像随手写日记。"
    )
    return await _run_fixed_message_prompt(
        prompt,
        save_diary=True,
        fallback_messages=[{"text": "今天先收尾 明天再慢慢来", "delay": 0}],
        trigger="fixed_night",
    )


async def _run_fixed_message_prompt(
    prompt: str,
    save_diary: bool,
    fallback_messages: list[dict] | None = None,
    trigger: str = "proactive",
) -> dict:
    contact = await memory_store.get_contact_state()
    if not contact.get("user_id"):
        logger.info("Fixed message skipped — no contact target yet")
        return {"sent": False, "reason": "no contact target yet"}
    contact_block_reason = _contact_send_block_reason(contact, bot_manager.current_bot_id)
    if contact_block_reason:
        logger.info(f"Fixed message skipped — {contact_block_reason}")
        return {"sent": False, "reason": contact_block_reason}
    if _bridge is None or not _bridge.token:
        logger.info("Fixed message skipped — WeChat bridge unavailable")
        return {"sent": False, "reason": "WeChat bridge unavailable"}

    user_id = contact["user_id"]

    # V2: Acquire per-user lock for fixed messages too
    lock = _get_user_lock(user_id)
    send_status = "delivered"

    async with lock:
        # V2.1: Resolve current bot before loading bot-scoped data
        current_bot_id = bot_manager.current_bot_id
        memories = await memory_store.search_bot(prompt, bot_id=current_bot_id, top_k=5)
        relationship = await memory_store.get_relationship_bot(current_bot_id)
        history = await get_history_async(user_id, bot_id=current_bot_id)
        history_summary = await memory_store.get_history_summary(user_id, bot_id=current_bot_id)
        latest_recap = await memory_store.get_latest_recap(user_id, current_bot_id)
        recap_text = latest_recap["recap_text"] if latest_recap else ""

        # V2: Route and context policy for fixed messages
        route = ROUTE_CHAT_FULL
        context_policy = CTX_HOT_FULL

        result = await chat_agent.chat(
            user_message=prompt,
            memories=memories,
            history=history,
            relationship=relationship,
            history_summary=history_summary,
            trigger=trigger,
            route=route,
            context_policy=context_policy,
            bot_id=current_bot_id,
            recap=recap_text,
        )
        turn_id = result["turn_id"]

        messages = [
            {"text": m["text"], "delay": m.get("delay", 0)}
            for m in result.get("messages", [])
            if m.get("text")
        ]
        if not messages:
            messages = fallback_messages or [{"text": "我刚刚卡了一下 重新来", "delay": 0}]

        # V2: Persist turn draft
        await memory_store.create_turn_draft(
            turn_id=turn_id,
            user_id=user_id,
            messages=messages,
            source="proactive",
            route=route,
        )

        # V2: Guard messages before send
        guarded = guard_messages(messages)

        try:
            await memory_store.record_outbound_messages(
                turn_id=turn_id,
                messages=guarded,
                user_id=user_id,
                bot_id=current_bot_id,
                source=result.get("source", "proactive"),
                author=result.get("author", "assistant"),
                trigger=result.get("trigger", "proactive_reason"),
                send_status="sending",
            )
            await _send_message_batch(
                user_id=user_id,
                context_token=contact.get("context_token", ""),
                messages=guarded,
            )
            await memory_store.mark_outbound_batch_status(turn_id, "delivered")
        except Exception as e:
            logger.error(f"Fixed message send failed: {e}")
            send_status = "failed"
            await memory_store.mark_turn_draft_status(turn_id, "failed", str(e)[:500])
            await memory_store.mark_outbound_batch_status(turn_id, "failed", str(e)[:500])

        if send_status == "delivered":
            await memory_store.mark_turn_draft_status(turn_id, "delivered")

        # V2: Record turn diagnostics
        await memory_store.record_turn_diagnostics(
            turn_id=turn_id,
            user_id=user_id,
            source="proactive",
            route=route,
            context_policy=context_policy,
            llm_status=result.get("llm_status", "ok"),
            token_count=result_token_count(result),
            elapsed_ms=int(result.get("elapsed_ms", 0) or 0),
            fallback_type=result_fallback_type(result),
            send_status=send_status,
            input_chars=len(prompt),
            output_messages=len(messages),
            tool_calls=len(result.get("tool_calls", [])),
        )

    if send_status == "failed":
        logger.error(f"Fixed message send failed, skipping post-send bookkeeping")
        return {"sent": False, "reason": "send failed"}

    await memory_store.record_usage_event(
        event_type="fixed_message",
        user_id=user_id,
        source="proactive",
        message_count=len(messages),
        token_count=result_token_count(result),
        elapsed_ms=int(result.get("elapsed_ms", 0) or 0),
        fallback_type=result_fallback_type(result),
        metadata={
            "detail": result_fallback_detail(result),
            "save_diary": save_diary,
            "token_estimated": bool((result.get("token_usage") or {}).get("estimated")),
            "turn_id": turn_id,
        },
    )

    await append_history_async(
        user_id,
        "assistant",
        _assistant_history_json(result),
        source="proactive",
        bot_id=current_bot_id,
    )

    text = "\n".join(m["text"] for m in messages)
    if save_diary and text.strip():
        await memory_store.save_memory(
            content=f"AI视角陪伴日记：{text}",
            category="journal",
            keywords="晚间复盘,陪伴日记",
            min_importance=0.0,
        )

    return {"sent": True, "count": len(messages)}


def _morning_fallback_messages(city: str, weather_result: str) -> list[dict]:
    """Task-specific fallback for the fixed 8am greeting."""
    weather_text = str(weather_result or "").strip()
    if not weather_text or "失败" in weather_text or "出错" in weather_text:
        middle = f"{city}天气我这边没查稳 出门前看一下"
    else:
        middle = weather_text[:80]
    return [
        {"text": "早", "delay": 0},
        {"text": middle, "delay": 1},
        {"text": "今天先慢慢开始", "delay": 1},
    ]


async def deliver_scheduled_messages() -> dict:
    """Send due one-shot scheduled messages from SQLite."""
    if _bridge is None or not _bridge.token:
        return {"sent": 0, "reason": "WeChat bridge unavailable"}

    # V2: Atomically claim pending tasks as 'sending' to prevent duplicate
    # delivery when multiple workers or concurrent triggers read the same tasks.
    tasks = await memory_store.claim_due_scheduled_messages(limit=20)
    sent = 0
    failed = 0

    for task in tasks:
        task_id = task["id"]
        try:
            user_id = task["user_id"]
            context_token = task.get("context_token", "")
            if not context_token:
                contact = await memory_store.get_contact_state()
                if contact.get("user_id") and contact.get("context_token"):
                    user_id = contact["user_id"]
                    context_token = contact["context_token"]
                else:
                    raise RuntimeError(
                        "no iLink contact context_token; send a message to the built-in bot first"
                    )
            # V2: Acquire per-user lock so scheduled delivery doesn't
            # interleave with an active chat turn for the same user.
            lock = _get_user_lock(user_id)
            async with lock:
                await _send_message_batch(
                    user_id=user_id,
                    context_token=context_token,
                    messages=task.get("messages", []),
                )
                await memory_store.mark_scheduled_message_status(task_id, "sent")
                sent += 1
            await memory_store.record_usage_event(
                event_type="scheduled_send",
                user_id=user_id,
                source="scheduler",
                message_count=len(task.get("messages", [])),
                metadata={"task_id": task_id},
            )
            logger.info(f"Scheduled message sent: {task_id}")
        except Exception as e:
            failed += 1
            await memory_store.mark_scheduled_message_status(
                task_id, "failed", str(e)
            )
            logger.exception(f"Scheduled message failed: {task_id}")

    return {"sent": sent, "failed": failed}


async def _send_message_batch(
    user_id: str,
    context_token: str,
    messages: list[dict],
):
    """Send a normalized message list through the active WeChat bridge.

    V2.1.1: empty messages are filtered before send.
    If the final batch is empty, does not call WeChat at all.
    """
    if _bridge is None:
        raise RuntimeError("WeChat bridge is not configured")
    if not context_token:
        raise RuntimeError("missing iLink context_token")

    # V2.1.1: Filter empty messages before even touching guard
    non_empty = [
        m for m in messages
        if isinstance(m, dict) and str(m.get("text", "")).strip()
    ]
    if not non_empty:
        logger.warning("Empty message batch, skipping WeChat send entirely")
        await memory_store.record_usage_event(
            event_type="empty_message_batch",
            user_id=user_id,
            source="send_guard",
            metadata={"reason": "all messages empty"},
        )
        return

    # V2: guard all messages before sending
    guarded = guard_messages(non_empty)

    if not guarded:
        logger.warning("All messages filtered by guard, skipping WeChat send")
        await memory_store.record_usage_event(
            event_type="empty_message_batch",
            user_id=user_id,
            source="send_guard",
            metadata={"reason": "all messages filtered by guard"},
        )
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


def _assistant_history_json(result: dict) -> str:
    """Represent assistant output in history using the required action format."""
    actions = []
    for m in result.get("messages", []):
        text = str(m.get("text", "")).strip()
        if text:
            actions.append({
                "type": "tool",
                "name": "wechat_reply",
                "args": {
                    "text": text,
                    "delay": m.get("delay", 0),
                },
            })

    for task in result.get("scheduled_messages", []):
        actions.append({
            "type": "tool",
            "name": "schedule_wechat_message",
            "args": {
                "delay_minutes": task.get("delay_minutes", 0),
                "messages": task.get("messages", []),
            },
        })

    return json.dumps(
        {"actions": actions[:cfg["conversation"].get("max_actions_per_turn", 8)], "done": True},
        ensure_ascii=False,
    )


async def _analyze_and_update_topic(
    user_id: str,
    bot_id: str,
    user_message: str,
    assistant_text: str,
    trigger: str = "user",
) -> dict[str, Any] | None:
    """V2.1.1: Rule-first topic state update after normal chat turns.

    Only extracts a topic when the turn contains a follow-up-worthy event.
    Returns the topic action taken, or None if skipped.
    """
    if trigger != "user" or not user_message.strip():
        return None

    # Rule-first high-confidence cases
    close_words = {"不要和你聊", "不用了", "算了", "不是发过了", "别问了", "别管了"}
    if any(w in user_message for w in close_words):
        await memory_store.close_active_topic(user_id, bot_id, "user rejection")
        logger.info(f"Topic closed: user rejection — '{user_message[:50]}'")
        return {"action": "close", "reason": "user rejection"}

    # Meal/planning/recommendation keywords — waiting_user_result
    meal_keywords = {"吃饭", "吃什么", "午饭", "晚饭", "早餐", "午餐", "晚餐", "餐厅", "点餐", "外卖"}
    plan_keywords = {"安排", "计划", "下午", "晚上", "今天", "明天", "日程"}
    project_keywords = {"项目", "代码", "bug", "更新", "实现", "测试", "部署", "修复"}

    is_meal = any(kw in user_message for kw in meal_keywords)
    is_plan = any(kw in user_message for kw in plan_keywords)
    is_project = any(kw in user_message for kw in project_keywords)

    now = time.time()
    pro_cfg = cfg.get("proactive", {}).get("topic", {})
    default_expire = pro_cfg.get("default_expire_minutes", 240) * 60
    meal_followup_min = pro_cfg.get("meal_followup_min_minutes", 40) * 60
    meal_followup_max = pro_cfg.get("meal_followup_max_minutes", 90) * 60

    current = await memory_store.get_active_topic(user_id, bot_id)

    if is_meal or is_plan or is_project:
        # New meaningful topic — create or replace
        if is_meal:
            status = "waiting_user_result"
            followup_after = now + random.uniform(meal_followup_min, meal_followup_max)
            expires = now + max(default_expire, meal_followup_max)
        elif is_project:
            status = "open"
            followup_after = now + default_expire / 2
            expires = now + default_expire
        else:
            status = "open"
            followup_after = now + default_expire / 2
            expires = now + default_expire

        title = user_message[:50]
        summary = f"User: {user_message[:80]} | Bot: {assistant_text[:80]}"

        # If different from current topic, mark previous as stale
        if current:
            current_title = current.get("title", "")
            # Simple keyword overlap check — if different enough, stale previous
            if not _topic_overlap(current_title, user_message):
                await memory_store.mark_active_topic_stale(
                    user_id, bot_id, "newer topic created"
                )

        await memory_store.upsert_active_topic(
            user_id=user_id,
            bot_id=bot_id,
            title=title,
            summary=summary,
            status=status,
            followup_after_ts=followup_after,
            expires_at=expires,
            last_user_ts=now,
            last_assistant_ts=now,
        )
        logger.info(f"Topic upserted: [{status}] '{title[:30]}'")
        return {"action": "upsert", "status": status}

    # Weather/schedule answer — mark topic answered
    weather_keywords = {"天气", "温度", "下雨", "晴", "雨"}
    if current and any(kw in user_message for kw in weather_keywords):
        current_status = current.get("status", "")
        if current_status not in ("closed", "stale"):
            # If assistant already replied with weather info, mark answered
            if assistant_text and len(assistant_text) > 10:
                await memory_store.update_active_topic(
                    user_id, bot_id, status="answered",
                )
                logger.info("Topic marked answered (weather query)")
                return {"action": "update", "status": "answered"}

    return None


def _topic_overlap(title: str, new_message: str) -> bool:
    """Simple CJK word overlap check for topic comparison."""
    title_set = set(title)
    msg_set = set(new_message)
    if not title_set or not msg_set:
        return False
    overlap = len(title_set & msg_set)
    return overlap >= min(len(title_set), len(msg_set)) * 0.4


async def _generate_recap(
    user_id: str,
    bot_id: str,
    recap_cfg: dict,
) -> str | None:
    """V2.1: Background recap generation from recent turns.

    Uses LLM to summarize conversation stage between the last recap
    and the current turn.
    """
    try:
        latest = await memory_store.get_latest_recap(user_id, bot_id)
        since_turn_id = latest.get("end_turn_id", "") if latest else ""

        turns = await memory_store.get_recent_turns(user_id, bot_id, limit=50)
        if not turns or len(turns) < 2:
            return None

        # Find the turn number of the last summarized turn
        since_number = 0
        if since_turn_id:
            for t in turns:
                if t.get("id") == since_turn_id:
                    since_number = t.get("turn_number", 0)
                    break

        # Only include turns after the last recap
        new_turns = [
            t for t in turns
            if t.get("turn_number", 0) > since_number
        ]
        if len(new_turns) < 3:
            return None

        start_id = new_turns[0]["id"] if new_turns else ""
        end_id = new_turns[-1]["id"] if new_turns else ""

        # Build summary text from turns
        turn_texts = []
        for t in new_turns:
            user_msg = str(t.get("user_messages", ""))[:200]
            assistant_action = str(t.get("assistant_actions", ""))[:200]
            turn_texts.append(f"User: {user_msg}\nBot: {assistant_action}")

        turns_summary = "\n---\n".join(turn_texts[-20:])  # last 20 turns

        prompt = (
            "你是一个对话摘要助手。基于以下对话回合，生成一个会话阶段摘要(recap)。"
            "recap应该包括：对话刚才到了哪里、未完成的话题、当前的语气/氛围、"
            "这个阶段用户表达的关切或偏好、最自然的下一步继续方向。"
            "不要包含：永久用户事实、推测性心理标签、原始历史细节。"
            "200字以内。只输出摘要文本，不要JSON。\n\n"
            f"对话记录：\n{turns_summary}"
        )

        llm_response = await llm_gateway.complete(
            LLMRequest(
                purpose="recap",
                model=model_name,
                messages=[
                    {"role": "system", "content": "你是一个简洁的对话摘要助手。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=float(recap_cfg.get("generation", {}).get("temperature", 0.5)),
                max_tokens=int(recap_cfg.get("generation", {}).get("max_chars", 500)),
            )
        )

        recap_text = llm_response.content.strip()
        if not recap_text:
            return None

        recap_id = await memory_store.save_recap(
            user_id=user_id,
            bot_id=bot_id,
            recap_text=recap_text,
            start_turn_id=start_id,
            end_turn_id=end_id,
            turn_count=len(new_turns),
        )
        logger.info(
            f"Recap generated: {recap_id} "
            f"({len(new_turns)} turns, bot={bot_id})"
        )
        return recap_id

    except Exception as e:
        logger.error(f"Recap generation failed: {e}", exc_info=True)
        return None


async def daily_reset():
    """Reset daily counters at midnight."""
    await scheduler_agent.reset_daily_counter()
    logger.info("Daily scheduler counter reset")


async def memory_decay():
    """Apply time-based importance decay to memories."""
    await memory_store.apply_decay()
    logger.info("Memory decay applied")


# ================================================================
# V2.1: Bot management routes
# ================================================================


@app.get("/admin/bots")
async def admin_list_bots(request: Request):
    """List all configured bots and active bot."""
    require_admin(request)
    bots_info = {}
    for bot_id, bot in bot_manager.bots.items():
        state = bot_manager.get_state(bot_id)
        bots_info[bot_id] = {
            "display_name": bot.display_name,
            "identity_file": bot.identity_file,
            "enabled_tools": bot.enabled_tools,
            "proactive_enabled": bot.proactive_enabled,
            "is_current": bot_id == bot_manager.current_bot_id,
            "recap": state.current_recap[:200] if state.current_recap else "",
        }
    return {
        "current_bot_id": bot_manager.current_bot_id,
        "bots": bots_info,
    }


@app.post("/admin/bots/switch")
async def admin_switch_bot(request: Request):
    """Switch the active bot."""
    require_admin(request)
    body = await request.json()
    bot_id = str(body.get("bot_id", "")).strip()
    if not bot_id:
        raise HTTPException(status_code=400, detail="bot_id required")
    if bot_id not in bot_manager.bots:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown bot: {bot_id}. Available: {bot_manager.list_bot_ids()}",
        )
    bot_manager.set_current_bot(bot_id)
    # Update runtime settings to persist choice
    apply_runtime_settings({"bots": {"_current_bot_id": bot_id}})
    return {
        "status": "switched",
        "current_bot_id": bot_id,
        "bot": {
            "display_name": bot_manager.current_bot.display_name,
            "proactive_enabled": bot_manager.current_bot.proactive_enabled,
        },
    }


@app.get("/admin/recaps")
async def admin_list_recaps(request: Request, limit: int = 10):
    """List recent recaps for the current bot and known contact."""
    require_admin(request)
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")
    recaps = await memory_store.get_recaps(
        user_id, bot_manager.current_bot_id, limit=limit
    )
    return {"recaps": recaps}


@app.post("/admin/recaps/generate")
async def admin_trigger_recap(request: Request):
    """Manually trigger recap generation."""
    require_admin(request)
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="没有联系人")
    recap_cfg = cfg.get("recap", {})
    recap_id = await _generate_recap(
        user_id, bot_manager.current_bot_id, recap_cfg
    )
    if recap_id:
        return {"status": "generated", "recap_id": recap_id}
    return {"status": "skipped", "reason": "not enough turns or error"}


@app.get("/admin/outbound")
async def admin_list_outbound(
    request: Request,
    turn_id: str = "",
    status: str = "",
    limit: int = 50,
):
    """List outbound messages with V2.1 semantics."""
    require_admin(request)
    messages = await memory_store.get_outbound_messages(
        turn_id=turn_id,
        send_status=status or "",
        limit=limit,
    )
    return {"outbound_messages": messages}


@app.get("/admin/turns")
async def admin_list_turns(request: Request, limit: int = 20):
    """List recent turns (L2) for the current bot."""
    require_admin(request)
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")
    turns = await memory_store.get_recent_turns(
        user_id, bot_manager.current_bot_id, limit=limit
    )
    return {"turns": turns}


@app.get("/admin/proactive/candidates")
async def admin_list_proactive_candidates(request: Request, limit: int = 20):
    """List recent proactive candidates (V2.1 3-stage pipeline)."""
    require_admin(request)
    candidates = await memory_store.get_recent_proactive_candidates(limit=limit)
    return {"candidates": candidates}


@app.get("/admin/shared-facts")
async def admin_list_shared_facts(request: Request, limit: int = 20):
    """List shared user facts."""
    require_admin(request)
    facts = await memory_store.get_shared_facts(limit=limit)
    return {"shared_facts": facts}


# ================================================================
# V2.1.1: Proactive state and topic admin endpoints
# ================================================================


@app.get("/admin/proactive/state")
async def admin_proactive_state(request: Request):
    """View proactive schedule state (next due time, last sent, etc.)."""
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")

    sched_state = await memory_store.get_proactive_schedule_state(user_id, bot_id)
    scheduler_state = await memory_store.get_scheduler_state()

    now = time.time()
    next_due = sched_state.get("next_due_ts", 0)
    last_sent = sched_state.get("last_sent_ts", 0)
    last_attempt = sched_state.get("last_attempt_ts", 0)

    return {
        "proactive_schedule": {
            "next_due_ts": next_due,
            "next_due_in_seconds": max(0, int(next_due - now)) if next_due else None,
            "next_due_time": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(next_due)
            ) if next_due else None,
            "last_attempt_ts": last_attempt,
            "last_sent_ts": last_sent,
            "is_due": next_due > 0 and now >= next_due,
        },
        "scheduler_state": scheduler_state,
        "timing_config": {
            "min_interval_minutes": scheduler_agent.min_interval,
            "max_interval_minutes": scheduler_agent.max_interval,
        },
    }


def _is_real_user_inbound(user_id: str, text: str, trigger: str, source: str) -> bool:
    """Only real WeChat user messages should clear clingy awaiting state.

    Excludes: empty text, default/anonymous users, admin/system/proactive
    triggers, debug events, and bot self-echo.
    """
    if not user_id or user_id == "default":
        return False
    if not text or not text.strip():
        return False
    if trigger not in ("user",):
        return False
    if source not in ("wechat", "wechat_inbound", "user", ""):
        return False
    return True


def _contact_send_block_reason(contact: dict[str, Any], bot_id: str) -> str:
    """Return why a stored WeChat contact cannot receive current-bot proactive sends."""
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
# V2.2: Emotion / Dialogue state helpers
# ================================================================

def emotion_bool(value: Any) -> bool:
    return bool(value)


def _max_silent_for_mood(mood: str, emotion_cfg: dict[str, Any]) -> int:
    """Get max silent messages allowed for a given mood."""
    silence_cfg = emotion_cfg.get("silence", {})
    mood_map = silence_cfg.get("max_silent_messages_by_mood", {})
    if isinstance(mood_map, dict):
        val = mood_map.get(mood)
        if val is not None:
            return int(val)
    return int(silence_cfg.get("default_max_silent_messages", 2))


async def _commit_dialogue_state_after_turn(
    user_id: str,
    bot_id: str,
    user_message: str,
    result: dict[str, Any],
    ds_raw: dict[str, Any],
    emotion_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Synchronous dialogue_state transition after a chat turn.

    MUST be awaited before releasing the per-user lock.
    Returns the updated ds dict (already persisted).
    """
    from agent.dialogue_state.engine import DialogueStateEngine
    from agent.dialogue_state.models import DialogueState

    eng_ds = DialogueStateEngine(config={
        **(emotion_cfg.get("dialogue_state", {})),
        "silence": emotion_cfg.get("silence", {}),
    })

    ds = DialogueState.from_dict(ds_raw)
    ds_event = None

    assistant_text = "\n".join(
        m["text"] for m in result.get("messages", []) if m.get("text")
    )
    skipped = bool(result.get("skip_reply")) and not result.get("messages")
    silence_overridden = bool(result.get("silence_overridden"))

    if skipped and not silence_overridden:
        ds, ds_event = eng_ds.enter_silent(
            ds,
            reason=f"bot skipped reply",
            max_silent_by_mood="",
        )
        ds.user_messages_since_silent += 1
    elif silence_overridden:
        ds, ds_event = eng_ds.exit_silent(ds, reason="forced reply override")
        if assistant_text:
            has_question = bool(re.search(r"[?？]", assistant_text[-200:])) if assistant_text else False
            has_choice = bool(re.search(r"(还是|或者|A[.、]|B[.、])", assistant_text[-200:])) if assistant_text else False
            needs_reply = bool(result.get("messages")) and (has_question or has_choice)
            ds, ds_event = eng_ds.record_assistant_message(
                ds, assistant_text, reason="forced reply after silence",
                needs_user_reply=needs_reply,
            )
    elif assistant_text:
        has_question = bool(re.search(r"[?？]", assistant_text[-200:])) if assistant_text else False
        has_choice = bool(re.search(r"(还是|或者|A[.、]|B[.、])", assistant_text[-200:])) if assistant_text else False
        needs_reply = bool(result.get("messages")) and (has_question or has_choice)
        ds, ds_event = eng_ds.record_assistant_message(
            ds, assistant_text, reason="normal reply",
            needs_user_reply=needs_reply,
        )
    else:
        if ds.wait_state == "bot_silent":
            ds.user_messages_since_silent += 1
            ds.updated_at = time.time()

    # Persist dialogue state synchronously
    await memory_store.upsert_dialogue_state(user_id, bot_id, ds.to_dict())
    if ds_event:
        await memory_store.insert_dialogue_state_event(
            user_id, bot_id,
            turn_id=0,
            old_state_json=ds_event.old_state_json,
            new_state_json=ds_event.new_state_json,
            reason=ds_event.reason,
            created_at=ds_event.created_at,
        )

    logger.info(
        "dialogue_state committed: %s/%s wait=%s silent_count=%d",
        user_id, bot_id, ds.wait_state, ds.user_messages_since_silent,
    )

    return ds.to_dict()


def _check_must_reply(
    ds_state: "DialogueState",
    emo_state: "BotEmotionState",
    emotion_cfg: dict[str, Any],
) -> bool:
    """Determine if bot must reply to this user message.

    True when:
    - silent count >= max_silent for this mood
    - silent time exceeded
    """
    if ds_state.wait_state != "bot_silent":
        return False

    max_count = _max_silent_for_mood(emo_state.bot_mood, emotion_cfg)
    count = ds_state.user_messages_since_silent + 1  # this message counts
    if count >= max_count:
        return True

    now = time.time()
    if ds_state.silent_must_reply_after_ts > 0 and now >= ds_state.silent_must_reply_after_ts:
        return True

    return False


async def _update_emotion_state_only(
    user_id: str,
    bot_id: str,
    user_message: str,
    result: dict[str, Any],
    emo_raw: dict[str, Any],
    emotion_cfg: dict[str, Any],
) -> None:
    """Update emotion state only (fire-and-forget, non-critical for consistency)."""
    from agent.emotion.engine import EmotionEngine
    from agent.emotion.models import BotEmotionState

    try:
        eng_emo = EmotionEngine(config=emotion_cfg)
        emo = BotEmotionState.from_dict(emo_raw)
        emo, emo_event = eng_emo.apply_trigger(
            emo, user_message, source="wechat", trigger_type="user",
        )
        await memory_store.upsert_bot_emotion_state(user_id, bot_id, emo.to_dict())
        if emo_event:
            await memory_store.insert_bot_emotion_event(
                user_id, bot_id,
                turn_id=0,
                source=emo_event.source,
                trigger_type=emo_event.trigger_type,
                event_type=emo_event.event_type,
                old_state_json=emo_event.old_state_json,
                new_state_json=emo_event.new_state_json,
                reason=emo_event.reason,
                confidence=emo_event.confidence,
                created_at=emo_event.created_at,
            )
    except Exception:
        logger.exception(f"Non-critical emotion update failed for {user_id}/{bot_id}")


async def _update_emotion_dialogue_after_turn(
    user_id: str,
    bot_id: str,
    user_message: str,
    result: dict[str, Any],
    emo_raw: dict[str, Any],
    ds_raw: dict[str, Any],
    emotion_cfg: dict[str, Any],
) -> None:
    """Update emotion and dialogue state after a user chat turn.

    Fire-and-forget background task.
    """
    from agent.emotion.engine import EmotionEngine
    from agent.emotion.models import BotEmotionState
    from agent.dialogue_state.engine import DialogueStateEngine
    from agent.dialogue_state.models import DialogueState

    eng_emo = EmotionEngine(config=emotion_cfg)
    eng_ds = DialogueStateEngine(config={
        **(cfg.get("dialogue_state", {})),
        "silence": emotion_cfg.get("silence", {}),
    })

    emo = BotEmotionState.from_dict(emo_raw)
    ds = DialogueState.from_dict(ds_raw)
    ds_event = None  # may be set in branches below
    event_already_written = False  # V2.2: Track whether ds_event was already persisted

    # 1. Classify user message and apply emotion rule
    emo, emo_event = eng_emo.apply_trigger(
        emo, user_message, source="wechat", trigger_type="user",
    )

    # 2. Check if this was a force-reply / override situation
    assistant_text = "\n".join(
        m["text"] for m in result.get("messages", []) if m.get("text")
    )
    skipped = bool(result.get("skip_reply")) and not result.get("messages")
    silence_overridden = bool(result.get("silence_overridden"))

    # 3. Update dialogue state
    if skipped and not silence_overridden:
        # Bot chose to stay silent
        ds, ds_event = eng_ds.enter_silent(
            ds,
            reason=f"bot {emo.bot_mood} silence",
            max_silent_by_mood=emo.bot_mood,
        )
        # Increment silent message count for the NEXT user message
        ds.user_messages_since_silent += 1
    elif silence_overridden:
        # Bot was forced to reply — exit silent mode
        ds, ds_event = eng_ds.exit_silent(ds, reason="forced reply override")
        await memory_store.upsert_dialogue_state(user_id, bot_id, ds.to_dict())
        if ds_event:
            await memory_store.insert_dialogue_state_event(
                user_id, bot_id,
                turn_id=0,
                old_state_json=ds_event.old_state_json,
                new_state_json=ds_event.new_state_json,
                reason=ds_event.reason,
                created_at=ds_event.created_at,
            )
        event_already_written = True  # V2.2: early write above, avoid duplicate in common tail
        # Also record the actual reply if there is one
        if assistant_text:
            ds2, ds_event2 = eng_ds.record_assistant_message(
                ds, assistant_text, reason="forced reply after silence",
                needs_user_reply=True,  # forced replies typically need user response
            )
            ds = ds2
            ds_event = ds_event2
            event_already_written = False  # V2.2: new event hasn't been written yet
    elif assistant_text:
        # Normal reply — determine if this reply needs a user response
        has_question = bool(re.search(r"[?？]", assistant_text[-200:])) if assistant_text else False
        has_choice = bool(re.search(r"(还是|或者|A[.、]|B[.、])", assistant_text[-200:])) if assistant_text else False
        needs_reply = bool(result.get("messages")) and (has_question or has_choice)

        ds, ds_event = eng_ds.record_assistant_message(
            ds, assistant_text, reason="normal reply",
            needs_user_reply=needs_reply,
        )
    else:
        # No messages, no explicit skip — track as silent if already in that state
        if ds.wait_state == "bot_silent":
            ds.user_messages_since_silent += 1
            ds.updated_at = time.time()

    # 4. Persist emotion state
    await memory_store.upsert_bot_emotion_state(user_id, bot_id, emo.to_dict())
    if emo_event:
        await memory_store.insert_bot_emotion_event(
            user_id, bot_id,
            turn_id=0,
            source=emo_event.source,
            trigger_type=emo_event.trigger_type,
            event_type=emo_event.event_type,
            old_state_json=emo_event.old_state_json,
            new_state_json=emo_event.new_state_json,
            reason=emo_event.reason,
            confidence=emo_event.confidence,
            created_at=emo_event.created_at,
        )

    # 5. Persist dialogue state
    await memory_store.upsert_dialogue_state(user_id, bot_id, ds.to_dict())
    if ds_event and not event_already_written:
        await memory_store.insert_dialogue_state_event(
            user_id, bot_id,
            turn_id=0,
            old_state_json=ds_event.old_state_json,
            new_state_json=ds_event.new_state_json,
            reason=ds_event.reason,
            created_at=ds_event.created_at,
        )

    logger.info(
        "emotion_dialogue updated: %s/%s mood=%s irr=%d aff=%d wait=%s silent_count=%d",
        user_id, bot_id, emo.bot_mood, emo.irritation,
        emo.affection, ds.wait_state, ds.user_messages_since_silent,
    )


@app.get("/admin/proactive/topic")
async def admin_proactive_topic(request: Request):
    """View current active topic state."""
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")

    topic = await memory_store.get_active_topic(user_id, bot_id)
    if topic:
        return {
            "active_topic": {
                "id": topic.get("id"),
                "title": topic.get("title"),
                "summary": topic.get("summary"),
                "status": topic.get("status"),
                "followup_count": topic.get("followup_count"),
                "followup_after_ts": topic.get("followup_after_ts"),
                "expires_at": topic.get("expires_at"),
                "created_at": topic.get("created_at"),
                "updated_at": topic.get("updated_at"),
            }
        }
    return {"active_topic": None}


@app.post("/admin/proactive/topic/close")
async def admin_close_topic(request: Request):
    """Manually close the current active topic."""
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="没有联系人")
    body = await request.json()
    reason = str(body.get("reason", "admin manual close"))
    closed = await memory_store.close_active_topic(user_id, bot_id, reason)
    return {"status": "ok", "closed": closed}


@app.post("/admin/proactive/trigger")
async def admin_trigger_proactive(request: Request):
    """Manually trigger a proactive send attempt (ignoring due time).

    Runs scheduler_tick in the background so the UI gets an immediate
    acknowledgement. The caller should poll dashboard/events for results.
    """
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="没有联系人")

    now = time.time()
    await memory_store.update_proactive_schedule_state(
        user_id=user_id, bot_id=bot_id,
        next_due_ts=now,
    )
    # Fire-and-forget: scheduler_tick may invoke LLM calls
    asyncio.create_task(_background_scheduler_tick())
    return {"status": "accepted", "message": "调度已触发，请刷新查看结果"}


async def _background_scheduler_tick():
    """Run scheduler_tick in background, logging result."""
    try:
        result = await scheduler_tick()
        logger.info(f"Background scheduler tick: sent={result.get('sent')}, stage={result.get('stage', '')}")
    except Exception as e:
        logger.error(f"Background scheduler tick failed: {e}")


@app.post("/admin/clingy/simulate")
async def admin_clingy_simulate(request: Request):
    """Dry-run clingy check — returns diagnostics without sending."""
    require_admin(request)
    import datetime as dt_module

    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="没有联系人")

    contact_block_reason = _contact_send_block_reason(contact, bot_id)
    now_ts = time.time()
    now_cst = dt_module.datetime.now(SCHEDULER_CST)

    clingy_state = await memory_store.get_clingy_state(user_id, bot_id)
    prereqs = {
        "bot_active": bot_id == bot_manager.current_bot_id,
        "proactive_enabled": bot_manager.current_bot.proactive_enabled,
        "bridge_available": _bridge is not None and bool(_bridge.token),
        "valid_contact": contact_block_reason == "",
        "contact_block_reason": contact_block_reason,
        "user_id": user_id,
        "bot_id": bot_id,
    }

    # Run the actual check (will return decision dict with stage/block reason)
    decision = await scheduler_agent.run_clingy_check(
        now_ts=now_ts,
        now_cst=now_cst,
        prerequisites=prereqs,
        bot_identity=bot_manager.current_bot.prompt_pack.get("bot_identity", "") or read_persona_file(),
        relationship=await memory_store.get_relationship_bot(bot_id),
        stable_memories=await memory_store.get_shared_facts(limit=5),
    )

    # Compute blocked reasons for UI
    blocked_reasons = []
    if not scheduler_agent.clingy_mode_enabled:
        blocked_reasons.append("clingy_mode_disabled")
    if not clingy_state.get("awaiting_user_reply"):
        blocked_reasons.append("not_awaiting_user_reply — bot 上一轮没有发出消息，或用户已经回复过")
    next_due = clingy_state.get("next_clingy_due_at", 0)
    if next_due > 0 and now_ts < next_due:
        blocked_reasons.append(f"not_yet_due — 还需等待 {int(next_due - now_ts)}s")
    count = clingy_state.get("clingy_followup_count", 0)
    max_count = scheduler_agent.max_followups_per_unanswered
    if count >= max_count:
        blocked_reasons.append(f"max_reached — 已追 {count}/{max_count} 次")
    if scheduler_agent.quiet_hours_enabled and scheduler_agent._is_quiet_hour(now_cst.hour):
        blocked_reasons.append("quiet_hours")
    today_start = dt_module.datetime.combine(
        now_cst.date(), dt_module.datetime.min.time(), tzinfo=SCHEDULER_CST
    ).timestamp()
    sent_today = await memory_store.count_today_proactive_sent(today_start)
    if sent_today >= scheduler_agent.max_daily:
        blocked_reasons.append(f"daily_cap — 今日已发 {sent_today}/{scheduler_agent.max_daily}")

    return {
        "status": "ok",
        "would_send": decision.get("sent", False),
        "stage": decision.get("stage", "clingy"),
        "reason": decision.get("reason", ""),
        "blocked_reasons": blocked_reasons if not decision.get("sent") else [],
        "clingy_state": {
            "awaiting_user_reply": bool(clingy_state.get("awaiting_user_reply")),
            "last_proactive_sent_at": clingy_state.get("last_proactive_sent_at", 0),
            "last_proactive_intent": clingy_state.get("last_proactive_intent", ""),
            "next_clingy_due_at": clingy_state.get("next_clingy_due_at", 0),
            "next_due_in_seconds": max(0, int(next_due - now_ts)) if next_due else None,
            "clingy_followup_count": clingy_state.get("clingy_followup_count", 0),
            "max_followups": scheduler_agent.max_followups_per_unanswered,
            "clingy_after_minutes": scheduler_agent.clingy_after_minutes,
            "clingy_mode_enabled": scheduler_agent.clingy_mode_enabled,
        },
        "generated_message": decision.get("message", "") if decision.get("sent") else "",
    }


@app.post("/admin/clingy/force")
async def admin_clingy_force(request: Request):
    """Force-trigger a clingy followup — bypasses after_minutes only.

    Does NOT fall through to proactive pipeline. Only runs clingy check.
    """
    require_admin(request)
    import datetime as dt_module

    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")
    context_token = contact.get("context_token", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="没有联系人")

    clingy_state = await memory_store.get_clingy_state(user_id, bot_id)

    # Gather diagnostic context
    recent_turns = await memory_store.get_recent_turns(user_id, bot_id, limit=5)
    last_bot_turn = None
    last_user_turn = None
    for t in reversed(recent_turns):
        if not last_bot_turn and str(t.get("assistant_actions", "")).strip():
            last_bot_turn = {"created_at": t.get("created_at"), "source": t.get("source")}
        if not last_user_turn and str(t.get("user_messages", "")).strip():
            last_user_turn = {"created_at": t.get("created_at"), "user_text": str(t.get("user_messages", ""))[:60]}
    sched_state = await memory_store.get_scheduler_state()

    diag = {
        "awaiting_user_reply": bool(clingy_state.get("awaiting_user_reply")),
        "last_proactive_sent_at": clingy_state.get("last_proactive_sent_at", 0),
        "last_proactive_intent": clingy_state.get("last_proactive_intent", ""),
        "next_clingy_due_at": clingy_state.get("next_clingy_due_at", 0),
        "clingy_followup_count": clingy_state.get("clingy_followup_count", 0),
        "clingy_mode_enabled": scheduler_agent.clingy_mode_enabled,
        "clingy_after_minutes": scheduler_agent.clingy_after_minutes,
        "last_chat_time": sched_state.get("last_chat_time", 0),
        "last_bot_turn_at": last_bot_turn["created_at"] if last_bot_turn else None,
        "last_bot_turn_source": last_bot_turn["source"] if last_bot_turn else None,
        "last_user_turn_at": last_user_turn["created_at"] if last_user_turn else None,
        "last_user_turn_text": last_user_turn["user_text"] if last_user_turn else None,
    }

    if not clingy_state.get("awaiting_user_reply"):
        reason_parts = ["awaiting_user_reply = false"]
        if not last_bot_turn:
            reason_parts.append("没有任何 bot 发出的消息记录")
        elif last_user_turn and last_user_turn["created_at"] and last_bot_turn["created_at"]:
            if last_user_turn["created_at"] > last_bot_turn["created_at"]:
                reason_parts.append("用户在 bot 消息之后已经回复过了，状态已被清理")
        if diag["last_proactive_intent"]:
            reason_parts.append(f"上次意图: {diag['last_proactive_intent']}")
        reason_parts.append("提示: 先让 bot 回复一条消息（不要 skip），再等用户不回即可触发")
        return {"status": "blocked", "reason": " | ".join(reason_parts), "diagnostics": diag}

    # Force next_clingy_due_at to now
    now = time.time()
    await memory_store.update_clingy_state(
        user_id=user_id, bot_id=bot_id,
        next_clingy_due_at=now,
    )

    # Run clingy check directly — never fall through to proactive
    now_cst = dt_module.datetime.now(SCHEDULER_CST)
    contact_block_reason = _contact_send_block_reason(contact, bot_id)
    current_bot = bot_manager.current_bot
    prereqs = {
        "bot_active": bot_id == bot_manager.current_bot_id,
        "proactive_enabled": current_bot.proactive_enabled,
        "bridge_available": _bridge is not None and bool(_bridge.token),
        "valid_contact": contact_block_reason == "",
        "contact_block_reason": contact_block_reason,
        "user_id": user_id,
        "bot_id": bot_id,
    }

    decision = await scheduler_agent.run_clingy_check(
        now_ts=now, now_cst=now_cst, prerequisites=prereqs,
        bot_identity=current_bot.prompt_pack.get("bot_identity", "") or read_persona_file(),
        relationship=await memory_store.get_relationship_bot(bot_id),
        stable_memories=await memory_store.get_shared_facts(limit=5),
    )

    if not decision.get("sent"):
        # Restore original due time
        await memory_store.update_clingy_state(
            user_id=user_id, bot_id=bot_id,
            next_clingy_due_at=clingy_state.get("next_clingy_due_at", now),
        )
        return {"status": "blocked", "reason": decision.get("reason", "clingy check blocked"), "stage": decision.get("stage", "")}

    # Clingy passed — send via bridge
    decision_messages = decision.get("messages") or [{"text": decision.get("message", ""), "delay": 0}]
    if not any(str(m.get("text", "")).strip() for m in decision_messages if isinstance(m, dict)):
        return {"status": "blocked", "reason": "empty clingy message"}

    turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    intent = decision.get("intent", "clingy_followup")
    candidate_id = decision.get("candidate_id", "")

    guarded = guard_messages(decision_messages, max_messages=scheduler_agent.max_proactive_messages)
    if not guarded:
        return {"status": "blocked", "reason": "empty after guard"}

    send_status = "delivered"
    send_error = ""
    try:
        await memory_store.record_outbound_messages(
            turn_id=turn_id, messages=guarded, user_id=user_id, bot_id=bot_id,
            source="proactive", author="assistant", trigger="proactive_reason",
            send_status="sending", candidate_id=candidate_id, batch_id=turn_id,
        )
        if _bridge is not None and _bridge.token and context_token:
            await _send_message_batch(user_id=user_id, context_token=context_token, messages=guarded)
        await memory_store.mark_outbound_batch_status(turn_id, "delivered")
    except Exception as e:
        logger.error(f"Force clingy send failed: {e}")
        send_status = "failed"
        send_error = str(e)
        await memory_store.mark_outbound_batch_status(turn_id, "failed", send_error)

    await memory_store.create_turn(
        turn_id=turn_id, user_id=user_id, bot_id=bot_id,
        user_messages="",
        assistant_actions=json.dumps(guarded, ensure_ascii=False),
        tool_calls=json.dumps([], ensure_ascii=False),
        source="proactive", trigger="proactive_reason", send_status=send_status,
    )
    await memory_store.record_turn_diagnostics(
        turn_id=turn_id, user_id=user_id, source="proactive",
        route=ROUTE_CHAT_FULL, context_policy=CTX_HOT_CHEAP,
        llm_status="ok", elapsed_ms=0, send_status=send_status,
        output_messages=len(guarded),
        metadata={"intent": intent, "candidate_id": candidate_id, "batch_id": turn_id,
                   "source": "proactive", "trigger": "proactive_reason", "send_status": send_status},
    )

    if send_status == "delivered":
        await scheduler_agent.commit_proactive_send(
            user_id=user_id, bot_id=bot_id,
            topic_id=decision.get("topic_id", ""), intent=intent,
        )
        if candidate_id:
            await memory_store.update_proactive_candidate(
                candidate_id,
                sent=(send_status == "delivered"),
                turn_id=turn_id,
                batch_id=turn_id,
                send_status=send_status,
            )
    else:
        await memory_store.update_clingy_state(
            user_id=user_id, bot_id=bot_id,
            next_clingy_due_at=clingy_state.get("next_clingy_due_at", now),
        )

    message = "\n".join(m["text"] for m in guarded if m.get("text"))
    return {
        "status": "ok",
        "result": {"sent": send_status == "delivered", "message": message, "intent": intent,
                    "reason": decision.get("reason", ""), "stage": decision.get("stage", "")},
    }


@app.get("/admin/clingy/debug-state")
async def admin_clingy_debug_state(request: Request):
    """Full clingy diagnostics: user_ids, state rows, turn ledger, derived awaiting."""
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    contact_user_id = contact.get("user_id", "")

    # All clingy_state rows
    db = await memory_store._get_db()
    all_rows = await db.execute_fetchall("SELECT * FROM clingy_state")
    all_clingy = [dict(r) for r in (all_rows or [])]

    # Current row
    current_row = await memory_store.get_clingy_state(contact_user_id, bot_id)

    # Last 10 turns
    turns = await memory_store.get_recent_turns(contact_user_id, bot_id, limit=10)
    turns_out = []
    last_delivered_assistant_at = None
    last_real_user_inbound_at = None
    for t in turns:
        created = t.get("created_at")
        source = str(t.get("source", ""))
        send_status = str(t.get("send_status", ""))
        has_user = bool(str(t.get("user_messages", "")).strip())
        has_asst = bool(str(t.get("assistant_actions", "")).strip())
        turns_out.append({
            "id": t.get("id"), "created_at": created,
            "source": source, "send_status": send_status,
            "has_user_msg": has_user, "has_asst_msg": has_asst,
            "user_text": str(t.get("user_messages", ""))[:80],
        })
        if not last_delivered_assistant_at and has_asst and send_status in ("delivered", ""):
            last_delivered_assistant_at = created
        if not last_real_user_inbound_at and has_user and source != "proactive":
            last_real_user_inbound_at = created

    # Last 10 outbound messages
    outbound = await memory_store.get_outbound_messages(user_id=contact_user_id, limit=10)

    # Derived awaiting
    derived = (last_delivered_assistant_at or 0) > (last_real_user_inbound_at or 0)
    stored = bool(current_row.get("awaiting_user_reply"))
    mismatch_reason = ""
    if derived != stored:
        if derived and not stored:
            mismatch_reason = "derived=True stored=False — 消息账本显示 bot 最后发了消息且用户未回，但 clingy_state 未标记"
        else:
            mismatch_reason = "derived=False stored=True — clingy_state 标记等待中，但消息账本显示用户已经回复过"

    return {
        "contact_user_id": contact_user_id,
        "current_bot_id": bot_id,
        "all_clingy_state_rows": all_clingy,
        "current_clingy_row": current_row,
        "last_10_turns": turns_out,
        "last_10_outbound": [{
            "id": o.get("id"), "turn_id": o.get("turn_id"),
            "send_status": o.get("send_status"),
            "text": str(o.get("text", ""))[:80],
            "created_at": o.get("created_at"),
        } for o in (outbound or [])[:10]],
        "last_delivered_assistant_at": last_delivered_assistant_at,
        "last_real_user_inbound_at": last_real_user_inbound_at,
        "derived_awaiting_user_reply": derived,
        "stored_awaiting_user_reply": stored,
        "effective_awaiting": derived or stored,
        "mismatch_reason": mismatch_reason,
        "clingy_config": {
            "clingy_mode_enabled": scheduler_agent.clingy_mode_enabled,
            "clingy_after_minutes": scheduler_agent.clingy_after_minutes,
            "max_followups_per_unanswered": scheduler_agent.max_followups_per_unanswered,
        },
    }


@app.post("/admin/clingy/prime")
async def admin_clingy_prime(request: Request):
    """Debug: manually set awaiting_user_reply so clingy can be tested.

    Simulates "bot sent a message, user hasn't replied yet."
    Sets next_clingy_due_at to now so force-trigger works immediately.
    """
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="没有联系人")

    now = time.time()
    await memory_store.update_clingy_state(
        user_id=user_id, bot_id=bot_id,
        awaiting_user_reply=1,
        last_proactive_sent_at=now,
        last_proactive_intent="check_in",  # use valid intent for debug priming
        next_clingy_due_at=now,
        clingy_followup_count=0,
    )
    return {"status": "ok", "message": "已手动设置 awaiting_user_reply = true，现在可以用强制触发粘人测试"}


# ================================================================
# V2.2: Emotion admin API
# ================================================================

@app.get("/admin/emotion/state")
async def admin_emotion_state(request: Request):
    """Get current bot emotion state for the active user+bot."""
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")

    emo_defaults = {
        "bot_mood": emotion_engine.baseline_mood,
        "affection": emotion_engine.initial_affection,
        "trust": emotion_engine.initial_trust,
        "attachment": emotion_engine.initial_attachment,
    }
    state = await memory_store.get_bot_emotion_state(user_id, bot_id, defaults=emo_defaults)
    events = await memory_store.get_recent_bot_emotion_events(user_id, bot_id, limit=10)

    return {
        "emotion_state": state,
        "recent_events": events,
    }


@app.get("/admin/emotion/events")
async def admin_emotion_events(request: Request, limit: int = 20):
    """Get recent emotion events for the active user+bot."""
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")

    events = await memory_store.get_recent_bot_emotion_events(user_id, bot_id, limit=limit)
    return {"events": events}


@app.post("/admin/emotion/reset")
async def admin_emotion_reset(request: Request):
    """Reset bot emotion state to defaults for the active user+bot."""
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")

    await memory_store.reset_bot_emotion_state(user_id, bot_id)
    return {"status": "ok", "message": "情绪状态已重置"}


@app.post("/admin/emotion/update")
async def admin_emotion_update(request: Request):
    """Manually update bot emotion state fields."""
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")

    body = await request.json()
    allowed = {
        "bot_mood", "mood_intensity", "relationship_stage",
        "affection", "trust", "irritation", "attachment", "jealousy",
        "last_emotion_reason",
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="没有可更新的字段")

    emo_defaults = {
        "bot_mood": emotion_engine.baseline_mood,
        "affection": emotion_engine.initial_affection,
        "trust": emotion_engine.initial_trust,
        "attachment": emotion_engine.initial_attachment,
    }
    current = await memory_store.get_bot_emotion_state(user_id, bot_id, defaults=emo_defaults)
    current.update(updates)
    current["user_id"] = user_id
    current["bot_id"] = bot_id

    await memory_store.upsert_bot_emotion_state(user_id, bot_id, current)
    return {"status": "ok", "updated": list(updates.keys())}


# ================================================================
# V2.2: Dialogue state admin API
# ================================================================

@app.get("/admin/dialogue/state")
async def admin_dialogue_state(request: Request):
    """Get current dialogue wait state for the active user+bot."""
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")

    state = await memory_store.get_dialogue_state(user_id, bot_id)
    events = await memory_store.get_recent_dialogue_state_events(user_id, bot_id, limit=10)

    return {
        "dialogue_state": state,
        "recent_events": events,
    }


@app.get("/admin/dialogue/events")
async def admin_dialogue_events(request: Request, limit: int = 20):
    """Get recent dialogue state events for the active user+bot."""
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")

    events = await memory_store.get_recent_dialogue_state_events(user_id, bot_id, limit=limit)
    return {"events": events}


@app.post("/admin/dialogue/reset")
async def admin_dialogue_reset(request: Request):
    """Reset dialogue state to defaults for the active user+bot."""
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")

    await memory_store.reset_dialogue_state(user_id, bot_id)
    return {"status": "ok", "message": "对话状态已重置"}


@app.post("/admin/dialogue/update")
async def admin_dialogue_update(request: Request):
    """Manually update dialogue state fields."""
    require_admin(request)
    bot_id = bot_manager.current_bot_id
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")

    body = await request.json()
    allowed = {
        "wait_state", "last_state_reason",
        "last_user_message_at", "last_assistant_message_at",
        "last_user_message_excerpt", "last_assistant_message_excerpt",
        "silent_started_at", "silent_reason",
        "user_messages_since_silent", "silent_must_reply_after_count",
        "silent_must_reply_after_ts", "proactive_paused_until",
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="没有可更新的字段")

    current = await memory_store.get_dialogue_state(user_id, bot_id)
    current.update(updates)
    current["user_id"] = user_id
    current["bot_id"] = bot_id

    await memory_store.upsert_dialogue_state(user_id, bot_id, current)
    return {"status": "ok", "updated": list(updates.keys())}


# ================================================================
# Entry point
# ================================================================

if __name__ == "__main__":
    import uvicorn

    host = cfg.get("server", {}).get("host", "0.0.0.0")
    port = int(cfg.get("server", {}).get("port", 8899))

    logger.info(f"Starting server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
