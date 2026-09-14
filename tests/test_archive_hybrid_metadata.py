from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.archive_hybrid import HybridArchiveStore


class FakeStore:
    def __init__(self, backend: str):
        self.backend = backend
        self.files: dict[str, bytes] = {}
        self.upload_calls: list[str] = []
        self.download_calls: list[str] = []

    async def ensure_folder_path(self, parts):
        return f"{self.backend}:" + "/".join(str(x) for x in parts)

    async def find_child(self, parent, name, **kwargs):
        del kwargs
        key = f"{parent}/{name}"
        data = self.files.get(key)
        if data is None:
            return None
        return SimpleNamespace(id=key, name=name, size=len(data), mime_type="text/csv")

    async def download_named(self, parent, name):
        key = f"{parent}/{name}"
        self.download_calls.append(key)
        item = await self.find_child(parent, name)
        if item is None:
            return None, None
        return item, self.files[key]

    async def upload_bytes(self, parent, name, data, *, mime_type="text/csv"):
        del mime_type
        key = f"{parent}/{name}"
        self.files[key] = data
        self.upload_calls.append(key)
        return SimpleNamespace(id=key, name=name, size=len(data), mime_type="text/csv")


def test_find_child_reports_drive_miss_without_restoring_yandex_backup():
    drive = FakeStore("drive")
    yandex = FakeStore("yandex")
    store = HybridArchiveStore(drive, yandex)

    locator = asyncio.run(
        store.ensure_folder_path(
            ["База данных", "WB", "wb_laser_master", "2026", "finance", "weekly", "main"]
        )
    )
    backup_parent = "yandex:База данных/WB/wb_laser_master/2026/finance/weekly/main"
    name = "wb_laser_master__weekly_main__2026.csv"
    yandex.files[f"{backup_parent}/{name}"] = b"large-backup-placeholder"

    item = asyncio.run(store.find_child(locator, name))

    assert item is None
    assert yandex.download_calls == []
    assert drive.upload_calls == []


def test_download_named_still_restores_missing_drive_file_from_yandex():
    drive = FakeStore("drive")
    yandex = FakeStore("yandex")
    store = HybridArchiveStore(drive, yandex)

    locator = asyncio.run(
        store.ensure_folder_path(
            ["База данных", "WB", "wb_laser_master", "2026", "finance", "weekly", "main"]
        )
    )
    backup_parent = "yandex:База данных/WB/wb_laser_master/2026/finance/weekly/main"
    drive_parent = "drive:База данных/WB/wb_laser_master/2026/finance/weekly/main"
    name = "wb_laser_master__weekly_main__2026.csv"
    yandex.files[f"{backup_parent}/{name}"] = b"backup-data"

    item, data = asyncio.run(store.download_named(locator, name))

    assert item is not None
    assert data == b"backup-data"
    assert drive.files[f"{drive_parent}/{name}"] == b"backup-data"
