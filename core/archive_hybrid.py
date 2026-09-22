"""Hybrid marketplace archive storage.

Google Drive is canonical for annual database files and the report registry.
Yandex Object Storage remains the durable home for queue/job state and staging,
and also receives a byte-for-byte backup of canonical files.

Canonical metadata always comes from Drive. When an exact Yandex backup can be
verified against Drive's SHA256 and size, content reads use those mirrored bytes
instead of moving a potentially large file through Apps Script. If verification
is unavailable or fails, the store falls back to the canonical Drive content
path. When a canonical file is missing on Drive but still exists in Object
Storage, the first content read migrates it to Drive before returning it.
Metadata-only probes never copy a potentially large backup file as a side effect.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from .archive_google import (
    ArchiveStorageError,
    GoogleDriveArchiveStore,
    build_google_archive_store_from_env,
)
from .archive_yandex import YandexObjectStorageArchiveStore, build_yandex_archive_store_from_env

_LOCATOR_PREFIX = "hybrid-v1:"
_YANDEX_ONLY_PREFIX = "yandex-v1:"


def _b64_encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _b64_decode(value: str) -> str:
    return base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")


def _is_sha256(value: str) -> bool:
    raw = str(value or "").strip().lower()
    return len(raw) == 64 and all(ch in "0123456789abcdef" for ch in raw)


class HybridArchiveStore:
    """Path-aware archive store with Drive canonical and Yandex durable support."""

    def __init__(
        self,
        drive: GoogleDriveArchiveStore,
        yandex: YandexObjectStorageArchiveStore,
    ) -> None:
        self.drive = drive
        self.yandex = yandex

    @staticmethod
    def _is_job_path(parts: list[str] | tuple[str, ...]) -> bool:
        normalized = [str(item).strip() for item in parts if str(item).strip()]
        return len(normalized) >= 2 and normalized[0] == "app" and normalized[1] == "jobs"

    @staticmethod
    def _hybrid_locator(drive_parent: str, yandex_parent: str) -> str:
        payload = json.dumps(
            {"drive": drive_parent, "yandex": yandex_parent},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return _LOCATOR_PREFIX + _b64_encode(payload)

    @staticmethod
    def _yandex_locator(parent: str) -> str:
        return _YANDEX_ONLY_PREFIX + _b64_encode(parent)

    @staticmethod
    def _decode_parent(parent_id: str) -> tuple[str, str | None, str | None]:
        value = str(parent_id)
        if value.startswith(_YANDEX_ONLY_PREFIX):
            return "yandex", None, _b64_decode(value[len(_YANDEX_ONLY_PREFIX):])
        if value.startswith(_LOCATOR_PREFIX):
            payload = json.loads(_b64_decode(value[len(_LOCATOR_PREFIX):]))
            return "hybrid", str(payload["drive"]), str(payload["yandex"])
        raise ValueError("Unknown hybrid archive parent locator")

    async def ensure_folder_path(self, parts: list[str] | tuple[str, ...]) -> str:
        if self._is_job_path(parts):
            yandex_parent = await self.yandex.ensure_folder_path(parts)
            return self._yandex_locator(yandex_parent)
        drive_parent = await self.drive.ensure_folder_path(parts)
        yandex_parent = await self.yandex.ensure_folder_path(parts)
        return self._hybrid_locator(drive_parent, yandex_parent)

    async def _migrate_if_needed(
        self,
        drive_parent: str,
        yandex_parent: str,
        name: str,
        *,
        mime_type: str = "text/csv",
    ):
        drive_item = await self.drive.find_child(drive_parent, name)
        if drive_item is not None:
            return drive_item
        yandex_item, yandex_data = await self.yandex.download_named(yandex_parent, name)
        if yandex_item is None or yandex_data is None:
            return None
        return await self.drive.upload_bytes(
            drive_parent,
            name,
            yandex_data,
            mime_type=yandex_item.mime_type or mime_type,
        )

    async def _verified_backup_bytes(
        self,
        drive_item,
        yandex_parent: str,
        name: str,
    ) -> bytes | None:
        """Return mirror bytes only when Drive metadata proves they are exact.

        Bridge v3 is intentionally a control plane for large files. Its
        ``metadata_by_id`` action returns Drive REST size/SHA256 without moving
        the blob through Apps Script. The Yandex object is accepted as content
        only if both properties match exactly. Any uncertainty falls back to the
        canonical Drive content path rather than silently trusting the mirror.
        """
        file_id = str(getattr(drive_item, "id", "") or "").strip()
        if not file_id:
            return None
        try:
            metadata = await self.drive.file_metadata(file_id)
        except ArchiveStorageError:
            return None

        try:
            expected_size = int(metadata.get("size"))
        except (TypeError, ValueError):
            return None
        expected_sha = str(metadata.get("sha256Checksum") or "").strip().lower()
        if expected_size < 0 or not _is_sha256(expected_sha):
            return None

        yandex_item, yandex_data = await self.yandex.download_named(yandex_parent, name)
        if yandex_item is None or yandex_data is None:
            return None
        if len(yandex_data) != expected_size:
            return None
        mirror_size = getattr(yandex_item, "size", None)
        if mirror_size is not None:
            try:
                if int(mirror_size) != expected_size:
                    return None
            except (TypeError, ValueError):
                return None
        if hashlib.sha256(yandex_data).hexdigest() != expected_sha:
            return None
        return yandex_data

    async def find_child(
        self,
        parent_id: str,
        name: str,
        *,
        mime_type: str | None = None,
    ):
        """Return metadata without triggering read-through migration.

        For canonical hybrid paths Google Drive is the source of truth, so a
        metadata probe reports only what is physically present on Drive. Actual
        content reads still use ``download_named`` and retain read-through
        recovery from the Yandex backup.
        """
        mode, drive_parent, yandex_parent = self._decode_parent(parent_id)
        if mode == "yandex":
            assert yandex_parent is not None
            return await self.yandex.find_child(yandex_parent, name, mime_type=mime_type)
        assert drive_parent is not None and yandex_parent is not None
        return await self.drive.find_child(drive_parent, name, mime_type=mime_type)

    async def download_bytes(self, file_id: str) -> bytes:
        # Canonical callers should normally use download_named. This helper keeps
        # the common storage interface available for explicit Drive file IDs.
        return await self.drive.download_bytes(file_id)

    async def download_named(self, parent_id: str, name: str):
        mode, drive_parent, yandex_parent = self._decode_parent(parent_id)
        if mode == "yandex":
            assert yandex_parent is not None
            return await self.yandex.download_named(yandex_parent, name)
        assert drive_parent is not None and yandex_parent is not None

        # Drive remains authoritative for identity and integrity. Avoid moving
        # large canonical bytes through Apps Script when the exact same bytes
        # are already present in the durable Yandex backup.
        drive_item = await self.drive.find_child(drive_parent, name)
        if drive_item is not None:
            verified_backup = await self._verified_backup_bytes(
                drive_item,
                yandex_parent,
                name,
            )
            if verified_backup is not None:
                return drive_item, verified_backup

            # Mirror missing/stale or Drive checksum unavailable: preserve the
            # old canonical behavior and read directly from Drive.
            canonical_item, canonical_data = await self.drive.download_named(drive_parent, name)
            if canonical_item is not None and canonical_data is not None:
                return canonical_item, canonical_data
            return None, None

        # Canonical missing: recover the durable backup back into Drive before
        # returning it, preserving the existing read-through migration rule.
        yandex_item, yandex_data = await self.yandex.download_named(yandex_parent, name)
        if yandex_item is None or yandex_data is None:
            return None, None
        migrated = await self.drive.upload_bytes(
            drive_parent,
            name,
            yandex_data,
            mime_type=yandex_item.mime_type or "text/csv",
        )
        return migrated, yandex_data

    async def upload_bytes(
        self,
        parent_id: str,
        name: str,
        data: bytes,
        *,
        mime_type: str = "text/csv",
    ):
        mode, drive_parent, yandex_parent = self._decode_parent(parent_id)
        if mode == "yandex":
            assert yandex_parent is not None
            return await self.yandex.upload_bytes(
                yandex_parent,
                name,
                data,
                mime_type=mime_type,
            )
        assert drive_parent is not None and yandex_parent is not None
        # Drive is canonical: fail the archive operation if the primary write
        # fails. Only after that succeeds do we update the Yandex backup.
        drive_item = await self.drive.upload_bytes(
            drive_parent,
            name,
            data,
            mime_type=mime_type,
        )
        await self.yandex.upload_bytes(
            yandex_parent,
            name,
            data,
            mime_type=mime_type,
        )
        return drive_item

    async def status(self) -> dict[str, Any]:
        drive_status = await self.drive.status()
        yandex_status = await self.yandex.status()
        return {
            "configured": True,
            "reachable": bool(drive_status.get("reachable")),
            "backend": "google_drive_primary",
            "root_folder_id": drive_status.get("root_folder_id"),
            "root_name": drive_status.get("root_name"),
            "canonical": drive_status,
            "queue_and_staging": yandex_status,
            "backup_mirror": yandex_status,
        }


class CanonicalDriveReadOnlyArchiveStore:
    """Read-only canonical archive mode for REMOTE after the Yandex migration.

    Google Drive remains the business source of truth.  The old Yandex Object
    Storage backend was used for queue/job state, staging and backup mirrors.
    Its absence must not make already-published canonical history unreadable.

    Mutations remain fail-closed until an explicit REMOTE durable backend is
    configured; this class intentionally exposes only canonical read methods.
    """

    read_only = True

    def __init__(self, drive: GoogleDriveArchiveStore) -> None:
        self.drive = drive

    @staticmethod
    def _is_job_path(parts: list[str] | tuple[str, ...]) -> bool:
        normalized = [str(item).strip() for item in parts if str(item).strip()]
        return len(normalized) >= 2 and normalized[0] == "app" and normalized[1] == "jobs"

    async def ensure_folder_path(self, parts: list[str] | tuple[str, ...]) -> str:
        if self._is_job_path(parts):
            raise ArchiveStorageError(
                "Archive queue/staging backend is not configured on REMOTE",
                code="DURABLE_BACKEND_NOT_CONFIGURED",
            )
        return await self.drive.ensure_folder_path(parts)

    async def find_child(self, parent_id: str, name: str, *, mime_type: str | None = None):
        return await self.drive.find_child(parent_id, name, mime_type=mime_type)

    async def download_bytes(self, file_id: str) -> bytes:
        return await self.drive.download_bytes(file_id)

    async def download_named(self, parent_id: str, name: str):
        return await self.drive.download_named(parent_id, name)

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
            "Archive mutation requires an explicitly configured REMOTE durable backend",
            code="DURABLE_BACKEND_NOT_CONFIGURED",
        )

    async def status(self) -> dict[str, Any]:
        drive_status = await self.drive.status()
        unavailable = {
            "configured": False,
            "reachable": False,
            "backend": None,
            "error": "durable_backend_not_configured",
        }
        return {
            "configured": True,
            "reachable": bool(drive_status.get("reachable")),
            "backend": "google_drive_primary",
            "root_folder_id": drive_status.get("root_folder_id"),
            "root_name": drive_status.get("root_name"),
            "canonical": drive_status,
            "queue_and_staging": dict(unavailable),
            "backup_mirror": dict(unavailable),
            "read_only": True,
        }


def build_hybrid_archive_store_from_env() -> HybridArchiveStore | CanonicalDriveReadOnlyArchiveStore | None:
    """Build REMOTE archive storage without hiding canonical Drive history.

    Canonical Drive is sufficient for read-only historical queries.  A durable
    backend is still mandatory for queue/staging/candidate/backup mutations.
    """
    drive = build_google_archive_store_from_env()
    if drive is None:
        return None
    yandex = build_yandex_archive_store_from_env()
    if yandex is None:
        return CanonicalDriveReadOnlyArchiveStore(drive)
    return HybridArchiveStore(drive, yandex)
