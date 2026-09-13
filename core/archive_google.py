"""Google Drive storage backend for the canonical marketplace archive.

The remote MCP owns its Google Drive session so archive updates work from any
ChatGPT/Codex client. OAuth refresh credentials are supplied by Yandex Lockbox;
access tokens exist only in process memory and are refreshed on demand.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"


class ArchiveStorageNotConfigured(RuntimeError):
    """Google Drive archive storage is not configured on the remote MCP."""


class ArchiveStorageError(RuntimeError):
    """Google Drive rejected or failed an archive operation."""


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int | None = None
    md5_checksum: str | None = None
    modified_time: str | None = None


class GoogleDriveArchiveStore:
    """Minimal async Google Drive client scoped to one archive root folder."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        root_folder_id: str,
        token_uri: str = DEFAULT_TOKEN_URI,
        timeout: float = 90.0,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.refresh_token = refresh_token.strip()
        self.root_folder_id = root_folder_id.strip()
        self.token_uri = token_uri.strip() or DEFAULT_TOKEN_URI
        self.timeout = timeout
        self._access_token = ""
        self._access_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        if not all((self.client_id, self.client_secret, self.refresh_token, self.root_folder_id)):
            raise ArchiveStorageNotConfigured("Google Drive OAuth or archive root folder is incomplete")

    @classmethod
    def from_env(cls) -> "GoogleDriveArchiveStore":
        raw = os.environ.get("MARKETPLACE_MCP_GOOGLE_DRIVE_OAUTH_JSON", "").strip()
        root = os.environ.get("MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID", "").strip()
        if not raw or not root:
            raise ArchiveStorageNotConfigured(
                "Set MARKETPLACE_MCP_GOOGLE_DRIVE_OAUTH_JSON and "
                "MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID"
            )
        try:
            cfg = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArchiveStorageNotConfigured("Google Drive OAuth JSON is invalid") from exc
        return cls(
            client_id=str(cfg.get("client_id", "")),
            client_secret=str(cfg.get("client_secret", "")),
            refresh_token=str(cfg.get("refresh_token", "")),
            token_uri=str(cfg.get("token_uri", DEFAULT_TOKEN_URI)),
            root_folder_id=root,
        )

    async def _token(self) -> str:
        now = time.monotonic()
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token
        async with self._token_lock:
            now = time.monotonic()
            if self._access_token and now < self._access_token_expires_at:
                return self._access_token
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self.token_uri,
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "refresh_token": self.refresh_token,
                        "grant_type": "refresh_token",
                    },
                    headers={"Accept": "application/json"},
                )
            if not resp.is_success:
                raise ArchiveStorageError(
                    f"Google OAuth refresh failed with HTTP {resp.status_code}: {resp.text[:300]}"
                )
            body = resp.json()
            token = str(body.get("access_token", "")).strip()
            if not token:
                raise ArchiveStorageError("Google OAuth refresh returned no access_token")
            try:
                ttl = max(60.0, float(body.get("expires_in", 3600)))
            except (TypeError, ValueError):
                ttl = 3600.0
            self._access_token = token
            self._access_token_expires_at = time.monotonic() + max(30.0, ttl - 60.0)
            return token

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._token()}"}

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers.update(await self._headers())
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401:
            self._access_token = ""
            headers.update(await self._headers())
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.request(method, url, headers=headers, **kwargs)
        if not resp.is_success:
            raise ArchiveStorageError(
                f"Google Drive {method} failed with HTTP {resp.status_code}: {resp.text[:500]}"
            )
        return resp

    @staticmethod
    def _q_literal(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    @staticmethod
    def _to_file(item: dict[str, Any]) -> DriveFile:
        try:
            size = int(item["size"]) if item.get("size") is not None else None
        except (TypeError, ValueError):
            size = None
        return DriveFile(
            id=str(item.get("id", "")),
            name=str(item.get("name", "")),
            mime_type=str(item.get("mimeType", "")),
            size=size,
            md5_checksum=item.get("md5Checksum"),
            modified_time=item.get("modifiedTime"),
        )

    async def find_child(
        self, parent_id: str, name: str, *, mime_type: str | None = None
    ) -> DriveFile | None:
        clauses = [
            f"'{self._q_literal(parent_id)}' in parents",
            f"name = '{self._q_literal(name)}'",
            "trashed = false",
        ]
        if mime_type:
            clauses.append(f"mimeType = '{self._q_literal(mime_type)}'")
        resp = await self._request(
            "GET",
            DRIVE_FILES_URL,
            params={
                "q": " and ".join(clauses),
                "fields": "files(id,name,mimeType,size,md5Checksum,modifiedTime,parents)",
                "pageSize": 10,
                "spaces": "drive",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        files = resp.json().get("files") or []
        if not files:
            return None
        if len(files) > 1:
            raise ArchiveStorageError(
                f"Drive path is ambiguous: {len(files)} children named {name!r} under {parent_id}"
            )
        return self._to_file(files[0])

    async def create_folder(self, parent_id: str, name: str) -> DriveFile:
        resp = await self._request(
            "POST",
            DRIVE_FILES_URL,
            params={"fields": "id,name,mimeType,modifiedTime", "supportsAllDrives": "true"},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            headers={"Content-Type": "application/json; charset=UTF-8"},
        )
        return self._to_file(resp.json())

    async def ensure_folder_path(self, parts: list[str] | tuple[str, ...]) -> str:
        parent = self.root_folder_id
        for raw_part in parts:
            part = str(raw_part).strip()
            if not part:
                continue
            child = await self.find_child(parent, part, mime_type=FOLDER_MIME)
            if child is None:
                child = await self.create_folder(parent, part)
            parent = child.id
        return parent

    async def download_bytes(self, file_id: str) -> bytes:
        resp = await self._request(
            "GET",
            f"{DRIVE_FILES_URL}/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
        return resp.content

    async def download_named(self, parent_id: str, name: str) -> tuple[DriveFile | None, bytes | None]:
        item = await self.find_child(parent_id, name)
        if item is None:
            return None, None
        return item, await self.download_bytes(item.id)

    async def _start_resumable(
        self,
        *,
        file_id: str | None,
        parent_id: str,
        name: str,
        mime_type: str,
        size: int,
    ) -> str:
        if file_id:
            url = f"{DRIVE_UPLOAD_URL}/{file_id}"
            method = "PATCH"
            metadata: dict[str, Any] = {"name": name}
        else:
            url = DRIVE_UPLOAD_URL
            method = "POST"
            metadata = {"name": name, "mimeType": mime_type, "parents": [parent_id]}
        resp = await self._request(
            method,
            url,
            params={"uploadType": "resumable", "supportsAllDrives": "true"},
            json=metadata,
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(size),
            },
        )
        location = resp.headers.get("Location", "")
        if not location:
            raise ArchiveStorageError("Google Drive resumable upload did not return a Location")
        return location

    async def upload_bytes(
        self, parent_id: str, name: str, data: bytes, *, mime_type: str = "text/csv"
    ) -> DriveFile:
        existing = await self.find_child(parent_id, name)
        location = await self._start_resumable(
            file_id=existing.id if existing else None,
            parent_id=parent_id,
            name=name,
            mime_type=mime_type,
            size=len(data),
        )
        resp = await self._request(
            "PUT",
            location,
            content=data,
            headers={"Content-Type": mime_type, "Content-Length": str(len(data))},
        )
        body = resp.json() if resp.content else {}
        file_id = str(body.get("id", "")) or (existing.id if existing else "")
        if file_id:
            meta = await self._request(
                "GET",
                f"{DRIVE_FILES_URL}/{file_id}",
                params={
                    "fields": "id,name,mimeType,size,md5Checksum,modifiedTime,parents",
                    "supportsAllDrives": "true",
                },
            )
            return self._to_file(meta.json())
        created = await self.find_child(parent_id, name)
        if created is None:
            raise ArchiveStorageError("Google Drive upload completed but the file cannot be resolved")
        return created

    async def status(self) -> dict[str, Any]:
        resp = await self._request(
            "GET",
            f"{DRIVE_FILES_URL}/{self.root_folder_id}",
            params={"fields": "id,name,mimeType", "supportsAllDrives": "true"},
        )
        body = resp.json()
        return {
            "configured": True,
            "reachable": True,
            "backend": "google_drive",
            "root_folder_id": str(body.get("id", "")),
            "root_name": str(body.get("name", "")),
        }


def build_google_archive_store_from_env() -> GoogleDriveArchiveStore | None:
    try:
        return GoogleDriveArchiveStore.from_env()
    except ArchiveStorageNotConfigured:
        return None
