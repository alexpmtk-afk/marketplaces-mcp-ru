from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.archive_hybrid import HybridArchiveStore


class FakeStore:
    def __init__(self, backend: str):
        self.backend = backend
        self.files: dict[str, bytes] = {}
        self.ensure_calls: list[tuple[str, ...]] = []
        self.upload_calls: list[str] = []

    async def ensure_folder_path(self, parts):
        path = "/".join(str(x) for x in parts)
        self.ensure_calls.append(tuple(str(x) for x in parts))
        return f"{self.backend}:{path}"

    async def find_child(self, parent, name, **kwargs):
        del kwargs
        key = f"{parent}/{name}"
        data = self.files.get(key)
        if data is None:
            return None
        return SimpleNamespace(
            id=key,
            name=name,
            size=len(data),
            mime_type="text/csv",
        )

    async def download_named(self, parent, name):
        item = await self.find_child(parent, name)
        if item is None:
            return None, None
        return item, self.files[item.id]

    async def download_bytes(self, file_id):
        return self.files[file_id]

    async def upload_bytes(self, parent, name, data, *, mime_type="text/csv"):
        del mime_type
        key = f"{parent}/{name}"
        self.files[key] = data
        self.upload_calls.append(key)
        return SimpleNamespace(
            id=key,
            name=name,
            size=len(data),
            mime_type="text/csv",
        )

    async def status(self):
        if self.backend == "drive":
            return {
                "configured": True,
                "reachable": True,
                "backend": "google_drive",
                "root_folder_id": "drive-root",
                "root_name": "MCP архив базы данных",
            }
        return {
            "configured": True,
            "reachable": True,
            "backend": "yandex_object_storage",
            "bucket": "archive-bucket",
        }


def _store():
    drive = FakeStore("drive")
    yandex = FakeStore("yandex")
    return HybridArchiveStore(drive, yandex), drive, yandex


def test_job_state_and_staging_use_yandex_only():
    store, drive, yandex = _store()
    locator = asyncio.run(store.ensure_folder_path(["app", "jobs", "wb-finance"]))
    asyncio.run(store.upload_bytes(locator, "job.json", b"{}", mime_type="application/json"))

    assert drive.ensure_calls == []
    assert drive.upload_calls == []
    assert yandex.ensure_calls == [("app", "jobs", "wb-finance")]
    assert len(yandex.upload_calls) == 1


def test_canonical_drive_miss_migrates_existing_yandex_file():
    store, drive, yandex = _store()
    locator = asyncio.run(store.ensure_folder_path([
        "База данных", "WB", "wb_novokshenov", "2026", "finance", "weekly", "main"
    ]))
    yandex_parent = "yandex:База данных/WB/wb_novokshenov/2026/finance/weekly/main"
    key = f"{yandex_parent}/wb_novokshenov__weekly_main__2026.csv"
    yandex.files[key] = b"old-yandex-data"

    item, data = asyncio.run(store.download_named(
        locator, "wb_novokshenov__weekly_main__2026.csv"
    ))

    assert data == b"old-yandex-data"
    assert item is not None
    drive_parent = "drive:База данных/WB/wb_novokshenov/2026/finance/weekly/main"
    assert drive.files[f"{drive_parent}/wb_novokshenov__weekly_main__2026.csv"] == b"old-yandex-data"


def test_canonical_write_updates_drive_first_and_yandex_backup():
    store, drive, yandex = _store()
    locator = asyncio.run(store.ensure_folder_path(["app", "registry"]))
    item = asyncio.run(store.upload_bytes(locator, "reports_registry.csv", b"registry"))

    drive_key = "drive:app/registry/reports_registry.csv"
    yandex_key = "yandex:app/registry/reports_registry.csv"
    assert drive.files[drive_key] == b"registry"
    assert yandex.files[yandex_key] == b"registry"
    assert item.id == drive_key


def test_existing_drive_file_is_source_of_truth():
    store, drive, yandex = _store()
    locator = asyncio.run(store.ensure_folder_path(["База данных", "WB", "wb_novokshenov"]))
    drive_key = "drive:База данных/WB/wb_novokshenov/base.csv"
    yandex_key = "yandex:База данных/WB/wb_novokshenov/base.csv"
    drive.files[drive_key] = b"drive-current"
    yandex.files[yandex_key] = b"yandex-stale"

    _, data = asyncio.run(store.download_named(locator, "base.csv"))
    assert data == b"drive-current"
    assert drive.upload_calls == []


def test_status_reports_google_drive_as_canonical():
    store, _, _ = _store()
    status = asyncio.run(store.status())

    assert status["backend"] == "google_drive_primary"
    assert status["root_folder_id"] == "drive-root"
    assert status["root_name"] == "MCP архив базы данных"
    assert status["queue_and_staging"]["backend"] == "yandex_object_storage"
