"""SQLite persistence — contact state, turns, raw events, context snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import aiosqlite

_SCHEMA_ID = "v3_daily_compression"


def datetime_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class MemoryStore:
    _instances: set["MemoryStore"] = set()

    def __init__(self, db_path: str = "./data/memory.db") -> None:
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None
        self._init_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._instances.add(self)

    # ── lifecycle ──────────────────────────────────────────────────

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is None:
            async with self._init_lock:
                if self._db is None:
                    db = await aiosqlite.connect(str(self.db_path), timeout=30)
                    db.row_factory = aiosqlite.Row
                    await db.execute("PRAGMA busy_timeout=30000")
                    await db.execute("PRAGMA foreign_keys=ON")
                    if str(self.db_path) != ":memory:":
                        await db.execute("PRAGMA journal_mode=WAL")
                    await self._migrate(db)
                    self._db = db
        return self._db

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS runtime_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = await db.execute_fetchall(
            "SELECT value FROM runtime_meta WHERE key = 'runtime_schema'"
        )
        current = row[0]["value"] if row else ""
        if current != _SCHEMA_ID:
            await self._reset(db)
        await self._create_tables(db)
        await self._ensure_turn_metric_columns(db)
        await self._ensure_app_message_remote_agent_id(db)
        await self._ensure_app_message_attachments_json(db)
        await db.execute(
            "INSERT OR REPLACE INTO runtime_meta(key, value) VALUES('runtime_schema', ?)",
            (_SCHEMA_ID,),
        )
        await db.commit()

    async def _reset(self, db: aiosqlite.Connection) -> None:
        rows = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        keep = {"runtime_meta", "bot_contact_state"}
        for row in rows:
            name = str(row["name"])
            if name not in keep:
                await db.execute(f'DROP TABLE IF EXISTS "{name}"')

    async def _create_tables(self, db: aiosqlite.Connection) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS bot_contact_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                user_id TEXT NOT NULL DEFAULT '',
                context_token TEXT NOT NULL DEFAULT '',
                bot_id TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                bot_id TEXT NOT NULL DEFAULT '',
                trim_marker_turn INTEGER NOT NULL DEFAULT 0,
                context_snapshot_id TEXT NOT NULL DEFAULT '',
                last_compressed_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                turn_number INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                token_count INTEGER NOT NULL DEFAULT 0,
                prompt_tokens_peak INTEGER NOT NULL DEFAULT 0,
                context_window_tokens INTEGER NOT NULL DEFAULT 0,
                model_name TEXT NOT NULL DEFAULT '',
                usage_estimated INTEGER NOT NULL DEFAULT 0,
                elapsed_ms INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                completed_at REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS raw_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content_json TEXT NOT NULL,
                call_id TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                UNIQUE(turn_id, seq)
            );

            CREATE TABLE IF NOT EXISTS context_snapshots (
                id TEXT PRIMARY KEY,
                protocol_text TEXT NOT NULL DEFAULT '',
                persona_text TEXT NOT NULL DEFAULT '',
                tool_version_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tool_versions (
                id TEXT PRIMARY KEY,
                tools_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                summary_date TEXT NOT NULL,
                summary_text TEXT NOT NULL,
                char_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_summaries_cid_date
                ON daily_summaries(conversation_id, summary_date);

            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                task_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                bot_id TEXT NOT NULL DEFAULT 'default',
                task_type TEXT NOT NULL CHECK(task_type IN ('user_reminder', 'proactive_speak')),
                trigger_at_utc TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK(status IN (
                        'scheduled','firing','sending','fired','cancelled','failed',
                        'unknown_after_attempt'
                    )),
                generation INTEGER NOT NULL DEFAULT 1,
                delivery_key TEXT NOT NULL UNIQUE,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                last_error TEXT,
                delivery_started_at TEXT,
                last_attempt_at TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                fired_at REAL
            );

            CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_status_trigger
                ON scheduled_tasks(status, trigger_at_utc);
            CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_user
                ON scheduled_tasks(user_id, status);

            CREATE TABLE IF NOT EXISTS proactive_quota (
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                used_count INTEGER NOT NULL DEFAULT 0,
                frequency_tier TEXT NOT NULL DEFAULT 'moderate',
                updated_at REAL NOT NULL,
                PRIMARY KEY(user_id, date)
            );

            CREATE TABLE IF NOT EXISTS app_messages (
                id TEXT PRIMARY KEY,
                client_message_id TEXT,
                remote_agent_id TEXT NOT NULL DEFAULT 'default',
                role TEXT NOT NULL CHECK(role IN ('user', 'agent', 'system')),
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                source_turn_id TEXT NOT NULL DEFAULT '',
                source_tool_call_id TEXT NOT NULL DEFAULT '',
                sequence INTEGER NOT NULL DEFAULT 0,
                attachments_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_app_messages_client_msg_id
                ON app_messages(remote_agent_id, client_message_id)
                WHERE client_message_id IS NOT NULL;

            CREATE INDEX IF NOT EXISTS idx_app_messages_created_at
                ON app_messages(remote_agent_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_app_messages_source_turn_id
                ON app_messages(remote_agent_id, source_turn_id);

            CREATE TABLE IF NOT EXISTS app_attachments (
                attachment_id TEXT PRIMARY KEY,
                type TEXT NOT NULL DEFAULT 'image',
                mime_type TEXT NOT NULL,
                file_name TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                storage_path TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            """
        )

    async def _ensure_turn_metric_columns(self, db: aiosqlite.Connection) -> None:
        rows = await db.execute_fetchall("PRAGMA table_info(turns)")
        existing = {str(row["name"]) for row in rows}
        additions = {
            "prompt_tokens_peak": "INTEGER NOT NULL DEFAULT 0",
            "context_window_tokens": "INTEGER NOT NULL DEFAULT 0",
            "model_name": "TEXT NOT NULL DEFAULT ''",
            "usage_estimated": "INTEGER NOT NULL DEFAULT 0",
            "source": "TEXT NOT NULL DEFAULT 'user_message'",
            "turn_type": "TEXT NOT NULL DEFAULT 'user_chat'",
        }
        for name, definition in additions.items():
            if name not in existing:
                await db.execute(
                    f'ALTER TABLE turns ADD COLUMN "{name}" {definition}'
                )

    async def _ensure_app_message_remote_agent_id(self, db: aiosqlite.Connection) -> None:
        """Incrementally add remote_agent_id to app_messages if missing.

        - Adds ``remote_agent_id TEXT NOT NULL DEFAULT 'default'`` column.
        - Rebuilds the client_message_id unique index as composite
          ``(remote_agent_id, client_message_id)``.
        - Rebuilds created_at and source_turn_id indexes to include
          ``remote_agent_id`` as the leading column.
        """
        rows = await db.execute_fetchall("PRAGMA table_info(app_messages)")
        existing = {str(row["name"]) for row in rows}

        if "remote_agent_id" not in existing:
            await db.execute(
                "ALTER TABLE app_messages ADD COLUMN remote_agent_id TEXT NOT NULL DEFAULT 'default'"
            )

            # Rebuild the old single-column client_message_id unique index
            # as a composite (remote_agent_id, client_message_id).
            await db.execute(
                "DROP INDEX IF EXISTS idx_app_messages_client_msg_id"
            )
            await db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_app_messages_client_msg_id
                   ON app_messages(remote_agent_id, client_message_id)
                   WHERE client_message_id IS NOT NULL"""
            )

            # Rebuild created_at index to include remote_agent_id.
            await db.execute("DROP INDEX IF EXISTS idx_app_messages_created_at")
            await db.execute(
                """CREATE INDEX IF NOT EXISTS idx_app_messages_created_at
                   ON app_messages(remote_agent_id, created_at)"""
            )

            # Rebuild source_turn_id index to include remote_agent_id.
            await db.execute("DROP INDEX IF EXISTS idx_app_messages_source_turn_id")
            await db.execute(
                """CREATE INDEX IF NOT EXISTS idx_app_messages_source_turn_id
                   ON app_messages(remote_agent_id, source_turn_id)"""
            )

            await db.commit()

    async def _ensure_app_message_attachments_json(self, db: aiosqlite.Connection) -> None:
        """Incrementally add attachments_json to app_messages if missing."""
        rows = await db.execute_fetchall("PRAGMA table_info(app_messages)")
        existing = {str(row["name"]) for row in rows}

        if "attachments_json" not in existing:
            await db.execute(
                "ALTER TABLE app_messages ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'"
            )
            await db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
        self._instances.discard(self)

    @classmethod
    async def close_all(cls) -> None:
        for store in list(cls._instances):
            await store.close()

    # ── contact state (unchanged) ───────────────────────────────────

    async def update_contact_state(
        self, user_id: str, context_token: str = "", bot_id: str = ""
    ) -> None:
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO bot_contact_state(id, user_id, context_token, bot_id, updated_at)
                   VALUES(1, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       user_id=excluded.user_id,
                       context_token=excluded.context_token,
                       bot_id=excluded.bot_id,
                       updated_at=excluded.updated_at""",
                (user_id, context_token, bot_id, time.time()),
            )
            await db.commit()

    async def get_contact_state(self) -> dict[str, Any]:
        db = await self._get_db()
        rows = await db.execute_fetchall("SELECT * FROM bot_contact_state WHERE id=1")
        return dict(rows[0]) if rows else {
            "user_id": "", "context_token": "", "bot_id": "", "updated_at": 0,
        }

    # ── context snapshots ───────────────────────────────────────────

    @staticmethod
    def _snapshot_hash(protocol: str, persona: str, tool_version_id: str) -> str:
        raw = f"{protocol}|{persona}|{tool_version_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    async def ensure_snapshot(
        self, protocol: str, persona: str, tool_version_id: str
    ) -> str:
        sid = self._snapshot_hash(protocol, persona, tool_version_id)
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """INSERT OR IGNORE INTO context_snapshots(id, protocol_text, persona_text, tool_version_id, created_at)
                   VALUES(?, ?, ?, ?, ?)""",
                (sid, protocol, persona, tool_version_id, time.time()),
            )
            await db.commit()
        return sid

    async def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM context_snapshots WHERE id=?", (snapshot_id,)
        )
        return dict(rows[0]) if rows else None

    async def ensure_tool_version(self, tools_json: str) -> str:
        tid = hashlib.sha256(tools_json.encode()).hexdigest()[:16]
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """INSERT OR IGNORE INTO tool_versions(id, tools_json, created_at)
                   VALUES(?, ?, ?)""",
                (tid, tools_json, time.time()),
            )
            await db.commit()
        return tid

    # ── conversation ────────────────────────────────────────────────

    async def ensure_conversation(
        self, user_id: str, bot_id: str = "", snapshot_id: str = ""
    ) -> str:
        cid = f"{user_id}:{bot_id}"
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """INSERT OR IGNORE INTO conversations(id, user_id, bot_id, trim_marker_turn, context_snapshot_id, created_at)
                   VALUES(?, ?, ?, 0, ?, ?)""",
                (cid, user_id, bot_id, snapshot_id, time.time()),
            )
            # Update snapshot if changed
            if snapshot_id:
                await db.execute(
                    "UPDATE conversations SET context_snapshot_id=? WHERE id=?",
                    (snapshot_id, cid),
                )
            await db.commit()
        return cid

    async def get_conversation(self, user_id: str, bot_id: str = "") -> dict[str, Any] | None:
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM conversations WHERE id=?",
            (f"{user_id}:{bot_id}",),
        )
        return dict(rows[0]) if rows else None

    # ── turns ───────────────────────────────────────────────────────

    async def begin_turn(
        self,
        turn_id: str,
        conversation_id: str,
        user_message: str,
        *,
        source: str = "user_message",
        turn_type: str = "user_chat",
    ) -> int:
        """Start a turn, write user_input as raw_event seq=0. Returns turn_number."""
        now = time.time()
        async with self._write_lock:
            db = await self._get_db()
            rows = await db.execute_fetchall(
                "SELECT COALESCE(MAX(turn_number), 0) + 1 AS value FROM turns WHERE conversation_id=?",
                (conversation_id,),
            )
            number = int(rows[0]["value"])
            await db.execute(
                """INSERT INTO turns(id, conversation_id, turn_number, status, created_at, source, turn_type)
                   VALUES(?, ?, ?, 'running', ?, ?, ?)""",
                (turn_id, conversation_id, number, now, source, turn_type),
            )
            await db.execute(
                """INSERT INTO raw_events(turn_id, seq, role, content_json, created_at)
                   VALUES(?, 0, 'user', ?, ?)""",
                (turn_id, json.dumps(user_message, ensure_ascii=False), now),
            )
            await db.commit()
        return number

    async def append_event(
        self,
        turn_id: str,
        role: str,
        content: Any,
        *,
        call_id: str = "",
    ) -> int:
        """Write one raw_event. Returns seq number."""
        async with self._write_lock:
            db = await self._get_db()
            rows = await db.execute_fetchall(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS value FROM raw_events WHERE turn_id=?",
                (turn_id,),
            )
            seq = int(rows[0]["value"])
            await db.execute(
                """INSERT INTO raw_events(turn_id, seq, role, content_json, call_id, created_at)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                (turn_id, seq, role, json.dumps(content, ensure_ascii=False), call_id, time.time()),
            )
            await db.commit()
        return seq

    async def finish_turn(
        self,
        turn_id: str,
        status: str = "completed",
        token_count: int = 0,
        elapsed_ms: int = 0,
        *,
        prompt_tokens_peak: int = 0,
        context_window_tokens: int = 0,
        model_name: str = "",
        usage_estimated: bool = False,
    ) -> None:
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """UPDATE turns
                   SET status=?, token_count=?, elapsed_ms=?,
                       prompt_tokens_peak=?, context_window_tokens=?,
                       model_name=?, usage_estimated=?, completed_at=?
                   WHERE id=?""",
                (
                    status,
                    token_count,
                    elapsed_ms,
                    prompt_tokens_peak,
                    context_window_tokens,
                    model_name,
                    int(usage_estimated),
                    time.time(),
                    turn_id,
                ),
            )
            await db.commit()

    async def get_latest_context_usage(
        self,
        user_id: str,
        bot_id: str = "",
    ) -> dict[str, Any] | None:
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT id AS turn_id, model_name, prompt_tokens_peak,
                      context_window_tokens, usage_estimated, completed_at
               FROM turns
               WHERE conversation_id = ?
                 AND completed_at > 0
                 AND prompt_tokens_peak > 0
                 AND context_window_tokens > 0
               ORDER BY completed_at DESC
               LIMIT 1""",
            (f"{user_id}:{bot_id}",),
        )
        return dict(rows[0]) if rows else None

    async def get_latest_user_turn_created_at(
        self,
        user_id: str,
        bot_id: str = "",
    ) -> float | None:
        """Return the previous real user turn timestamp for idle-gap checks."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT created_at
               FROM turns
               WHERE conversation_id = ?
                 AND turn_number > 0
               ORDER BY turn_number DESC
               LIMIT 1""",
            (f"{user_id}:{bot_id}",),
        )
        return float(rows[0]["created_at"]) if rows else None

    # ── scheduled tasks ─────────────────────────────────────────────

    async def create_scheduled_task(
        self,
        *,
        task_id: str,
        user_id: str,
        bot_id: str,
        task_type: str,
        trigger_at_utc: str,
        timezone_name: str,
        payload: dict[str, Any],
        delivery_key: str,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        now = time.time()
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO scheduled_tasks(
                       task_id, user_id, bot_id, task_type, trigger_at_utc, timezone,
                       payload_json, status, generation, delivery_key, max_attempts,
                       created_at, updated_at
                   )
                   VALUES(?, ?, ?, ?, ?, ?, ?, 'scheduled', 1, ?, ?, ?, ?)""",
                (
                    task_id,
                    user_id,
                    bot_id,
                    task_type,
                    trigger_at_utc,
                    timezone_name,
                    json.dumps(payload, ensure_ascii=False),
                    delivery_key,
                    max_attempts,
                    now,
                    now,
                ),
            )
            await db.commit()
        task = await self.get_scheduled_task(task_id)
        if task is None:
            raise RuntimeError("scheduled task was not created")
        return task

    async def get_scheduled_task(self, task_id: str) -> dict[str, Any] | None:
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM scheduled_tasks WHERE task_id=?",
            (task_id,),
        )
        return dict(rows[0]) if rows else None

    async def list_scheduled_tasks(
        self,
        *,
        user_id: str | None = None,
        task_type: str | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        db = await self._get_db()
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        if task_type is not None:
            clauses.append("task_type=?")
            params.append(task_type)
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" for _ in statuses))
            params.extend(statuses)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await db.execute_fetchall(
            "SELECT * FROM scheduled_tasks" + where + " ORDER BY trigger_at_utc ASC",
            tuple(params),
        )
        return [dict(row) for row in rows]

    async def cancel_scheduled_task(self, task_id: str, user_id: str) -> bool:
        async with self._write_lock:
            db = await self._get_db()
            cursor = await db.execute(
                """UPDATE scheduled_tasks
                   SET status='cancelled', generation=generation+1, updated_at=?
                   WHERE task_id=? AND user_id=? AND status='scheduled'""",
                (time.time(), task_id, user_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def cancel_scheduled_tasks(
        self,
        *,
        user_id: str,
        task_type: str,
        reason: str = "",
    ) -> int:
        async with self._write_lock:
            db = await self._get_db()
            now = time.time()
            cursor = await db.execute(
                """UPDATE scheduled_tasks
                   SET status='cancelled',
                       generation=generation+1,
                       last_error=?,
                       updated_at=?
                   WHERE user_id=?
                     AND task_type=?
                     AND status IN ('scheduled','firing')""",
                (reason[:500], now, user_id, task_type),
            )
            await db.commit()
            return cursor.rowcount

    async def claim_scheduled_task(self, task_id: str, expected_generation: int) -> dict[str, Any] | None:
        async with self._write_lock:
            db = await self._get_db()
            now = time.time()
            cursor = await db.execute(
                """UPDATE scheduled_tasks
                   SET status='firing', last_attempt_at=?, updated_at=?
                   WHERE task_id=? AND generation=? AND status='scheduled'""",
                (now, now, task_id, expected_generation),
            )
            await db.commit()
            if cursor.rowcount <= 0:
                return None
        return await self.get_scheduled_task(task_id)

    async def mark_task_sending(self, task_id: str, generation: int) -> bool:
        async with self._write_lock:
            db = await self._get_db()
            now_iso = datetime_utc_iso()
            cursor = await db.execute(
                """UPDATE scheduled_tasks
                   SET status='sending', delivery_started_at=?, updated_at=?
                   WHERE task_id=? AND generation=? AND status='firing'""",
                (now_iso, time.time(), task_id, generation),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def mark_task_unknown(self, task_id: str, generation: int, reason: str) -> None:
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """UPDATE scheduled_tasks
                   SET status='unknown_after_attempt', last_error=?, updated_at=?
                   WHERE task_id=? AND generation=?""",
                (reason[:500], time.time(), task_id, generation),
            )
            await db.commit()

    async def mark_task_failed_or_retry(self, task_id: str, generation: int, reason: str) -> None:
        async with self._write_lock:
            db = await self._get_db()
            rows = await db.execute_fetchall(
                "SELECT attempt_count, max_attempts FROM scheduled_tasks WHERE task_id=?",
                (task_id,),
            )
            if not rows:
                return
            attempts = int(rows[0]["attempt_count"]) + 1
            max_attempts = int(rows[0]["max_attempts"])
            status = "failed" if attempts >= max_attempts else "scheduled"
            await db.execute(
                """UPDATE scheduled_tasks
                   SET status=?, attempt_count=?, last_error=?, updated_at=?
                   WHERE task_id=? AND generation=?""",
                (status, attempts, reason[:500], time.time(), task_id, generation),
            )
            await db.commit()

    async def commit_delivered_reminder(
        self,
        *,
        task: dict[str, Any],
        anchor: str,
        delivered_text: str,
    ) -> str:
        conversation_id = f"{task['user_id']}:{task.get('bot_id') or 'default'}"
        turn_id = f"reminder_{task['task_id']}"
        now = time.time()
        db = await self._get_db()
        async with self._write_lock:
            await db.execute("BEGIN")
            try:
                rows = await db.execute_fetchall(
                    "SELECT COALESCE(MAX(turn_number), 0) + 1 AS value FROM turns WHERE conversation_id=?",
                    (conversation_id,),
                )
                number = int(rows[0]["value"])
                await db.execute(
                    """INSERT INTO turns(id, conversation_id, turn_number, status, created_at,
                                         completed_at, source, turn_type)
                       VALUES(?, ?, ?, 'completed', ?, ?, 'reminder_due', 'reminder_due')""",
                    (turn_id, conversation_id, number, now, now),
                )
                await db.execute(
                    """INSERT INTO raw_events(turn_id, seq, role, content_json, active, created_at)
                       VALUES(?, 0, 'user', ?, 1, ?)""",
                    (
                        turn_id,
                        json.dumps([{"type": "text", "text": anchor}], ensure_ascii=False),
                        now,
                    ),
                )
                await db.execute(
                    """INSERT INTO raw_events(turn_id, seq, role, content_json, active, created_at)
                       VALUES(?, 1, 'assistant', ?, 1, ?)""",
                    (
                        turn_id,
                        json.dumps([{"type": "text", "text": delivered_text}], ensure_ascii=False),
                        now,
                    ),
                )
                await db.execute(
                    """UPDATE scheduled_tasks
                       SET status='fired', fired_at=?, updated_at=?
                       WHERE task_id=? AND generation=? AND status='sending'""",
                    (now, now, task["task_id"], task["generation"]),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return turn_id

    async def get_proactive_quota_used(
        self,
        *,
        user_id: str,
        date: str,
    ) -> int:
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT used_count FROM proactive_quota WHERE user_id=? AND date=?",
            (user_id, date),
        )
        return int(rows[0]["used_count"]) if rows else 0

    async def commit_proactive_success(
        self,
        *,
        user_id: str,
        bot_id: str,
        date: str,
        frequency_tier: str,
        current_task_id: str | None,
        current_generation: int | None,
        next_task: dict[str, Any] | None,
    ) -> dict[str, Any]:
        now = time.time()
        db = await self._get_db()
        async with self._write_lock:
            await db.execute("BEGIN")
            try:
                if current_task_id:
                    cursor = await db.execute(
                        """UPDATE scheduled_tasks
                           SET status='fired', fired_at=?, updated_at=?
                           WHERE task_id=?
                             AND generation=?
                             AND task_type='proactive_speak'
                             AND status='firing'""",
                        (now, now, current_task_id, current_generation),
                    )
                    if cursor.rowcount <= 0:
                        raise RuntimeError("proactive task generation mismatch")

                await db.execute(
                    """INSERT INTO proactive_quota(user_id, date, used_count, frequency_tier, updated_at)
                       VALUES(?, ?, 1, ?, ?)
                       ON CONFLICT(user_id, date) DO UPDATE SET
                         used_count=used_count+1,
                         frequency_tier=excluded.frequency_tier,
                         updated_at=excluded.updated_at""",
                    (user_id, date, frequency_tier, now),
                )

                created_next = False
                if next_task is not None:
                    await db.execute(
                        """INSERT INTO scheduled_tasks(
                               task_id, user_id, bot_id, task_type, trigger_at_utc, timezone,
                               payload_json, status, generation, delivery_key, max_attempts,
                               created_at, updated_at
                           )
                           VALUES(?, ?, ?, 'proactive_speak', ?, ?, ?, 'scheduled', 1, ?, ?, ?, ?)""",
                        (
                            next_task["task_id"],
                            user_id,
                            bot_id,
                            next_task["trigger_at_utc"],
                            next_task.get("timezone", "Asia/Shanghai"),
                            json.dumps(next_task.get("payload", {}), ensure_ascii=False),
                            next_task["delivery_key"],
                            int(next_task.get("max_attempts", 1)),
                            now,
                            now,
                        ),
                    )
                    created_next = True

                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return {"status": "committed", "next_task_created": created_next}

    # ── rebuild (restart) ───────────────────────────────────────────

    async def get_recent_events(
        self, conversation_id: str, turn_limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return active raw_events ordered for context rebuild."""
        del turn_limit  # No longer used — active=1 is the implicit limit
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT re.*, t.turn_number FROM raw_events re
               JOIN turns t ON re.turn_id = t.id
               WHERE t.conversation_id = ? AND re.active = 1
               ORDER BY t.turn_number, re.seq""",
            (conversation_id,),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                content = json.loads(row["content_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            result.append({
                "turn_id": row["turn_id"],
                "seq": row["seq"],
                "role": row["role"],
                "content": content,
                "call_id": row["call_id"],
                "ts": row["created_at"],
            })
        return result

    # ── compress ────────────────────────────────────────────────────

    async def apply_compression(
        self,
        conversation_id: str,
        compressed_messages: list[dict[str, Any]],
    ) -> bool:
        """Soft-delete all events, then write the compressed message array.

        *compressed_messages* is the new messages array (from
        ConversationContext.replace_with_compressed) that replaces all
        previous history.  Each message is written as a raw_event with
        active=1 so rebuild restores the correct state after a restart.
        """
        import uuid

        db = await self._get_db()
        async with self._write_lock:
            # 1. Soft-delete all existing events
            await db.execute(
                """UPDATE raw_events SET active = 0
                   WHERE turn_id IN (
                       SELECT id FROM turns WHERE conversation_id = ?
                   )""",
                (conversation_id,),
            )

            # 2. Create a synthetic turn for the compressed state
            turn_id = f"compressed_{uuid.uuid4().hex[:12]}"
            await db.execute(
                """INSERT INTO turns(id, conversation_id, turn_number, status, created_at)
                   VALUES(?, ?, 0, 'compressed', ?)""",
                (turn_id, conversation_id, time.time()),
            )

            # 3. Write each compressed message as a raw_event (active=1)
            for seq, msg in enumerate(compressed_messages):
                await db.execute(
                    """INSERT INTO raw_events(turn_id, seq, role, content_json, active, created_at)
                       VALUES(?, ?, ?, ?, 1, ?)""",
                    (turn_id, seq, msg["role"],
                     json.dumps(msg["content"], ensure_ascii=False),
                     time.time()),
                )

            # 4. Update last_compressed_at
            await db.execute(
                "UPDATE conversations SET last_compressed_at = ? WHERE id = ?",
                (time.time(), conversation_id),
            )

            await db.commit()
        return True

    async def commit_compression(
        self,
        conversation_id: str,
        compressed_messages: list[dict[str, Any]],
        summary_date: str,
        summary_text: str,
    ) -> bool:
        """Atomically persist compressed history and its dated summary."""
        import uuid

        db = await self._get_db()
        async with self._write_lock:
            await db.execute("BEGIN")
            try:
                await db.execute(
                    """UPDATE raw_events SET active = 0
                       WHERE turn_id IN (
                           SELECT id FROM turns WHERE conversation_id = ?
                       )""",
                    (conversation_id,),
                )
                turn_id = f"compressed_{uuid.uuid4().hex[:12]}"
                await db.execute(
                    """INSERT INTO turns(id, conversation_id, turn_number, status, created_at)
                       VALUES(?, ?, 0, 'compressed', ?)""",
                    (turn_id, conversation_id, time.time()),
                )
                for seq, msg in enumerate(compressed_messages):
                    await db.execute(
                        """INSERT INTO raw_events(turn_id, seq, role, content_json, active, created_at)
                           VALUES(?, ?, ?, ?, 1, ?)""",
                        (
                            turn_id,
                            seq,
                            msg["role"],
                            json.dumps(msg["content"], ensure_ascii=False),
                            time.time(),
                        ),
                    )
                await db.execute(
                    "UPDATE conversations SET last_compressed_at = ? WHERE id = ?",
                    (time.time(), conversation_id),
                )
                await db.execute(
                    """INSERT OR REPLACE INTO daily_summaries
                       (conversation_id, summary_date, summary_text, char_count, created_at)
                       VALUES(?, ?, ?, ?, ?)""",
                    (
                        conversation_id,
                        summary_date,
                        summary_text,
                        len(summary_text),
                        time.time(),
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return True

    # ── daily summaries ────────────────────────────────────────────

    async def save_daily_summary(
        self,
        conversation_id: str,
        summary_date: str,
        summary_text: str,
    ) -> None:
        """Persist a daily summary. Same date overwrites (INSERT OR REPLACE)."""
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """INSERT OR REPLACE INTO daily_summaries
                       (conversation_id, summary_date, summary_text, char_count, created_at)
                       VALUES(?, ?, ?, ?, ?)""",
                (conversation_id, summary_date, summary_text, len(summary_text), time.time()),
            )
            await db.commit()

    async def get_daily_summaries(
        self,
        conversation_id: str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Return daily summaries newest-first."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT * FROM daily_summaries
               WHERE conversation_id = ?
               ORDER BY summary_date DESC
               LIMIT ?""",
            (conversation_id, limit),
        )
        return [dict(r) for r in rows]

    # ── app_messages ──────────────────────────────────────────────────

    async def insert_app_message(
        self,
        *,
        remote_id: str,
        client_message_id: str | None,
        role: str,
        content: str,
        source_turn_id: str = "",
        source_tool_call_id: str = "",
        sequence: int = 0,
        remote_agent_id: str = "default",
        created_at: float | None = None,
        attachments: list[dict] | None = None,
    ) -> None:
        """Insert a single app message (user or agent reply).

        *created_at* may be supplied to persist a caller-provided timestamp
        (e.g. an attached-session callback reply ``createdAt``); otherwise
        the current time is used.
        """
        now = created_at if created_at is not None else time.time()
        attachments_json = json.dumps(
            attachments if attachments else [], ensure_ascii=False,
        )
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO app_messages(
                       id, client_message_id, remote_agent_id, role, content,
                       created_at, source_turn_id, source_tool_call_id, sequence,
                       attachments_json
                   )
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    remote_id,
                    client_message_id,
                    remote_agent_id,
                    role,
                    content,
                    now,
                    source_turn_id,
                    source_tool_call_id,
                    sequence,
                    attachments_json,
                ),
            )
            await db.commit()

    async def insert_app_turn_messages(
        self,
        *,
        user_message: dict[str, Any],
        agent_replies: list[dict[str, Any]],
    ) -> None:
        """Atomically insert a user message and its agent replies.

        All rows share the same ``source_turn_id`` and are written in a
        single ``write_lock`` + transaction.  If any insert fails the
        entire transaction is rolled back, so no half-success history is
        left behind.

        *user_message* keys: ``remote_id``, ``client_message_id``,
        ``content``, ``source_turn_id``, ``remote_agent_id``,
        ``created_at`` (optional), ``attachments`` (optional list of dicts).

        Each *agent_replies* entry keys: ``remote_id``, ``content``,
        ``source_turn_id``, ``sequence``, ``remote_agent_id``,
        ``source_tool_call_id`` (optional), ``created_at`` (optional).
        """
        async with self._write_lock:
            db = await self._get_db()
            try:
                user_ts = user_message.get("created_at")
                if user_ts is None:
                    user_ts = time.time()
                user_attachments_json = json.dumps(
                    user_message.get("attachments") or [], ensure_ascii=False,
                )
                await db.execute(
                    """INSERT INTO app_messages(
                           id, client_message_id, remote_agent_id, role, content,
                           created_at, source_turn_id, source_tool_call_id, sequence,
                           attachments_json
                       )
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user_message["remote_id"],
                        user_message.get("client_message_id"),
                        user_message.get("remote_agent_id", "default"),
                        "user",
                        user_message["content"],
                        user_ts,
                        user_message.get("source_turn_id", ""),
                        "",
                        0,
                        user_attachments_json,
                    ),
                )
                for reply in agent_replies:
                    reply_ts = reply.get("created_at")
                    if reply_ts is None:
                        reply_ts = time.time()
                    reply_attachments_json = json.dumps(
                        reply.get("attachments") or [], ensure_ascii=False,
                    )
                    await db.execute(
                        """INSERT INTO app_messages(
                               id, client_message_id, remote_agent_id, role, content,
                               created_at, source_turn_id, source_tool_call_id, sequence,
                               attachments_json
                           )
                           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            reply["remote_id"],
                            None,
                            reply.get("remote_agent_id", "default"),
                            "agent",
                            reply["content"],
                            reply_ts,
                            reply.get("source_turn_id", ""),
                            reply.get("source_tool_call_id", ""),
                            reply.get("sequence", 0),
                            reply_attachments_json,
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def find_app_message_by_client_message_id(
        self, client_message_id: str,
        remote_agent_id: str = "default",
    ) -> dict[str, Any] | None:
        """Look up an app message by its client-generated idempotency key.

        Only matches within *remote_agent_id* so different agents can share
        the same ``clientMessageId`` without colliding.
        """
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM app_messages WHERE remote_agent_id = ? AND client_message_id = ?",
            (remote_agent_id, client_message_id),
        )
        return dict(rows[0]) if rows else None

    async def find_app_agent_replies_by_turn_id(
        self, source_turn_id: str,
        remote_agent_id: str = "default",
    ) -> list[dict[str, Any]]:
        """Return agent replies for a given turn, scoped to *remote_agent_id*."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT * FROM app_messages
               WHERE remote_agent_id = ? AND source_turn_id = ? AND role = 'agent'
               ORDER BY sequence ASC""",
            (remote_agent_id, source_turn_id),
        )
        return [dict(row) for row in rows]

    async def get_app_messages_after_cursor(
        self,
        cursor: str | None = None,
        limit: int = 50,
        remote_agent_id: str = "default",
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """Return app messages scoped to *remote_agent_id*, ordered by created_at.

        cursor  – the id of the last-seen message (exclusive), or None for
                  the first page.  A cursor from a different
                  ``remote_agent_id`` is treated as None.
        limit   – maximum messages to return (1-200, clamped).
        Returns (messages, next_cursor, has_more).

        Rows are ordered by the deterministic composite key
        ``(created_at, source_turn_id, sequence, id)``.  ``created_at`` alone
        is not a stable sort key: a user message and its agent reply can be
        written within a single ``time.time()`` tick and end up with an
        identical timestamp, at which point SQLite's tie-break is
        non-deterministic and (worse) the lexicographic ``id`` tie-break
        (``app_rm_agent_*`` < ``app_rm_user_*``) flips the reply ahead of the
        user.  ``sequence`` (0 for the user message, >= 1 for each reply,
        shared ``source_turn_id``) keeps a turn's user message ahead of its
        replies; ``id`` is the final unique tie-break so the order is total.
        The same composite key is used for cursor pagination so pages neither
        drop nor duplicate rows that share a timestamp.
        """
        limit = max(1, min(int(limit), 200))
        db = await self._get_db()

        # Validate that cursor belongs to this remote_agent_id;
        # if not, treat it as None so we don't cross-scope.
        cursor_ts: float | None = None
        cursor_turn: str = ""
        cursor_seq: int = 0
        if cursor is not None:
            cr = await db.execute_fetchall(
                "SELECT remote_agent_id, created_at, source_turn_id, sequence "
                "FROM app_messages WHERE id = ?",
                (cursor,),
            )
            if cr and str(cr[0]["remote_agent_id"]) == remote_agent_id:
                cursor_ts = float(cr[0]["created_at"])
                cursor_turn = str(cr[0]["source_turn_id"] or "")
                cursor_seq = int(cr[0]["sequence"] or 0)
            # else: cursor is from a different scope → fall through to
            #        the cursor-is-None branch.

        if cursor_ts is not None:
            # Paginate within the same remote_agent_id using the same
            # composite key the ORDER BY uses, so the page walk is a strict
            # continuation of the ordering (no row dropped or duplicated).
            rows = await db.execute_fetchall(
                """SELECT * FROM app_messages
                   WHERE remote_agent_id = ?
                     AND (created_at > ?
                          OR (created_at = ? AND source_turn_id > ?)
                          OR (created_at = ? AND source_turn_id = ? AND sequence > ?)
                          OR (created_at = ? AND source_turn_id = ? AND sequence = ? AND id > ?))
                   ORDER BY created_at ASC, source_turn_id ASC, sequence ASC, id ASC
                   LIMIT ?""",
                (
                    remote_agent_id,
                    cursor_ts,
                    cursor_ts, cursor_turn,
                    cursor_ts, cursor_turn, cursor_seq,
                    cursor_ts, cursor_turn, cursor_seq, cursor,
                    limit + 1,
                ),
            )
        else:
            rows = await db.execute_fetchall(
                """SELECT * FROM app_messages
                   WHERE remote_agent_id = ?
                   ORDER BY created_at ASC, source_turn_id ASC, sequence ASC, id ASC
                   LIMIT ?""",
                (remote_agent_id, limit + 1),
            )

        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

        messages = [dict(row) for row in rows]
        next_cursor = messages[-1]["id"] if messages else cursor
        return messages, next_cursor, has_more

    # ── app_attachments ─────────────────────────────────────────────

    async def insert_app_attachment(
        self,
        *,
        attachment_id: str,
        type: str = "image",
        mime_type: str,
        file_name: str,
        size_bytes: int,
        storage_path: str,
        created_at: float | None = None,
    ) -> None:
        """Persist attachment metadata."""
        now = created_at if created_at is not None else time.time()
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO app_attachments(
                       attachment_id, type, mime_type, file_name, size_bytes,
                       storage_path, created_at
                   )
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (attachment_id, type, mime_type, file_name, size_bytes, storage_path, now),
            )
            await db.commit()

    async def get_app_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        """Look up a single attachment by id."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM app_attachments WHERE attachment_id = ?",
            (attachment_id,),
        )
        return dict(rows[0]) if rows else None

    async def get_app_attachments(self, attachment_ids: list[str]) -> list[dict[str, Any]]:
        """Look up multiple attachments by their ids.  Missing ids are silently dropped."""
        if not attachment_ids:
            return []
        db = await self._get_db()
        placeholders = ", ".join("?" for _ in attachment_ids)
        rows = await db.execute_fetchall(
            f"SELECT * FROM app_attachments WHERE attachment_id IN ({placeholders})",
            tuple(attachment_ids),
        )
        return [dict(row) for row in rows]

    # ── clear ───────────────────────────────────────────────────────

    async def clear_all(self) -> dict[str, int]:
        """Clear all data except bot_contact_state and runtime_meta."""
        tables = ("conversations", "turns", "raw_events", "context_snapshots", "tool_versions", "daily_summaries", "scheduled_tasks", "app_messages", "app_attachments")
        counts: dict[str, int] = {}
        async with self._write_lock:
            db = await self._get_db()
            for table in tables:
                rows = await db.execute_fetchall(f"SELECT COUNT(*) AS value FROM {table}")
                counts[table] = int(rows[0]["value"])
                await db.execute(f"DELETE FROM {table}")
            await db.commit()
        return counts
