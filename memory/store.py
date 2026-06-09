"""SQLite persistence for WeChat contact state and ordinary chat history."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA_ID = "chat_only_1"


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
        if not row or row[0]["value"] != _SCHEMA_ID:
            await self._reset_non_contact_tables(db)
        await self._create_tables(db)
        await db.execute(
            "INSERT OR REPLACE INTO runtime_meta(key, value) VALUES('runtime_schema', ?)",
            (_SCHEMA_ID,),
        )
        await db.commit()

    async def _reset_non_contact_tables(self, db: aiosqlite.Connection) -> None:
        rows = await db.execute_fetchall(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        keep = {"runtime_meta", "bot_contact_state"}
        for row in rows:
            name = str(row["name"])
            if name not in keep:
                escaped = name.replace('"', '""')
                await db.execute(f'DROP TABLE IF EXISTS "{escaped}"')

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

            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                bot_id TEXT NOT NULL DEFAULT '',
                turn_number INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                trigger TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                send_status TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                completed_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS protocol_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                bot_id TEXT NOT NULL DEFAULT '',
                call_id TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL,
                message_kind TEXT NOT NULL DEFAULT '',
                content_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(turn_id, seq)
            );

            CREATE TABLE IF NOT EXISTS outbound_messages (
                id TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL,
                user_id TEXT NOT NULL DEFAULT '',
                bot_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'user_reply',
                author TEXT NOT NULL DEFAULT 'assistant',
                trigger TEXT NOT NULL DEFAULT 'user_message',
                send_status TEXT NOT NULL DEFAULT 'created',
                text TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                send_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS turn_drafts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'generated',
                messages_json TEXT NOT NULL DEFAULT '[]',
                send_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

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
                input_chars INTEGER NOT NULL DEFAULT 0,
                output_messages INTEGER NOT NULL DEFAULT 0,
                tool_calls INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                ts REAL NOT NULL
            );

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
            );

            CREATE TABLE IF NOT EXISTS visible_activity (
                user_id TEXT NOT NULL,
                bot_id TEXT NOT NULL DEFAULT '',
                last_visible_at REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(user_id, bot_id)
            );

            CREATE TABLE IF NOT EXISTS llm_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL DEFAULT '',
                turn_id TEXT NOT NULL DEFAULT '',
                call_id TEXT NOT NULL DEFAULT '',
                task_type TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                thinking TEXT NOT NULL DEFAULT '',
                response_mode TEXT NOT NULL DEFAULT '',
                latency_ms INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 0,
                status_code INTEGER,
                error_type TEXT,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
                cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                request_json TEXT NOT NULL DEFAULT '{}',
                response_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
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

    async def update_contact_state(
        self,
        user_id: str,
        context_token: str = "",
        bot_id: str = "",
    ) -> None:
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """
                INSERT INTO bot_contact_state(id, user_id, context_token, bot_id, updated_at)
                VALUES(1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    user_id = excluded.user_id,
                    context_token = excluded.context_token,
                    bot_id = excluded.bot_id,
                    updated_at = excluded.updated_at
                """,
                (user_id, context_token, bot_id, time.time()),
            )
            await db.commit()

    async def get_contact_state(self) -> dict[str, Any]:
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT * FROM bot_contact_state WHERE id = 1"
        )
        return dict(rows[0]) if rows else {
            "user_id": "",
            "context_token": "",
            "bot_id": "",
            "updated_at": 0,
        }

    async def begin_protocol_turn(
        self,
        turn_id: str,
        user_id: str,
        bot_id: str = "",
        source: str = "",
        trigger: str = "",
        user_message: str | None = None,
    ) -> str:
        now = time.time()
        async with self._write_lock:
            db = await self._get_db()
            rows = await db.execute_fetchall(
                "SELECT COALESCE(MAX(turn_number), 0) + 1 AS value "
                "FROM turns WHERE user_id = ? AND bot_id = ?",
                (user_id, bot_id),
            )
            number = int(rows[0]["value"])
            await db.execute(
                """
                INSERT INTO turns(
                    id, user_id, bot_id, turn_number, source, trigger,
                    status, send_status, created_at, completed_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'running', '', ?, 0, ?)
                """,
                (turn_id, user_id, bot_id, number, source, trigger, now, now),
            )
            if user_message is not None:
                await db.execute(
                    """
                    INSERT INTO protocol_messages(
                        turn_id, seq, user_id, bot_id, role, message_kind,
                        content_json, created_at
                    ) VALUES(?, 0, ?, ?, 'user', 'user_input', ?, ?)
                    """,
                    (
                        turn_id,
                        user_id,
                        bot_id,
                        json.dumps(user_message, ensure_ascii=False),
                        now,
                    ),
                )
            await db.commit()
        return turn_id

    async def append_protocol_message(
        self,
        turn_id: str,
        user_id: str,
        bot_id: str,
        role: str,
        content: Any,
        *,
        call_id: str = "",
        message_kind: str = "",
    ) -> int:
        if role not in {"user", "assistant"}:
            raise ValueError(f"unsupported protocol role: {role}")
        async with self._write_lock:
            db = await self._get_db()
            rows = await db.execute_fetchall(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS value "
                "FROM protocol_messages WHERE turn_id = ?",
                (turn_id,),
            )
            seq = int(rows[0]["value"])
            await db.execute(
                """
                INSERT INTO protocol_messages(
                    turn_id, seq, user_id, bot_id, call_id, role,
                    message_kind, content_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    seq,
                    user_id,
                    bot_id,
                    call_id,
                    role,
                    message_kind,
                    json.dumps(content, ensure_ascii=False),
                    time.time(),
                ),
            )
            await db.commit()
        return seq

    async def finish_protocol_turn(
        self,
        turn_id: str,
        status: str,
        send_status: str = "",
    ) -> None:
        now = time.time()
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """
                UPDATE turns
                SET status = ?, send_status = ?, completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, send_status, now, now, turn_id),
            )
            await db.commit()

    async def get_recent_protocol_history(
        self,
        user_id: str,
        bot_id: str = "",
        turn_limit: int = 20,
    ) -> list[dict[str, Any]]:
        db = await self._get_db()
        turns = await db.execute_fetchall(
            """
            SELECT id, turn_number FROM turns
            WHERE user_id = ? AND bot_id = ? AND status = 'completed'
            ORDER BY turn_number DESC LIMIT ?
            """,
            (user_id, bot_id, max(1, int(turn_limit))),
        )
        turn_ids = [str(row["id"]) for row in reversed(turns)]
        if not turn_ids:
            return []
        placeholders = ",".join("?" for _ in turn_ids)
        rows = await db.execute_fetchall(
            f"""
            SELECT * FROM protocol_messages
            WHERE turn_id IN ({placeholders})
            """,
            turn_ids,
        )
        order = {turn_id: index for index, turn_id in enumerate(turn_ids)}
        result: list[dict[str, Any]] = []
        for row in sorted(
            rows,
            key=lambda value: (order[str(value["turn_id"])], int(value["seq"])),
        ):
            try:
                content = json.loads(row["content_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            result.append({
                "id": row["id"],
                "turn_id": row["turn_id"],
                "seq": row["seq"],
                "role": row["role"],
                "content": content,
                "call_id": row["call_id"],
                "message_kind": row["message_kind"],
                "source": "protocol",
                "ts": row["created_at"],
            })
        return result

    async def clear_protocol_history(self, user_id: str, bot_id: str = "") -> int:
        async with self._write_lock:
            db = await self._get_db()
            rows = await db.execute_fetchall(
                "SELECT id FROM turns WHERE user_id = ? AND bot_id = ?",
                (user_id, bot_id),
            )
            ids = [str(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                for table, column in (
                    ("protocol_messages", "turn_id"),
                    ("outbound_messages", "turn_id"),
                    ("turn_drafts", "id"),
                    ("turn_diagnostics", "turn_id"),
                    ("turns", "id"),
                ):
                    await db.execute(
                        f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                        ids,
                    )
            await db.commit()
        return len(ids)

    async def get_recent_turns(
        self,
        user_id: str,
        bot_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        db = await self._get_db()
        rows = await db.execute_fetchall(
            """
            SELECT * FROM turns WHERE user_id = ? AND bot_id = ?
            ORDER BY turn_number DESC LIMIT ?
            """,
            (user_id, bot_id, max(1, int(limit))),
        )
        result: list[dict[str, Any]] = []
        for row in reversed(rows):
            item = dict(row)
            messages = await db.execute_fetchall(
                "SELECT role, message_kind, content_json "
                "FROM protocol_messages WHERE turn_id = ? ORDER BY seq",
                (item["id"],),
            )
            user_texts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for message in messages:
                try:
                    content = json.loads(message["content_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if message["message_kind"] == "user_input":
                    user_texts.append(str(content))
                if message["role"] == "assistant" and isinstance(content, list):
                    tool_calls.extend(
                        part for part in content
                        if isinstance(part, dict) and part.get("type") == "tool_use"
                    )
            outbound = await db.execute_fetchall(
                "SELECT text, send_status FROM outbound_messages "
                "WHERE turn_id = ? ORDER BY position, created_at",
                (item["id"],),
            )
            item["user_messages"] = "\n".join(user_texts)
            item["assistant_actions"] = json.dumps(
                [{"text": value["text"]} for value in outbound],
                ensure_ascii=False,
            )
            item["tool_calls"] = json.dumps(tool_calls, ensure_ascii=False)
            result.append(item)
        return result

    async def record_outbound_event(
        self,
        message_id: str,
        turn_id: str,
        text: str,
        *,
        user_id: str = "",
        bot_id: str = "",
        source: str = "user_reply",
        author: str = "assistant",
        trigger: str = "user_message",
        send_status: str = "created",
        send_error: str = "",
        position: int = 0,
    ) -> None:
        now = time.time()
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """
                INSERT INTO outbound_messages(
                    id, turn_id, user_id, bot_id, source, author, trigger,
                    send_status, text, position, send_error, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    send_status = excluded.send_status,
                    send_error = excluded.send_error,
                    updated_at = excluded.updated_at
                """,
                (
                    message_id,
                    turn_id,
                    user_id,
                    bot_id,
                    source,
                    author,
                    trigger,
                    send_status,
                    text,
                    position,
                    send_error[:500],
                    now,
                    now,
                ),
            )
            await db.commit()

    async def get_outbound_messages(
        self,
        turn_id: str = "",
        user_id: str = "",
        send_status: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        db = await self._get_db()
        conditions: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("turn_id", turn_id),
            ("user_id", user_id),
            ("send_status", send_status),
        ):
            if value:
                conditions.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(max(1, int(limit)))
        rows = await db.execute_fetchall(
            f"SELECT * FROM outbound_messages {where} "
            "ORDER BY created_at DESC LIMIT ?",
            params,
        )
        return [dict(row) for row in rows]

    async def create_turn_draft(
        self,
        turn_id: str,
        user_id: str,
        messages: list[dict[str, Any]],
        source: str = "",
        route: str = "",
    ) -> str:
        now = time.time()
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """
                INSERT OR REPLACE INTO turn_drafts(
                    id, user_id, source, route, status, messages_json,
                    send_error, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'generated', ?, '', ?, ?)
                """,
                (
                    turn_id,
                    user_id,
                    source,
                    route,
                    json.dumps(messages, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            await db.commit()
        return turn_id

    async def mark_turn_draft_status(
        self,
        turn_id: str,
        status: str,
        send_error: str = "",
    ) -> bool:
        async with self._write_lock:
            db = await self._get_db()
            cursor = await db.execute(
                "UPDATE turn_drafts SET status = ?, send_error = ?, updated_at = ? "
                "WHERE id = ?",
                (status, send_error[:500], time.time(), turn_id),
            )
            await db.commit()
        return cursor.rowcount > 0

    async def list_turn_drafts(
        self,
        user_id: str = "",
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        db = await self._get_db()
        conditions: list[str] = []
        params: list[Any] = []
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(max(1, int(limit)))
        rows = await db.execute_fetchall(
            f"SELECT * FROM turn_drafts {where} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        result = []
        for row in rows:
            item = dict(row)
            item["messages"] = _json_value(item.pop("messages_json"), [])
            result.append(item)
        return result

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
        input_chars: int = 0,
        output_messages: int = 0,
        tool_calls: int = 0,
        metadata: dict[str, Any] | None = None,
        **unused: Any,
    ) -> None:
        del unused
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """
                INSERT OR REPLACE INTO turn_diagnostics(
                    turn_id, user_id, source, route, context_policy,
                    llm_status, token_count, elapsed_ms, fallback_type,
                    send_status, input_chars, output_messages, tool_calls,
                    metadata_json, ts
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    user_id,
                    source,
                    route,
                    context_policy,
                    llm_status,
                    int(token_count),
                    int(elapsed_ms),
                    fallback_type,
                    send_status,
                    int(input_chars),
                    int(output_messages),
                    int(tool_calls),
                    json.dumps(metadata or {}, ensure_ascii=False),
                    time.time(),
                ),
            )
            await db.commit()

    async def update_turn_send_status(
        self,
        turn_id: str,
        send_status: str,
        error: str = "",
    ) -> None:
        async with self._write_lock:
            db = await self._get_db()
            rows = await db.execute_fetchall(
                "SELECT metadata_json FROM turn_diagnostics WHERE turn_id = ?",
                (turn_id,),
            )
            metadata = _json_value(rows[0]["metadata_json"], {}) if rows else {}
            if error:
                metadata["send_error"] = error[:500]
            await db.execute(
                "UPDATE turn_diagnostics SET send_status = ?, metadata_json = ? "
                "WHERE turn_id = ?",
                (send_status, json.dumps(metadata, ensure_ascii=False), turn_id),
            )
            await db.commit()

    async def get_recent_turn_diagnostics(
        self,
        limit: int = 20,
        user_id: str = "",
        fallback_only: bool = False,
    ) -> list[dict[str, Any]]:
        db = await self._get_db()
        conditions: list[str] = []
        params: list[Any] = []
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if fallback_only:
            conditions.append("fallback_type != ''")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(max(1, int(limit)))
        rows = await db.execute_fetchall(
            f"SELECT * FROM turn_diagnostics {where} ORDER BY ts DESC LIMIT ?",
            params,
        )
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = _json_value(item.pop("metadata_json"), {})
            result.append(item)
        return result

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
    ) -> None:
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """
                INSERT INTO usage_events(
                    event_type, user_id, source, message_count, token_count,
                    elapsed_ms, fallback_type, metadata_json, ts
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    user_id,
                    source,
                    int(message_count),
                    int(token_count),
                    int(elapsed_ms),
                    fallback_type,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    time.time() if ts is None else float(ts),
                ),
            )
            await db.commit()

    async def get_usage_summary(self, today_start_ts: float) -> dict[str, Any]:
        db = await self._get_db()

        async def totals(since: float | None) -> dict[str, int]:
            where = "WHERE ts >= ?" if since is not None else ""
            params = (since,) if since is not None else ()
            rows = await db.execute_fetchall(
                f"""
                SELECT COUNT(*) AS events,
                    COALESCE(SUM(message_count), 0) AS messages,
                    COALESCE(SUM(token_count), 0) AS tokens,
                    COALESCE(SUM(elapsed_ms), 0) AS elapsed_ms,
                    COALESCE(SUM(CASE WHEN fallback_type != '' THEN 1 ELSE 0 END), 0)
                        AS fallbacks
                FROM usage_events {where}
                """,
                params,
            )
            return {key: int(rows[0][key] or 0) for key in (
                "events", "messages", "tokens", "elapsed_ms", "fallbacks"
            )}

        return {
            "today": await totals(today_start_ts),
            "total": await totals(None),
            "recent_fallbacks": [],
        }

    async def update_last_visible_message_at(
        self,
        user_id: str,
        bot_id: str,
        timestamp: float,
        source: str = "",
    ) -> None:
        async with self._write_lock:
            db = await self._get_db()
            await db.execute(
                """
                INSERT INTO visible_activity(user_id, bot_id, last_visible_at, source)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(user_id, bot_id) DO UPDATE SET
                    last_visible_at = excluded.last_visible_at,
                    source = excluded.source
                """,
                (user_id, bot_id, float(timestamp), source),
            )
            await db.commit()

    async def clear_developer_memory_state(self) -> dict[str, int]:
        tables = (
            "turns",
            "protocol_messages",
            "outbound_messages",
            "turn_drafts",
            "turn_diagnostics",
            "usage_events",
            "visible_activity",
            "llm_traces",
        )
        counts: dict[str, int] = {}
        async with self._write_lock:
            db = await self._get_db()
            for table in tables:
                rows = await db.execute_fetchall(f"SELECT COUNT(*) AS value FROM {table}")
                counts[table] = int(rows[0]["value"])
                await db.execute(f"DELETE FROM {table}")
            await db.commit()
        return counts

    async def save_llm_trace(self, trace: dict[str, Any]) -> int:
        columns = (
            "request_id",
            "turn_id",
            "call_id",
            "task_type",
            "provider",
            "model",
            "thinking",
            "response_mode",
            "latency_ms",
            "attempt_count",
            "success",
            "status_code",
            "error_type",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cache_hit_tokens",
            "cache_miss_tokens",
            "reasoning_tokens",
            "request_json",
            "response_json",
        )
        values = [trace.get(column) for column in columns]
        async with self._write_lock:
            db = await self._get_db()
            cursor = await db.execute(
                f"""
                INSERT INTO llm_traces({', '.join(columns)}, created_at)
                VALUES({', '.join('?' for _ in columns)}, ?)
                """,
                (*values, time.time()),
            )
            await db.commit()
        return int(cursor.lastrowid)


def _json_value(value: Any, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
