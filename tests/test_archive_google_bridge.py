from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest

from core.archive_google import (
    ArchiveStorageError,
    ArchiveStorageNotConfigured,
    GoogleDriveArchiveStore,
)


def _store() -> GoogleDriveArchiveStore:
    return GoogleDriveArchiveStore(
        bridge_url="https://script.google.com/macros/s/test-deployment/exec",
        bridge_secret="secret-value",
        root_folder_id="root-id",
    )


def test_bridge_requires_expected_configuration():
    with pytest.raises(ArchiveStorageNotConfigured):
        GoogleDriveArchiveStore(
            bridge_url="",
            bridge_secret="secret",
            root_folder_id="root-id",
        )
    with pytest.raises(ArchiveStorageNotConfigured):
        GoogleDriveArchiveStore(
            bridge_url="https://example.com/not-apps-script",
            bridge_secret="secret",
            root_folder_id="root-id",
        )


def test_folder_locator_is_relative_to_fixed_root():
    store = _store()
    path = asyncio.run(store.ensure_folder_path([
        "База данных", "WB", "wb_novokshenov", "2026", "finance", "weekly", "main"
    ]))
    assert path == "База данных/WB/wb_novokshenov/2026/finance/weekly/main"


def test_upload_sends_base64_and_checksum_to_bridge():
    store = _store()
    captured = {}

    async def fake_post(action, **payload):
        captured["action"] = action
        captured.update(payload)
        return {
            "ok": True,
            "sha256": hashlib.sha256(b"archive").hexdigest(),
            "file": {
                "id": "drive-file-id",
                "name": "annual.csv",
                "mime_type": "text/csv",
                "size": 7,
            },
        }

    store._post = fake_post  # type: ignore[method-assign]
    item = asyncio.run(store.upload_bytes("База данных/WB/test", "annual.csv", b"archive"))

    assert item.id == "drive-file-id"
    assert captured["action"] == "write"
    assert captured["path"] == "База данных/WB/test"
    assert captured["content_base64"] == base64.b64encode(b"archive").decode("ascii")
    assert captured["sha256"] == hashlib.sha256(b"archive").hexdigest()


def test_download_named_decodes_bridge_payload():
    store = _store()

    async def fake_post(action, **payload):
        assert action == "read"
        assert payload["path"] == "База данных/WB/test"
        return {
            "ok": True,
            "found": True,
            "file": {
                "id": "drive-file-id",
                "name": "annual.csv",
                "mime_type": "text/csv",
                "size": 7,
            },
            "content_base64": base64.b64encode(b"archive").decode("ascii"),
        }

    store._post = fake_post  # type: ignore[method-assign]
    item, data = asyncio.run(store.download_named("База данных/WB/test", "annual.csv"))
    assert item is not None and item.id == "drive-file-id"
    assert data == b"archive"


def test_status_fails_closed_on_wrong_drive_root():
    store = _store()

    async def fake_post(action, **payload):
        del payload
        assert action == "health"
        return {
            "ok": True,
            "version": 2,
            "root_id": "wrong-root",
            "root_name": "MCP архив базы данных",
        }

    store._post = fake_post  # type: ignore[method-assign]
    with pytest.raises(ArchiveStorageError, match="root mismatch"):
        asyncio.run(store.status())
