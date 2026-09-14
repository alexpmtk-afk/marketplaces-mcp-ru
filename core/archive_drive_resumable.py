"""Google Drive resumable uploader for large canonical archive files.

Apps Script authenticates only creation of the official Drive resumable session.
Yandex then uploads bounded chunks directly to that opaque session URI, so no
Google OAuth refresh token is stored in Yandex.

The session URI is effectively a bearer credential. Never expose it in logs,
errors, MCP responses, or user-visible status.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from .archive_google import ArchiveStorageNotConfigured

CHUNK_GRANULARITY = 256 * 1024
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
MAX_CHUNK_SIZE = 32 * 1024 * 1024
_RANGE_RE = re.compile(r"bytes=0-(\d+)$")
_AMBIGUOUS_TRANSIENT_STATUSES = {408, 500, 502, 503, 504}
_BACKOFF_ONLY_STATUSES = {425, 429}
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


class ResumableUploadError(RuntimeError):
    """Drive resumable upload failed without exposing the session URI."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)
        self.retry_after_seconds = max(0.0, float(retry_after_seconds or 0.0))


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
        if (
            self.chunk_size < CHUNK_GRANULARITY
            or self.chunk_size > MAX_CHUNK_SIZE
            or self.chunk_size % CHUNK_GRANULARITY
        ):
            raise ArchiveStorageNotConfigured(
                "Google Drive resumable chunk size must be 256 KiB aligned and between 256 KiB and 32 MiB"
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
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path.startswith("/upload/drive/")
        ):
            raise ResumableUploadError("Invalid Google Drive resumable session URI")
        return value

    async def _request(self, method: str, session_uri: str, **kwargs: Any) -> httpx.Response:
        url = self._validate_session_uri(session_uri)
        try:
            # The session URI is a bearer-like capability. Never automatically
            # forward it or upload bytes to a redirect target.
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                return await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ResumableUploadError(
                f"Google Drive resumable request failed before a response: {type(exc).__name__}",
                retryable=True,
            ) from exc

    @staticmethod
    def _confirmed_offset(response: httpx.Response) -> int:
        value = str(response.headers.get("Range") or "").strip()
        if not value:
            # Google documents that a 308 without Range means zero bytes have
            # been durably received.
            return 0
        match = _RANGE_RE.fullmatch(value)
        if not match:
            raise ResumableUploadError(
                "Google Drive returned a malformed resumable Range header",
                retryable=True,
            )
        return int(match.group(1)) + 1

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        raw = str(response.headers.get("Retry-After") or "").strip()
        if not raw:
            return 0.0
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                when = parsedate_to_datetime(raw)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return 0.0

    @staticmethod
    def _is_rate_limit_403(response: httpx.Response) -> bool:
        if response.status_code != 403:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        error = payload.get("error") if isinstance(payload, dict) else None
        entries = error.get("errors") if isinstance(error, dict) else None
        if not isinstance(entries, list):
            return False
        return any(
            isinstance(item, dict) and str(item.get("reason") or "") in _RATE_LIMIT_REASONS
            for item in entries
        )

    @classmethod
    def _retryable_response(cls, response: httpx.Response) -> bool:
        return (
            response.status_code in _AMBIGUOUS_TRANSIENT_STATUSES
            or response.status_code in _BACKOFF_ONLY_STATUSES
            or cls._is_rate_limit_403(response)
        )

    @classmethod
    def _session_must_restart(cls, response: httpx.Response) -> bool:
        # Drive's resumable-upload guide says any non-rate-limit 4xx during a
        # resumable upload means the session should be restarted. 408/425/429
        # and 403 rate limits are handled as transient backoff instead.
        return (
            400 <= response.status_code < 500
            and not cls._retryable_response(response)
        )

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
        if self._session_must_restart(response):
            return UploadProgress("expired", 0, None)
        raise ResumableUploadError(
            f"Google Drive resumable status failed with HTTP {response.status_code}",
            retryable=self._retryable_response(response),
            retry_after_seconds=self._retry_after(response),
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
            # The request may have reached Google even when the client saw a
            # timeout/disconnect. Never blindly resend; ask Drive first.
            return await self.query_status(session_uri, total_bytes)
        if response.status_code in {200, 201}:
            return UploadProgress("complete", int(total_bytes), self._json_dict(response))
        if response.status_code == 308:
            return UploadProgress("incomplete", self._confirmed_offset(response), None)
        if response.status_code in _AMBIGUOUS_TRANSIENT_STATUSES:
            # 5xx/408 can be ambiguous: bytes may have been committed. Query the
            # authoritative server offset before any resend.
            return await self.query_status(session_uri, total_bytes)
        if self._session_must_restart(response):
            return UploadProgress("expired", 0, None)
        if response.status_code in _BACKOFF_ONLY_STATUSES or self._is_rate_limit_403(response):
            raise ResumableUploadError(
                f"Google Drive resumable chunk throttled with HTTP {response.status_code}",
                retryable=True,
                retry_after_seconds=self._retry_after(response),
            )
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
