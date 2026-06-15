"""提醒持久化存储。

管理 reminders 表的 CRUD 操作，与主 Store 共享数据库路径。
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime

logger = logging.getLogger("memory")


class ReminderStore:
    """提醒的 SQLite 持久化存储。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._ensure_table()

    def _get_conn(self) -> sqlite3.Connection:
        """Reuse the same connection for :memory: databases."""
        if self._conn is not None:
            return self._conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        if self.db_path == ":memory:":
            self._conn = conn
        return conn

    def _ensure_table(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_reminders_user_status
                ON reminders(user_id, status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_reminders_remind_at
                ON reminders(remind_at)
            """)

    async def create(
        self, user_id: str, message: str, remind_at: str, timezone: str = "Asia/Shanghai"
    ) -> dict:
        """创建提醒。"""
        reminder_id = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat()

        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO reminders (id, user_id, message, remind_at, timezone, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (reminder_id, user_id, message, remind_at, timezone, now),
            )

        return {
            "id": reminder_id, "user_id": user_id, "message": message,
            "remind_at": remind_at, "timezone": timezone,
            "status": "pending", "created_at": now,
        }

    async def get_due(self, user_id: str) -> list[dict]:
        """获取已到期且未发送的提醒。"""
        now = datetime.now().isoformat()
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM reminders
                   WHERE user_id = ? AND status = 'pending' AND remind_at <= ?
                   ORDER BY remind_at ASC""",
                (user_id, now),
            ).fetchall()
        return [dict(r) for r in rows]

    async def mark_sent(self, reminder_id: str) -> bool:
        """标记提醒为已发送。"""
        now = datetime.now().isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE reminders SET status = 'sent', sent_at = ? WHERE id = ? AND status = 'pending'",
                (now, reminder_id),
            )
            return cursor.rowcount > 0

    async def list_by_user(self, user_id: str, status: str | None = None) -> list[dict]:
        """列出用户的提醒。"""
        with self._get_conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE user_id = ? AND status = ? ORDER BY remind_at ASC",
                    (user_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE user_id = ? ORDER BY remind_at ASC",
                    (user_id,),
                ).fetchall()
        return [dict(r) for r in rows]

    async def cancel(self, reminder_id: str, user_id: str) -> bool:
        """取消提醒。"""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'pending'",
                (reminder_id, user_id),
            )
            return cursor.rowcount > 0
