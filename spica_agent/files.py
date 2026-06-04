from __future__ import annotations

import hashlib
import mimetypes
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Callable


IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


class FileStoreError(ValueError):
    """Raised when a file operation would violate local store policy."""


@dataclass(frozen=True)
class StoredFile:
    id: str
    path: Path
    name: str
    size: int
    mime_type: str
    source: str
    created_at: float

    @property
    def is_photo(self) -> bool:
        return self.path.suffix.lower() in IMAGE_EXTENSIONS


class SpicaFileStore:
    def __init__(
        self,
        *,
        root: Path,
        output_roots: tuple[Path, ...],
        allowed_extensions: frozenset[str],
        max_upload_bytes: int,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.upload_root = (self.root / "uploads").resolve()
        self.output_roots = tuple(path.expanduser().resolve() for path in output_roots)
        self.allowed_extensions = allowed_extensions
        self.max_upload_bytes = max_upload_bytes
        self._now = time.time if now is None else now
        self._lock = Lock()
        self._files: dict[str, StoredFile] = {}
        self._last_by_chat: dict[int, str] = {}

    def save_upload(
        self,
        *,
        chat_id: int,
        original_name: str,
        source: str,
        content: bytes,
    ) -> StoredFile:
        if len(content) > self.max_upload_bytes:
            raise FileStoreError("文件超过允许的上传大小。")

        name = _safe_filename(original_name)
        extension = Path(name).suffix.lower()
        if extension not in self.allowed_extensions:
            raise FileStoreError(f"不支持的文件类型：{extension or 'unknown'}")

        day = datetime.fromtimestamp(self._now()).strftime("%Y-%m-%d")
        directory = self._safe_upload_dir(chat_id, day)
        directory.mkdir(parents=True, exist_ok=True)
        destination = _unique_path(directory / name)
        destination.write_bytes(content)

        stored = self._stored_file(destination, source=source)
        with self._lock:
            self._files[stored.id] = stored
            self._last_by_chat[chat_id] = stored.id
        return stored

    def clear_last_for_chat(self, chat_id: int) -> None:
        with self._lock:
            self._last_by_chat.pop(chat_id, None)

    def last_for_chat(self, chat_id: int) -> StoredFile | None:
        with self._lock:
            file_id = self._last_by_chat.get(chat_id)
        if file_id is None:
            return None
        return self.get(file_id)

    def get(self, file_id: str) -> StoredFile | None:
        self.refresh_outputs()
        with self._lock:
            stored = self._files.get(file_id)
        if stored is None or not stored.path.is_file():
            return None
        if not self._is_allowed_path(stored.path):
            return None
        return self._stored_file(stored.path, source=stored.source)

    def list_recent(self, *, limit: int = 10) -> list[StoredFile]:
        self.refresh_outputs()
        with self._lock:
            files = list(self._files.values())
        valid = [
            self._stored_file(item.path, source=item.source)
            for item in files
            if item.path.is_file() and self._is_allowed_path(item.path)
        ]
        return sorted(valid, key=lambda item: item.created_at, reverse=True)[:limit]

    def refresh_outputs(self, *, limit_per_root: int = 100) -> None:
        roots = [self.upload_root, *self.output_roots]
        discovered: list[StoredFile] = []
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            for path in _recent_files(root, limit=limit_per_root):
                if path.suffix.lower() not in self.allowed_extensions:
                    continue
                if not self._is_allowed_path(path):
                    continue
                source = "upload" if _is_relative_to(path.resolve(), self.upload_root) else "output"
                discovered.append(self._stored_file(path, source=source))

        with self._lock:
            for item in discovered:
                self._files[item.id] = item

    def status_text(self) -> str:
        return "\n".join(
            [
                "文件功能: 已启用",
                f"文件根目录: {self.root}",
                f"最近文件数: {len(self.list_recent(limit=100))}",
            ]
        )

    def _safe_upload_dir(self, chat_id: int, day: str) -> Path:
        path = (self.upload_root / str(chat_id) / day).resolve()
        if not _is_relative_to(path, self.upload_root):
            raise FileStoreError("上传目录不安全。")
        return path

    def _stored_file(self, path: Path, *, source: str) -> StoredFile:
        resolved = path.expanduser().resolve()
        if not self._is_allowed_path(resolved):
            raise FileStoreError("文件不在允许的目录内。")
        stat = resolved.stat()
        digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:10]
        mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        return StoredFile(
            id=digest,
            path=resolved,
            name=resolved.name,
            size=stat.st_size,
            mime_type=mime_type,
            source=source,
            created_at=stat.st_mtime,
        )

    def _is_allowed_path(self, path: Path) -> bool:
        resolved = path.expanduser().resolve()
        roots = [self.upload_root, *self.output_roots]
        return any(_is_relative_to(resolved, root) for root in roots)


def format_file_list(files: list[StoredFile]) -> str:
    if not files:
        return "没有找到可回传的文件。"
    lines = ["最近可回传文件："]
    for item in files:
        command = "/photo" if item.is_photo else "/file"
        lines.append(
            f"- {item.id} {item.name} ({_format_size(item.size)}) "
            f"[{item.source}] -> {command} {item.id}"
        )
    return "\n".join(lines)


def file_context_message(path: Path, prompt: str) -> str:
    return "\n".join(
        [
            "用户上传/引用了本机文件：",
            str(path),
            "",
            "用户指令：",
            prompt,
        ]
    )


def _recent_files(root: Path, *, limit: int) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file():
            paths.append(path)
    return sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True)[:limit]


def _safe_filename(name: str) -> str:
    candidate = Path(name or "telegram-file").name.strip()
    candidate = re.sub(r"[^A-Za-z0-9._ -]+", "_", candidate)
    candidate = candidate.strip(" .")
    return candidate or "telegram-file"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileStoreError("无法为上传文件生成唯一文件名。")


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
