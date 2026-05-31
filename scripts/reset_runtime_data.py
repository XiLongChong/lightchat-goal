#!/usr/bin/env python
"""Reset runtime data while preserving persona, config, and auth bindings.

Usage:
    python scripts/reset_runtime_data.py              # dry-run: show what would be cleared
    python scripts/reset_runtime_data.py --confirm    # actually clear + vacuum
"""

import sqlite3
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/memory.db")
BACKUP_DIR = Path("data/backups")

TABLES_TO_CLEAR = [
    # Runtime / chat history
    "conversation_history",
    "turns",
    "outbound_messages",
    "turn_diagnostics",
    "turn_drafts",
    "llm_traces",
    "usage_events",
    "recaps",
    "history_summaries",
    "memories",
    "shared_user_facts",
    # Legacy module residue
    "relationship_state",
    "bot_emotion_state",
    "bot_emotion_events",
    "dialogue_state",
    "dialogue_state_events",
    "clingy_state",
    "active_topics",
    "scheduled_messages",
    "scheduler_state",
    "proactive_candidates",
    "proactive_schedule_state",
    "pending_persona_change",
]

TABLES_TO_KEEP = [
    "bot_contact_state",
]


def backup_db() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"goal_backup_before_reset_{ts}.sqlite"
    dest = BACKUP_DIR / name
    shutil.copy2(DB_PATH, dest)
    size = dest.stat().st_size
    print(f"Backup: {dest} ({size:,} bytes)")
    return dest


def show_state(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {}
    all_tables = TABLES_TO_CLEAR + TABLES_TO_KEEP
    for t in all_tables:
        try:
            cnt = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
            counts[t] = cnt
        except Exception:
            counts[t] = -1
    return counts


def scan_call(conn: sqlite3.Connection) -> list[str]:
    findings = []
    target_tables = ["conversation_history", "turns", "outbound_messages",
                     "turn_diagnostics", "turn_drafts"]
    for table in target_tables:
        try:
            rows = conn.execute(f"SELECT * FROM [{table}]").fetchall()
        except Exception:
            continue
        for row in rows:
            for col in row:
                if col and "call_" in str(col):
                    snippet = str(col)[:100].replace("\n", " ")
                    findings.append(f"{table}: {snippet}")
                    break
    return findings


def clear_tables(conn: sqlite3.Connection) -> None:
    for t in TABLES_TO_CLEAR:
        try:
            conn.execute(f"DELETE FROM [{t}]")
        except Exception as e:
            print(f"  SKIP {t}: {e}")
    conn.commit()


def vacuum_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")


def main():
    confirm = "--confirm" in sys.argv

    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found")
        sys.exit(1)

    # Backup first (always, even for dry-run)
    if confirm:
        backup_path = backup_db()
    else:
        print("[DRY RUN] — use --confirm to actually reset")
        backup_path = None

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Before counts
    before = show_state(conn)
    call_findings = scan_call(conn)

    # Print status
    print(f"\n{'='*60}")
    print(f"{'Table':<32} {'Before':>8} {'Action':>8}")
    print(f"{'-'*60}")
    for t in TABLES_TO_CLEAR:
        cnt = before.get(t, 0)
        action = "CLEAR" if confirm and cnt > 0 else ("-" if cnt == 0 else "WOULD CLEAR")
        print(f"{t:<32} {cnt:>8} {action:>8}")

    print(f"\n{'Tables to keep:':<32}")
    for t in TABLES_TO_KEEP:
        cnt = before.get(t, 0)
        print(f"  {t}: {cnt} rows")

    # call_ findings
    print(f"\n{'call_ contamination:':<32}")
    if call_findings:
        for f in call_findings[:5]:
            print(f"  {f}")
        if len(call_findings) > 5:
            print(f"  ... and {len(call_findings) - 5} more")
    else:
        print("  (none)")

    if not confirm:
        print(f"\n[DRY RUN complete — no changes made]")
        conn.close()
        return

    # --- CONFIRMED: execute cleanup ---
    print(f"\n{'='*60}")
    print("CLEARING tables...")
    clear_tables(conn)

    print("VACUUM...")
    try:
        vacuum_db(conn)
    except Exception as e:
        print(f"  VACUUM error (non-fatal): {e}")

    # After counts
    after = show_state(conn)
    call_after = scan_call(conn)

    print(f"\n{'='*60}")
    print(f"{'Table':<32} {'Before':>8} {'After':>8}")
    print(f"{'-'*60}")
    for t in TABLES_TO_CLEAR:
        b = before.get(t, 0)
        a = after.get(t, 0)
        print(f"{t:<32} {b:>8} {a:>8}")

    print(f"\n{'call_ after cleanup:':<32}")
    if call_after:
        print(f"  WARNING: {len(call_after)} call_ findings still present!")
        for f in call_after:
            print(f"  {f}")
    else:
        print("  (none) — clean")

    db_size = DB_PATH.stat().st_size
    print(f"\nDB size after VACUUM: {db_size:,} bytes")
    print(f"Backup: {backup_path}")

    conn.close()
    print("Reset complete.")


if __name__ == "__main__":
    main()
