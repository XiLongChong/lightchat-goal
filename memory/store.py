"""SQLite-based memory store with WAL mode and CJK retrieval.

Privacy: all data stays on disk, no cloud dependency.
Portability: single .db file, easy to backup/migrate.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from memory.dedup import is_duplicate, compute_importance, jaccard_similarity


class MemoryStore:
    """Persistent memory storage with keyword-based retrieval.

    Thread/async safety: all writes go through a single asyncio.Lock.
    SQLite WAL mode allows concurrent reads during writes.
    """

    def __init__(self, db_path: str = "./data/memory.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._db: aiosqlite.Connection | None = None

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(str(self.db_path))
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA busy_timeout=5000")
            await self._db.execute("PRAGMA foreign_keys=ON")
            self._db.row_factory = aiosqlite.Row
            await self._migrate()
        return self._db

    async def _migrate(self):
        db = self._db
        if db is None:
            return
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'other',
                importance REAL NOT NULL DEFAULT 0.5,
                keywords TEXT NOT NULL DEFAULT '',
                source_turn_id TEXT DEFAULT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_category
            ON memories(category)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_importance
            ON memories(importance DESC)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS relationship_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                familiarity INTEGER NOT NULL DEFAULT 5,
                chat_boundary TEXT NOT NULL DEFAULT '',
                disliked_expressions TEXT NOT NULL DEFAULT '',
                recent_changes TEXT NOT NULL DEFAULT '',
                current_atmosphere TEXT NOT NULL DEFAULT 'neutral',
                last_topic TEXT NOT NULL DEFAULT '',
                user_mood_recent TEXT NOT NULL DEFAULT '',
                interaction_style TEXT NOT NULL DEFAULT 'casual',
                updated_at REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                consecutive_ignored INTEGER NOT NULL DEFAULT 0,
                messages_today INTEGER NOT NULL DEFAULT 0,
                last_proactive_time REAL NOT NULL DEFAULT 0,
                last_chat_time REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                context_token TEXT NOT NULL DEFAULT '',
                messages_json TEXT NOT NULL,
                due_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_scheduled_messages_due
            ON scheduled_messages(status, due_at)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_contact_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                user_id TEXT NOT NULL DEFAULT '',
                context_token TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_persona_change (
                id INTEGER PRIMARY KEY DEFAULT 1,
                content TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS history_summaries (
                user_id TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                ts REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversation_history_user_ts
            ON conversation_history(user_id, ts DESC)
        """)
        await db.commit()

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    # ================================================================
    # Memory CRUD
    # ================================================================

    async def save_memory(
        self,
        content: str,
        category: str = "other",
        keywords: str = "",
        source_turn_id: str = "",
        min_importance: float = 0.4,
    ) -> str | None:
        """Save a memory entry. Returns the memory ID, or None if rejected.

        Safeguard #5: low importance → not saved.
        Dedup check before write.
        """
        if not content or not content.strip():
            return None

        content = content.strip()

        # Check dedup against existing
        existing = await self.list_all()
        existing_contents = [{"content": m["content"]} for m in existing]
        if is_duplicate(content, existing_contents):
            return None

        # Compute importance
        # Count existing in same category as signal
        cat_count = sum(1 for m in existing if m.get("category") == category)
        importance = compute_importance(content, cat_count)

        # Safeguard #5: importance threshold
        if importance < min_importance:
            return None

        import uuid
        now = time.time()
        mem_id = f"mem_{uuid.uuid4().hex[:12]}"

        async with self._lock:  # Safeguard #8: serialized writes
            db = await self._get_db()
            await db.execute(
                """INSERT INTO memories
                   (id, content, category, importance, keywords, source_turn_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (mem_id, content, category, importance,
                 keywords, source_turn_id, now, now),
            )
            await db.commit()
        return mem_id

    async def search(
        self,
        query: str,
        top_k: int = 5,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search memories by keyword overlap with query.

        Uses CJK bigram overlap scoring + importance weighting.
        Fast, private, no external embedding dependency.
        """
        db = await self._get_db()
        if category:
            rows = await db.execute_fetchall(
                "SELECT * FROM memories WHERE category = ? ORDER BY importance DESC",
                (category,),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM memories ORDER BY importance DESC",
            )

        # Score each memory by bigram overlap with query
        scored = []
        for row in rows:
            memory_content = row["content"]
            sim = jaccard_similarity(query, memory_content)

            # Weighted score: 50% similarity + 50% importance
            importance = row["importance"]
            score = sim * 0.5 + importance * 0.5

            scored.append((score, dict(row)))

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        return [item for _, item in scored[:top_k]]

    async def list_all(self) -> list[dict[str, Any]]:
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM memories ORDER BY importance DESC"
        )
        return [dict(r) for r in rows]

    async def delete_memory(self, mem_id: str) -> bool:
        async with self._lock:
            db = await self._get_db()
            cursor = await db.execute(
                "DELETE FROM memories WHERE id = ?", (mem_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def clear_memories(self) -> int:
        """Delete all long-term memories."""
        async with self._lock:
            db = await self._get_db()
            cursor = await db.execute("DELETE FROM memories")
            await db.commit()
            return cursor.rowcount

    async def import_memories(
        self,
        entries: list[dict[str, Any]],
        clear_existing: bool = False,
    ) -> dict[str, int]:
        """Bulk import normalized memory entries.

        Import bypasses the importance threshold but still deduplicates against
        current DB content and entries imported earlier in the same batch.
        """
        import uuid

        now = time.time()
        imported = 0
        skipped = 0
        cleared = 0

        async with self._lock:
            db = await self._get_db()
            if clear_existing:
                cursor = await db.execute("DELETE FROM memories")
                cleared = cursor.rowcount

            rows = await db.execute_fetchall("SELECT content FROM memories")
            seen = [{"content": r["content"]} for r in rows]

            for entry in entries:
                content = str(entry.get("content", "")).strip()
                if not content:
                    skipped += 1
                    continue
                if is_duplicate(content, seen):
                    skipped += 1
                    continue

                category = str(entry.get("category", "imported") or "imported")[:50]
                keywords = str(entry.get("keywords", "") or "")[:500]
                source_turn_id = str(entry.get("source_turn_id", "") or "")[:200]
                importance = float(entry.get("importance", 0.72) or 0.72)
                importance = max(0.0, min(1.0, importance))
                created_at = float(entry.get("created_at", now) or now)
                mem_id = str(entry.get("id", "") or f"mem_{uuid.uuid4().hex[:12]}")

                await db.execute(
                    """INSERT OR REPLACE INTO memories
                       (id, content, category, importance, keywords,
                        source_turn_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        mem_id,
                        content,
                        category,
                        importance,
                        keywords,
                        source_turn_id,
                        created_at,
                        now,
                    ),
                )
                seen.append({"content": content})
                imported += 1

            await db.commit()

        return {"imported": imported, "skipped": skipped, "cleared": cleared}

    async def apply_decay(self):
        """Apply time-based importance decay to all memories.

        Safeguard #5: old, low-importance memories naturally fade.
        Should be called by a periodic task (e.g., daily).
        """
        now = time.time()
        db = await self._get_db()
        rows = await db.execute_fetchall("SELECT * FROM memories")

        async with self._lock:
            for row in rows:
                days = (now - row["created_at"]) / 86400.0
                new_importance = compute_importance(
                    row["content"],
                    existing_count=0,
                    days_since_created=days,
                )
                await db.execute(
                    "UPDATE memories SET importance = ?, updated_at = ? WHERE id = ?",
                    (new_importance, now, row["id"]),
                )
            await db.commit()

    # ================================================================
    # Relationship state
    # ================================================================

    async def get_relationship(self) -> dict[str, Any]:
        db = await self._get_db()
        row = await db.execute_fetchall(
            "SELECT * FROM relationship_state WHERE id = 1"
        )
        if row:
            return dict(row[0])
        return {
            "familiarity": 5,
            "chat_boundary": "",
            "disliked_expressions": "",
            "recent_changes": "",
            "current_atmosphere": "neutral",
            "last_topic": "",
            "user_mood_recent": "",
            "interaction_style": "casual",
        }

    async def update_relationship(self, updates: dict[str, Any]):
        db = await self._get_db()
        now = time.time()

        # Ensure row exists
        await db.execute(
            """INSERT OR IGNORE INTO relationship_state (id, updated_at)
               VALUES (1, ?)""",
            (now,),
        )

        set_parts = []
        values = []
        allowed_fields = [
            "familiarity", "chat_boundary", "disliked_expressions",
            "recent_changes", "current_atmosphere", "last_topic",
            "user_mood_recent", "interaction_style",
        ]
        for field in allowed_fields:
            if field in updates:
                set_parts.append(f"{field} = ?")
                values.append(updates[field])

        if not set_parts:
            return

        set_parts.append("updated_at = ?")
        values.append(now)
        values.append(1)  # WHERE id = 1

        async with self._lock:
            await db.execute(
                f"UPDATE relationship_state SET {', '.join(set_parts)} WHERE id = ?",
                values,
            )
            await db.commit()

    # ================================================================
    # Scheduler state
    # ================================================================

    async def get_scheduler_state(self) -> dict[str, Any]:
        db = await self._get_db()
        row = await db.execute_fetchall(
            "SELECT * FROM scheduler_state WHERE id = 1"
        )
        if row:
            return dict(row[0])
        return {
            "consecutive_ignored": 0,
            "messages_today": 0,
            "last_proactive_time": 0,
            "last_chat_time": 0,
        }

    async def update_scheduler_state(self, updates: dict[str, Any]):
        db = await self._get_db()
        now = time.time()

        await db.execute(
            """INSERT OR IGNORE INTO scheduler_state (id, updated_at)
               VALUES (1, ?)""",
            (now,),
        )

        set_parts = []
        values = []
        for field in ["consecutive_ignored", "messages_today",
                       "last_proactive_time", "last_chat_time"]:
            if field in updates:
                set_parts.append(f"{field} = ?")
                values.append(updates[field])

        if not set_parts:
            return

        set_parts.append("updated_at = ?")
        values.append(now)
        values.append(1)

        async with self._lock:
            await db.execute(
                f"UPDATE scheduler_state SET {', '.join(set_parts)} WHERE id = ?",
                values,
            )
            await db.commit()

    # ================================================================
    # Scheduled message queue
    # ================================================================

    async def create_scheduled_message(
        self,
        user_id: str,
        context_token: str,
        messages: list[dict[str, Any]],
        due_at: float,
    ) -> str:
        """Persist a future message task.

        Stores the normalized message array as JSON so an unexpected restart
        does not lose reminders.
        """
        import uuid

        task_id = f"sch_{uuid.uuid4().hex[:12]}"
        now = time.time()
        payload = json.dumps(messages, ensure_ascii=False)

        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO scheduled_messages
                   (id, user_id, context_token, messages_json, due_at,
                    status, error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', '', ?, ?)""",
                (task_id, user_id, context_token, payload, due_at, now, now),
            )
            await db.commit()

        return task_id

    async def get_due_scheduled_messages(
        self,
        now_ts: float | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return pending scheduled messages whose due time has passed."""
        db = await self._get_db()
        now_ts = now_ts if now_ts is not None else time.time()
        rows = await db.execute_fetchall(
            """SELECT * FROM scheduled_messages
               WHERE status = 'pending' AND due_at <= ?
               ORDER BY due_at ASC
               LIMIT ?""",
            (now_ts, limit),
        )

        tasks = []
        for row in rows:
            item = dict(row)
            try:
                item["messages"] = json.loads(item.pop("messages_json"))
            except (json.JSONDecodeError, TypeError):
                item["messages"] = []
            tasks.append(item)
        return tasks

    async def mark_scheduled_message_status(
        self,
        task_id: str,
        status: str,
        error: str = "",
    ):
        """Mark a scheduled task as sent or failed."""
        if status not in {"sent", "failed", "pending"}:
            raise ValueError(f"invalid scheduled message status: {status}")

        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """UPDATE scheduled_messages
                   SET status = ?, error = ?, updated_at = ?
                   WHERE id = ?""",
                (status, error[:500], time.time(), task_id),
            )
            await db.commit()

    async def list_scheduled_messages(
        self,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List scheduled message tasks for inspection."""
        db = await self._get_db()
        if status:
            rows = await db.execute_fetchall(
                """SELECT * FROM scheduled_messages
                   WHERE status = ?
                   ORDER BY due_at DESC
                   LIMIT ?""",
                (status, limit),
            )
        else:
            rows = await db.execute_fetchall(
                """SELECT * FROM scheduled_messages
                   ORDER BY due_at DESC
                   LIMIT ?""",
                (limit,),
            )

        result = []
        for row in rows:
            item = dict(row)
            try:
                item["messages"] = json.loads(item.pop("messages_json"))
            except (json.JSONDecodeError, TypeError):
                item["messages"] = []
            result.append(item)
        return result

    # ================================================================
    # Single-user contact state
    # ================================================================

    async def update_contact_state(self, user_id: str, context_token: str = ""):
        """Remember the latest reachable WeChat target for single-user mode."""
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO bot_contact_state
                   (id, user_id, context_token, updated_at)
                   VALUES (1, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       user_id = excluded.user_id,
                       context_token = excluded.context_token,
                       updated_at = excluded.updated_at""",
                (user_id, context_token, now),
            )
            await db.commit()

    async def get_contact_state(self) -> dict[str, Any]:
        """Return the latest reachable WeChat target."""
        db = await self._get_db()
        row = await db.execute_fetchall(
            "SELECT * FROM bot_contact_state WHERE id = 1"
        )
        if row:
            return dict(row[0])
        return {"user_id": "", "context_token": "", "updated_at": 0}

    # ================================================================
    # Persona change confirmation
    # ================================================================

    async def set_pending_persona_change(self, content: str, reason: str = ""):
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO pending_persona_change
                   (id, content, reason, created_at)
                   VALUES (1, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       content = excluded.content,
                       reason = excluded.reason,
                       created_at = excluded.created_at""",
                (content, reason, now),
            )
            await db.commit()

    async def get_pending_persona_change(self) -> dict[str, Any] | None:
        db = await self._get_db()
        row = await db.execute_fetchall(
            "SELECT * FROM pending_persona_change WHERE id = 1"
        )
        if row and row[0]["content"]:
            return dict(row[0])
        return None

    async def clear_pending_persona_change(self):
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                "UPDATE pending_persona_change SET content = '', reason = '', created_at = 0 WHERE id = 1"
            )
            await db.commit()

    # ================================================================
    # Compacted conversation summaries
    # ================================================================

    async def get_history_summary(self, user_id: str) -> str:
        db = await self._get_db()
        row = await db.execute_fetchall(
            "SELECT summary FROM history_summaries WHERE user_id = ?",
            (user_id,),
        )
        if row:
            return str(row[0]["summary"] or "")
        return ""

    async def save_history_summary(self, user_id: str, summary: str):
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO history_summaries(user_id, summary, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       summary = excluded.summary,
                       updated_at = excluded.updated_at""",
                (user_id, summary, now),
            )
            await db.commit()

    async def append_conversation_history(
        self,
        user_id: str,
        role: str,
        content: str,
        source: str = "",
        ts: float | None = None,
    ):
        """Persist one raw conversation message for restart recovery."""
        if not user_id or not content:
            return
        now = time.time() if ts is None else float(ts)
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO conversation_history(user_id, role, content, source, ts)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, role, content, source, now),
            )
            await db.commit()

    async def get_recent_conversation_history(
        self,
        user_id: str,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """Load recent raw messages in chronological order."""
        if not user_id:
            return []
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT role, content, source, ts
               FROM conversation_history
               WHERE user_id = ?
               ORDER BY ts DESC
               LIMIT ?""",
            (user_id, max(1, int(limit))),
        )
        return [dict(r) for r in reversed(rows)]

    async def clear_conversation_history(self, user_id: str) -> int:
        """Delete persisted raw conversation history for one user."""
        async with self._lock:
            db = await self._get_db()
            cursor = await db.execute(
                "DELETE FROM conversation_history WHERE user_id = ?",
                (user_id,),
            )
            await db.commit()
            return cursor.rowcount
