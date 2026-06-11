"""File-backed long-term memory for one user."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


INDEX_NAME = "MEMORY.md"
INDEX_HEADER = "# 长期记忆索引\n"
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25 * 1024
MAX_FILE_BYTES = 32 * 1024
MAX_USER_BYTES = 256 * 1024
_TOPIC_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
_INDEX_LINK_RE = re.compile(
    r"^\s*-\s+\[(?P<title>[^\]]+)\]\((?P<file>[a-z0-9][a-z0-9-]*\.md)\)"
    r"\s+-\s+(?P<summary>.+?)\s*$"
)


class LongTermMemoryError(Exception):
    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


@dataclass(slots=True)
class MemoryOperation:
    command: str
    path: str
    file_text: str = ""
    old_str: str = ""
    new_str: str = ""
    reason: str = ""
    evidence: str = ""


@dataclass(slots=True)
class BatchReceipt:
    service: "LongTermMemoryService"
    user_id: str
    snapshots: dict[Path, bytes | None]
    changed_paths: list[str]
    active: bool = True

    async def rollback(self) -> None:
        if not self.active:
            return
        async with self.service._lock_for(self.user_id):
            self.service._restore_snapshots(self.snapshots)
        self.active = False

    def finalize(self) -> None:
        self.active = False
        self.snapshots.clear()


@dataclass(slots=True)
class ClearReceipt:
    service: "LongTermMemoryService"
    user_id: str
    backup_dir: Path | None
    removed_files: int
    active: bool = True

    async def rollback(self) -> None:
        if not self.active or self.backup_dir is None:
            return
        async with self.service._lock_for(self.user_id):
            target = self.service.user_dir(self.user_id)
            if target.exists():
                shutil.rmtree(target)
            os.replace(self.backup_dir, target)
        self.active = False

    def finalize(self) -> None:
        if self.backup_dir is not None and self.backup_dir.exists():
            shutil.rmtree(self.backup_dir)
        self.active = False


class LongTermMemoryService:
    def __init__(self, root_dir: str | Path = "./data/long_term_memory") -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, user_id: str) -> asyncio.Lock:
        key = self.user_key(user_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @staticmethod
    def user_key(user_id: str) -> str:
        value = str(user_id or "")
        if not value:
            raise LongTermMemoryError("memory_user_required", "user_id is required")
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    def user_dir(self, user_id: str) -> Path:
        return self.root_dir / self.user_key(user_id)

    def ensure_user(self, user_id: str) -> Path:
        directory = self.user_dir(user_id)
        directory.mkdir(parents=True, exist_ok=True)
        index = directory / INDEX_NAME
        if not index.exists():
            self._atomic_write(index, INDEX_HEADER.encode("utf-8"))
        return directory

    def _resolve(self, user_id: str, relative_path: str, *, allow_index: bool = True) -> Path:
        raw = str(relative_path or "").strip()
        if not raw or Path(raw).is_absolute() or "/" in raw or "\\" in raw or ".." in raw:
            raise LongTermMemoryError("memory_invalid_path", "invalid memory path", path=raw)
        if raw == INDEX_NAME:
            if not allow_index:
                raise LongTermMemoryError("memory_index_protected", "MEMORY.md is protected", path=raw)
        elif not _TOPIC_RE.fullmatch(raw):
            raise LongTermMemoryError(
                "memory_invalid_path",
                "topic filename must be lowercase kebab-case Markdown",
                path=raw,
            )
        directory = self.ensure_user(user_id).resolve()
        target = (directory / raw).resolve()
        if target.parent != directory:
            raise LongTermMemoryError("memory_path_escape", "path escapes memory directory", path=raw)
        return target

    def read_index(self, user_id: str) -> str:
        return self._resolve(user_id, INDEX_NAME).read_text(encoding="utf-8")

    def view(self, user_id: str, relative_path: str = INDEX_NAME) -> dict[str, Any]:
        path = self._resolve(user_id, relative_path)
        if not path.exists():
            raise LongTermMemoryError("memory_not_found", "memory file not found", path=relative_path)
        data = path.read_bytes()
        return {
            "ok": True,
            "code": "memory_ok",
            "command": "view",
            "path": path.name,
            "bytes": len(data),
            "message": data.decode("utf-8"),
        }

    async def execute(
        self,
        user_id: str,
        operation: MemoryOperation,
    ) -> dict[str, Any]:
        async with self._lock_for(user_id):
            if operation.command == "view":
                return self.view(user_id, operation.path)
            receipt = self._apply_batch_locked(user_id, [operation])
            receipt.finalize()
            size = self._resolve(user_id, operation.path).stat().st_size
            return {
                "ok": True,
                "code": "memory_ok",
                "command": operation.command,
                "path": operation.path,
                "bytes": size,
                "message": "memory operation completed",
            }

    async def apply_batch(
        self,
        user_id: str,
        operations: list[MemoryOperation],
    ) -> BatchReceipt:
        async with self._lock_for(user_id):
            return self._apply_batch_locked(user_id, operations)

    def _apply_batch_locked(
        self,
        user_id: str,
        operations: list[MemoryOperation],
    ) -> BatchReceipt:
        if not operations:
            return BatchReceipt(self, user_id, {}, [])
        snapshots: dict[Path, bytes | None] = {}
        staged: dict[Path, bytes] = {}
        for operation in operations:
            if operation.command not in {"create", "str_replace"}:
                raise LongTermMemoryError(
                    "memory_invalid_command",
                    f"unsupported memory command: {operation.command}",
                    path=operation.path,
                )
            path = self._resolve(user_id, operation.path)
            current = staged[path] if path in staged else (
                path.read_bytes() if path.exists() else None
            )
            if path not in snapshots:
                snapshots[path] = path.read_bytes() if path.exists() else None
            if operation.command == "create":
                if current is not None:
                    raise LongTermMemoryError(
                        "memory_already_exists", "memory file already exists", path=operation.path
                    )
                new_data = operation.file_text.encode("utf-8")
            else:
                if current is None:
                    raise LongTermMemoryError(
                        "memory_not_found", "memory file not found", path=operation.path
                    )
                text = current.decode("utf-8")
                count = text.count(operation.old_str)
                if not operation.old_str or count != 1:
                    raise LongTermMemoryError(
                        "memory_ambiguous_replace",
                        "old_str must occur exactly once",
                        path=operation.path,
                    )
                new_data = text.replace(operation.old_str, operation.new_str, 1).encode("utf-8")
            self._validate_file(path.name, new_data)
            staged[path] = new_data

        projected = self._current_total_bytes(user_id)
        for path, data in staged.items():
            projected -= path.stat().st_size if path.exists() else 0
            projected += len(data)
        if projected > MAX_USER_BYTES:
            raise LongTermMemoryError("memory_total_too_large", "user memory limit exceeded")

        try:
            for path, data in staged.items():
                self._atomic_write(path, data)
        except Exception as exc:
            self._restore_snapshots(snapshots)
            if isinstance(exc, LongTermMemoryError):
                raise
            raise LongTermMemoryError("memory_io_error", str(exc)) from exc
        return BatchReceipt(self, user_id, snapshots, [p.name for p in staged])

    def _validate_file(self, name: str, data: bytes) -> None:
        limit = MAX_INDEX_BYTES if name == INDEX_NAME else MAX_FILE_BYTES
        if len(data) > limit:
            raise LongTermMemoryError("memory_file_too_large", f"{name} exceeds size limit", path=name)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LongTermMemoryError("memory_invalid_utf8", "memory must be UTF-8", path=name) from exc
        if name == INDEX_NAME and len(text.splitlines()) > MAX_INDEX_LINES:
            raise LongTermMemoryError("memory_index_too_long", "MEMORY.md exceeds 200 lines", path=name)
        if name == INDEX_NAME:
            lines = text.splitlines()
            if not lines or lines[0].strip() != "# 长期记忆索引":
                raise LongTermMemoryError(
                    "memory_invalid_format",
                    "MEMORY.md must start with the long-term memory index heading",
                    path=name,
                )
            for line in lines[1:]:
                if line.strip() and not _INDEX_LINK_RE.match(line):
                    raise LongTermMemoryError(
                        "memory_invalid_format",
                        "MEMORY.md contains an invalid index line",
                        path=name,
                    )
        else:
            lines = text.splitlines()
            if not lines or not lines[0].startswith("# "):
                raise LongTermMemoryError(
                    "memory_invalid_format",
                    "topic memory must start with a Markdown title",
                    path=name,
                )
            if not any(line.startswith("更新时间：") for line in lines):
                raise LongTermMemoryError(
                    "memory_invalid_format",
                    "topic memory must include 更新时间",
                    path=name,
                )

    def _current_total_bytes(self, user_id: str) -> int:
        directory = self.ensure_user(user_id)
        return sum(path.stat().st_size for path in directory.glob("*.md") if path.is_file())

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _restore_snapshots(self, snapshots: dict[Path, bytes | None]) -> None:
        for path, data in snapshots.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                self._atomic_write(path, data)

    def diagnostics(self, user_id: str) -> dict[str, Any]:
        directory = self.ensure_user(user_id)
        index_text = (directory / INDEX_NAME).read_text(encoding="utf-8")
        entries: list[dict[str, Any]] = []
        warnings: list[dict[str, str]] = []
        indexed: set[str] = set()
        for line_number, line in enumerate(index_text.splitlines(), start=1):
            match = _INDEX_LINK_RE.match(line)
            if not match:
                continue
            filename = match.group("file")
            indexed.add(filename)
            path = directory / filename
            entries.append({
                "title": match.group("title"),
                "summary": match.group("summary"),
                "filename": filename,
                "exists": path.exists(),
                "bytes": path.stat().st_size if path.exists() else 0,
                "updated_at": path.stat().st_mtime if path.exists() else 0,
            })
            if not path.exists():
                warnings.append({
                    "code": "memory_dangling_index",
                    "message": f"index line {line_number} points to missing {filename}",
                })
        for path in directory.glob("*.md"):
            if path.name != INDEX_NAME and path.name not in indexed:
                warnings.append({
                    "code": "memory_orphan_file",
                    "message": f"{path.name} is not listed in MEMORY.md",
                })
        return {"entries": entries, "warnings": warnings, "index": index_text}

    def all_indexed_content(self, user_id: str) -> str:
        diagnostic = self.diagnostics(user_id)
        parts = [f"## {INDEX_NAME}\n{diagnostic['index'].strip()}"]
        directory = self.ensure_user(user_id)
        for entry in diagnostic["entries"]:
            if not entry["exists"]:
                continue
            filename = entry["filename"]
            parts.append(f"## {filename}\n{(directory / filename).read_text(encoding='utf-8').strip()}")
        return "\n\n".join(parts)

    async def delete_for_admin(self, user_id: str, filename: str) -> dict[str, Any]:
        async with self._lock_for(user_id):
            path = self._resolve(user_id, filename, allow_index=False)
            if not path.exists():
                raise LongTermMemoryError("memory_not_found", "memory file not found", path=filename)
            index_path = self._resolve(user_id, INDEX_NAME)
            original_file = path.read_bytes()
            original_index = index_path.read_bytes()
            index_text = original_index.decode("utf-8")
            kept = [
                line for line in index_text.splitlines()
                if not (_INDEX_LINK_RE.match(line) and _INDEX_LINK_RE.match(line).group("file") == filename)
            ]
            new_index = ("\n".join(kept).rstrip() + "\n").encode("utf-8")
            try:
                path.unlink()
                self._atomic_write(index_path, new_index)
            except Exception as exc:
                self._atomic_write(path, original_file)
                self._atomic_write(index_path, original_index)
                raise LongTermMemoryError("memory_io_error", str(exc), path=filename) from exc
            return {"ok": True, "code": "memory_deleted", "path": filename, "bytes": len(original_file)}

    async def stage_clear(self, user_id: str) -> ClearReceipt:
        async with self._lock_for(user_id):
            directory = self.user_dir(user_id)
            if not directory.exists():
                return ClearReceipt(self, user_id, None, 0)
            removed = sum(1 for path in directory.glob("*.md") if path.is_file())
            backup = directory.with_name(
                f".{directory.name}.clear-backup-{uuid.uuid4().hex[:8]}"
            )
            os.replace(directory, backup)
            return ClearReceipt(self, user_id, backup, removed)
