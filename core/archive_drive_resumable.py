"""Direct Google Drive resumable uploader for large canonical archive files.

Small archive operations continue to use the owner-operated Apps Script bridge.
Large annual CSV candidates use the official Drive API so an interrupted upload
can resume from the last byte confirmed by Google instead of resending the
whole base64 payload.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .archive_google import ArchiveStorageNotConfigured

TOKEN_URI_DEFAULT = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
CHUNK_GRANULARITY = 256 * 1024
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
_RANGE_RE = re.compile(r"bytes=0-(\d+)$")


class ResumableUploadError(RuntimeError):
    """Drive resumable upload failed.

    ``retryable`` means the durable session/offset should be kept and retried by
    a later worker invocation. The exception text never contains OAuth tokens or
    the resumable session URI.
    """

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
    """Minimal async Drive API client using an OAuth refresh token."""

    def __init__(
        self,
        *,
        oauth_json: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        timeout: float = 90.0,
    ) -> None:
        try:
            cfg = json.loads(oauth_json)
        except Exception as exc:  # pragma: no cover - exact decoder text is irrelevant
            raise ArchiveStorageNotConfigured("Google Drive OAuth JSON is invalid") from exc
        self.client_id = str(cfg.get("client_id") or "").strip()
        self.client_secret = str(cfg.get("client_secret") or "").strip()
        self.refresh_token = str(cfg.get("refresh_token") or "").strip()
        self.token_uri = str(cfg.get("token_uri") or TOKEN_URI_DEFAULT).strip()
        if not all((self.client_id, self.client_secret, self.refresh_token, self.token_uri)):
            raise ArchiveStorageNotConfigured("Google Drive OAuth JSON is incomplete")
        self.chunk_size = int(chunk_size)
        if self.chunk_size < CHUNK_GRANULARITY or self.chunk_size % CHUNK_GRANULARITY:
            raise ArchiveStorageNotConfigured(
                "Google Drive resumable chunk size must be a positive multiple of 256 KiB"
            )
        self.timeout = float(timeout)
        self._access_token = ""
        self._access_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> "GoogleDriveResumableUploader":
        raw = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_OAUTH_JSON", "").strip()
        if not raw:
            raise ArchiveStorageNotConfigured("Set MARKETPLACE_MCP_GOOGLE_DRIVE_OAUTH_JSON")
        raw_chunk = os.environ.get("MARKETPLACE_MCP_DRIVE_RESUMABLE_CHUNK_BYTES", "").strip()
        chunk_size = int(raw_chunk) if raw_chunk else DEFAULT_CHUNK_SIZE
        return cls(oauth_json=raw, chunk_size=chunk_size)

    async def _token(self, *, force: bool = False) -> str:
        now = time.monotonic()
        if not force and self._access_token and now < self._access_expires_at:
            return self._access_token
        async with self._token_lock:
            now = time.monotonic()
            if not force and self._access_token and now < self._access_expires_at:
                return self._access_token
            async with httpx.AsyncClient(timeout=min(self.timeout, 30.0)) as client:
                response = await client.post(
                    self.token_uri,
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "refresh_token": self.refresh_token,
                        "grant_type": "refresh_token",
                    },
                    headers={"Accept": "application/json"},
                )
            if not response.is_success:
                raise ResumableUploadError(
                    f"Google OAuth refresh failed with HTTP {response.status_code}",
                    retryable=response.status_code in {408, 425, 429, 500, 502, 503, 504},
                )
            body = response.json()
            token = str(body.get("access_token") or "").strip()
            if not token:
                raise ResumableUploadError("Google OAuth refresh returned no access_token")
            try:
                expires_in = max(60.0, float(body.get("expires_in", 3600)))
            except (TypeError, ValueError):
                expires_in = 3600.0
            self._access_token = token
            self._access_expires_at = time.monotonic() + max(30.0, expires_in - 60.0)
            return token

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {await self._token()}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ResumableUploadError(
                f"Google Drive request failed before a response: {type(exc).__name__}",
                retryable=True,
            ) from exc
        if response.status_code == 401:
            headers["Authorization"] = f"Bearer {await self._token(force=True)}"
            try:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                    response = await client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise ResumableUploadError(
                    f"Google Drive retry failed before a response: {type(exc).__name__}",
                    retryable=True,
                ) from exc
        return response

    @staticmethod
    def _confirmed_offset(response: httpx.Response) -> int:
        value = str(response.headers.get("Range") or "").strip()
        match = _RANGE_RE.fullmatch(value)
        return int(match.group(1)) + 1 if match else 0

    async def find_named_file(self, parent_id: str, name: str) -> dict[str, Any] | None:
        escaped_parent = str(parent_id).replace("'", "\\'")
        escaped_name = str(name).replace("'", "\\'")
        response = await self._request(
            "GET",
            f"{DRIVE_API}/files",
            params={
                "q": f"'{escaped_parent}' in parents and name = '{escaped_name}' and trashed = false",
                "spaces": "drive",
                "pageSize": "2",
                "fields": "files(id,name,size,md5Checksum,mimeType,modifiedTime)",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        if not response.is_success:
            raise ResumableUploadError(
                f"Google Drive file lookup failed with HTTP {response.status_code}",
                retryable=response.status_code in {408, 425, 429, 500, 502, 503, 504},
            )
        files = list((response.json() or {}).get("files") or [])
        if len(files) > 1:
            raise ResumableUploadError("Google Drive contains duplicate canonical filenames")
        return dict(files[0]) if files else None

    async def start_session(
        self,
        *,
        parent_id: str,
        name: str,
        total_bytes: int,
        mime_type: str = "text/csv",
    ) -> UploadSession:
        existing = await self.find_named_file(parent_id, name)
        params = {
            "uploadType": "resumable",
            "supportsAllDrives": "true",
            "fields": "id,name,size,md5Checksum,mimeType,modifiedTime",
        }
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime_type,
            "X-Upload-Content-Length": str(int(total_bytes)),
        }
        if existing:
            file_id = str(existing.get("id") or "")
            response = await self._request(
                "PATCH",
                f"{DRIVE_UPLOAD_API}/files/{file_id}",
                params=params,
                headers=headers,
                json={"name": name, "mimeType": mime_type},
            )
        else:
            file_id = None
            response = await self._request(
                "POST",
                f"{DRIVE_UPLOAD_API}/files",
                params=params,
                headers=headers,
                json={"name": name, "mimeType": mime_type, "parents": [parent_id]},
            )
        if not response.is_success:
            raise ResumableUploadError(
                f"Google Drive resumable session start failed with HTTP {response.status_code}",
                retryable=response.status_code in {408, 425, 429, 500, 502, 503, 504},
            )
        uri = str(response.headers.get("Location") or "").strip()
        if not uri:
            raise ResumableUploadError("Google Drive returned no resumable session URI")
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
        if response.status_code == 404:
            return UploadProgress("expired", 0, None)
        raise ResumableUploadError(
            f"Google Drive resumable status failed with HTTP {response.status_code}",
            retryable=response.status_code in {408, 425, 429, 500, 502, 503, 504},
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
        end = int(offset) + len(data) - 1
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
            # The connection may have failed after Drive persisted the chunk.
            # Query the authoritative session offset instead of blindly
            # resending the same bytes.
            return await self.query_status(session_uri, total_bytes)
        if response.status_code in {200, 201}:
            return UploadProgress("complete", int(total_bytes), self._json_dict(response))
        if response.status_code == 308:
            return UploadProgress("incomplete", self._confirmed_offset(response), None)
        if response.status_code == 404:
            return UploadProgress("expired", 0, None)
        if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
            return await self.query_status(session_uri, total_bytes)
        raise ResumableUploadError(
            f"Google Drive resumable chunk failed with HTTP {response.status_code}"
        )

    async def file_metadata(self, file_id: str) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"{DRIVE_API}/files/{file_id}",
            params={
                "fields": "id,name,size,md5Checksum,mimeType,modifiedTime",
                "supportsAllDrives": "true",
            },
        )
        if not response.is_success:
            raise ResumableUploadError(
                f"Google Drive metadata verification failed with HTTP {response.status_code}",
                retryable=response.status_code in {408, 425, 429, 500, 502, 503, 504},
            )
        return self._json_dict(response)

    @staticmethod
    def _json_dict(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError:
            return {}
        return dict(value) if isinstance(value, dict) else {}


def build_drive_resumable_uploader_from_env() -> GoogleDriveResumableUploader | None:
    try:
        return GoogleDriveResumableUploader.from_env()
    except ArchiveStorageNotConfigured:
        return None
