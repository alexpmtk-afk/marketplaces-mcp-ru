"""Direct read-only Google Drive backend for Marketplaces archive."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .archive_google import (
    ArchiveStorageError,
    ArchiveStorageNotConfigured,
    DriveFile,
)
from .google_drive_cache import (
    ArchiveSourceUnavailable,
    GoogleDriveVerifiedCache,
)
from .google_drive_direct import GoogleDriveDirectStore


DEFAULT_CREDENTIAL = (
    "/opt/mcp/secrets/marketplaces/google-drive-reader.json"
)

DEFAULT_CACHE_ROOT = (
    "/opt/mcp/data/marketplaces/archive-cache"
)


class DirectGoogleDriveArchiveStore:
    """Read-only implementation of the existing archive storage contract."""

    read_only = True

    def __init__(
        self,
        *,
        credential_path: str,
        root_folder_id: str,
        cache_root: str,
        timeout: int = 120,
    ) -> None:
        credential_path = str(credential_path).strip()
        root_folder_id = str(root_folder_id).strip()
        cache_root = str(cache_root).strip()

        if not credential_path:
            raise ArchiveStorageNotConfigured(
                "Direct Google Drive credential path is empty"
            )

        if not root_folder_id:
            raise ArchiveStorageNotConfigured(
                "Direct Google Drive archive root ID is empty"
            )

        if not os.path.isfile(credential_path):
            raise ArchiveStorageNotConfigured(
                "Direct Google Drive credential file is missing"
            )

        self.credential_path = credential_path
        self.root_folder_id = root_folder_id
        self.cache_root = cache_root

        self.direct = GoogleDriveDirectStore(
            credential_path=credential_path,
            archive_root_id=root_folder_id,
            timeout=timeout,
        )

        self.cache = GoogleDriveVerifiedCache(
            self.direct,
            cache_root,
        )

        self.last_read_provenance: dict[str, Any] | None = None

    @staticmethod
    def _path(parts: list[str] | tuple[str, ...]) -> str:
        clean: list[str] = []

        for raw in parts:
            value = str(raw).strip().strip("/")

            if not value:
                continue

            for part in value.split("/"):
                part = part.strip()

                if not part:
                    continue

                if part in {".", ".."} or "\\" in part:
                    raise ArchiveStorageError(
                        "Invalid Drive archive path segment"
                    )

                clean.append(part)

        return "/".join(clean)

    def _resolve_parent_sync(self, parent: str) -> str | None:
        value = str(parent or "").strip().strip("/")

        if not value or value == self.root_folder_id:
            return self.root_folder_id

        # Parent may already be a Google Drive folder ID.
        if "/" not in value:
            try:
                meta = self.direct.get_metadata(value)

                if (
                    meta.get("mimeType")
                    == "application/vnd.google-apps.folder"
                ):
                    self.direct.assert_within_root(value)
                    return value
            except Exception:
                pass

        current = self.root_folder_id

        for segment in self._path((value,)).split("/"):
            children = self.direct.list_children(current)

            matches = [
                item
                for item in children
                if item.get("name") == segment
                and item.get("mimeType")
                == "application/vnd.google-apps.folder"
            ]

            if not matches:
                return None

            if len(matches) != 1:
                raise ArchiveStorageError(
                    f"Ambiguous archive path segment: {segment}",
                    code="AMBIGUOUS_ARCHIVE_PATH",
                )

            current = str(matches[0]["id"])

        return current

    @staticmethod
    def _to_file(item: dict[str, Any]) -> DriveFile:
        raw_size = item.get("size")

        try:
            size = int(raw_size) if raw_size is not None else None
        except (TypeError, ValueError):
            size = None

        return DriveFile(
            id=str(item.get("id") or item.get("file_id") or ""),
            name=str(item.get("name") or ""),
            mime_type=str(
                item.get("mimeType")
                or item.get("mime_type")
                or "text/csv"
            ),
            size=size,
            md5_checksum=(
                str(
                    item.get("md5Checksum")
                    or item.get("md5_checksum")
                    or ""
                ).strip()
                or None
            ),
            sha256_checksum=(
                str(
                    item.get("sha256Checksum")
                    or item.get("sha256")
                    or ""
                ).strip()
                or None
            ),
            modified_time=(
                item.get("modifiedTime")
                or item.get("modified_time")
            ),
        )

    def _cached_pair_by_id(
        self,
        file_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        metadata_path = (
            Path(self.cache_root)
            / str(file_id)
            / "metadata.json"
        )

        if not metadata_path.exists():
            return None

        try:
            meta = json.loads(
                metadata_path.read_text(encoding="utf-8")
            )
        except Exception:
            return None

        name = str(meta.get("name") or "").strip()

        if not name:
            return None

        cached = self.cache.get_verified_cached(
            str(file_id),
            name,
        )

        if not cached:
            return None

        return meta, cached

    def _cached_candidates_by_name(
        self,
        name: str,
    ) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
        root = Path(self.cache_root)

        if not root.exists():
            return []

        result = []

        for metadata_path in root.glob("*/metadata.json"):
            try:
                meta = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                )
            except Exception:
                continue

            if str(meta.get("name") or "") != str(name):
                continue

            file_id = str(
                meta.get("file_id")
                or metadata_path.parent.name
            ).strip()

            if not file_id:
                continue

            cached = self.cache.get_verified_cached(
                file_id,
                str(name),
            )

            if cached:
                result.append(
                    (file_id, meta, cached)
                )

        return result

    async def ensure_folder_path(
        self,
        parts: list[str] | tuple[str, ...],
    ) -> str:
        # Same path-oriented interface used by Bridge v3.
        return self._path(parts)

    async def find_child(
        self,
        parent_id: str,
        name: str,
        *,
        mime_type: str | None = None,
    ) -> DriveFile | None:
        try:
            parent = await asyncio.to_thread(
                self._resolve_parent_sync,
                parent_id,
            )

            if parent is None:
                return None

            children = await asyncio.to_thread(
                self.direct.list_children,
                parent,
            )

            matches = [
                item
                for item in children
                if item.get("name") == str(name)
            ]

            if not matches:
                return None

            if len(matches) != 1:
                raise ArchiveStorageError(
                    f"Ambiguous archive filename: {name}",
                    code="AMBIGUOUS_ARCHIVE_FILE",
                )

            result = self._to_file(matches[0])

            if (
                mime_type
                and result.mime_type
                and result.mime_type != mime_type
            ):
                return None

            return result

        except ArchiveStorageError:
            raise

        except Exception as exc:
            # Drive unavailable: only previously verified cache
            # may be used.
            cached = await asyncio.to_thread(
                self._cached_candidates_by_name,
                str(name),
            )

            if len(cached) == 1:
                file_id, meta, verified = cached[0]

                return self._to_file(
                    {
                        **meta,
                        "id": file_id,
                        "size": verified["size"],
                        "sha256": verified["sha256"],
                    }
                )

            raise ArchiveStorageError(
                "ARCHIVE_SOURCE_UNAVAILABLE",
                retryable=True,
                code="ARCHIVE_SOURCE_UNAVAILABLE",
            ) from exc

    async def file_metadata(
        self,
        file_id: str,
    ) -> dict[str, Any]:
        try:
            meta = await asyncio.to_thread(
                self.direct.get_metadata,
                str(file_id),
            )

            return {
                "id": str(meta.get("id") or ""),
                "name": str(meta.get("name") or ""),
                "size": meta.get("size"),
                "md5Checksum": meta.get("md5Checksum"),
                "sha256Checksum": None,
                "mimeType": meta.get("mimeType"),
                "modifiedTime": meta.get("modifiedTime"),
            }

        except Exception as exc:
            cached = await asyncio.to_thread(
                self._cached_pair_by_id,
                str(file_id),
            )

            if cached:
                meta, verified = cached

                return {
                    "id": str(file_id),
                    "name": str(meta.get("name") or ""),
                    "size": verified["size"],
                    "md5Checksum": None,
                    "sha256Checksum": verified["sha256"],
                    "mimeType": meta.get(
                        "mimeType",
                        "text/csv",
                    ),
                    "modifiedTime": (
                        meta.get("modified_time")
                        or meta.get("modifiedTime")
                    ),
                    "source": "verified_cache",
                    "cacheAgeSeconds": verified[
                        "cache_age_seconds"
                    ],
                }

            raise ArchiveStorageError(
                "ARCHIVE_SOURCE_UNAVAILABLE",
                retryable=True,
                code="ARCHIVE_SOURCE_UNAVAILABLE",
            ) from exc

    async def download_bytes(
        self,
        file_id: str,
    ) -> bytes:
        file_id = str(file_id)

        try:
            meta = await asyncio.to_thread(
                self.direct.get_metadata,
                file_id,
            )

            name = str(meta.get("name") or "")

        except Exception:
            cached = await asyncio.to_thread(
                self._cached_pair_by_id,
                file_id,
            )

            if not cached:
                raise ArchiveStorageError(
                    "ARCHIVE_SOURCE_UNAVAILABLE",
                    retryable=True,
                    code="ARCHIVE_SOURCE_UNAVAILABLE",
                )

            meta, _ = cached
            name = str(meta.get("name") or "")

        try:
            result = await asyncio.to_thread(
                self.cache.sync_or_cached,
                file_id,
                name,
            )

        except ArchiveSourceUnavailable as exc:
            raise ArchiveStorageError(
                "ARCHIVE_SOURCE_UNAVAILABLE",
                retryable=True,
                code="ARCHIVE_SOURCE_UNAVAILABLE",
            ) from exc

        except Exception as exc:
            raise ArchiveStorageError(
                "ARCHIVE_SOURCE_UNAVAILABLE",
                retryable=True,
                code="ARCHIVE_SOURCE_UNAVAILABLE",
            ) from exc

        self.last_read_provenance = {
            "file_id": file_id,
            "name": name,
            "source": result.get("source"),
            "drive_available": result.get(
                "drive_available"
            ),
            "cache_age_seconds": result.get(
                "cache_age_seconds",
                0,
            ),
            "size": result.get("size"),
            "sha256": result.get("sha256"),
        }

        path = Path(result["path"])

        return await asyncio.to_thread(
            path.read_bytes
        )

    async def download_named(
        self,
        parent_id: str,
        name: str,
    ):
        item = await self.find_child(
            parent_id,
            name,
        )

        if item is None:
            return None, None

        data = await self.download_bytes(
            item.id
        )

        return item, data

    async def upload_bytes(
        self,
        parent_id: str,
        name: str,
        data: bytes,
        *,
        mime_type: str = "text/csv",
    ):
        del parent_id, name, data, mime_type

        raise ArchiveStorageError(
            "Direct Google Drive archive is read-only",
            code="DURABLE_BACKEND_NOT_CONFIGURED",
        )

    async def status(self) -> dict[str, Any]:
        try:
            root = await asyncio.to_thread(
                self.direct.get_metadata,
                self.root_folder_id,
            )

            return {
                "configured": True,
                "reachable": True,
                "backend": "google_drive_direct_api",
                "root_folder_id": self.root_folder_id,
                "root_name": root.get("name"),
                "read_only": True,
            }

        except Exception as exc:
            return {
                "configured": True,
                "reachable": False,
                "backend": "google_drive_direct_api",
                "root_folder_id": self.root_folder_id,
                "root_name": None,
                "read_only": True,
                "error": type(exc).__name__,
            }


def build_direct_google_archive_store_from_env(
) -> DirectGoogleDriveArchiveStore | None:
    credential = os.environ.get(
        "MARKETPLACE_MCP_ARCHIVE_GOOGLE_CREDENTIAL",
        DEFAULT_CREDENTIAL,
    ).strip()

    root = os.environ.get(
        "MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID",
        "",
    ).strip()

    cache_root = os.environ.get(
        "MARKETPLACE_MCP_ARCHIVE_CACHE_ROOT",
        DEFAULT_CACHE_ROOT,
    ).strip()

    if not root:
        return None

    if not credential or not os.path.isfile(credential):
        return None

    return DirectGoogleDriveArchiveStore(
        credential_path=credential,
        root_folder_id=root,
        cache_root=cache_root,
    )


__all__ = [
    "DirectGoogleDriveArchiveStore",
    "build_direct_google_archive_store_from_env",
]
