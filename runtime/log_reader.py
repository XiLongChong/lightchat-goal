"""Read recent log lines from a log file without loading it entirely into memory."""

from pathlib import Path
from typing import Any


def read_recent_log_lines(
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
