"""Canonical Marketplaces Google Drive archive backend via Bridge v3.

Marketplaces uses one Drive transport only: the hardened project-specific
Google Apps Script Bridge v3. Shared Bridge Protocol v1 is retired for this
project and is never selected as a fallback.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Any

import httpx

BRIDGE_VERSION = 3
_RETIRED_V1_MODES = {"1", "v1", "protocol-v1"}


class ArchiveStorageNotConfigured(RuntimeError):
    """Google Drive archive storage is not configured on the remote MCP."""


class ArchiveStorageError(RuntimeError):
    """The Google Apps Script Drive bridge rejected or failed an operation."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)
        self.code = str(code or "").strip() or None


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
    """Async client for the canonical Marketplaces Apps Script Bridge v3."""

    _MAX_ATTEMPTS = 3
    _RETRYABLE_HTTP_STATUSES = {404, 408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        bridge_url: str,
        bridge_secret: str,
        root_folder_id: str,
        timeout: float = 120.0,
    ) -> None:
        self.bridge_url = bridge_url.strip()
        self.bridge_secret = bridge_secret.strip()
        self.root_folder_id = root_folder_id.strip()
        self.timeout = float(timeout)
        if not all((self.bridge_url, self.bridge_secret, self.root_folder_id)):
            raise ArchiveStorageNotConfigured(
                "Google Drive Bridge v3 URL, secret, or archive root is incomplete"
            )
        if not self.bridge_url.startswith("https://script.google.com/macros/s/"):
            raise ArchiveStorageNotConfigured("Google Drive Bridge v3 URL is invalid")

    @classmethod
    def from_env(cls) -> "GoogleDriveArchiveStore":
        mode = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_PROTOCOL", "v3").strip().lower()
        if mode in _RETIRED_V1_MODES:
            raise ArchiveStorageNotConfigured(
                "Shared Google Drive Bridge Protocol v1 is retired for Marketplaces; use Bridge v3"
            )
        if mode not in {"", "v3", "3", "legacy"}:
            raise ArchiveStorageNotConfigured(
                f"Unsupported Marketplaces Google Drive bridge mode: {mode!r}"
            )
        url = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL", "").strip()
        secret = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET", "").strip()
        root = os.environ.get("MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID", "").strip()
        if not url or not secret or not root:
            raise ArchiveStorageNotConfigured(
                "Set MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL, "
                "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET and "
                "MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID for Bridge v3"
            )
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
        body = {"secret": self.bridge_secret, "action": str(action).strip().lower(), **payload}
        last_error: Exception | None = None
        last_response: httpx.Response | None = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                    resp = await client.post(
                        self.bridge_url,
                        json=body,
                        headers={"Accept": "application/json"},
                    )
                last_response = resp
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self._MAX_ATTEMPTS:
                    await asyncio.sleep(0.75 * attempt)
                    continue
                raise ArchiveStorageError(
                    f"Google Drive Bridge v3 request failed after {attempt} attempts: {type(exc).__name__}",
                    retryable=True,
                    code="TRANSPORT_ERROR",
                ) from exc
            if resp.is_success:
                break
            retryable = resp.status_code in self._RETRYABLE_HTTP_STATUSES
            if retryable and attempt < self._MAX_ATTEMPTS:
                await asyncio.sleep(0.75 * attempt)
                continue
            raise ArchiveStorageError(
                f"Google Drive Bridge v3 HTTP {resp.status_code}: {resp.text[:500]}",
                retryable=retryable,
                code="HTTP_ERROR",
            )
        else:
            if last_error is not None:
                raise ArchiveStorageError(
                    f"Google Drive Bridge v3 request failed: {type(last_error).__name__}",
                    retryable=True,
                    code="TRANSPORT_ERROR",
                ) from last_error
            if last_response is not None:
                status = last_response.status_code
                raise ArchiveStorageError(
                    f"Google Drive Bridge v3 HTTP {status}: {last_response.text[:500]}",
                    retryable=status in self._RETRYABLE_HTTP_STATUSES,
                    code="HTTP_ERROR",
                )
            raise ArchiveStorageError(
                "Google Drive Bridge v3 request failed",
                retryable=True,
                code="TRANSPORT_ERROR",
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise ArchiveStorageError(
                "Google Drive Bridge v3 returned a non-JSON response; check web-app access settings",
                code="NON_JSON_RESPONSE",
            ) from exc
        if not isinstance(data, dict) or data.get("ok") is not True:
            retryable = bool(data.get("retryable")) if isinstance(data, dict) else False
            error = str(data.get("error") or "bridge_rejected") if isinstance(data, dict) else "invalid_response"
            raise ArchiveStorageError(
                f"Google Drive Bridge v3 rejected {action}: {error}",
                retryable=retryable,
                code=error.upper(),
            )
        return data

    # Compatibility alias for older tests/callers while the implementation is V3-only.
    async def _post_legacy(self, action: str, **payload: Any) -> dict[str, Any]:
        return await self._post(action, **payload)

    async def ensure_folder_path(self, parts: list[str] | tuple[str, ...]) -> str:
        return self._path(parts)

    async def find_child(
        self,
        parent_id: str,
        name: str,
        *,
        mime_type: str | None = None,
    ) -> DriveFile | None:
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
            raise ArchiveStorageError("Google Drive Bridge v3 metadata returned no file id")
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
            raise ArchiveStorageError("Google Drive Bridge v3 failed to trash diagnostic file")

    async def promote_verified_file(
        self,
        *,
        parent_id: str,
        file_id: str,
        staging_name: str,
        canonical_name: str,
        expected_bytes: int,
        expected_sha256: str,
        previous_file_id: str | None = None,
    ) -> DriveFile:
        data = await self._post(
            "promote_verified",
            path=self._path((parent_id,)),
            file_id=str(file_id),
            staging_filename=str(staging_name),
            canonical_filename=str(canonical_name),
            expected_bytes=int(expected_bytes),
            expected_sha256=str(expected_sha256).lower(),
            previous_file_id=str(previous_file_id or ""),
        )
        item = self._to_file(dict(data.get("file") or {}))
        if not item.id:
            raise ArchiveStorageError("Google Drive Bridge v3 promotion returned no file id")
        if item.id != str(file_id):
            raise ArchiveStorageError("Google Drive Bridge v3 promoted an unexpected file id")
        if item.name != str(canonical_name):
            raise ArchiveStorageError("Google Drive Bridge v3 promotion returned the wrong canonical name")
        if item.size != int(expected_bytes) or item.sha256_checksum != str(expected_sha256).lower():
            raise ArchiveStorageError(
                "Google Drive Bridge v3 promotion failed final size/SHA256 verification"
            )
        if (
            previous_file_id
            and str(previous_file_id) != str(file_id)
            and data.get("previous_file_trashed") is not True
        ):
            raise ArchiveStorageError(
                "Google Drive Bridge v3 did not confirm previous canonical cleanup",
                retryable=True,
                code="PREVIOUS_CLEANUP_UNCONFIRMED",
            )
        return item

    async def start_resumable_session(
        self,
        *,
        parent_id: str,
        name: str,
        total_bytes: int,
        mime_type: str = "text/csv",
    ) -> dict[str, Any]:
        data = await self._post(
            "resumable_start",
            path=self._path((parent_id,)),
            filename=str(name),
            mime_type=str(mime_type),
            total_bytes=int(total_bytes),
        )
        session_uri = str(data.get("session_uri") or "").strip()
        if not session_uri:
            raise ArchiveStorageError("Google Drive Bridge v3 returned no resumable session URI")
        return {
            "session_uri": session_uri,
            "file_id": str(data.get("file_id") or "").strip() or None,
            "staging_filename": str(data.get("filename") or name).strip() or None,
        }

    async def download_bytes(self, file_id: str) -> bytes:
        data = await self._post("read_by_id", file_id=str(file_id))
        encoded = str(data.get("content_base64", ""))
        if not encoded:
            return b""
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ArchiveStorageError("Google Drive Bridge v3 returned invalid base64") from exc

    async def download_named(
        self,
        parent_id: str,
        name: str,
    ) -> tuple[DriveFile | None, bytes | None]:
        data = await self._post("read", path=self._path((parent_id,)), filename=str(name))
        if not data.get("found"):
            return None, None
        item = self._to_file(dict(data.get("file") or {}))
        encoded = str(data.get("content_base64", ""))
        try:
            raw = base64.b64decode(encoded, validate=True) if encoded else b""
        except ValueError as exc:
            raise ArchiveStorageError("Google Drive Bridge v3 returned invalid base64") from exc
        if item.size is not None and len(raw) != item.size:
            raise ArchiveStorageError(
                f"Google Drive Bridge v3 size mismatch for {name!r}: {len(raw)} != {item.size}"
            )
        return item, raw

    async def upload_bytes(
        self,
        parent_id: str,
        name: str,
        data: bytes,
        *,
        mime_type: str = "text/csv",
    ) -> DriveFile:
        """Write a bounded object with replay protection for identical content.

        A lost response can cause the caller to retry. If the exact named file is
        already present with identical bytes, return it instead of replacing it.
        The server-side v3 source also contains the same SHA-aware replay guard.
        """
        existing, existing_raw = await self.download_named(parent_id, name)
        if existing is not None and existing_raw == data:
            return existing

        sha256 = hashlib.sha256(data).hexdigest()
        result = await self._post(
            "write",
            path=self._path((parent_id,)),
            filename=str(name),
            mime_type=mime_type,
            content_base64=base64.b64encode(data).decode("ascii"),
            sha256=sha256,
        )
        returned_sha = str(result.get("sha256", "")).lower()
        if returned_sha and returned_sha != sha256:
            raise ArchiveStorageError(f"Google Drive Bridge v3 checksum mismatch for {name!r}")
        item = self._to_file(dict(result.get("file") or {}))
        if not item.id:
            raise ArchiveStorageError("Google Drive Bridge v3 upload returned no file id")
        if item.size is not None and item.size != len(data):
            raise ArchiveStorageError(f"Google Drive Bridge v3 uploaded size mismatch for {name!r}")
        return item

    async def status(self) -> dict[str, Any]:
        data = await self._post("health")
        actual_root = str(data.get("root_id", ""))
        root_name = str(data.get("root_name", ""))
        version = int(data.get("version") or 0)
        if version != BRIDGE_VERSION:
            raise ArchiveStorageError(
                f"Marketplaces Google Drive bridge version mismatch: {version} != {BRIDGE_VERSION}",
                code="BRIDGE_VERSION_MISMATCH",
            )
        if actual_root != self.root_folder_id:
            raise ArchiveStorageError(
                f"Google Drive Bridge v3 root mismatch: {actual_root!r} != {self.root_folder_id!r}",
                code="ROOT_MISMATCH",
            )
        return {
            "configured": True,
            "reachable": True,
            "backend": "google_drive_apps_script_bridge_v3",
            "root_folder_id": actual_root,
            "root_name": root_name,
            "bridge_version": version,
            "capabilities": dict(data.get("capabilities") or {}),
        }


def build_google_archive_store_from_env() -> GoogleDriveArchiveStore | None:
    try:
        return GoogleDriveArchiveStore.from_env()
    except ArchiveStorageNotConfigured:
        return None
