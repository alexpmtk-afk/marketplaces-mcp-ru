from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

from core.archive_hybrid import HybridArchiveStore


class FakeStore:
    def __init__(self, backend: str):
        self.backend = backend
        self.files: dict[str, bytes] = {}
        self.ensure_calls: list[tuple[str, ...]] = []
        self.upload_calls: list[str] = []
        self.download_calls: list[str] = []
        self.metadata_calls: list[str] = []
        self.expose_sha256 = True

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

    async def file_metadata(self, file_id):
        self.metadata_calls.append(file_id)
        data = self.files[file_id]
        return {
            "id": file_id,
            "name": file_id.rsplit("/", 1)[-1],
            "size": len(data),
            "sha256Checksum": hashlib.sha256(data).hexdigest() if self.expose_sha256 else None,
            "mimeType": "text/csv",
        }

    async def download_named(self, parent, name):
        key = f"{parent}/{name}"
        self.download_calls.append(key)
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


def test_verified_yandex_backup_supplies_content_without_drive_blob_read():
    store, drive, yandex = _store()
    locator = asyncio.run(store.ensure_folder_path(["База данных", "WB", "wb_laser_master"]))
    drive_key = "drive:База данных/WB/wb_laser_master/base.csv"
    yandex_key = "yandex:База данных/WB/wb_laser_master/base.csv"
    payload = b"same-canonical-bytes"
    drive.files[drive_key] = payload
    yandex.files[yandex_key] = payload

    item, data = asyncio.run(store.download_named(locator, "base.csv"))

    assert item is not None
    assert item.id == drive_key
    assert data == payload
    assert drive.metadata_calls == [drive_key]
    assert drive.download_calls == []
    assert yandex.download_calls == [yandex_key]


def test_stale_yandex_backup_falls_back_to_drive_canonical():
    store, drive, yandex = _store()
    locator = asyncio.run(store.ensure_folder_path(["База данных", "WB", "wb_novokshenov"]))
    drive_key = "drive:База данных/WB/wb_novokshenov/base.csv"
    yandex_key = "yandex:База данных/WB/wb_novokshenov/base.csv"
    drive.files[drive_key] = b"drive-current"
    yandex.files[yandex_key] = b"yandex-stale"

    _, data = asyncio.run(store.download_named(locator, "base.csv"))

    assert data == b"drive-current"
    assert drive.download_calls == [drive_key]
    assert drive.upload_calls == []


def test_missing_yandex_backup_falls_back_to_drive_canonical():
    store, drive, _ = _store()
    locator = asyncio.run(store.ensure_folder_path(["База данных", "WB", "wb_laser_master"]))
    drive_key = "drive:База данных/WB/wb_laser_master/base.csv"
    drive.files[drive_key] = b"drive-only"

    _, data = asyncio.run(store.download_named(locator, "base.csv"))

    assert data == b"drive-only"
    assert drive.download_calls == [drive_key]


def test_drive_checksum_unavailable_falls_back_to_drive_canonical():
    store, drive, yandex = _store()
    locator = asyncio.run(store.ensure_folder_path(["База данных", "WB", "wb_laser_master"]))
    drive_key = "drive:База данных/WB/wb_laser_master/base.csv"
    yandex_key = "yandex:База данных/WB/wb_laser_master/base.csv"
    payload = b"same-canonical-bytes"
    drive.files[drive_key] = payload
    yandex.files[yandex_key] = payload
    drive.expose_sha256 = False

    _, data = asyncio.run(store.download_named(locator, "base.csv"))

    assert data == payload
    assert drive.download_calls == [drive_key]
    assert yandex.download_calls == []


def test_status_reports_google_drive_as_canonical():
    store, _, _ = _store()
    status = asyncio.run(store.status())

    assert status["backend"] == "google_drive_primary"
    assert status["root_folder_id"] == "drive-root"
    assert status["root_name"] == "MCP архив базы данных"
    assert status["queue_and_staging"]["backend"] == "yandex_object_storage"


def test_drive_only_store_reads_canonical_history_and_blocks_mutation():
    from core.archive_hybrid import CanonicalDriveReadOnlyArchiveStore
    from core.archive_google import ArchiveStorageError

    drive = FakeStore("drive")
    store = CanonicalDriveReadOnlyArchiveStore(drive)
    parent = asyncio.run(store.ensure_folder_path([
        "База данных", "WB", "wb_laser_master", "2026", "finance", "weekly", "main"
    ]))
    name = "wb_laser_master__weekly_main__2026.csv"
    drive.files[f"{parent}/{name}"] = b"canonical-history"

    item, data = asyncio.run(store.download_named(parent, name))
    assert item is not None
    assert data == b"canonical-history"

    try:
        asyncio.run(store.upload_bytes(parent, "x.csv", b"x"))
    except ArchiveStorageError as exc:
        assert exc.code == "DURABLE_BACKEND_NOT_CONFIGURED"
    else:
        raise AssertionError("drive-only REMOTE mode must fail closed on mutation")


def test_drive_only_store_rejects_queue_paths():
    from core.archive_hybrid import CanonicalDriveReadOnlyArchiveStore
    from core.archive_google import ArchiveStorageError

    store = CanonicalDriveReadOnlyArchiveStore(FakeStore("drive"))
    try:
        asyncio.run(store.ensure_folder_path(["app", "jobs", "wb-finance"]))
    except ArchiveStorageError as exc:
        assert exc.code == "DURABLE_BACKEND_NOT_CONFIGURED"
    else:
        raise AssertionError("queue state must require an explicit durable backend")


def test_drive_only_status_marks_durable_backend_unavailable():
    from core.archive_hybrid import CanonicalDriveReadOnlyArchiveStore

    store = CanonicalDriveReadOnlyArchiveStore(FakeStore("drive"))
    status = asyncio.run(store.status())

    assert status["configured"] is True
    assert status["reachable"] is True
    assert status["backend"] == "google_drive_primary"
    assert status["read_only"] is True
    assert status["queue_and_staging"]["configured"] is False
    assert status["backup_mirror"]["error"] == "durable_backend_not_configured"


def test_builder_keeps_canonical_drive_readable_without_yandex(monkeypatch):
    import core.archive_hybrid as hybrid

    drive = FakeStore("drive")
    monkeypatch.setattr(hybrid, "build_google_archive_store_from_env", lambda: drive)
    monkeypatch.setattr(hybrid, "build_yandex_archive_store_from_env", lambda: None)

    store = hybrid.build_hybrid_archive_store_from_env()

    assert isinstance(store, hybrid.CanonicalDriveReadOnlyArchiveStore)
    assert store.drive is drive
