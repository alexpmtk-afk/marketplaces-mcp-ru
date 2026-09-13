"""Yandex Object Storage backend for the shared marketplace archive.

The remote MCP uses the Serverless Container runtime service account. IAM tokens
are obtained from the GCE-compatible metadata endpoint and kept only in memory.
No static S3 key, Google OAuth credential, or client-local state is required.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

METADATA_TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
)
DEFAULT_ENDPOINT = "https://storage.yandexcloud.net"


class ArchiveStorageNotConfigured(RuntimeError):
    """The central Yandex archive storage is not configured."""


class ArchiveStorageError(RuntimeError):
    """Yandex Object Storage rejected or failed an archive operation."""


@dataclass(frozen=True)
class ArchiveObject:
    """Minimal object metadata used by the archive manager."""

    id: str
    name: str
    mime_type: str
    size: int | None = None
    etag: str | None = None
    modified_time: str | None = None


class YandexObjectStorageArchiveStore:
    """Async Object Storage client using the container service-account IAM token."""

    def __init__(
        self,
        *,
        bucket: str,
        root_prefix: str = "",
        endpoint: str = DEFAULT_ENDPOINT,
        metadata_token_url: str = METADATA_TOKEN_URL,
        timeout: float = 90.0,
    ) -> None:
        self.bucket = bucket.strip()
        self.root_prefix = self._clean_prefix(root_prefix)
        self.endpoint = endpoint.rstrip("/")
        self.metadata_token_url = metadata_token_url.strip()
        self.timeout = float(timeout)
        self._access_token = ""
        self._access_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        if not self.bucket:
            raise ArchiveStorageNotConfigured("MARKETPLACE_MCP_ARCHIVE_BUCKET is empty")

    @classmethod
    def from_env(cls) -> "YandexObjectStorageArchiveStore":
        bucket = os.environ.get("MARKETPLACE_MCP_ARCHIVE_BUCKET", "").strip()
        if not bucket:
            raise ArchiveStorageNotConfigured("Set MARKETPLACE_MCP_ARCHIVE_BUCKET")
        return cls(
            bucket=bucket,
            root_prefix=os.environ.get("MARKETPLACE_MCP_ARCHIVE_PREFIX", ""),
            endpoint=os.environ.get("MARKETPLACE_MCP_ARCHIVE_ENDPOINT", DEFAULT_ENDPOINT),
        )

    @staticmethod
    def _clean_prefix(value: str) -> str:
        return "/".join(part for part in str(value).strip("/").split("/") if part)

    @classmethod
    def _join_key(cls, *parts: str) -> str:
        values: list[str] = []
        for raw in parts:
            clean = cls._clean_prefix(raw)
            if clean:
                values.append(clean)
        return "/".join(values)

    def _object_url(self, key: str) -> str:
        encoded_key = quote(key, safe="/-_.~")
        return f"{self.endpoint}/{self.bucket}/{encoded_key}"

    async def _token(self, *, force: bool = False) -> str:
        now = time.monotonic()
        if not force and self._access_token and now < self._access_token_expires_at:
            return self._access_token
        async with self._token_lock:
            now = time.monotonic()
            if not force and self._access_token and now < self._access_token_expires_at:
                return self._access_token
            async with httpx.AsyncClient(timeout=min(self.timeout, 15.0)) as client:
                resp = await client.get(
                    self.metadata_token_url,
                    headers={"Metadata-Flavor": "Google"},
                )
            if not resp.is_success:
                raise ArchiveStorageError(
                    f"Yandex metadata IAM token failed with HTTP {resp.status_code}: "
                    f"{resp.text[:300]}"
                )
            body = resp.json()
            token = str(body.get("access_token", "")).strip()
            if not token:
                raise ArchiveStorageError("Yandex metadata service returned no access_token")
            try:
                ttl = max(60.0, float(body.get("expires_in", 3600)))
            except (TypeError, ValueError):
                ttl = 3600.0
            self._access_token = token
            self._access_token_expires_at = time.monotonic() + max(30.0, ttl - 60.0)
            return token

    async def _request(
        self,
        method: str,
        url: str,
        *,
        allow_not_found: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {await self._token()}"
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401:
            headers["Authorization"] = f"Bearer {await self._token(force=True)}"
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.request(method, url, headers=headers, **kwargs)
        if allow_not_found and resp.status_code == 404:
            return resp
        if not resp.is_success:
            raise ArchiveStorageError(
                f"Yandex Object Storage {method} failed with HTTP {resp.status_code}: "
                f"{resp.text[:500]}"
            )
        return resp

    async def ensure_folder_path(self, parts: list[str] | tuple[str, ...]) -> str:
        """Return an object-key prefix; Object Storage needs no physical folders."""
        return self._join_key(self.root_prefix, *(str(part) for part in parts))

    async def find_child(
        self, parent_id: str, name: str, *, mime_type: str | None = None
    ) -> ArchiveObject | None:
        del mime_type  # Object Storage has no folder MIME type.
        key = self._join_key(parent_id, name)
        resp = await self._request("HEAD", self._object_url(key), allow_not_found=True)
        if resp.status_code == 404:
            return None
        try:
            size = int(resp.headers.get("Content-Length", "0"))
        except ValueError:
            size = None
        return ArchiveObject(
            id=key,
            name=name,
            mime_type=resp.headers.get("Content-Type", "application/octet-stream"),
            size=size,
            etag=resp.headers.get("ETag", "").strip('"') or None,
            modified_time=resp.headers.get("Last-Modified"),
        )

    async def download_bytes(self, file_id: str) -> bytes:
        key = self._clean_prefix(file_id)
        resp = await self._request("GET", self._object_url(key))
        return resp.content

    async def download_named(
        self, parent_id: str, name: str
    ) -> tuple[ArchiveObject | None, bytes | None]:
        item = await self.find_child(parent_id, name)
        if item is None:
            return None, None
        return item, await self.download_bytes(item.id)

    async def upload_bytes(
        self,
        parent_id: str,
        name: str,
        data: bytes,
        *,
        mime_type: str = "text/csv",
    ) -> ArchiveObject:
        key = self._join_key(parent_id, name)
        md5_raw = hashlib.md5(data, usedforsecurity=False).digest()
        await self._request(
            "PUT",
            self._object_url(key),
            content=data,
            headers={
                "Content-Type": mime_type,
                "Content-Length": str(len(data)),
                "Content-MD5": base64.b64encode(md5_raw).decode("ascii"),
            },
        )
        item = await self.find_child(parent_id, name)
        if item is None:
            raise ArchiveStorageError("Object upload succeeded but HEAD cannot resolve it")
        return item

    async def status(self) -> dict[str, Any]:
        resp = await self._request(
            "GET",
            f"{self.endpoint}/{self.bucket}",
            params={"list-type": "2", "max-keys": "1", "prefix": self.root_prefix},
        )
        return {
            "configured": True,
            "reachable": True,
            "backend": "yandex_object_storage",
            "bucket": self.bucket,
            "root_prefix": self.root_prefix,
            "http_status": resp.status_code,
        }


def build_yandex_archive_store_from_env() -> YandexObjectStorageArchiveStore | None:
    try:
        return YandexObjectStorageArchiveStore.from_env()
    except ArchiveStorageNotConfigured:
        return None
