"""Google Drive archive backend via the owner's Google Apps Script web app.

The remote MCP remains hosted in Yandex Cloud. The Apps Script deployment runs
as the archive owner and exposes a narrow authenticated bridge into the fixed
Google Drive archive root. The bridge secret is injected from Yandex Lockbox.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Any

import httpx


class ArchiveStorageNotConfigured(RuntimeError):
    """Google Drive archive storage is not configured on the remote MCP."""


class ArchiveStorageError(RuntimeError):
    """The Google Apps Script Drive bridge rejected or failed an operation."""


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int | None = None
    md5_checksum: str | None = None
    sha256_checksum: str | None = None
    modified_time: str | None = None


class GoogleDriveArchiveStore:
    """Async client for the authenticated Apps Script Drive bridge."""

    _MAX_ATTEMPTS = 3
    _RETRYABLE_HTTP_STATUSES = {404, 408, 425, 429, 500, 502, 503, 504}

    def __init__(self, *, bridge_url: str, bridge_secret: str, root_folder_id: str, timeout: float = 120.0) -> None:
        self.bridge_url = bridge_url.strip()
        self.bridge_secret = bridge_secret.strip()
        self.root_folder_id = root_folder_id.strip()
        self.timeout = float(timeout)
        if not all((self.bridge_url, self.bridge_secret, self.root_folder_id)):
            raise ArchiveStorageNotConfigured("Google Drive Apps Script bridge URL, secret, or archive root is incomplete")
        if not self.bridge_url.startswith("https://script.google.com/macros/s/"):
            raise ArchiveStorageNotConfigured("Google Drive Apps Script bridge URL is invalid")

    @classmethod
    def from_env(cls) -> "GoogleDriveArchiveStore":
        url = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL", "").strip()
        secret = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET", "").strip()
        root = os.environ.get("MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID", "").strip()
        if not url or not secret or not root:
            raise ArchiveStorageNotConfigured("Set MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL, MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET and MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID")
        return cls(bridge_url=url, bridge_secret=secret, root_folder_id=root)

    @staticmethod
    def _path(parts: list[str] | tuple[str, ...]) -> str:
        clean: list[str] = []
        for raw in parts:
            part = str(raw).strip().strip("/")
            if not part:
                continue
            if part in {".", ".."} or "\\" in part:
                raise ArchiveStorageError(f"Invalid Drive archive path segment: {part!r}")
            clean.append(part)
        return "/".join(clean)

    @staticmethod
    def _to_file(item: dict[str, Any]) -> DriveFile:
        try:
            size = int(item["size"]) if item.get("size") is not None else None
        except (TypeError, ValueError):
            size = None
        md5 = str(item.get("md5_checksum") or item.get("md5Checksum") or "").strip() or None
        sha256 = str(item.get("sha256_checksum") or item.get("sha256Checksum") or "").strip() or None
        return DriveFile(
            id=str(item.get("id", "")),
            name=str(item.get("name", "")),
            mime_type=str(item.get("mime_type") or item.get("mimeType") or ""),
            size=size,
            md5_checksum=md5,
            sha256_checksum=sha256,
            modified_time=item.get("modified_time") or item.get("modifiedTime"),
        )

    async def _post(self, action: str, **payload: Any) -> dict[str, Any]:
        body = {"secret": self.bridge_secret, "action": action, **payload}
        last_error: Exception | None = None
        last_response: httpx.Response | None = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                    resp = await client.post(self.bridge_url, json=body, headers={"Accept": "application/json"})
                last_response = resp
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self._MAX_ATTEMPTS:
                    await asyncio.sleep(0.75 * attempt)
                    continue
                raise ArchiveStorageError(f"Apps Script Drive bridge request failed after {attempt} attempts: {exc}") from exc
            if resp.is_success:
                break
            if resp.status_code in self._RETRYABLE_HTTP_STATUSES and attempt < self._MAX_ATTEMPTS:
                await asyncio.sleep(0.75 * attempt)
                continue
            raise ArchiveStorageError(f"Apps Script Drive bridge HTTP {resp.status_code}: {resp.text[:500]}")
        else:
            if last_error is not None:
                raise ArchiveStorageError(f"Apps Script Drive bridge request failed: {last_error}") from last_error
            if last_response is not None:
                raise ArchiveStorageError(f"Apps Script Drive bridge HTTP {last_response.status_code}: {last_response.text[:500]}")
            raise ArchiveStorageError("Apps Script Drive bridge request failed")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ArchiveStorageError("Apps Script Drive bridge returned a non-JSON response; check web-app access settings") from exc
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise ArchiveStorageError(f"Apps Script Drive bridge rejected {action}: {str(data)[:500]}")
        return data

    async def ensure_folder_path(self, parts: list[str] | tuple[str, ...]) -> str:
        return self._path(parts)

    async def find_child(self, parent_id: str, name: str, *, mime_type: str | None = None) -> DriveFile | None:
        data = await self._post("stat", path=self._path((parent_id,)), filename=str(name))
        if not data.get("found"):
            return None
        item = self._to_file(dict(data.get("file") or {}))
        if mime_type and item.mime_type and item.mime_type != mime_type:
            return None
        return item

    async def file_metadata(self, file_id: str) -> dict[str, Any]:
        data = await self._post("metadata_by_id", file_id=str(file_id))
        item = self._to_file(dict(data.get("file") or {}))
        if not item.id:
            raise ArchiveStorageError("Apps Script Drive bridge metadata returned no file id")
        return {
            "id": item.id,
            "name": item.name,
            "size": item.size,
            "md5Checksum": item.md5_checksum,
            "sha256Checksum": item.sha256_checksum,
            "mimeType": item.mime_type,
            "modifiedTime": item.modified_time,
        }

    async def trash_file(self, file_id: str) -> None:
        data = await self._post("trash_by_id", file_id=str(file_id))
        if data.get("trashed") is not True:
            raise ArchiveStorageError("Apps Script Drive bridge failed to trash diagnostic file")

    async def start_resumable_session(self, *, parent_id: str, name: str, total_bytes: int, mime_type: str = "text/csv") -> dict[str, Any]:
        data = await self._post("resumable_start", path=self._path((parent_id,)), filename=str(name), mime_type=str(mime_type), total_bytes=int(total_bytes))
        session_uri = str(data.get("session_uri") or "").strip()
        if not session_uri:
            raise ArchiveStorageError("Apps Script Drive bridge returned no resumable session URI")
        return {"session_uri": session_uri, "file_id": str(data.get("file_id") or "").strip() or None}

    async def download_bytes(self, file_id: str) -> bytes:
        data = await self._post("read_by_id", file_id=str(file_id))
        encoded = str(data.get("content_base64", ""))
        if not encoded:
            return b""
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ArchiveStorageError("Apps Script Drive bridge returned invalid base64") from exc

    async def download_named(self, parent_id: str, name: str) -> tuple[DriveFile | None, bytes | None]:
        data = await self._post("read", path=self._path((parent_id,)), filename=str(name))
        if not data.get("found"):
            return None, None
        item = self._to_file(dict(data.get("file") or {}))
        encoded = str(data.get("content_base64", ""))
        try:
            raw = base64.b64decode(encoded, validate=True) if encoded else b""
        except ValueError as exc:
            raise ArchiveStorageError("Apps Script Drive bridge returned invalid base64") from exc
        if item.size is not None and len(raw) != item.size:
            raise ArchiveStorageError(f"Apps Script Drive bridge size mismatch for {name!r}: {len(raw)} != {item.size}")
        return item, raw

    async def upload_bytes(self, parent_id: str, name: str, data: bytes, *, mime_type: str = "text/csv") -> DriveFile:
        sha256 = hashlib.sha256(data).hexdigest()
        result = await self._post("write", path=self._path((parent_id,)), filename=str(name), mime_type=mime_type, content_base64=base64.b64encode(data).decode("ascii"), sha256=sha256)
        returned_sha = str(result.get("sha256", "")).lower()
        if returned_sha and returned_sha != sha256:
            raise ArchiveStorageError(f"Apps Script Drive bridge checksum mismatch for {name!r}")
        item = self._to_file(dict(result.get("file") or {}))
        if not item.id:
            raise ArchiveStorageError("Apps Script Drive bridge upload returned no file id")
        if item.size is not None and item.size != len(data):
            raise ArchiveStorageError(f"Apps Script Drive bridge uploaded size mismatch for {name!r}")
        return item

    async def status(self) -> dict[str, Any]:
        data = await self._post("health")
        actual_root = str(data.get("root_id", ""))
        root_name = str(data.get("root_name", ""))
        if actual_root != self.root_folder_id:
            raise ArchiveStorageError(f"Apps Script Drive bridge root mismatch: {actual_root!r} != {self.root_folder_id!r}")
        return {"configured": True, "reachable": True, "backend": "google_drive_apps_script_bridge", "root_folder_id": actual_root, "root_name": root_name, "bridge_version": data.get("version")}


def build_google_archive_store_from_env() -> GoogleDriveArchiveStore | None:
    try:
        return GoogleDriveArchiveStore.from_env()
    except ArchiveStorageNotConfigured:
        return None
