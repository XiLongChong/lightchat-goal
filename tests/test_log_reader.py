"""Tests for runtime.log_reader."""

import tempfile
from pathlib import Path

from runtime.log_reader import read_recent_log_lines


def test_read_recent_log_lines_empty_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, encoding="utf-8") as f:
        f.write("")
        path = f.name
    try:
        result = read_recent_log_lines(path, limit=50)
        assert result["exists"] is True
        assert result["lines"] == []
        assert result["truncated"] is False
    finally:
        Path(path).unlink(missing_ok=True)


def test_read_recent_log_lines_basic():
    lines = [f"2024-01-01 12:00:00 [INFO] goal: line {i}" for i in range(100)]
    content = "\n".join(lines) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, encoding="utf-8") as f:
        f.write(content)
        path = f.name
    try:
        result = read_recent_log_lines(path, limit=10)
        assert result["exists"] is True
        assert len(result["lines"]) == 10
        assert result["lines"][-1] == lines[-1]
        assert result["truncated"] is False
    finally:
        Path(path).unlink(missing_ok=True)


def test_read_recent_log_lines_level_filter():
    lines = [
        "2024-01-01 12:00:00 [INFO] goal: info msg",
        "2024-01-01 12:00:01 [WARNING] goal: warn msg",
        "2024-01-01 12:00:02 [ERROR] goal: error msg",
        "2024-01-01 12:00:03 [INFO] goal: another info",
    ]
    content = "\n".join(lines) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, encoding="utf-8") as f:
        f.write(content)
        path = f.name
    try:
        result = read_recent_log_lines(path, limit=50, level="ERROR")
        assert len(result["lines"]) == 1
        assert "error msg" in result["lines"][0].lower()
    finally:
        Path(path).unlink(missing_ok=True)


def test_read_recent_log_lines_contains_filter():
    lines = [
        "2024-01-01 12:00:00 [INFO] goal: hello world",
        "2024-01-01 12:00:01 [INFO] goal: foo bar",
        "2024-01-01 12:00:02 [INFO] goal: hello again",
    ]
    content = "\n".join(lines) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, encoding="utf-8") as f:
        f.write(content)
        path = f.name
    try:
        result = read_recent_log_lines(path, limit=50, contains="hello")
        assert len(result["lines"]) == 2
    finally:
        Path(path).unlink(missing_ok=True)


def test_read_recent_log_lines_missing_file():
    result = read_recent_log_lines("/nonexistent/path/to/log.log", limit=50)
    assert result["exists"] is False
    assert result["lines"] == []
    assert result["truncated"] is False


def test_read_recent_log_lines_large_file_truncation():
    """Test that large files report truncated=True."""
    lines = [f"2024-01-01 12:00:00 [INFO] goal: very long log line number {i} " + "x" * 200 for i in range(2000)]
    content = "\n".join(lines) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, encoding="utf-8") as f:
        f.write(content)
        path = f.name
    try:
        result = read_recent_log_lines(path, limit=10)
        assert result["exists"] is True
        # With a large file, truncation should be reported
        assert result["truncated"] is True
    finally:
        Path(path).unlink(missing_ok=True)
