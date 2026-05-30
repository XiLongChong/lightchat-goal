"""Goal Chat Agent — main entry point.

A lightweight, privacy-preserving chat agent with:
  - Multi-message output (JSON format, DeepSeek-compatible)
  - Simple tool calling (weather, time)
  - Long-term memory (SQLite + CJK bigram retrieval)
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
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from agent.chat_agent import ChatAgent, ROUTE_SYSTEM_FAST, ROUTE_CHAT_LITE, ROUTE_CHAT_FULL
from agent.chat_agent import CTX_HOT_ONLY, CTX_HOT_CHEAP, CTX_HOT_FULL
from agent.context_builder import compact_history, build_chat_system_prompt
from agent.llm_gateway import LLMGateway
from agent.llm_types import LLMRequest
from agent.output_guard import guard_messages, guard_single_message
from agent.state_machine import TurnResult, TurnState
from agent.tools import configure_runtime, configure_tools, execute_tool, compute_message_delay
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
BOT_ID = "default"  # Phase 1: single persona, no multi-bot


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
    # Phase 1: force re-read persona.md on next turn
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

# Register LLM trace callback
async def _on_llm_trace(trace: dict):
    try:
        await memory_store.save_llm_trace(trace)
    except Exception:
        pass

llm_gateway.on_trace(_on_llm_trace)

# Phase 1: Single persona (no multi-bot, no memory_worker)
def _read_persona() -> str:
    path = Path(cfg.get("agent", {}).get("persona_file", "./prompts/persona.md"))
    if path.exists():
        return path.read_text(encoding="utf-8")
    return cfg["agent"].get("persona", "")

def _read_personality() -> str:
    return cfg["agent"].get("personality", "")

_feature_on = lambda name: (
    (cfg.get("features") or {}).get(name, False) is True
)

chat_agent = ChatAgent(
    model=model_name,
    persona=_read_persona(),           # persona.md file content
    personality=_read_personality(),    # cfg personality extension (usually empty)
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
    json_output_enabled=False,  # Phase 1: JSON actions disabled
    test_mode=cfg.get("wechat", {}).get("send_delay", {}).get("test_mode_zero_delay", False),
)

# v0.2 Phase 5: Memory hook — wires MemoryStore + MemoryWorker into
# LOAD_MEMORY_CONTEXT and UPDATE_MEMORY positions.
# Controlled by features.memory flag (default false).
memory_hook = None
if _feature_on("memory"):
    from agent.memory_worker import MemoryWorker
    from agent.memory_hook import MemoryHook

    memory_worker = MemoryWorker(
        store=memory_store,
        model=model_name,
        min_importance=cfg["memory"].get("min_importance_save", 0.4),
        prompts_dir=cfg.get("prompt_pack", {}).get("directory", "./prompts"),
        llm_gateway=llm_gateway,
    )
    memory_hook = MemoryHook(
        store=memory_store,
        worker=memory_worker,
        bot_id=BOT_ID,
        max_context_memories=cfg["memory"].get("max_context_memories", 3),
    )
    logger.info("Memory hook: enabled (LOAD_MEMORY_CONTEXT + UPDATE_MEMORY)")
else:
    memory_worker = None
    memory_hook = None

# v0.2: System prompt builder — minimal, no memory/recap/relationship.
# Memory context is loaded separately via build_enriched_system_prompt().
def build_system_prompt_for_turn() -> str:
    """Build minimal system prompt for a chat turn."""
    persona = _read_persona()
    personality = _read_personality()
    default_city = cfg.get("weather", {}).get("default_city", "")
    tool_names = cfg.get("model", {}).get("native_tool_calls", {}).get(
        "allowed_tools", ["get_time", "get_weather", "send_wechat_messages"]
    )
    return build_chat_system_prompt(
        persona=persona,
        personality=personality,
        default_city=default_city,
        tool_names=tool_names,
    )


async def build_enriched_system_prompt(user_id: str, user_message: str) -> str:
    """Build system prompt, optionally enriched with memory context."""
    prompt = build_system_prompt_for_turn()
    if memory_hook:
        try:
            mem_ctx = await memory_hook.load_context(user_id, user_message)
            if mem_ctx:
                prompt = prompt + "\n\n" + mem_ctx
        except Exception:
            logger.warning("Memory context load failed, continuing without", exc_info=True)
    return prompt

# v0.2: wechat_sender callback — pure WeChat transport, no guard logic.
# Messages arrive already guarded (by state machine GUARD_OUTPUT or
# _send_message_batch guard_messages).  This callback only handles
# transport: delay, send, status tracking.
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
            m["send_status"] = "failed"
            m["send_error"] = str(e)[:200]

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
    )

# ================================================================
# V2: Per-user turn lock (Phase 4)
# TODO: single-process only; multi-process / multi-replica needs a distributed lock
# ================================================================

_user_turn_locks: dict[str, asyncio.Lock] = {}
_user_turn_lock_access: dict[str, float] = {}
_MAX_USER_TURN_LOCKS = 512
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]+$")

# ── v1.2: Interrupt checker — track latest message per user ───
# Maps user_id → timestamp (seconds) of the most recent inbound message.
_user_latest_message_ts: dict[str, float] = {}


def _record_user_message_arrival(user_id: str) -> None:
    """Record that a message arrived for user_id right now."""
    _user_latest_message_ts[user_id] = time.time()


def _make_interrupt_checker(user_id: str):
    """Return an InterruptChecker for the given user.

    Format of interrupt_token:  it_{turn_id}_{timestamp_ms}
    The checker extracts the turn's creation timestamp and compares it
    against the latest message arrival time for this user.

    Returns a callable: (interrupt_token: str) -> bool
    """
    # Capture the user_id in closure
    def _check(interrupt_token: str) -> bool:
        try:
            # Extract timestamp from token:  it_xxxxxxxxxxxx_1234567890123
            parts = interrupt_token.rsplit("_", 1)
            if len(parts) != 2:
                return False
            turn_start_ms = int(parts[1])
            turn_start_s = turn_start_ms / 1000.0
        except (ValueError, IndexError):
            return False

        latest = _user_latest_message_ts.get(user_id, 0)
        # If a message arrived after the turn started, interrupt
        return latest > turn_start_s

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
    - ChatFull: complex input or tool likely
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
        "admin": "后台手动",
    }.get(source or "", source or "未知")


async def build_admin_event_dashboard(limit: int = 20) -> dict[str, Any]:
    """Build a compact admin dashboard without proactive/emotion state."""
    limit = max(5, min(int(limit or 20), 80))
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")
    bot_id = BOT_ID

    turns = await memory_store.get_recent_turns(user_id, bot_id, limit=limit) if user_id else []
    diagnostics = await memory_store.get_recent_turn_diagnostics(limit=limit, user_id=user_id) if user_id else []
    diag_by_turn = {d.get("turn_id"): d for d in diagnostics}
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
        actions = _safe_json_list(turn.get("assistant_actions"))
        tools = _safe_json_list(turn.get("tool_calls"))
        replies = [
            str(a.get("text", "")).strip()
            for a in actions
            if isinstance(a, dict) and str(a.get("text", "")).strip()
        ]
        events.append({
            "id": turn_id,
            "kind": "turn",
            "time": _admin_time(turn.get("created_at")),
            "created_at": turn.get("created_at", 0),
            "title": "chat turn",
            "user_text": turn.get("user_messages", ""),
            "intent": "",
            "is_proactive": False,
            "reply_texts": replies,
            "tool_calls": tools,
            "source": _source_label(turn.get("source", "")),
            "raw_source": turn.get("source", ""),
            "trigger": turn.get("trigger", ""),
            "batch_id": turn_id,
            "candidate_id": "",
            "skip_reply": (
                bool(diag_meta.get("skip_reply"))
                or diag_meta.get("terminal_state") in ("end_skipped", "TurnState.END_SKIPPED")
            ),
            "skip_reason": diag_meta.get("skip_reason", ""),
            "terminal_state": diag_meta.get("terminal_state", ""),
            "context_sample": diag_meta.get("context_sample", []),
            "outbound_messages": outbound_by_turn.get(turn_id, []),
            "route": _route_label(diag.get("route", "")),
            "context_policy": _policy_label(diag.get("context_policy", "")),
            "elapsed_ms": diag.get("elapsed_ms", 0),
            "token_count": diag.get("token_count", 0),
            "send_status": _send_status_label(diag.get("send_status") or turn.get("send_status", "")),
            "memory_enqueued": bool(diag.get("memory_enqueued", 0)),
        })

    events.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)

    return {
        "contact": contact,
        "bot": {
            "current_bot_id": bot_id,
            "display_name": "bot",
        },
        "events": events[:limit],
        "turns": turns[-limit:],
        "outbound_messages": outbound,
        "memory": {
            "recaps": recaps,
            "shared_facts": shared_facts,
        },
    }

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

    # Start WeChat bridge if configured
    if _bridge is not None and _bridge.token:
        await _bridge.start()

    yield

    # Shutdown
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
    current_bot_id = BOT_ID
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
    # Phase 1: force re-read persona.md on next turn
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
    await memory_store.record_usage_event(
        event_type="manual_send",
        user_id=contact["user_id"],
        source="admin",
        message_count=len(messages),
    )
    return {"status": "sent", "count": len(messages)}


# ================================================================
# v1.2 Phase 5A: Extracted model-history writer
# ================================================================

_IS_DELIVERED = {"delivered", "sent_confirmed"}


async def _write_model_history(
    result: TurnResult,
    user_id: str,
    bot_id: str,
    user_message: str,
) -> None:
    """Write model-visible history according to terminal state.

    v1.2 §15.1 history matrix:
    - END_SENT / END_PARTIAL_SENT → user + SENT_CONFIRMED assistant
    - END_FALLBACK_SENT → user, assistant:fallback if text exists
    - END_SKIPPED / END_FAILED / END_CANCELLED → user only (no assistant)
    """
    await append_history_async(user_id, "user", user_message, source="wechat", bot_id=bot_id)

    if result.was_sent:
        delivered_texts = [
            m["text"] for m in result.visible_messages
            if (m.get("send_status") in _IS_DELIVERED
                or m.get("delivery_status") in _IS_DELIVERED)
            and m.get("text")
        ]
        if delivered_texts:
            await append_history_async(
                user_id, "assistant", "\n".join(delivered_texts),
                source="assistant", bot_id=bot_id,
            )
    elif result.was_fallback:
        if result.visible_messages:
            fallback_texts = [
                m["text"] for m in result.visible_messages if m.get("text")
            ]
            if fallback_texts:
                await append_history_async(
                    user_id, "assistant", "\n".join(fallback_texts),
                    source="assistant:fallback", bot_id=bot_id,
                )
    # END_SKIPPED / END_FAILED / END_CANCELLED: no assistant history


# ================================================================
# v1.2 Phase 5B: Extracted post-turn hooks
# ================================================================

async def _hook_update_summary(
    result: TurnResult,
    user_id: str,
    bot_id: str,
    **__: Any,
) -> None:
    """UPDATE_SUMMARY — placeholder. Summary is handled by separate recap trigger."""
    pass


async def _hook_update_memory(
    result: TurnResult,
    user_id: str,
    user_message: str,
    memory_hook: Any,
    **__: Any,
) -> None:
    """UPDATE_MEMORY — fire-and-forget memory extraction."""
    if not memory_hook:
        return
    assistant_text = "\n".join(
        m["text"] for m in result.visible_messages
        if m.get("text")
        and (m.get("send_status") in _IS_DELIVERED
             or m.get("delivery_status") in _IS_DELIVERED)
    )
    if assistant_text:
        await memory_hook.update(
            user_message=user_message,
            assistant_text=assistant_text,
            user_id=user_id,
        )


async def _hook_update_emotion(
    result: TurnResult,
    **__: Any,
) -> None:
    """UPDATE_EMOTION — placeholder for future emotion engine."""
    pass


async def _hook_write_dev_logs(
    result: TurnResult,
    turn_id: str,
    user_id: str,
    bot_id: str,
    route: str,
    context_policy: str,
    user_message: str,
    raw_messages: list[str],
    send_status: str,
    memory_store: Any,
    **__: Any,
) -> None:
    """WRITE_DEV_LOGS — turn diagnostics, usage events, draft/outbound status."""
    diag_fallback = ",".join(
        e.get("type", "") for e in (result.fallback_events or [])[:3]
    ) if result.fallback_events else ""
    token_count = result.token_usage.get("total_tokens", 0) if result.token_usage else 0

    await memory_store.record_turn_diagnostics(
        turn_id=turn_id,
        user_id=user_id,
        source="wechat",
        route=route,
        context_policy=context_policy,
        llm_status=result.llm_status,
        token_count=token_count,
        elapsed_ms=result.elapsed_ms,
        fallback_type=diag_fallback,
        send_status=send_status,
        memory_enqueued=0,
        input_chars=len(user_message),
        output_messages=result.delivered_count,
        tool_calls=len(result.tool_results),
        metadata={
            "terminal_state": str(result.terminal_state),
            "context_sample": result.context_sample,
        },
    )

    await memory_store.record_usage_event(
        event_type="chat_turn",
        user_id=user_id,
        source="wechat",
        message_count=len(raw_messages) + result.delivered_count,
        token_count=token_count,
        elapsed_ms=result.elapsed_ms,
        fallback_type=diag_fallback,
        metadata={
            "input_chars": len(user_message),
            "output_messages": result.delivered_count,
            "tool_calls": len(result.tool_results),
            "token_estimated": bool(
                (result.token_usage or {}).get("estimated")
            ),
            "turn_id": turn_id,
            "route": route,
            "context_policy": context_policy,
            "terminal_state": str(result.terminal_state),
        },
    )

    # Mark draft / outbound status
    if result.was_skipped:
        await memory_store.mark_turn_draft_status(turn_id, "skipped")
    elif result.was_cancelled:
        await memory_store.mark_turn_draft_status(turn_id, "cancelled")
    elif result.was_sent or result.was_fallback:
        await memory_store.mark_turn_draft_status(turn_id, "delivered")
        await memory_store.mark_outbound_batch_status(turn_id, "delivered")
    elif result.was_failed:
        await memory_store.mark_turn_draft_status(turn_id, "failed")
    else:
        await memory_store.mark_turn_draft_status(turn_id, "generated")

    await memory_store.update_turn_send_status(turn_id, send_status)


async def _hook_quality_metrics(
    result: TurnResult,
    **__: Any,
) -> None:
    """QUALITY_METRICS — placeholder for future quality tracking."""
    pass


async def _run_all_post_turn_hooks(
    result: TurnResult,
    turn_id: str,
    user_id: str,
    bot_id: str,
    route: str,
    context_policy: str,
    user_message: str,
    raw_messages: list[str],
    send_status: str,
    memory_hook: Any,
    memory_store: Any,
) -> None:
    """Execute all post-turn hooks in order. Individual failures do not block others."""
    hooks = [
        ("UPDATE_SUMMARY", _hook_update_summary),
        ("UPDATE_MEMORY", _hook_update_memory),
        ("UPDATE_EMOTION", _hook_update_emotion),
        ("WRITE_DEV_LOGS", _hook_write_dev_logs),
        ("QUALITY_METRICS", _hook_quality_metrics),
    ]
    common = dict(
        result=result, turn_id=turn_id, user_id=user_id, bot_id=bot_id,
        route=route, context_policy=context_policy, user_message=user_message,
        raw_messages=raw_messages, send_status=send_status,
        memory_hook=memory_hook, memory_store=memory_store,
    )
    for name, hook in hooks:
        try:
            await hook(**common)
        except Exception:
            logger.exception("post_turn_hook %s failed", name)


@app.post("/chat")
async def chat(request: Request):
    """Phase 1 chat endpoint — native tool calls primary.

    Compatible with emoRebot bridge:
      POST /chat {"messages": ["你好"], "user_id": "wxid_xxx"}

    Returns:
      {"messages": [{"text": "...", "delay": 1.0}, ...], "done": true}
    """
    api_cfg = _api_config()
    bot_id = "default"  # Phase 1: single persona, no multi-bot

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

    # ── v1.2: Record message arrival for interrupt checking ──
    _record_user_message_arrival(user_id) if inbound_trigger == "user" else None
    inbound_source = str(body.get("source") or "wechat")

    logger.info(f"[{user_id}] Received {len(str(user_message))} chars")

    lock = _get_user_lock(user_id)
    turn_id = ""
    send_status = ""
    memory_enqueued = 0

    try:
        async with lock:
            # Phase 1: pending persona confirmation (single persona)
            confirmation_result = await handle_pending_persona_confirmation(user_message)
            if confirmation_result:
                await append_history_async(user_id, "user", user_message, source="wechat", bot_id=bot_id)
                confirm_visible = "\n".join(
                    m["text"] for m in confirmation_result.get("messages", []) if m.get("text")
                )
                await append_history_async(
                    user_id, "assistant", confirm_visible,
                    source="assistant", bot_id=bot_id,
                )
                await memory_store.record_usage_event(
                    event_type="chat_turn", user_id=user_id, source="wechat",
                    message_count=len(raw_messages) + len(confirmation_result.get("messages", [])),
                    elapsed_ms=int(confirmation_result.get("elapsed_ms", 0) or 0),
                    metadata={"handled_by": "pending_persona_confirmation"},
                )
                return confirmation_result

            route = _select_route(user_message, trigger="user")
            context_policy = CTX_HOT_ONLY  # Phase 1: no memory, hot context only

            # Short-term history only (Phase 1: no memory search, no recap, no relationship)
            history = await get_history_async(user_id, bot_id=bot_id)

            # Build system prompt (with memory context if enabled)
            system_prompt = await build_enriched_system_prompt(user_id, user_message)

            # Build wechat_sender bound to this user/context
            async def _bound_sender(messages: list[dict]) -> list[dict]:
                return await wechat_sender(user_id, context_token, messages)

            # Run Chat Agent (v1.2 state machine)
            interrupt_checker = _make_interrupt_checker(user_id)
            result: TurnResult = await chat_agent.chat(
                user_message=user_message,
                history=history,
                trigger="user",
                route=route,
                context_policy=context_policy,
                bot_id=bot_id,
                system_prompt=system_prompt,
                wechat_sender=_bound_sender,
                interrupt_checker=interrupt_checker,
            )

            turn_id = result.turn_id

            # ── Determine send_status from terminal_state ─────────
            match result.terminal_state:
                case TurnState.END_SENT | TurnState.END_PARTIAL_SENT:
                    send_status = "delivered"
                case TurnState.END_SKIPPED:
                    send_status = "skipped"
                case TurnState.END_FALLBACK_SENT:
                    send_status = "fallback_sent"
                case TurnState.END_FAILED:
                    send_status = "failed"
                case TurnState.END_CANCELLED:
                    send_status = "cancelled"
                case _:
                    send_status = "generated"

            # ── Record turn (L2) ──────────────────────────────────
            assistant_actions_json = json.dumps(
                [{"text": m.get("text", ""), "delay": m.get("delay", 0)}
                 for m in result.visible_messages],
                ensure_ascii=False,
            )
            asyncio.create_task(
                memory_store.create_turn(
                    turn_id=turn_id,
                    user_id=user_id,
                    bot_id=bot_id,
                    user_messages=user_message,
                    assistant_actions=assistant_actions_json,
                    tool_calls=json.dumps(result.tool_results, ensure_ascii=False),
                    source=result.source,
                    trigger=result.trigger,
                    send_status=send_status,
                )
            )
            if result.visible_messages:
                await memory_store.record_outbound_messages(
                    turn_id=turn_id,
                    messages=result.visible_messages,
                    user_id=user_id,
                    bot_id=bot_id,
                    source=result.source,
                    author=result.author,
                    trigger=result.trigger,
                )

            # Remember the latest reachable contact
            if user_id and user_id != "default" and context_token:
                await memory_store.update_contact_state(
                    user_id, context_token, bot_id=bot_id,
                )

            await memory_store.create_turn_draft(
                turn_id=turn_id, user_id=user_id,
                messages=result.visible_messages,
                source="wechat", route=route,
            )

            scheduled_task_ids: list[str] = []

            # ── v1.2 Phase 5A: Write model history ─────────────────
            await _write_model_history(result, user_id, bot_id, user_message)

            # ── v1.2 Phase 5B: Post-turn hooks ─────────────────────
            await _run_all_post_turn_hooks(
                result=result,
                turn_id=turn_id,
                user_id=user_id,
                bot_id=bot_id,
                route=route,
                context_policy=context_policy,
                user_message=user_message,
                raw_messages=raw_messages,
                send_status=send_status,
                memory_hook=memory_hook,
                memory_store=memory_store,
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
        # Return empty so the bridge poller doesn't double-send.
        return {
            "messages": [],
            "tool_calls": result.tool_results,
            "scheduled_task_ids": scheduled_task_ids,
            "iterations": result.iterations,
            "elapsed_ms": result.elapsed_ms,
            "turn_id": turn_id,
            "terminal_state": str(result.terminal_state),
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
    history = await get_history_async(user_id, bot_id=BOT_ID)
    return {"history": history[-20:]}  # Last 20 messages


@app.delete("/history/{user_id}")
async def clear_history(user_id: str, request: Request):
    """Clear conversation history for a user and current bot."""
    require_admin(request)
    key = _history_key(user_id, BOT_ID)
    conversation_histories[key] = []
    deleted = await memory_store.clear_conversation_history(user_id, bot_id=BOT_ID)
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


@app.get("/admin/bots")
async def admin_list_bots(request: Request):
    """Phase 1: Single persona only."""
    require_admin(request)
    persona = read_persona_file()
    return {
        "current_bot_id": BOT_ID,
        "bots": {
            BOT_ID: {
                "display_name": "bot",
                "identity_file": str(cfg.get("agent", {}).get("persona_file", "./prompts/persona.md")),
                "enabled_tools": cfg.get("model", {}).get("native_tool_calls", {}).get("allowed_tools", []),
                "is_current": True,
                "recap": "",
            }
        },
    }


@app.post("/admin/bots/switch")
async def admin_switch_bot(request: Request):
    """Phase 1: No-op — single persona only."""
    require_admin(request)
    body = await request.json()
    bot_id = str(body.get("bot_id", "")).strip()
    if bot_id and bot_id != BOT_ID:
        raise HTTPException(status_code=400, detail=f"Phase 1: only '{BOT_ID}' persona is available")
    return {
        "status": "ok",
        "current_bot_id": BOT_ID,
        "bot": {"display_name": "bot"},
    }


@app.get("/admin/recaps")
async def admin_list_recaps(request: Request, limit: int = 10):
    """List recent recaps for the current bot and known contact."""
    require_admin(request)
    contact = await memory_store.get_contact_state()
    user_id = contact.get("user_id", "")
    recaps = await memory_store.get_recaps(
        user_id, BOT_ID, limit=limit
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
        user_id, BOT_ID, recap_cfg
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
        user_id, BOT_ID, limit=limit
    )
    return {"turns": turns}


@app.get("/admin/shared-facts")
async def admin_list_shared_facts(request: Request, limit: int = 20):
    """List shared user facts."""
    require_admin(request)
    facts = await memory_store.get_shared_facts(limit=limit)
    return {"shared_facts": facts}


# ================================================================
# Contact send helpers
# ================================================================


def _is_real_user_inbound(user_id: str, text: str, trigger: str, source: str) -> bool:
    """Return whether an inbound event is a real user message."""
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


