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
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from agent.chat_agent import ChatAgent, ROUTE_SYSTEM_FAST, ROUTE_CHAT_LITE, ROUTE_CHAT_FULL
from agent.chat_agent import CTX_HOT_ONLY, CTX_HOT_CHEAP, CTX_HOT_FULL
from agent.context_builder import compact_history
from agent.memory_worker import MemoryWorker
from agent.output_guard import guard_messages, guard_single_message
from agent.scheduler import SchedulerAgent
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
configure_tools(cfg)

# ================================================================
# Clients and services
# ================================================================

llm_client = AsyncOpenAI(
    api_key=cfg["model"]["api_key"],
    base_url=cfg["model"]["base_url"],
)

model_name = cfg["model"]["name"]
temperature = cfg["model"].get("temperature", 0.7)

memory_store = MemoryStore(
    db_path=cfg["memory"].get("db_path", "./data/memory.db"),
)
configure_runtime(memory_store=memory_store)

chat_agent = ChatAgent(
    client=llm_client,
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
)

memory_worker = MemoryWorker(
    store=memory_store,
    client=llm_client,
    model=model_name,
    min_importance=cfg["memory"].get("min_importance_save", 0.4),
)

scheduler_agent = SchedulerAgent(
    store=memory_store,
    client=llm_client,
    model=model_name,
    config=cfg.get("scheduler"),
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
        poll_timeout_ms=wcfg.get("poll_timeout_ms", 35_000),
        message_delay=wcfg.get("message_delay", 1.2),
        buffer_debounce_seconds=wcfg.get("buffer_debounce_seconds", 5.0),
        buffer_max_wait_seconds=wcfg.get("buffer_max_wait_seconds", 12.0),
    )

# ================================================================
# V2: Per-user turn lock (Phase 4)
# ================================================================

_user_turn_locks: dict[str, asyncio.Lock] = {}


def _get_user_lock(user_id: str) -> asyncio.Lock:
    """Get or create a per-user async lock for turn ordering."""
    if user_id not in _user_turn_locks:
        _user_turn_locks[user_id] = asyncio.Lock()
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

    # SystemFastPath: explicit continuation confirmations
    if stripped in {"继续", "嗯", "好", "哦", "行", "可以", "对", "是的", "ok", "OK", "嗯嗯", "哈哈"}:
        return ROUTE_SYSTEM_FAST

    # ChatLite: short message, no obvious tool/complexity signal
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

# Simple in-memory history store. For multi-user/production,
# replace with a per-user history table in SQLite.
conversation_histories: dict[str, list[dict]] = {}
MAX_HISTORY_TURNS = cfg["conversation"].get("max_history_turns", 20)
COMPACT_AT_TURNS = cfg["conversation"].get("compact_at_turns", 40)


def get_history(user_id: str) -> list[dict]:
    if user_id not in conversation_histories:
        conversation_histories[user_id] = []
    return conversation_histories[user_id]


async def get_history_async(user_id: str) -> list[dict]:
    """Return in-memory history, restoring recent raw history after restart."""
    history = get_history(user_id)
    if history:
        return history
    restored = await memory_store.get_recent_conversation_history(
        user_id,
        limit=MAX_HISTORY_TURNS * 2,
    )
    if restored:
        conversation_histories[user_id] = restored
    return conversation_histories[user_id]


def append_history(user_id: str, role: str, content: str, source: str = "user"):
    history = get_history(user_id)
    history.append({
        "role": role,
        "content": content,
        "ts": time.time(),
        "source": source,
    })

    # Safeguard #9: compact if too long
    if len(history) > COMPACT_AT_TURNS * 2:
        compacted, _ = compact_history(history, MAX_HISTORY_TURNS, COMPACT_AT_TURNS)
        conversation_histories[user_id] = compacted


