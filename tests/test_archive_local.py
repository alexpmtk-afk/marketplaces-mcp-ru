from __future__ import annotations

import asyncio
import hashlib

import pytest

from core.archive_google import ArchiveStorageError
from core.archive_local import LocalFilesystemArchiveStore


def test_local_store_round_trip_is_atomic_and_verified(tmp_path):
    store = LocalFilesystemArchiveStore(str(tmp_path / "archive"))
    parent = asyncio.run(store.ensure_folder_path(["app", "jobs", "ozon"]))
    payload = b'{"state":"queued"}'

    item = asyncio.run(
        store.upload_bytes(parent, "task.json", payload, mime_type="application/json")
    )
    found, raw = asyncio.run(store.download_named(parent, "task.json"))
    meta = asyncio.run(store.file_metadata(item.id))

    assert found is not None
    assert raw == payload
    assert item.size == len(payload)
    assert item.sha256_checksum == hashlib.sha256(payload).hexdigest()
    assert meta["sha256Checksum"] == item.sha256_checksum
    assert not list((tmp_path / "archive" / "app" / "jobs" / "ozon").glob(".archive-*.tmp"))


def test_local_store_replaces_existing_file_without_partial_content(tmp_path):
    store = LocalFilesystemArchiveStore(str(tmp_path / "archive"))
    parent = asyncio.run(store.ensure_folder_path(["backup"]))
    asyncio.run(store.upload_bytes(parent, "x.csv", b"old"))
    asyncio.run(store.upload_bytes(parent, "x.csv", b"new-complete"))

    _, raw = asyncio.run(store.download_named(parent, "x.csv"))
    assert raw == b"new-complete"


def test_local_store_download_range(tmp_path):
    store = LocalFilesystemArchiveStore(str(tmp_path / "archive"))
    parent = asyncio.run(store.ensure_folder_path(["staging"]))
    item = asyncio.run(store.upload_bytes(parent, "blob.bin", b"0123456789"))
    assert asyncio.run(store.download_range(item.id, 2, 6)) == b"2345"


@pytest.mark.parametrize(
    "parts",
    [
        [".."],
        ["safe", ".."],
        ["a/b"],
        ["a\\b"],
    ],
)
def test_local_store_rejects_path_escape_segments(tmp_path, parts):
    store = LocalFilesystemArchiveStore(str(tmp_path / "archive"))
    with pytest.raises(ArchiveStorageError):
        asyncio.run(store.ensure_folder_path(parts))


def test_local_store_rejects_unsafe_filename(tmp_path):
    store = LocalFilesystemArchiveStore(str(tmp_path / "archive"))
    parent = asyncio.run(store.ensure_folder_path(["safe"]))
    with pytest.raises(ArchiveStorageError):
        asyncio.run(store.upload_bytes(parent, "../escape.txt", b"x"))


def test_local_store_status_reports_write_capability(tmp_path):
    store = LocalFilesystemArchiveStore(str(tmp_path / "archive"))
    status = asyncio.run(store.status())
    assert status["reachable"] is True
    assert status["read_only"] is False
    assert status["backend"] == "local_filesystem_archive"
    assert status["atomic_replace"] is True
