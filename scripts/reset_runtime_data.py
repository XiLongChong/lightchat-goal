#!/usr/bin/env python
"""Clear chat/test data while preserving the WeChat contact binding."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("data/memory.db")
TABLES_TO_CLEAR = (
    "turns",
    "raw_events",
    "conversations",
    "context_snapshots",
    "tool_versions",
    "daily_summaries",
    "app_messages",
    "app_attachments",
    "phone_context_snapshots",
    "scheduled_tasks",
    "proactive_quota",
    "reminders",
)


def main() -> None:
    if "--confirm" not in sys.argv:
        print("Dry run. Use --confirm to clear chat/test data.")
        return
    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH} not found")

    connection = sqlite3.connect(DB_PATH)
    try:
        for table in TABLES_TO_CLEAR:
            connection.execute(f'DELETE FROM "{table}"')
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()

    print("Chat/test data cleared. bot_contact_state was preserved.")


if __name__ == "__main__":
    main()