async def append_history_async(user_id: str, role: str, content: str, source: str = "user"):
    ts = time.time()
    history = get_history(user_id)
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
    )

    if len(history) > COMPACT_AT_TURNS * 2:
        compacted, summary = compact_history(history, MAX_HISTORY_TURNS, COMPACT_AT_TURNS)
        previous = await memory_store.get_history_summary(user_id)
        merged = "\n".join(part for part in [previous, summary] if part).strip()
        if merged:
            await memory_store.save_history_summary(user_id, merged[-4000:])
        conversation_histories[user_id] = compacted


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
            "interval_minutes": cfg.get("scheduler", {}).get("interval_minutes", 30),
            "scheduled_check_seconds": cfg.get("scheduler", {}).get("scheduled_check_seconds", 10),
            "sleep_start": cfg.get("scheduler", {}).get("sleep_start", 0),
            "sleep_end": cfg.get("scheduler", {}).get("sleep_end", 7),
            "min_chat_gap_hours": cfg.get("scheduler", {}).get("min_chat_gap_hours", 2),
            "max_daily_proactive": cfg.get("scheduler", {}).get("max_daily_proactive", 5),
            "min_proactive_interval": cfg.get("scheduler", {}).get("min_proactive_interval", 90),
        },
        "wechat": {
            "message_delay": cfg.get("wechat", {}).get("message_delay", 1.2),
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


def apply_runtime_settings(updates: dict):
    """Persist and apply runtime-editable settings."""
    _deep_update(runtime_settings, updates)
    save_runtime_settings(runtime_settings)
    _deep_update(cfg, updates)
    configure_tools(cfg)
    configure_runtime(memory_store=memory_store)

    scfg = cfg.get("scheduler", {})
    scheduler_agent.sleep_start = scfg.get("sleep_start", 0)
    scheduler_agent.sleep_end = scfg.get("sleep_end", 7)
    scheduler_agent.min_chat_gap_hours = scfg.get("min_chat_gap_hours", 2)
    scheduler_agent.max_ignored_streak = scfg.get("max_ignored_streak", 3)
    scheduler_agent.max_daily = scfg.get("max_daily_proactive", 5)
    scheduler_agent.min_interval = scfg.get("min_proactive_interval", 90)
    chat_agent.defaults = {
        "default_city": cfg.get("weather", {}).get(
            "default_city",
            cfg.get("fixed_messages", {}).get("morning", {}).get("city", ""),
        )
    }

    if _bridge is not None:
        _bridge.message_delay = float(
            cfg.get("wechat", {}).get("message_delay", 1.2)
        )

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
        poll_timeout_ms=wcfg.get("poll_timeout_ms", 35_000),
        message_delay=wcfg.get("message_delay", 1.2),
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
            and contact.get("user_id")
            and contact.get("context_token")
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
    return {"status": "saved", "path": str(path)}


@app.post("/admin/settings")
async def admin_save_settings(request: Request):
    require_admin(request)
    body = await request.json()
    allowed = {}
    for section in ["scheduler", "wechat", "weather", "email"]:
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
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Parse input (compatible with emoRebot format: messages array or single message)
    raw_messages = body.get("messages")
    if raw_messages is None:
        raw_messages = [body.get("message", "")]
    if isinstance(raw_messages, str):
        raw_messages = [raw_messages]
    if not isinstance(raw_messages, list) or not raw_messages:
        raise HTTPException(status_code=400, detail="messages required")

    user_message = "\n".join(str(m) for m in raw_messages[:8])  # Max 8 input messages
    user_id = str(body.get("user_id", "default"))
    context_token = str(body.get("context_token", ""))

    logger.info(f"[{user_id}] Received {len(str(user_message))} chars")

    # V2: Acquire per-user turn lock (Phase 4)
    lock = _get_user_lock(user_id)
    turn_id = ""
    send_status = ""
    memory_enqueued = 0

    async with lock:
        confirmation_result = await handle_pending_persona_confirmation(user_message)
        if confirmation_result:
            await append_history_async(user_id, "user", user_message, source="wechat")
            await append_history_async(
                user_id,
                "assistant",
                _assistant_history_json(confirmation_result),
                source="assistant",
            )
            await memory_store.record_usage_event(
                event_type="chat_turn",
                user_id=user_id,
                source="wechat",
                message_count=len(raw_messages[:8]) + len(confirmation_result.get("messages", [])),
                elapsed_ms=int(confirmation_result.get("elapsed_ms", 0) or 0),
                metadata={"handled_by": "pending_persona_confirmation"},
            )
            return confirmation_result

        # V2: Select route and context policy (Phase 5 + 6)
        route = _select_route(user_message, trigger="user")

        # Retrieve memories
        memories = await memory_store.search(user_message, top_k=5)

        context_policy = _select_context_policy(route, len(memories))

        # Get relationship state
        relationship = await memory_store.get_relationship()

        # Get conversation history
        history = await get_history_async(user_id)
        history_summary = await memory_store.get_history_summary(user_id)

        # Run Chat Agent (V2: with route and context_policy)
        result = await chat_agent.chat(
            user_message=user_message,
            memories=memories,
            history=history,
            relationship=relationship,
            history_summary=history_summary,
            trigger="user",
            route=route,
            context_policy=context_policy,
        )

        turn_id = result["turn_id"]

        # Remember the latest reachable contact
        if user_id and user_id != "default" and context_token:
            await memory_store.update_contact_state(user_id, context_token)

        # V2: Persist turn draft BEFORE sending (Phase 3)
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
        await append_history_async(user_id, "user", user_message, source="wechat")
        assistant_text = "\n".join(
            m["text"] for m in result["messages"] if m.get("text")
        )
        if result["messages"] or result.get("scheduled_messages"):
            await append_history_async(
                user_id,
                "assistant",
                _assistant_history_json(result),
                source="assistant",
            )

        # Fire-and-forget memory extraction
        if len(user_message.strip()) >= 10 and assistant_text.strip():
            asyncio.create_task(
                memory_worker.extract(user_message, assistant_text, memories)
            )
            memory_enqueued = 1

        # Update scheduler state
        await scheduler_agent.record_chat_activity()

        # V2: Record turn diagnostics (Phase 2)
        await memory_store.record_turn_diagnostics(
            turn_id=turn_id,
            user_id=user_id,
            source="wechat",
            route=route,
            context_policy=context_policy,
            llm_status=result.get("llm_status", "ok"),
            token_count=result_token_count(result),
            elapsed_ms=int(result.get("elapsed_ms", 0) or 0),
            fallback_type=result_fallback_type(result),
            send_status="generated",
            memory_enqueued=memory_enqueued,
            input_chars=len(user_message),
            output_messages=len(result.get("messages", [])),
            tool_calls=len(result.get("tool_calls", [])),
        )

        # Also record usage event (keep backward compat)
        await memory_store.record_usage_event(
            event_type="chat_turn",
            user_id=user_id,
            source="wechat",
            message_count=len(raw_messages[:8]) + len(result.get("messages", [])),
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

    # Lock released — now send messages and mark draft status
    send_status = "delivered"
    if _bridge is not None and _bridge.token and context_token:
        try:
            await _send_message_batch(
                user_id=user_id,
                context_token=context_token,
                messages=result["messages"],
            )
        except Exception as e:
            logger.error(f"[{user_id}] Send failed for turn {turn_id}: {e}")
            send_status = "failed"
            await memory_store.mark_turn_draft_status(turn_id, "failed", str(e)[:500])
    else:
        # No bridge — messages will be picked up by poller's reply path
        send_status = "delivered"

    if send_status == "delivered":
        await memory_store.mark_turn_draft_status(turn_id, "delivered")

    # Update turn diagnostics with final send status
    await memory_store.record_turn_diagnostics(
        turn_id=turn_id,
        user_id=user_id,
        send_status=send_status,
    )

    return {
        "messages": [
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
    """Get conversation history for a user."""
    require_admin(request)
    history = await get_history_async(user_id)
    return {"history": history[-20:]}  # Last 20 messages


@app.delete("/history/{user_id}")
async def clear_history(user_id: str, request: Request):
    """Clear conversation history for a user."""
    require_admin(request)
    conversation_histories[user_id] = []
    deleted = await memory_store.clear_conversation_history(user_id)
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


# ================================================================
# Background tasks
# ================================================================


async def scheduler_tick() -> dict:
    """Periodic scheduler check. Called by APScheduler or manual trigger.

    V2: acquires per-user lock before sending proactive messages.
    """
    try:
        memories = await memory_store.search("", top_k=5)
        decision = await scheduler_agent.should_proactive_message(memories)

        if decision.get("should_message"):
            msg = decision.get("message", "")
            emotion = decision.get("emotion", "neutral")
            reason = decision.get("reason", "")
            contact = await memory_store.get_contact_state()
            user_id = contact.get("user_id", "")
            if not user_id:
                logger.info("Scheduler: skip — no contact target yet")
                return {"sent": False, "reason": "no contact target yet"}
            if _bridge is None or not _bridge.token:
                logger.info("Scheduler: skip — WeChat bridge unavailable")
                return {"sent": False, "reason": "WeChat bridge unavailable"}

            # V2: Acquire per-user lock
            lock = _get_user_lock(user_id)
            async with lock:
                send_status = "delivered"
                try:
                    guarded = guard_messages(
                        [{"text": msg, "delay": 0}],
                        max_messages=1,
                    )
                    await _send_message_batch(
                        user_id=user_id,
                        context_token=contact.get("context_token", ""),
                        messages=guarded,
                    )
                except Exception as e:
                    logger.error(f"Proactive send failed: {e}")
                    send_status = "failed"

                # V2: Record turn diagnostics
                turn_id = f"turn_{uuid.uuid4().hex[:12]}"
                await memory_store.record_turn_diagnostics(
                    turn_id=turn_id,
                    user_id=user_id,
                    source="scheduler",
                    route=ROUTE_CHAT_FULL,
                    context_policy=CTX_HOT_CHEAP,
                    llm_status="ok",
                    elapsed_ms=0,
                    send_status=send_status,
                    output_messages=1,
                )

            await memory_store.record_usage_event(
                event_type="proactive_send",
                user_id=user_id,
                source="scheduler",
                message_count=1,
                metadata={"reason": reason, "emotion": emotion},
            )
            await append_history_async(
                user_id,
                "system",
                f"[主动消息判断] {reason}",
                source="proactive",
            )
            await append_history_async(
                user_id,
                "assistant",
                _assistant_history_json({"messages": [{"text": msg, "delay": 0}]}),
                source="proactive",
            )
            await scheduler_agent.record_sent()
            logger.info(f"Scheduler: SENT — {reason}")

            return {
                "sent": True,
                "message": msg,
                "emotion": emotion,
                "reason": reason,
            }
        else:
            reason = decision.get("reason", "unknown")
            logger.info(f"Scheduler: skip — {reason}")
            return {
                "sent": False,
                "reason": reason,
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
        f"现在是早上8点。主动给辣椒发早安消息。"
        f"天气工具结果：{weather_result}。"
        "基于这个真实结果，用几条短微信说早安、天气、今天可以做的一两件小事。"
        "不要再调用 get_weather，不要编造工具没有返回的天气细节。"
    )
    return await _run_fixed_message_prompt(
        prompt,
        save_diary=False,
        fallback_messages=_morning_fallback_messages(city, weather_result),
    )


async def fixed_night_recap() -> dict:
    prompt = (
        "现在是晚上11点。请从AI的角度写一小段今天的陪伴日记并发给辣椒。"
        "基于今天真实聊过的内容，说我今天和你聊到了什么、我看见了什么状态、"
        "我想轻轻替你记住什么。不要冒充用户本人，不要编造没聊过的现实事实。"
        "200字以内，像随手写日记。"
    )
    return await _run_fixed_message_prompt(
        prompt,
        save_diary=True,
        fallback_messages=[{"text": "今天先收住 明天再慢慢来", "delay": 0}],
    )


async def _run_fixed_message_prompt(
    prompt: str,
    save_diary: bool,
    fallback_messages: list[dict] | None = None,
) -> dict:
    contact = await memory_store.get_contact_state()
    if not contact.get("user_id"):
        logger.info("Fixed message skipped — no contact target yet")
        return {"sent": False, "reason": "no contact target yet"}
    if _bridge is None or not _bridge.token:
        logger.info("Fixed message skipped — WeChat bridge unavailable")
        return {"sent": False, "reason": "WeChat bridge unavailable"}

    user_id = contact["user_id"]

    # V2: Acquire per-user lock for fixed messages too
    lock = _get_user_lock(user_id)
    send_status = "delivered"

    async with lock:
        memories = await memory_store.search(prompt, top_k=5)
        relationship = await memory_store.get_relationship()
        history = await get_history_async(user_id)
        history_summary = await memory_store.get_history_summary(user_id)

        # V2: Route and context policy for fixed messages
        route = ROUTE_CHAT_FULL
        context_policy = CTX_HOT_FULL

        result = await chat_agent.chat(
            user_message=prompt,
            memories=memories,
            history=history,
            relationship=relationship,
            history_summary=history_summary,
            trigger="proactive",
            route=route,
            context_policy=context_policy,
        )
        turn_id = result["turn_id"]

        messages = [
            {"text": m["text"], "delay": m.get("delay", 0)}
            for m in result.get("messages", [])
            if m.get("text")
        ]
        if not messages:
            messages = fallback_messages or [{"text": "我刚刚卡了一下 重新说", "delay": 0}]

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
            await _send_message_batch(
                user_id=user_id,
                context_token=contact.get("context_token", ""),
                messages=guarded,
            )
        except Exception as e:
            logger.error(f"Fixed message send failed: {e}")
            send_status = "failed"
            await memory_store.mark_turn_draft_status(turn_id, "failed", str(e)[:500])

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
        "system",
        f"[固定主动消息] {prompt}",
        source="proactive",
    )
    await append_history_async(
        user_id,
        "assistant",
        _assistant_history_json(result),
        source="proactive",
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
        middle = f"{city}天气我这边没查稳 出门前看一眼"
    else:
        middle = weather_text[:80]
    return [
        {"text": "早", "delay": 0},
        {"text": middle, "delay": 1},
        {"text": "今天先慢慢开机", "delay": 1},
    ]


async def deliver_scheduled_messages() -> dict:
    """Send due one-shot scheduled messages from SQLite."""
    if _bridge is None or not _bridge.token:
        return {"sent": 0, "reason": "WeChat bridge unavailable"}

    tasks = await memory_store.get_due_scheduled_messages(limit=20)
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

    V2: all messages pass through UnifiedOutputGuard before sending.
    """
    if _bridge is None:
        raise RuntimeError("WeChat bridge is not configured")
    if not context_token:
        raise RuntimeError("missing iLink context_token")

    # V2: guard all messages before sending
    guarded = guard_messages(messages)

    min_gap = float(cfg.get("wechat", {}).get("message_delay", 1.2) or 1.2)
    for i, m in enumerate(guarded):
        text = guard_single_message(m.get("text", ""))
        if not text:
            continue
        delay = float(m.get("delay", 0) or 0)
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


async def daily_reset():
    """Reset daily counters at midnight."""
    await scheduler_agent.reset_daily_counter()
    logger.info("Daily scheduler counter reset")


async def memory_decay():
    """Apply time-based importance decay to memories."""
    await memory_store.apply_decay()
    logger.info("Memory decay applied")


# ================================================================
# Entry point
# ================================================================

if __name__ == "__main__":
    import uvicorn

    host = cfg.get("server", {}).get("host", "0.0.0.0")
    port = int(cfg.get("server", {}).get("port", 8899))

    logger.info(f"Starting server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
