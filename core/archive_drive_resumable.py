"""Google Drive resumable uploader for large canonical archive files.

The owner-operated Apps Script bridge authenticates only the *session start*.
Google then returns a resumable session URI. Yandex uploads bounded chunks
directly to that URI, so no Google OAuth refresh token is stored in Yandex.

The session URI is effectively a bearer credential. Never expose it in logs,
errors, MCP responses, or user-visible status.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .archive_google import ArchiveStorageNotConfigured

CHUNK_GRANULARITY = 256 * 1024
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
_RANGE_RE = re.compile(r"bytes=0-(\d+)$")
_TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_RESTART_SESSION_STATUSES = {400, 401, 403, 404, 410}


class ResumableUploadError(RuntimeError):
    """Drive resumable upload failed without exposing the session URI."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)


@dataclass(frozen=True)
class UploadSession:
    uri: str
    file_id: str | None
    offset: int = 0


@dataclass(frozen=True)
class UploadProgress:
    state: str  # incomplete | complete | expired
    offset: int
    file: dict[str, Any] | None = None


class GoogleDriveResumableUploader:
    """Chunk transport using a resumable session brokered by Apps Script."""

    def __init__(
        self,
        *,
        session_broker: Any,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        timeout: float = 90.0,
    ) -> None:
        if session_broker is None:
            raise ArchiveStorageNotConfigured("Google Drive resumable session broker is missing")
        self.session_broker = session_broker
        self.chunk_size = int(chunk_size)
        if self.chunk_size < CHUNK_GRANULARITY or self.chunk_size % CHUNK_GRANULARITY:
            raise ArchiveStorageNotConfigured(
                "Google Drive resumable chunk size must be a positive multiple of 256 KiB"
            )
        self.timeout = float(timeout)

    @classmethod
    def from_bridge(cls, bridge: Any) -> "GoogleDriveResumableUploader":
        raw_chunk = os.environ.get("MARKETPLACE_MCP_DRIVE_RESUMABLE_CHUNK_BYTES", "").strip()
        chunk_size = int(raw_chunk) if raw_chunk else DEFAULT_CHUNK_SIZE
        return cls(session_broker=bridge, chunk_size=chunk_size)

    @staticmethod
    def _validate_session_uri(uri: str) -> str:
        value = str(uri or "").strip()
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.googleapis.com"
            or not parsed.path.startswith("/upload/drive/")
        ):
            raise ResumableUploadError("Invalid Google Drive resumable session URI")
        return value

    async def _request(self, method: str, session_uri: str, **kwargs: Any) -> httpx.Response:
        url = self._validate_session_uri(session_uri)
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                return await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ResumableUploadError(
                f"Google Drive resumable request failed before a response: {type(exc).__name__}",
                retryable=True,
            ) from exc

    @staticmethod
    def _confirmed_offset(response: httpx.Response) -> int:
        value = str(response.headers.get("Range") or "").strip()
        match = _RANGE_RE.fullmatch(value)
        return int(match.group(1)) + 1 if match else 0

    async def start_session(
        self,
        *,
        parent_id: str,
        name: str,
        total_bytes: int,
        mime_type: str = "text/csv",
    ) -> UploadSession:
        if int(total_bytes) <= 0:
            raise ResumableUploadError("Refusing to start a resumable session for an empty file")
        result = await self.session_broker.start_resumable_session(
            parent_id=parent_id,
            name=name,
            total_bytes=total_bytes,
            mime_type=mime_type,
        )
        uri = self._validate_session_uri(str(result.get("session_uri") or ""))
        file_id = str(result.get("file_id") or "").strip() or None
        return UploadSession(uri=uri, file_id=file_id, offset=0)

    async def query_status(self, session_uri: str, total_bytes: int) -> UploadProgress:
        response = await self._request(
            "PUT",
            session_uri,
            headers={
                "Content-Length": "0",
                "Content-Range": f"bytes */{int(total_bytes)}",
            },
            content=b"",
        )
        if response.status_code in {200, 201}:
            return UploadProgress("complete", int(total_bytes), self._json_dict(response))
        if response.status_code == 308:
            return UploadProgress("incomplete", self._confirmed_offset(response), None)
        if response.status_code in _RESTART_SESSION_STATUSES:
            return UploadProgress("expired", 0, None)
        raise ResumableUploadError(
            f"Google Drive resumable status failed with HTTP {response.status_code}",
            retryable=response.status_code in _TRANSIENT_STATUSES,
        )

    async def upload_chunk(
        self,
        *,
        session_uri: str,
        offset: int,
        total_bytes: int,
        data: bytes,
    ) -> UploadProgress:
        if not data:
            raise ResumableUploadError("Refusing to upload an empty resumable chunk")
        if int(offset) < 0 or int(offset) >= int(total_bytes):
            raise ResumableUploadError("Invalid resumable chunk offset")
        end = int(offset) + len(data) - 1
        if end >= int(total_bytes):
            end = int(total_bytes) - 1
        if len(data) != end - int(offset) + 1:
            raise ResumableUploadError("Resumable chunk length exceeds declared file size")
        is_final = end + 1 == int(total_bytes)
        if not is_final and len(data) % CHUNK_GRANULARITY:
            raise ResumableUploadError("Non-final Drive chunk must be a multiple of 256 KiB")
        try:
            response = await self._request(
                "PUT",
                session_uri,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {int(offset)}-{end}/{int(total_bytes)}",
                },
                content=data,
            )
        except ResumableUploadError as exc:
            if not exc.retryable:
                raise
            return await self.query_status(session_uri, total_bytes)
        if response.status_code in {200, 201}:
            return UploadProgress("complete", int(total_bytes), self._json_dict(response))
        if response.status_code == 308:
            return UploadProgress("incomplete", self._confirmed_offset(response), None)
        if response.status_code in _RESTART_SESSION_STATUSES:
            return UploadProgress("expired", 0, None)
        if response.status_code in _TRANSIENT_STATUSES:
            return await self.query_status(session_uri, total_bytes)
        raise ResumableUploadError(
            f"Google Drive resumable chunk failed with HTTP {response.status_code}"
        )

    async def file_metadata(self, file_id: str) -> dict[str, Any]:
        return await self.session_broker.file_metadata(str(file_id))

    async def find_named_file(self, parent_id: str, name: str) -> dict[str, Any] | None:
        item = await self.session_broker.find_child(parent_id, name)
        if item is None:
            return None
        return {
            "id": item.id,
            "name": item.name,
            "size": item.size,
            "md5Checksum": item.md5_checksum,
            "sha256Checksum": item.sha256_checksum,
            "mimeType": item.mime_type,
            "modifiedTime": item.modified_time,
        }

    @staticmethod
    def _json_dict(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError:
            return {}
        return dict(value) if isinstance(value, dict) else {}


def build_drive_resumable_uploader_from_bridge(bridge: Any) -> GoogleDriveResumableUploader:
    return GoogleDriveResumableUploader.from_bridge(bridge)
