"""Hybrid marketplace archive storage.

Google Drive is canonical for annual database files and the report registry.
Yandex Object Storage remains the durable home for queue/job state and staging,
and also receives a byte-for-byte backup of canonical files.

When a canonical file is missing on Drive but still exists in Object Storage,
the first content read migrates it to Drive before returning it. Metadata-only
probes never copy a potentially large backup file as a side effect.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from .archive_google import GoogleDriveArchiveStore, build_google_archive_store_from_env
from .archive_yandex import YandexObjectStorageArchiveStore, build_yandex_archive_store_from_env

_LOCATOR_PREFIX = "hybrid-v1:"
_YANDEX_ONLY_PREFIX = "yandex-v1:"


def _b64_encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _b64_decode(value: str) -> str:
    return base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")


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
        drive_item, drive_data = await self.drive.download_named(drive_parent, name)
        if drive_item is not None and drive_data is not None:
            return drive_item, drive_data
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


def build_hybrid_archive_store_from_env() -> HybridArchiveStore | None:
    """Fail closed unless both canonical Drive and durable Yandex are configured."""
    drive = build_google_archive_store_from_env()
    yandex = build_yandex_archive_store_from_env()
    if drive is None or yandex is None:
        return None
    return HybridArchiveStore(drive, yandex)
