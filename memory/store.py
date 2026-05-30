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


async def _migrate_history_summaries(db: aiosqlite.Connection):
    """Ensure history_summaries has PRIMARY KEY (user_id, bot_id).

    Handles three cases:
    - Table missing (fresh DB): create new table, return.
    - Table exists with correct schema: return.
    - Old schema (user_id alone as PK): rebuild via rename → create → copy → drop.
    """
    # Case 1: Table doesn't exist yet (fresh DB)
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='history_summaries'"
    )
    if not await cur.fetchone():
        await db.execute("""
            CREATE TABLE history_summaries (
                user_id TEXT NOT NULL DEFAULT '',
                bot_id TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, bot_id)
            )
        """)
        await db.commit()
        return

    # Check schema: if bot_id column exists, already migrated
    cur = await db.execute("PRAGMA table_info('history_summaries')")
    columns = {row[1] for row in await cur.fetchall()}
    if "bot_id" in columns:
        return  # Case 2: already correct

    # Case 3: Old schema — rebuild
    cur = await db.execute("SELECT user_id, summary, updated_at FROM history_summaries")
    old_rows = await cur.fetchall()

    await db.execute("DROP TABLE IF EXISTS history_summaries")
    await db.execute("""
        CREATE TABLE history_summaries (
            user_id TEXT NOT NULL DEFAULT '',
            bot_id TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, bot_id)
        )
    """)
    for row in old_rows:
        await db.execute(
            """INSERT INTO history_summaries(user_id, bot_id, summary, updated_at)
               VALUES (?, ?, ?, ?)""",
            (row[0], "", row[1], row[2]),
        )
    await db.commit()


