"""Durable local filesystem backend for REMOTE marketplace archive work.

Google Drive remains canonical business storage. This backend replaces the
retired Yandex Object Storage role for durable queue state, staging and exact
backup mirrors on the REMOTE server.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive_google import ArchiveStorageError, ArchiveStorageNotConfigured

_LOCAL_DIR_PREFIX = "local-dir-v1:"
_LOCAL_FILE_PREFIX = "local-file-v1:"
DEFAULT_LOCAL_ROOT = "/opt/mcp/data/marketplaces/archive-durable"


@dataclass(frozen=True)
class LocalArchiveFile:
    id: str
    name: str
    mime_type: str
    size: int
    sha256_checksum: str
    modified_time: str | None = None


def _b64_encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _b64_decode(value: str) -> str:
    try:
        return base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise ArchiveStorageError(
            "Invalid local archive locator",
            code="INVALID_LOCAL_ARCHIVE_LOCATOR",
        ) from exc


class LocalFilesystemArchiveStore:
    """Root-confined durable local archive store with atomic file replacement."""

    read_only = False

    def __init__(self, root: str) -> None:
        raw = str(root or "").strip()
        if not raw:
            raise ArchiveStorageNotConfigured("Local archive durable root is empty")
        self.root = Path(raw).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _segment(value: Any) -> str:
        text = str(value or "").strip()
        if (
            not text
            or text in {".", ".."}
            or "/" in text
            or "\\" in text
            or "\x00" in text
        ):
            raise ArchiveStorageError(
                "Invalid local archive path segment",
                code="INVALID_LOCAL_ARCHIVE_PATH",
            )
        return text

    def _inside_root(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ArchiveStorageError(
                "Local archive path escapes configured root",
                code="LOCAL_ARCHIVE_PATH_ESCAPE",
            ) from exc
        return resolved

    def _relative_path(self, parts: list[str] | tuple[str, ...]) -> Path:
        clean = [self._segment(item) for item in parts if str(item or "").strip()]
        path = self.root.joinpath(*clean)
        return self._inside_root(path)

    def _dir_locator(self, path: Path) -> str:
        rel = self._inside_root(path).relative_to(self.root).as_posix()
        return _LOCAL_DIR_PREFIX + _b64_encode(rel)

    def _file_locator(self, path: Path) -> str:
        rel = self._inside_root(path).relative_to(self.root).as_posix()
        return _LOCAL_FILE_PREFIX + _b64_encode(rel)

    def _decode_dir(self, locator: str) -> Path:
        value = str(locator or "")
        if not value.startswith(_LOCAL_DIR_PREFIX):
            raise ArchiveStorageError(
                "Unknown local archive parent locator",
                code="INVALID_LOCAL_ARCHIVE_LOCATOR",
            )
        rel = _b64_decode(value[len(_LOCAL_DIR_PREFIX):])
        candidate = self.root if not rel else self.root.joinpath(*Path(rel).parts)
        return self._inside_root(candidate)

    def _decode_file(self, locator: str) -> Path:
        value = str(locator or "")
        if not value.startswith(_LOCAL_FILE_PREFIX):
            raise ArchiveStorageError(
                "Unknown local archive file locator",
                code="INVALID_LOCAL_ARCHIVE_LOCATOR",
            )
        rel = _b64_decode(value[len(_LOCAL_FILE_PREFIX):])
        if not rel:
            raise ArchiveStorageError(
                "Empty local archive file locator",
                code="INVALID_LOCAL_ARCHIVE_LOCATOR",
            )
        return self._inside_root(self.root.joinpath(*Path(rel).parts))

    @staticmethod
    def _mime(name: str, fallback: str = "application/octet-stream") -> str:
        return mimetypes.guess_type(name)[0] or fallback

    def _item_sync(self, path: Path) -> LocalArchiveFile | None:
        safe = self._inside_root(path)
        if not safe.exists():
            return None
        if safe.is_symlink() or not safe.is_file():
            raise ArchiveStorageError(
                "Local archive object is not a regular file",
                code="INVALID_LOCAL_ARCHIVE_OBJECT",
            )
        data = safe.read_bytes()
        stat = safe.stat()
        return LocalArchiveFile(
            id=self._file_locator(safe),
            name=safe.name,
            mime_type=self._mime(safe.name),
            size=len(data),
            sha256_checksum=hashlib.sha256(data).hexdigest(),
            modified_time=str(stat.st_mtime_ns),
        )

    async def ensure_folder_path(self, parts: list[str] | tuple[str, ...]) -> str:
        path = self._relative_path(parts)
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
        return self._dir_locator(path)

    async def find_child(
        self,
        parent_id: str,
        name: str,
        *,
        mime_type: str | None = None,
    ) -> LocalArchiveFile | None:
        parent = self._decode_dir(parent_id)
        child = self._inside_root(parent / self._segment(name))
        item = await asyncio.to_thread(self._item_sync, child)
        if item is None:
            return None
        if mime_type and item.mime_type != mime_type:
            return None
        return item

    async def file_metadata(self, file_id: str) -> dict[str, Any]:
        path = self._decode_file(file_id)
        item = await asyncio.to_thread(self._item_sync, path)
        if item is None:
            raise ArchiveStorageError(
                "Local archive file not found",
                code="LOCAL_ARCHIVE_FILE_NOT_FOUND",
            )
        return {
            "id": item.id,
            "name": item.name,
            "size": item.size,
            "sha256Checksum": item.sha256_checksum,
            "mimeType": item.mime_type,
            "modifiedTime": item.modified_time,
        }

    async def download_bytes(self, file_id: str) -> bytes:
        path = self._decode_file(file_id)
        item = await asyncio.to_thread(self._item_sync, path)
        if item is None:
            raise ArchiveStorageError(
                "Local archive file not found",
                code="LOCAL_ARCHIVE_FILE_NOT_FOUND",
            )
        return await asyncio.to_thread(path.read_bytes)

    async def download_range(self, file_id: str, start: int, end: int) -> bytes:
        if start < 0 or end < start:
            raise ValueError("Invalid local archive byte range")
        data = await self.download_bytes(file_id)
        return data[int(start):int(end)]

    async def download_named(
        self,
        parent_id: str,
        name: str,
    ) -> tuple[LocalArchiveFile | None, bytes | None]:
        item = await self.find_child(parent_id, name)
        if item is None:
            return None, None
        return item, await self.download_bytes(item.id)

    def _atomic_write_sync(
        self,
        parent: Path,
        name: str,
        data: bytes,
        mime_type: str,
    ) -> LocalArchiveFile:
        del mime_type
        parent = self._inside_root(parent)
        parent.mkdir(parents=True, exist_ok=True)
        target = self._inside_root(parent / self._segment(name))
        fd, temp_name = tempfile.mkstemp(prefix=".archive-", suffix=".tmp", dir=str(parent))
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp, 0o640)
            except OSError:
                pass
            os.replace(temp, target)
            try:
                os.chmod(target, 0o640)
            except OSError:
                pass
            try:
                dir_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
        item = self._item_sync(target)
        if item is None:
            raise ArchiveStorageError(
                "Atomic local archive write completed but file is missing",
                code="LOCAL_ARCHIVE_WRITE_VERIFY_FAILED",
            )
        expected = hashlib.sha256(data).hexdigest()
        if item.size != len(data) or item.sha256_checksum != expected:
            raise ArchiveStorageError(
                "Atomic local archive write failed size/SHA256 verification",
                code="LOCAL_ARCHIVE_WRITE_VERIFY_FAILED",
            )
        return item

    async def upload_bytes(
        self,
        parent_id: str,
        name: str,
        data: bytes,
        *,
        mime_type: str = "text/csv",
    ) -> LocalArchiveFile:
        parent = self._decode_dir(parent_id)
        return await asyncio.to_thread(
            self._atomic_write_sync,
            parent,
            name,
            bytes(data),
            mime_type,
        )

    async def status(self) -> dict[str, Any]:
        def probe() -> bool:
            return self.root.exists() and self.root.is_dir() and os.access(self.root, os.R_OK | os.W_OK | os.X_OK)

        reachable = await asyncio.to_thread(probe)
        return {
            "configured": True,
            "reachable": bool(reachable),
            "backend": "local_filesystem_archive",
            "root": str(self.root),
            "read_only": False,
            "atomic_replace": True,
        }


def build_local_archive_store_from_env() -> LocalFilesystemArchiveStore | None:
    enabled = os.environ.get(
        "MARKETPLACE_MCP_ARCHIVE_LOCAL_DURABLE",
        "0",
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None
    root = os.environ.get(
        "MARKETPLACE_MCP_ARCHIVE_LOCAL_ROOT",
        DEFAULT_LOCAL_ROOT,
    ).strip()
    if not root:
        return None
    return LocalFilesystemArchiveStore(root)


__all__ = [
    "DEFAULT_LOCAL_ROOT",
    "LocalArchiveFile",
    "LocalFilesystemArchiveStore",
    "build_local_archive_store_from_env",
]
