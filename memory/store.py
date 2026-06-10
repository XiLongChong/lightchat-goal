"""SQLite persistence — contact state, turns, raw events, context snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA_ID = "v3_daily_compression"


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
            """
        )

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
                """INSERT INTO turns(id, conversation_id, turn_number, status, created_at)
                   VALUES(?, ?, ?, 'running', ?)""",
                (turn_id, conversation_id, number, now),
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
        self, turn_id: str, status: str = "completed", token_count: int = 0, elapsed_ms: int = 0
    ) -> None:
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """UPDATE turns SET status=?, token_count=?, elapsed_ms=?, completed_at=? WHERE id=?""",
                (status, token_count, elapsed_ms, time.time(), turn_id),
            )
            await db.commit()

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

    # ── clear ───────────────────────────────────────────────────────

    async def clear_all(self) -> dict[str, int]:
        """Clear all data except bot_contact_state and runtime_meta."""
        tables = ("conversations", "turns", "raw_events", "context_snapshots", "tool_versions", "daily_summaries")
        counts: dict[str, int] = {}
        async with self._write_lock:
            db = await self._get_db()
            for table in tables:
                rows = await db.execute_fetchall(f"SELECT COUNT(*) AS value FROM {table}")
                counts[table] = int(rows[0]["value"])
                await db.execute(f"DELETE FROM {table}")
            await db.commit()
        return counts