class MemoryStore:
    """Persistent memory storage with keyword-based retrieval.

    Thread/async safety: all writes go through a single asyncio.Lock.
    SQLite WAL mode allows concurrent reads during writes.
    """

    _instances: set["MemoryStore"] = set()

    def __init__(self, db_path: str = "./data/memory.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._db: aiosqlite.Connection | None = None
        self._instances.add(self)

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
                bot_id TEXT NOT NULL DEFAULT '',
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
        # history_summaries may have old schema (user_id alone as PK)
        # on upgraded DBs. Rebuild if needed.
        await _migrate_history_summaries(db)
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                user_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                message_count INTEGER NOT NULL DEFAULT 0,
                token_count INTEGER NOT NULL DEFAULT 0,
                elapsed_ms INTEGER NOT NULL DEFAULT 0,
                fallback_type TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                ts REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_events_ts
            ON usage_events(ts DESC)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_events_type_ts
            ON usage_events(event_type, ts DESC)
        """)
        # V2 tables
        await db.execute("""
            CREATE TABLE IF NOT EXISTS turn_drafts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'generated',
                messages_json TEXT NOT NULL,
                send_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_turn_drafts_user_status
            ON turn_drafts(user_id, status)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS turn_diagnostics (
                turn_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '',
                context_policy TEXT NOT NULL DEFAULT '',
                llm_status TEXT NOT NULL DEFAULT '',
                token_count INTEGER NOT NULL DEFAULT 0,
                elapsed_ms INTEGER NOT NULL DEFAULT 0,
                fallback_type TEXT NOT NULL DEFAULT '',
                send_status TEXT NOT NULL DEFAULT '',
                memory_enqueued INTEGER NOT NULL DEFAULT 0,
                input_chars INTEGER NOT NULL DEFAULT 0,
                output_messages INTEGER NOT NULL DEFAULT 0,
                tool_calls INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                ts REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_turn_diagnostics_user_ts
            ON turn_diagnostics(user_id, ts DESC)
        """)
        # V2.1 tables
        # L2: Semantic conversation turns
        await db.execute("""
            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                bot_id TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                turn_number INTEGER NOT NULL DEFAULT 0,
                user_messages TEXT NOT NULL DEFAULT '',
                assistant_actions TEXT NOT NULL DEFAULT '',
                tool_calls TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                trigger TEXT NOT NULL DEFAULT '',
                send_status TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_turns_user_bot
            ON turns(user_id, bot_id, turn_number)
        """)
        # L3: Recap — session-stage summaries
        await db.execute("""
            CREATE TABLE IF NOT EXISTS recaps (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                bot_id TEXT NOT NULL DEFAULT '',
                recap_text TEXT NOT NULL,
                start_turn_id TEXT NOT NULL DEFAULT '',
                end_turn_id TEXT NOT NULL DEFAULT '',
                turn_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_recaps_user_bot
            ON recaps(user_id, bot_id, created_at DESC)
        """)
        # Outbound message tracking (V2.1 semantics)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_messages (
                id TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL,
                batch_id TEXT NOT NULL DEFAULT '',
                candidate_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                bot_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT 'assistant',
                trigger TEXT NOT NULL DEFAULT '',
                visible_to_user INTEGER NOT NULL DEFAULT 1,
                send_status TEXT NOT NULL DEFAULT 'created',
                text TEXT NOT NULL DEFAULT '',
                delay REAL NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0,
                send_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_outbound_messages_turn
            ON outbound_messages(turn_id, position)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_outbound_messages_status
            ON outbound_messages(send_status)
        """)
        # Proactive candidate reasons (V2.1 3-stage pipeline)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS proactive_candidates (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT '',
                bot_id TEXT NOT NULL DEFAULT '',
                turn_id TEXT NOT NULL DEFAULT '',
                batch_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'proactive',
                trigger TEXT NOT NULL DEFAULT 'proactive_reason',
                send_status TEXT NOT NULL DEFAULT '',
                reason_type TEXT NOT NULL,
                reason_detail TEXT NOT NULL DEFAULT '',
                passed_gate INTEGER NOT NULL DEFAULT 0,
                gate_detail TEXT NOT NULL DEFAULT '',
                generated_message TEXT NOT NULL DEFAULT '',
                sent INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        # Shared user facts (independent of bot)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shared_user_facts (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'fact',
                confidence REAL NOT NULL DEFAULT 0.7,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        # V2.1.1: Active topic state (one row per user_id + bot_id)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_topics (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                bot_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_user_ts REAL NOT NULL DEFAULT 0,
                last_assistant_ts REAL NOT NULL DEFAULT 0,
                followup_after_ts REAL NOT NULL DEFAULT 0,
                expires_at REAL NOT NULL DEFAULT 0,
                followup_count INTEGER NOT NULL DEFAULT 0,
                source_turn_id TEXT NOT NULL DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_topics_user_bot
            ON active_topics(user_id, bot_id)
        """)
        # V2.1.1: Proactive schedule state (next-due time tracking)
        # Must use UUID PK so multiple (user_id, bot_id) rows can coexist.
        # If the old single-row schema exists (id INTEGER PRIMARY KEY DEFAULT 1),
        # drop and recreate.
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='proactive_schedule_state'"
        )
        if await cur.fetchone():
            cur = await db.execute("PRAGMA table_info('proactive_schedule_state')")
            cols = {row[1]: row[2] for row in await cur.fetchall()}
            pk_col = cols.get("id", "")
            if pk_col and "INTEGER" in str(pk_col).upper():
                # Old single-row schema — drop and recreate
                await db.execute("DROP TABLE IF EXISTS proactive_schedule_state")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS proactive_schedule_state (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT '',
                bot_id TEXT NOT NULL DEFAULT '',
                next_due_ts REAL NOT NULL DEFAULT 0,
                last_attempt_ts REAL NOT NULL DEFAULT 0,
                last_sent_ts REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_proactive_schedule_user_bot
            ON proactive_schedule_state(user_id, bot_id)
        """)
        # V2.1.2: Clingy followup state machine — independent of topic/proactive schedule
        await db.execute("""
            CREATE TABLE IF NOT EXISTS clingy_state (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT '',
                bot_id TEXT NOT NULL DEFAULT '',
                awaiting_user_reply INTEGER NOT NULL DEFAULT 0,
                last_proactive_sent_at REAL NOT NULL DEFAULT 0,
                last_proactive_intent TEXT NOT NULL DEFAULT '',
                last_proactive_message_id TEXT NOT NULL DEFAULT '',
                next_clingy_due_at REAL NOT NULL DEFAULT 0,
                clingy_followup_count INTEGER NOT NULL DEFAULT 0,
                last_clingy_sent_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_clingy_state_user_bot
            ON clingy_state(user_id, bot_id)
        """)
        # V2.2: Bot emotion state
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_emotion_state (
                user_id TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                bot_mood TEXT DEFAULT 'calm',
                mood_intensity INTEGER DEFAULT 0,
                relationship_stage TEXT DEFAULT 'stranger',
                affection INTEGER DEFAULT 10,
                trust INTEGER DEFAULT 10,
                irritation INTEGER DEFAULT 0,
                attachment INTEGER DEFAULT 0,
                jealousy INTEGER DEFAULT 0,
                last_emotion_reason TEXT,
                updated_at REAL NOT NULL,
                version INTEGER DEFAULT 1,
                PRIMARY KEY(user_id, bot_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_emotion_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                turn_id INTEGER,
                source TEXT,
                trigger_type TEXT,
                event_type TEXT,
                old_state_json TEXT,
                new_state_json TEXT,
                reason TEXT,
                confidence REAL DEFAULT 0.0,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_bot_emotion_events_user_bot
            ON bot_emotion_events(user_id, bot_id, created_at DESC)
        """)
        # V2.2: Dialogue wait state
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dialogue_state (
                user_id TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                wait_state TEXT DEFAULT 'none',
                last_state_reason TEXT,
                last_user_message_at REAL,
                last_assistant_message_at REAL,
                last_user_message_excerpt TEXT,
                last_assistant_message_excerpt TEXT,
                silent_started_at REAL,
                silent_reason TEXT,
                user_messages_since_silent INTEGER DEFAULT 0,
                silent_must_reply_after_count INTEGER DEFAULT 2,
                silent_must_reply_after_ts REAL,
                proactive_paused_until REAL,
                updated_at REAL NOT NULL,
                version INTEGER DEFAULT 1,
                PRIMARY KEY(user_id, bot_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dialogue_state_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                turn_id INTEGER,
                old_state_json TEXT,
                new_state_json TEXT,
                reason TEXT,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_dialogue_state_events_user_bot
            ON dialogue_state_events(user_id, bot_id, created_at DESC)
        """)
        # V2.1: Add bot_id column to existing tables (if not present)
        # Use ALTER TABLE IF NOT EXISTS pattern — SQLite doesn't support
        # conditional column add, so we catch the "duplicate column" error
        for col in [("memories", "bot_id", "TEXT NOT NULL DEFAULT ''"),
                     ("relationship_state", "bot_id", "TEXT NOT NULL DEFAULT ''"),
                     ("conversation_history", "bot_id", "TEXT NOT NULL DEFAULT ''"),
                     ("bot_contact_state", "bot_id", "TEXT NOT NULL DEFAULT ''"),
                     ("proactive_candidates", "user_id", "TEXT NOT NULL DEFAULT ''"),
                     ("proactive_candidates", "bot_id", "TEXT NOT NULL DEFAULT ''"),
                     ("proactive_candidates", "turn_id", "TEXT NOT NULL DEFAULT ''"),
                     ("proactive_candidates", "batch_id", "TEXT NOT NULL DEFAULT ''"),
                     ("proactive_candidates", "source", "TEXT NOT NULL DEFAULT 'proactive'"),
                     ("proactive_candidates", "trigger", "TEXT NOT NULL DEFAULT 'proactive_reason'"),
                     ("proactive_candidates", "send_status", "TEXT NOT NULL DEFAULT ''"),
                     ("outbound_messages", "batch_id", "TEXT NOT NULL DEFAULT ''"),
                     ("outbound_messages", "candidate_id", "TEXT NOT NULL DEFAULT ''")]:
            try:
                await db.execute(
                    f"ALTER TABLE {col[0]} ADD COLUMN {col[1]} {col[2]}"
                )
            except Exception:
                pass  # Column already exists
        # LLMGateway trace log (V2.3: LLM observability)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS llm_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL DEFAULT '',
                task_type TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                thinking TEXT NOT NULL DEFAULT 'disabled',
                response_mode TEXT NOT NULL DEFAULT 'text',
                latency_ms INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 1,
                success INTEGER NOT NULL DEFAULT 0,
                status_code INTEGER,
                error_type TEXT,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
                cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_llm_traces_created
            ON llm_traces(created_at DESC)
        """)
        await db.commit()

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None
        self._instances.discard(self)

    @classmethod
    async def close_all(cls):
        for store in list(cls._instances):
            await store.close()

    async def save_llm_trace(self, trace: dict) -> int | None:
        """Persist an LLM gateway trace record.

        trace is a flat dict built by LLMGateway._build_trace(). Only the
        whitelisted fields are stored; raw_response and reasoning_content are
        never written.
        """
        db = await self._get_db()
        if db is None:
            return None
        cursor = await db.execute(
            """INSERT INTO llm_traces
               (request_id, task_type, provider, model, thinking, response_mode,
                latency_ms, attempt_count, success, status_code, error_type,
                prompt_tokens, completion_tokens, total_tokens,
                cache_hit_tokens, cache_miss_tokens, reasoning_tokens, created_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trace.get("request_id", ""),
                trace.get("task_type", ""),
                trace.get("provider", ""),
                trace.get("model", ""),
                trace.get("thinking", "disabled"),
                trace.get("response_mode", "text"),
                trace.get("latency_ms", 0),
                trace.get("attempt_count", 1),
                trace.get("success", 0),
                trace.get("status_code"),
                trace.get("error_type"),
                trace.get("prompt_tokens", 0),
                trace.get("completion_tokens", 0),
                trace.get("total_tokens", 0),
                trace.get("cache_hit_tokens", 0),
                trace.get("cache_miss_tokens", 0),
                trace.get("reasoning_tokens", 0),
                time.time(),
            ),
        )
        await db.commit()
        return cursor.lastrowid

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

    async def list_bot(self, bot_id: str = "", include_shared: bool = False):
        """List memories for a specific bot.

        Args:
            bot_id: The bot to list memories for
            include_shared: If True, also include shared (bot_id='') memories
        """
        db = await self._get_db()
        if include_shared:
            rows = await db.execute_fetchall(
                "SELECT * FROM memories WHERE bot_id = ? OR bot_id = '' ORDER BY importance DESC",
                (bot_id,),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM memories WHERE bot_id = ? ORDER BY importance DESC",
                (bot_id,),
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

    async def clear_developer_memory_state(self) -> dict[str, int]:
        """Clear model-visible conversation state while keeping prompts/config.

        This is a developer reset, not a database wipe. It removes persisted
        data that can affect future model context or reintroduce old content,
        but leaves prompt files, runtime config, auth/session data, bridge
        contact state, and usage telemetry intact.
        """
        tables = [
            "memories",
            "shared_user_facts",
            "conversation_history",
            "history_summaries",
            "turns",
            "recaps",
            "relationship_state",
            "active_topics",
            "proactive_candidates",
            "proactive_schedule_state",
            "scheduled_messages",
            "turn_drafts",
            "turn_diagnostics",
            "outbound_messages",
        ]
        counts: dict[str, int] = {}
        async with self._lock:
            db = await self._get_db()
            for table in tables:
                cursor = await db.execute(f"DELETE FROM {table}")
                counts[table] = max(0, int(cursor.rowcount or 0))
            await db.commit()
        return counts

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

    async def claim_due_scheduled_messages(
        self,
        now_ts: float | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Atomically claim pending due tasks by marking them 'sending'.

        Returns only the tasks this call actually claimed.
        Prevents duplicate sends when multiple workers or concurrent
        triggers read the same pending tasks.
        """
        now_ts = now_ts if now_ts is not None else time.time()
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            # 1. Read pending due IDs
            pending = await db.execute_fetchall(
                """SELECT id FROM scheduled_messages
                   WHERE status = 'pending' AND due_at <= ?
                   ORDER BY due_at ASC
                   LIMIT ?""",
                (now_ts, limit),
            )
            ids = [row["id"] for row in pending]
            if not ids:
                await db.commit()
                return []

            placeholders = ",".join("?" for _ in ids)
            # 2. Atomically transition only those rows that are still pending
            await db.execute(
                f"""UPDATE scheduled_messages
                    SET status = 'sending', updated_at = ?
                    WHERE id IN ({placeholders}) AND status = 'pending'""",
                (now, *ids),
            )
            # 3. Read back only the rows this call actually transitioned
            rows = await db.execute_fetchall(
                f"""SELECT * FROM scheduled_messages
                    WHERE id IN ({placeholders}) AND status = 'sending'
                    ORDER BY due_at ASC""",
                tuple(ids),
            )
            await db.commit()

        tasks = []
        for row in rows:
            item = dict(row)
            try:
                item["messages"] = json.loads(item.pop("messages_json"))
            except (json.JSONDecodeError, TypeError):
                item["messages"] = []
            tasks.append(item)
        return tasks

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
        """Mark a scheduled task as sent, failed, or sending."""
        if status not in {"sent", "failed", "pending", "sending"}:
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

    async def update_contact_state(
        self,
        user_id: str,
        context_token: str = "",
        bot_id: str = "",
    ):
        """Remember the latest reachable WeChat target for single-user mode."""
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO bot_contact_state
                   (id, user_id, context_token, bot_id, updated_at)
                   VALUES (1, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       user_id = excluded.user_id,
                       context_token = excluded.context_token,
                       bot_id = excluded.bot_id,
                       updated_at = excluded.updated_at""",
                (user_id, context_token, bot_id, now),
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

    async def get_history_summary(self, user_id: str, bot_id: str = "") -> str:
        db = await self._get_db()
        row = await db.execute_fetchall(
            "SELECT summary FROM history_summaries WHERE user_id = ? AND bot_id = ?",
            (user_id, bot_id),
        )
        if row:
            return str(row[0]["summary"] or "")
        return ""

    async def save_history_summary(self, user_id: str, summary: str, bot_id: str = ""):
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            # Delete-then-insert avoids ON CONFLICT schema compatibility issues
            # with databases created before the (user_id, bot_id) PK migration.
            await db.execute(
                "DELETE FROM history_summaries WHERE user_id = ? AND bot_id = ?",
                (user_id, bot_id),
            )
            await db.execute(
                """INSERT INTO history_summaries(user_id, bot_id, summary, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (user_id, bot_id, summary, now),
            )
            await db.commit()

    async def append_conversation_history(
        self,
        user_id: str,
        role: str,
        content: str,
        source: str = "",
        ts: float | None = None,
        bot_id: str = "",
    ):
        """Persist one raw conversation message for restart recovery."""
        if not user_id or not content:
            return
        now = time.time() if ts is None else float(ts)
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO conversation_history(user_id, role, content, source, ts, bot_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, role, content, source, now, bot_id),
            )
            await db.commit()

    async def get_recent_conversation_history(
        self,
        user_id: str,
        limit: int = 40,
        bot_id: str = "",
    ) -> list[dict[str, Any]]:
        """Load recent raw messages in chronological order, scoped by bot.

        V7.5: ORDER BY ts DESC, id DESC ensures stable ordering when
        multiple rows share the same ts value.
        """
        if not user_id:
            return []
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT id, role, content, source, ts
               FROM conversation_history
               WHERE user_id = ? AND bot_id = ?
               ORDER BY ts DESC, id DESC
               LIMIT ?""",
            (user_id, bot_id, max(1, int(limit))),
        )
        return [dict(r) for r in reversed(rows)]

    async def clear_conversation_history(self, user_id: str, bot_id: str = "") -> int:
        """Delete persisted raw conversation history and summary for one user+bot."""
        async with self._lock:
            db = await self._get_db()
            cursor = await db.execute(
                "DELETE FROM conversation_history WHERE user_id = ? AND bot_id = ?",
                (user_id, bot_id),
            )
            await db.execute(
                "DELETE FROM history_summaries WHERE user_id = ? AND bot_id = ?",
                (user_id, bot_id),
            )
            await db.commit()
            return cursor.rowcount

    # ================================================================
    # Usage telemetry
    # ================================================================

    async def record_usage_event(
        self,
        event_type: str,
        user_id: str = "",
        source: str = "",
        message_count: int = 0,
        token_count: int = 0,
        elapsed_ms: int = 0,
        fallback_type: str = "",
        metadata: dict[str, Any] | None = None,
        ts: float | None = None,
    ):
        """Persist lightweight local usage telemetry for the admin dashboard."""
        now = time.time() if ts is None else float(ts)
        payload = json.dumps(metadata or {}, ensure_ascii=False)
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO usage_events
                   (event_type, user_id, source, message_count, token_count,
                    elapsed_ms, fallback_type, metadata_json, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(event_type)[:80],
                    str(user_id)[:200],
                    str(source)[:80],
                    max(0, int(message_count or 0)),
                    max(0, int(token_count or 0)),
                    max(0, int(elapsed_ms or 0)),
                    str(fallback_type)[:120],
                    payload,
                    now,
                ),
            )
            await db.commit()

    async def get_usage_summary(self, today_start_ts: float) -> dict[str, Any]:
        """Return today and all-time usage totals for the admin dashboard."""
        db = await self._get_db()
        today = await self._usage_totals(db, today_start_ts)
        total = await self._usage_totals(db, None)
        recent_fallbacks = await db.execute_fetchall(
            """SELECT event_type, user_id, source, fallback_type,
                      elapsed_ms, metadata_json, ts
               FROM usage_events
               WHERE fallback_type != ''
               ORDER BY ts DESC
               LIMIT 5"""
        )
        return {
            "today": today,
            "total": total,
            "recent_fallbacks": [
                self._usage_fallback_row(row) for row in recent_fallbacks
            ],
        }

    async def _usage_totals(
        self,
        db: aiosqlite.Connection,
        since_ts: float | None,
    ) -> dict[str, int]:
        where = "WHERE ts >= ?" if since_ts is not None else ""
        params = (since_ts,) if since_ts is not None else ()
        rows = await db.execute_fetchall(
            f"""SELECT
                    COALESCE(SUM(message_count), 0) AS messages,
                    COALESCE(SUM(token_count), 0) AS tokens,
                    COALESCE(SUM(elapsed_ms), 0) AS elapsed_ms,
                    COALESCE(SUM(CASE WHEN fallback_type != '' THEN 1 ELSE 0 END), 0) AS fallbacks,
                    COUNT(*) AS events
                FROM usage_events
                {where}""",
            params,
        )
        row = rows[0]
        return {
            "messages": int(row["messages"] or 0),
            "tokens": int(row["tokens"] or 0),
            "elapsed_ms": int(row["elapsed_ms"] or 0),
            "fallbacks": int(row["fallbacks"] or 0),
            "events": int(row["events"] or 0),
        }

    def _usage_fallback_row(self, row: aiosqlite.Row) -> dict[str, Any]:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        return {
            "event_type": row["event_type"],
            "user_id": row["user_id"],
            "source": row["source"],
            "fallback_type": row["fallback_type"],
            "elapsed_ms": int(row["elapsed_ms"] or 0),
            "ts": float(row["ts"] or 0),
            "detail": str(metadata.get("detail", ""))[:200],
        }

    # ================================================================
    # V2: Turn drafts (Phase 3 — Draft And Delivery State)
    # ================================================================

    async def create_turn_draft(
        self,
        turn_id: str,
        user_id: str,
        messages: list[dict[str, Any]],
        source: str = "",
        route: str = "",
    ) -> str:
        """Persist a turn draft before sending. Status: 'generated'."""
        now = time.time()
        payload = json.dumps(messages, ensure_ascii=False)
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT OR REPLACE INTO turn_drafts
                   (id, user_id, source, route, status, messages_json, send_error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'generated', ?, '', ?, ?)""",
                (turn_id, user_id, source, route, payload, now, now),
            )
            await db.commit()
        return turn_id

    async def mark_turn_draft_status(
        self,
        turn_id: str,
        status: str,
        send_error: str = "",
    ) -> bool:
        """Mark a turn draft as sending/delivered/failed/retry_pending/cancelled/skipped."""
        valid = {"generated", "sending", "delivered", "failed", "retry_pending",
                 "cancelled", "skipped"}
        if status not in valid:
            raise ValueError(f"invalid turn draft status: {status}")
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            cursor = await db.execute(
                """UPDATE turn_drafts
                   SET status = ?, send_error = ?, updated_at = ?
                   WHERE id = ?""",
                (status, send_error[:500], now, turn_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_turn_draft(self, turn_id: str) -> dict[str, Any] | None:
        """Get a turn draft by id. Used for outbound idempotency check."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM turn_drafts WHERE id = ?", (turn_id,)
        )
        if rows:
            item = dict(rows[0])
            try:
                item["messages"] = json.loads(item.pop("messages_json"))
            except (json.JSONDecodeError, TypeError):
                item["messages"] = []
            return item
        return None

    async def list_turn_drafts(
        self,
        user_id: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List turn drafts for inspection."""
        db = await self._get_db()
        conditions = []
        params: list[Any] = []
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = await db.execute_fetchall(
            f"SELECT * FROM turn_drafts {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
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
    # V2: Turn diagnostics (Phase 2 — Turn Observability)
    # ================================================================

    async def record_turn_diagnostics(
        self,
        turn_id: str,
        user_id: str,
        source: str = "",
        route: str = "",
        context_policy: str = "",
        llm_status: str = "",
        token_count: int = 0,
        elapsed_ms: int = 0,
        fallback_type: str = "",
        send_status: str = "",
        memory_enqueued: int = 0,
        input_chars: int = 0,
        output_messages: int = 0,
        tool_calls: int = 0,
        metadata: dict[str, Any] | None = None,
    ):
        """Record structured turn diagnostics for every turn."""
        now = time.time()
        payload = json.dumps(metadata or {}, ensure_ascii=False)
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT OR REPLACE INTO turn_diagnostics
                   (turn_id, user_id, source, route, context_policy,
                    llm_status, token_count, elapsed_ms, fallback_type,
                    send_status, memory_enqueued, input_chars, output_messages,
                    tool_calls, metadata_json, ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    turn_id,
                    user_id,
                    source,
                    route,
                    context_policy,
                    llm_status,
                    token_count,
                    elapsed_ms,
                    fallback_type,
                    send_status,
                    memory_enqueued,
                    input_chars,
                    output_messages,
                    tool_calls,
                    payload,
                    now,
                ),
            )
            await db.commit()

    async def update_turn_send_status(
        self,
        turn_id: str,
        send_status: str,
        error: str = "",
    ):
        """Update only the send_status of an existing turn diagnostics row.
        If error is provided, merges into existing metadata_json."""
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                "UPDATE turn_diagnostics SET send_status = ? WHERE turn_id = ?",
                (send_status, turn_id),
            )
            if error:
                row = await db.execute_fetchall(
                    "SELECT metadata_json FROM turn_diagnostics WHERE turn_id = ?",
                    (turn_id,),
                )
                existing = {}
                if row:
                    try:
                        existing = json.loads(row[0]["metadata_json"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        existing = {}
                existing["send_error"] = error[:500]
                await db.execute(
                    "UPDATE turn_diagnostics SET metadata_json = ? WHERE turn_id = ?",
                    (json.dumps(existing, ensure_ascii=False), turn_id),
                )
            await db.commit()

    async def get_recent_turn_diagnostics(
        self,
        limit: int = 20,
        user_id: str = "",
        fallback_only: bool = False,
    ) -> list[dict[str, Any]]:
        """List recent turn diagnostics for the admin panel."""
        db = await self._get_db()
        conditions = []
        params: list[Any] = []
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if fallback_only:
            conditions.append("fallback_type != ''")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = await db.execute_fetchall(
            f"SELECT * FROM turn_diagnostics {where} ORDER BY ts DESC LIMIT ?",
            tuple(params),
        )
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json", "{}"))
            except (json.JSONDecodeError, TypeError):
                item["metadata"] = {}
            result.append(item)
        return result

    # ================================================================
    # V2.1: Turns (L2) — semantic conversation turns
    # ================================================================

    async def create_turn(
        self,
        turn_id: str,
        user_id: str,
        bot_id: str = "",
        user_messages: str = "",
        assistant_actions: str = "",
        tool_calls: str = "",
        source: str = "",
        trigger: str = "",
        send_status: str = "",
    ) -> str:
        """Record a semantic conversation turn (L2)."""
        import uuid
        if not turn_id:
            turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        now = time.time()

        db = await self._get_db()
        row = await db.execute_fetchall(
            """SELECT COALESCE(MAX(turn_number), 0) + 1 AS next
               FROM turns WHERE user_id = ? AND bot_id = ?""",
            (user_id, bot_id),
        )
        turn_number = int(row[0]["next"]) if row else 1

        session_id = f"{user_id}_{bot_id}"

        async with self._lock:
            await db.execute(
                """INSERT OR REPLACE INTO turns
                   (id, user_id, bot_id, session_id, turn_number,
                    user_messages, assistant_actions, tool_calls,
                    source, trigger, send_status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (turn_id, user_id, bot_id, session_id, turn_number,
                 user_messages, assistant_actions, tool_calls,
                 source, trigger, send_status, now),
            )
            await db.commit()
        return turn_id

    async def get_recent_turns(
        self,
        user_id: str,
        bot_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get recent turns for a user+bot."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT * FROM turns
               WHERE user_id = ? AND bot_id = ?
               ORDER BY turn_number DESC
               LIMIT ?""",
            (user_id, bot_id, limit),
        )
        return [dict(r) for r in reversed(rows)]

    async def count_unsummarized_turns(
        self,
        user_id: str,
        bot_id: str = "",
        since_turn_id: str = "",
    ) -> int:
        """Count turns that need recap generation."""
        db = await self._get_db()
        if since_turn_id:
            row = await db.execute_fetchall(
                """SELECT turn_number FROM turns WHERE id = ?""",
                (since_turn_id,),
            )
            since_number = int(row[0]["turn_number"]) if row else 0
            rows = await db.execute_fetchall(
                """SELECT COUNT(*) AS cnt FROM turns
                   WHERE user_id = ? AND bot_id = ? AND turn_number > ?""",
                (user_id, bot_id, since_number),
            )
        else:
            rows = await db.execute_fetchall(
                """SELECT COUNT(*) AS cnt FROM turns
                   WHERE user_id = ? AND bot_id = ?""",
                (user_id, bot_id),
            )
        return int(rows[0]["cnt"]) if rows else 0

    # ================================================================
    # V2.1: Recap (L3) — session-stage summaries
    # ================================================================

    async def save_recap(
        self,
        user_id: str,
        bot_id: str,
        recap_text: str,
        start_turn_id: str = "",
        end_turn_id: str = "",
        turn_count: int = 0,
    ) -> str:
        """Save a recap entry (L3)."""
        import uuid
        recap_id = f"recap_{uuid.uuid4().hex[:12]}"
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO recaps
                   (id, user_id, bot_id, recap_text,
                    start_turn_id, end_turn_id, turn_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (recap_id, user_id, bot_id, str(recap_text)[:4000],
                 start_turn_id, end_turn_id, turn_count, now),
            )
            await db.commit()
        return recap_id

    async def get_latest_recap(
        self,
        user_id: str,
        bot_id: str = "",
    ) -> dict[str, Any] | None:
        """Get the most recent recap for a user+bot."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT * FROM recaps
               WHERE user_id = ? AND bot_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, bot_id),
        )
        return dict(rows[0]) if rows else None

    async def get_recaps(
        self,
        user_id: str,
        bot_id: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Get recent recaps for inspection."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT * FROM recaps
               WHERE user_id = ? AND bot_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, bot_id, limit),
        )
        return [dict(r) for r in rows]

    # ================================================================
    # V2.1: Outbound message tracking
    # ================================================================

    async def record_outbound_messages(
        self,
        turn_id: str,
        messages: list[dict[str, Any]],
        user_id: str = "",
        bot_id: str = "",
        source: str = "user_reply",
        author: str = "assistant",
        trigger: str = "user_message",
        visible_to_user: bool = True,
        send_status: str = "created",
        candidate_id: str = "",
        batch_id: str = "",
    ) -> list[str]:
        """Record outbound messages with full V2.1 semantics."""
        import uuid
        now = time.time()
        batch_id = batch_id or turn_id
        message_ids: list[str] = []

        async with self._lock:
            db = await self._get_db()
            for i, msg in enumerate(messages):
                msg_id = f"om_{uuid.uuid4().hex[:12]}"
                text = str(msg.get("text", ""))
                delay = float(msg.get("delay", 0) or 0)
                await db.execute(
                    """INSERT INTO outbound_messages
                       (id, turn_id, batch_id, candidate_id,
                        user_id, bot_id, source, author,
                        trigger, visible_to_user, send_status,
                        text, delay, position, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (msg_id, turn_id, batch_id, candidate_id,
                     user_id, bot_id, source, author,
                     trigger, int(visible_to_user), send_status,
                     text, delay, i, now, now),
                )
                message_ids.append(msg_id)
            await db.commit()
        return message_ids

    async def update_outbound_status(
        self,
        message_id: str,
        send_status: str,
        send_error: str = "",
    ):
        """Update status of a single outbound message."""
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """UPDATE outbound_messages
                   SET send_status = ?, send_error = ?, updated_at = ?
                   WHERE id = ?""",
                (send_status, send_error[:500], now, message_id),
            )
            await db.commit()

    async def mark_outbound_batch_status(
        self,
        turn_id: str,
        send_status: str,
        send_error: str = "",
    ):
        """Update status of all outbound messages for a turn."""
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """UPDATE outbound_messages
                   SET send_status = ?, send_error = ?, updated_at = ?
                   WHERE turn_id = ?""",
                (send_status, send_error[:500], now, turn_id),
            )
            await db.commit()

    async def get_outbound_messages(
        self,
        turn_id: str = "",
        user_id: str = "",
        send_status: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get outbound messages with optional filters."""
        db = await self._get_db()
        conditions: list[str] = []
        params: list[Any] = []
        if turn_id:
            conditions.append("turn_id = ?")
            params.append(turn_id)
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if send_status:
            conditions.append("send_status = ?")
            params.append(send_status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = await db.execute_fetchall(
            f"SELECT * FROM outbound_messages {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
        return [dict(r) for r in rows]

    # ================================================================
    # V2.1: Proactive candidates
    # ================================================================

    async def create_proactive_candidate(
        self,
        reason_type: str,
        reason_detail: str = "",
        passed_gate: bool = False,
        gate_detail: str = "",
        user_id: str = "",
        bot_id: str = "",
        source: str = "proactive",
        trigger: str = "proactive_reason",
    ) -> str:
        """Record a proactive candidate reason (V2.1 Stage 1)."""
        import uuid
        candidate_id = f"pc_{uuid.uuid4().hex[:12]}"
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO proactive_candidates
                   (id, user_id, bot_id, source, trigger,
                    reason_type, reason_detail, passed_gate,
                    gate_detail, generated_message, sent, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?)""",
                (candidate_id, user_id, bot_id, source, trigger,
                 reason_type, reason_detail, int(passed_gate), gate_detail, now),
            )
            await db.commit()
        return candidate_id

    async def update_proactive_candidate(
        self,
        candidate_id: str,
        passed_gate: bool | None = None,
        gate_detail: str = "",
        generated_message: str = "",
        sent: bool = False,
        turn_id: str = "",
        batch_id: str = "",
        send_status: str = "",
    ):
        """Update a proactive candidate after decision gate or send."""
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            set_parts: list[str] = []
            values: list[Any] = []
            if passed_gate is not None:
                set_parts.append("passed_gate = ?")
                values.append(int(passed_gate))
            if gate_detail:
                set_parts.append("gate_detail = ?")
                values.append(gate_detail[:500])
            if generated_message:
                set_parts.append("generated_message = ?")
                values.append(generated_message[:1000])
            if sent:
                set_parts.append("sent = 1")
            if turn_id:
                set_parts.append("turn_id = ?")
                values.append(turn_id[:120])
            if batch_id:
                set_parts.append("batch_id = ?")
                values.append(batch_id[:120])
            if send_status:
                set_parts.append("send_status = ?")
                values.append(send_status[:40])
            if set_parts:
                values.append(candidate_id)
                await db.execute(
                    f"UPDATE proactive_candidates SET {', '.join(set_parts)} WHERE id = ?",
                    values,
                )
                await db.commit()

    async def get_recent_proactive_candidates(
        self,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get recent proactive candidate records."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM proactive_candidates ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def count_today_proactive_sent(self, today_start_ts: float) -> int:
        """Count proactive messages sent today."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT COUNT(*) AS cnt FROM proactive_candidates
               WHERE sent = 1 AND created_at >= ?""",
            (today_start_ts,),
        )
        return int(rows[0]["cnt"]) if rows else 0

    # ================================================================
    # V2.1: Shared user facts (independent of bot)
    # ================================================================

    async def save_shared_fact(
        self,
        content: str,
        category: str = "user_preference",
        confidence: float = 0.7,
    ) -> str:
        """Save a shared user fact."""
        import uuid
        fact_id = f"fact_{uuid.uuid4().hex[:12]}"
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT OR REPLACE INTO shared_user_facts
                   (id, content, category, confidence, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (fact_id, content[:2000], category, confidence, now, now),
            )
            await db.commit()
        return fact_id

    async def get_shared_facts(
        self,
        category: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get shared user facts."""
        db = await self._get_db()
        if category:
            rows = await db.execute_fetchall(
                """SELECT * FROM shared_user_facts
                   WHERE category = ?
                   ORDER BY confidence DESC LIMIT ?""",
                (category, limit),
            )
        else:
            rows = await db.execute_fetchall(
                """SELECT * FROM shared_user_facts
                   ORDER BY confidence DESC LIMIT ?""",
                (limit,),
            )
        return [dict(r) for r in rows]

    # ================================================================
    # V2.1.1: Active topic state
    # ================================================================

    async def upsert_active_topic(
        self,
        user_id: str,
        bot_id: str,
        title: str = "",
        summary: str = "",
        status: str = "open",
        followup_after_ts: float = 0,
        expires_at: float = 0,
        last_user_ts: float = 0,
        last_assistant_ts: float = 0,
        source_turn_id: str = "",
    ) -> str:
        """Create or replace the active topic for a user+bot.

        V2.1.1: One row per (user_id, bot_id). New topic replaces old.
        """
        import uuid
        now = time.time()
        topic_id = f"topic_{uuid.uuid4().hex[:12]}"

        async with self._lock:
            db = await self._get_db()
            # Delete existing active topic for this user+bot (single-row model)
            await db.execute(
                "DELETE FROM active_topics WHERE user_id = ? AND bot_id = ?",
                (user_id, bot_id),
            )
            await db.execute(
                """INSERT INTO active_topics
                   (id, user_id, bot_id, title, summary, status,
                    created_at, updated_at, last_user_ts, last_assistant_ts,
                    followup_after_ts, expires_at, followup_count,
                    source_turn_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (topic_id, user_id, bot_id, title, summary, status,
                 now, now, last_user_ts, last_assistant_ts,
                 followup_after_ts, expires_at, source_turn_id),
            )
            await db.commit()
        return topic_id

    async def get_active_topic(
        self,
        user_id: str,
        bot_id: str = "",
    ) -> dict[str, Any] | None:
        """Get the current active topic for a user+bot."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT * FROM active_topics
               WHERE user_id = ? AND bot_id = ?
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id, bot_id),
        )
        return dict(rows[0]) if rows else None

    async def update_active_topic(
        self,
        user_id: str,
        bot_id: str,
        title: str | None = None,
        summary: str | None = None,
        status: str | None = None,
        followup_after_ts: float | None = None,
        expires_at: float | None = None,
        last_user_ts: float | None = None,
        last_assistant_ts: float | None = None,
        source_turn_id: str | None = None,
    ) -> bool:
        """Update fields on the current active topic."""
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            set_parts = ["updated_at = ?"]
            values: list[Any] = [now]
            field_map = {
                "title": title, "summary": summary, "status": status,
                "followup_after_ts": followup_after_ts,
                "expires_at": expires_at,
                "last_user_ts": last_user_ts,
                "last_assistant_ts": last_assistant_ts,
                "source_turn_id": source_turn_id,
            }
            for field, val in field_map.items():
                if val is not None:
                    set_parts.append(f"{field} = ?")
                    values.append(val)
            values.extend([user_id, bot_id])
            cursor = await db.execute(
                f"UPDATE active_topics SET {', '.join(set_parts)} WHERE user_id = ? AND bot_id = ?",
                values,
            )
            await db.commit()
            return cursor.rowcount > 0

    async def close_active_topic(
        self,
        user_id: str,
        bot_id: str,
        reason: str = "",
    ) -> bool:
        """Close the current active topic."""
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            summary_suffix = f" (closed: {reason})" if reason else " (closed)"
            cursor = await db.execute(
                """UPDATE active_topics
                   SET status = 'closed', updated_at = ?,
                       summary = summary || ?
                   WHERE user_id = ? AND bot_id = ?""",
                (now, summary_suffix[:200], user_id, bot_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def mark_active_topic_stale(
        self,
        user_id: str,
        bot_id: str,
        reason: str = "",
    ) -> bool:
        """Mark the current active topic as stale."""
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            summary_suffix = f" (stale: {reason})" if reason else " (stale)"
            cursor = await db.execute(
                """UPDATE active_topics
                   SET status = 'stale', updated_at = ?,
                       summary = summary || ?
                   WHERE user_id = ? AND bot_id = ?""",
                (now, summary_suffix[:200], user_id, bot_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def increment_topic_followup(self, topic_id: str) -> bool:
        """Increment followup count for a topic."""
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            cursor = await db.execute(
                """UPDATE active_topics
                   SET followup_count = followup_count + 1,
                       updated_at = ?
                   WHERE id = ?""",
                (now, topic_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    # ================================================================
    # V2.1.1: Proactive schedule state (next-due)
    # ================================================================

    async def get_proactive_schedule_state(
        self,
        user_id: str = "",
        bot_id: str = "",
    ) -> dict[str, Any]:
        """Get proactive schedule state for a user+bot."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT * FROM proactive_schedule_state
               WHERE user_id = ? AND bot_id = ?
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id, bot_id),
        )
        if rows:
            return dict(rows[0])
        return {
            "next_due_ts": 0,
            "last_attempt_ts": 0,
            "last_sent_ts": 0,
        }

    async def update_proactive_schedule_state(
        self,
        user_id: str,
        bot_id: str,
        next_due_ts: float | None = None,
        last_attempt_ts: float | None = None,
        last_sent_ts: float | None = None,
    ) -> bool:
        """Update proactive schedule state for a user+bot pair.

        Creates a new row if none exists for this (user_id, bot_id).
        """
        import uuid
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            # Check if row exists for this user+bot
            cur = await db.execute(
                "SELECT id FROM proactive_schedule_state WHERE user_id = ? AND bot_id = ?",
                (user_id, bot_id),
            )
            existing = await cur.fetchone()
            if existing:
                row_id = existing[0]
            else:
                # Create new row with UUID
                row_id = f"pss_{uuid.uuid4().hex[:12]}"
                await db.execute(
                    """INSERT INTO proactive_schedule_state
                       (id, user_id, bot_id, updated_at)
                       VALUES (?, ?, ?, ?)""",
                    (row_id, user_id, bot_id, now),
                )

            set_parts = ["updated_at = ?"]
            values: list[Any] = [now]
            if next_due_ts is not None:
                set_parts.append("next_due_ts = ?")
                values.append(next_due_ts)
            if last_attempt_ts is not None:
                set_parts.append("last_attempt_ts = ?")
                values.append(last_attempt_ts)
            if last_sent_ts is not None:
                set_parts.append("last_sent_ts = ?")
                values.append(last_sent_ts)
            values.extend([user_id, bot_id])
            await db.execute(
                f"UPDATE proactive_schedule_state SET {', '.join(set_parts)} WHERE user_id = ? AND bot_id = ?",
                values,
            )
            await db.commit()
            return True

    # ================================================================
    # V2.1.2: Clingy followup state machine
    # ================================================================

    async def get_clingy_state(
        self,
        user_id: str = "",
        bot_id: str = "",
    ) -> dict[str, Any]:
        """Get clingy followup state for a user+bot."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT * FROM clingy_state
               WHERE user_id = ? AND bot_id = ?
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id, bot_id),
        )
        if rows:
            return dict(rows[0])
        return {
            "awaiting_user_reply": False,
            "last_proactive_sent_at": 0,
            "last_proactive_intent": "",
            "last_proactive_message_id": "",
            "next_clingy_due_at": 0,
            "clingy_followup_count": 0,
            "last_clingy_sent_at": 0,
        }

    async def update_clingy_state(
        self,
        user_id: str,
        bot_id: str,
        **kwargs: Any,
    ) -> bool:
        """Update clingy followup state for a user+bot pair."""
        import uuid
        now = time.time()
        allowed = {
            "awaiting_user_reply", "last_proactive_sent_at",
            "last_proactive_intent", "last_proactive_message_id",
            "next_clingy_due_at", "clingy_followup_count",
            "last_clingy_sent_at",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        async with self._lock:
            db = await self._get_db()
            cur = await db.execute(
                "SELECT id FROM clingy_state WHERE user_id = ? AND bot_id = ?",
                (user_id, bot_id),
            )
            existing = await cur.fetchone()
            if existing:
                row_id = existing[0]
            else:
                row_id = f"cs_{uuid.uuid4().hex[:12]}"
                await db.execute(
                    """INSERT INTO clingy_state (id, user_id, bot_id, updated_at)
                       VALUES (?, ?, ?, ?)""",
                    (row_id, user_id, bot_id, now),
                )
            set_parts = ["updated_at = ?"]
            values: list[Any] = [now]
            for field, value in updates.items():
                set_parts.append(f"{field} = ?")
                values.append(value)
            values.extend([user_id, bot_id])
            await db.execute(
                f"UPDATE clingy_state SET {', '.join(set_parts)} WHERE user_id = ? AND bot_id = ?",
                values,
            )
            await db.commit()
            return True

    async def clear_clingy_awaiting(self, user_id: str, bot_id: str) -> bool:
        """Clear clingy awaiting state when user sends a real message."""
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """UPDATE clingy_state
                   SET awaiting_user_reply = 0,
                       next_clingy_due_at = 0,
                       clingy_followup_count = 0,
                       updated_at = ?
                   WHERE user_id = ? AND bot_id = ?""",
                (now, user_id, bot_id),
            )
            await db.commit()
            return True

    # ================================================================
    # V2.1: Bot-scoped helpers
    # ================================================================

    async def save_memory_bot(
        self,
        content: str,
        bot_id: str = "",
        category: str = "other",
        keywords: str = "",
        source_turn_id: str = "",
        min_importance: float = 0.4,
    ) -> str | None:
        """Save memory scoped to a bot."""
        if not content or not content.strip():
            return None
        content = content.strip()

        existing = await self.list_bot(bot_id)
        existing_contents = [{"content": m["content"]} for m in existing]
        if is_duplicate(content, existing_contents):
            return None

        cat_count = sum(1 for m in existing if m.get("category") == category)
        importance = compute_importance(content, cat_count)
        if importance < min_importance:
            return None

        import uuid as _uuid
        now = time.time()
        mem_id = f"mem_{_uuid.uuid4().hex[:12]}"

        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT INTO memories
                   (id, content, category, importance, keywords,
                    source_turn_id, bot_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mem_id, content, category, importance,
                 keywords, source_turn_id, bot_id, now, now),
            )
            await db.commit()
        return mem_id

    async def search_bot(
        self,
        query: str,
        bot_id: str = "",
        top_k: int = 5,
        category: str | None = None,
        include_shared: bool = False,
    ) -> list[dict[str, Any]]:
        """Search memories scoped to a bot.

        Args:
            include_shared: If True, also include shared (bot_id='') memories
        """
        db = await self._get_db()
        if category:
            if include_shared:
                rows = await db.execute_fetchall(
                    """SELECT * FROM memories
                       WHERE category = ? AND (bot_id = ? OR bot_id = '')
                       ORDER BY importance DESC""",
                    (category, bot_id),
                )
            else:
                rows = await db.execute_fetchall(
                    """SELECT * FROM memories
                       WHERE category = ? AND bot_id = ?
                       ORDER BY importance DESC""",
                    (category, bot_id),
                )
        else:
            if include_shared:
                rows = await db.execute_fetchall(
                    """SELECT * FROM memories
                       WHERE bot_id = ? OR bot_id = ''
                       ORDER BY importance DESC""",
                    (bot_id,),
                )
            else:
                rows = await db.execute_fetchall(
                    """SELECT * FROM memories
                       WHERE bot_id = ?
                       ORDER BY importance DESC""",
                    (bot_id,),
                )

        scored = []
        for row in rows:
            rd = dict(row)
            # Add scope marker
            if rd.get("bot_id") == "":
                rd["scope"] = "shared_user_fact"
            else:
                rd["scope"] = "bot_private"
            memory_content = str(rd["content"])
            sim = jaccard_similarity(query, memory_content)
            importance = float(rd["importance"])
            score = sim * 0.5 + importance * 0.5
            scored.append((score, rd))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:top_k]]

    async def get_relationship_bot(self, bot_id: str) -> dict[str, Any]:
        """Get relationship state scoped to a bot."""
        db = await self._get_db()
        row = await db.execute_fetchall(
            "SELECT * FROM relationship_state WHERE bot_id = ?",
            (bot_id,),
        )
        if row:
            return dict(row[0])
        return {
            "familiarity": 5,
            "chat_boundary": "",
            "current_atmosphere": "neutral",
            "interaction_style": "casual",
        }

    async def update_relationship_bot(self, bot_id: str, updates: dict[str, Any]):
        """Update relationship state for a specific bot."""
        db = await self._get_db()
        now = time.time()

        await db.execute(
            """INSERT OR IGNORE INTO relationship_state
               (id, bot_id, updated_at) VALUES (NULL, ?, ?)""",
            (bot_id, now),
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
        values.append(bot_id)

        async with self._lock:
            await db.execute(
                f"UPDATE relationship_state SET {', '.join(set_parts)} WHERE bot_id = ? AND id IS NOT NULL",
                values,
            )
            await db.commit()

    # ================================================================
    # V2.2: Bot emotion state CRUD
    # ================================================================

    async def get_bot_emotion_state(
        self,
        user_id: str,
        bot_id: str,
        defaults: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get bot emotion state for a user+bot. Returns defaults if no record.

        If `defaults` is provided and no DB row exists, those values are used
        instead of the hardcoded legacy defaults. `version` is set to 0 so
        callers can detect an unpersisted state.
        """
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT * FROM bot_emotion_state
               WHERE user_id = ? AND bot_id = ?""",
            (user_id, bot_id),
        )
        if rows:
            return dict(rows[0])
        if defaults:
            return {
                "user_id": user_id,
                "bot_id": bot_id,
                "bot_mood": str(defaults.get("bot_mood", "calm")),
                "mood_intensity": int(defaults.get("mood_intensity", 0)),
                "relationship_stage": str(defaults.get("relationship_stage", "stranger")),
                "affection": int(defaults.get("affection", 10)),
                "trust": int(defaults.get("trust", 10)),
                "irritation": int(defaults.get("irritation", 0)),
                "attachment": int(defaults.get("attachment", 0)),
                "jealousy": int(defaults.get("jealousy", 0)),
                "last_emotion_reason": str(defaults.get("last_emotion_reason", "")),
                "updated_at": time.time(),
                "version": 0,  # 0 = not persisted yet
            }
        return {
            "user_id": user_id,
            "bot_id": bot_id,
            "bot_mood": "calm",
            "mood_intensity": 0,
            "relationship_stage": "stranger",
            "affection": 10,
            "trust": 10,
            "irritation": 0,
            "attachment": 0,
            "jealousy": 0,
            "last_emotion_reason": "",
            "updated_at": time.time(),
            "version": 1,
        }

    async def upsert_bot_emotion_state(
        self,
        user_id: str,
        bot_id: str,
        state: dict[str, Any],
    ) -> bool:
        """Insert or replace bot emotion state for a user+bot pair."""
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT OR REPLACE INTO bot_emotion_state
                   (user_id, bot_id, bot_mood, mood_intensity,
                    relationship_stage, affection, trust,
                    irritation, attachment, jealousy,
                    last_emotion_reason, updated_at, version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, bot_id,
                    str(state.get("bot_mood", "calm")),
                    int(state.get("mood_intensity", 0)),
                    str(state.get("relationship_stage", "stranger")),
                    max(0, min(100, int(state.get("affection", 10)))),
                    max(0, min(100, int(state.get("trust", 10)))),
                    max(0, min(100, int(state.get("irritation", 0)))),
                    max(0, min(100, int(state.get("attachment", 0)))),
                    max(0, min(100, int(state.get("jealousy", 0)))),
                    str(state.get("last_emotion_reason", "")),
                    now,
                    int(state.get("version", 1)) + 1,
                ),
            )
            await db.commit()
        return True

    async def insert_bot_emotion_event(
        self,
        user_id: str,
        bot_id: str,
        turn_id: int = 0,
        source: str = "",
        trigger_type: str = "",
        event_type: str = "",
        old_state_json: str = "{}",
        new_state_json: str = "{}",
        reason: str = "",
        confidence: float = 0.0,
        created_at: float | None = None,
    ) -> int:
        """Insert an emotion event record. Returns the event id."""
        now = created_at if created_at is not None else time.time()
        async with self._lock:
            db = await self._get_db()
            cursor = await db.execute(
                """INSERT INTO bot_emotion_events
                   (user_id, bot_id, turn_id, source, trigger_type,
                    event_type, old_state_json, new_state_json,
                    reason, confidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, bot_id, turn_id, source, trigger_type,
                    event_type, old_state_json, new_state_json,
                    reason, confidence, now,
                ),
            )
            await db.commit()
            return cursor.lastrowid or 0

    async def get_recent_bot_emotion_events(
        self,
        user_id: str,
        bot_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get recent emotion events for a user+bot."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT * FROM bot_emotion_events
               WHERE user_id = ? AND bot_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, bot_id, limit),
        )
        return [dict(r) for r in (rows or [])]

    async def reset_bot_emotion_state(
        self,
        user_id: str,
        bot_id: str,
    ) -> bool:
        """Delete the emotion state row for a user+bot, restoring defaults."""
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                "DELETE FROM bot_emotion_state WHERE user_id = ? AND bot_id = ?",
                (user_id, bot_id),
            )
            await db.commit()
        return True

    # ================================================================
    # V2.2: Dialogue state CRUD
    # ================================================================

    async def get_dialogue_state(
        self,
        user_id: str,
        bot_id: str,
    ) -> dict[str, Any]:
        """Get dialogue wait state for a user+bot. Returns defaults if no record."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT * FROM dialogue_state
               WHERE user_id = ? AND bot_id = ?""",
            (user_id, bot_id),
        )
        if rows:
            return dict(rows[0])
        return {
            "user_id": user_id,
            "bot_id": bot_id,
            "wait_state": "none",
            "last_state_reason": "",
            "last_user_message_at": 0,
            "last_assistant_message_at": 0,
            "last_user_message_excerpt": "",
            "last_assistant_message_excerpt": "",
            "silent_started_at": 0,
            "silent_reason": "",
            "user_messages_since_silent": 0,
            "silent_must_reply_after_count": 2,
            "silent_must_reply_after_ts": 0,
            "proactive_paused_until": 0,
            "updated_at": time.time(),
            "version": 1,
        }

    async def upsert_dialogue_state(
        self,
        user_id: str,
        bot_id: str,
        state: dict[str, Any],
    ) -> bool:
        """Insert or replace dialogue state for a user+bot pair."""
        now = time.time()
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                """INSERT OR REPLACE INTO dialogue_state
                   (user_id, bot_id, wait_state, last_state_reason,
                    last_user_message_at, last_assistant_message_at,
                    last_user_message_excerpt, last_assistant_message_excerpt,
                    silent_started_at, silent_reason,
                    user_messages_since_silent, silent_must_reply_after_count,
                    silent_must_reply_after_ts, proactive_paused_until,
                    updated_at, version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, bot_id,
                    str(state.get("wait_state", "none")),
                    str(state.get("last_state_reason", "")),
                    float(state.get("last_user_message_at", 0)),
                    float(state.get("last_assistant_message_at", 0)),
                    str(state.get("last_user_message_excerpt", ""))[:200],
                    str(state.get("last_assistant_message_excerpt", ""))[:200],
                    float(state.get("silent_started_at", 0)),
                    str(state.get("silent_reason", "")),
                    max(0, int(state.get("user_messages_since_silent", 0))),
                    max(0, int(state.get("silent_must_reply_after_count", 2))),
                    float(state.get("silent_must_reply_after_ts", 0)),
                    float(state.get("proactive_paused_until", 0)),
                    now,
                    int(state.get("version", 1)) + 1,
                ),
            )
            await db.commit()
        return True

    async def insert_dialogue_state_event(
        self,
        user_id: str,
        bot_id: str,
        turn_id: int = 0,
        old_state_json: str = "{}",
        new_state_json: str = "{}",
        reason: str = "",
        created_at: float | None = None,
    ) -> int:
        """Insert a dialogue state event record. Returns the event id."""
        now = created_at if created_at is not None else time.time()
        async with self._lock:
            db = await self._get_db()
            cursor = await db.execute(
                """INSERT INTO dialogue_state_events
                   (user_id, bot_id, turn_id, old_state_json,
                    new_state_json, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, bot_id, turn_id, old_state_json,
                 new_state_json, reason, now),
            )
            await db.commit()
            return cursor.lastrowid or 0

    async def get_recent_dialogue_state_events(
        self,
        user_id: str,
        bot_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get recent dialogue state events for a user+bot."""
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """SELECT * FROM dialogue_state_events
               WHERE user_id = ? AND bot_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, bot_id, limit),
        )
        return [dict(r) for r in (rows or [])]

    async def reset_dialogue_state(
        self,
        user_id: str,
        bot_id: str,
    ) -> bool:
        """Delete the dialogue state row for a user+bot, restoring defaults."""
        async with self._lock:
            db = await self._get_db()
            await db.execute(
                "DELETE FROM dialogue_state WHERE user_id = ? AND bot_id = ?",
                (user_id, bot_id),
            )
            await db.commit()
        return True
