from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.archive_google import ArchiveStorageError
from core.archive_hybrid import RemoteDurableArchiveStore


class FakeStore:
    def __init__(self, backend: str, order: list[str] | None = None):
        self.backend = backend
        self.order = order if order is not None else []
        self.files: dict[str, bytes] = {}
        self.fail_upload = False
        self.ensure_calls: list[tuple[str, ...]] = []

    async def ensure_folder_path(self, parts):
        values = tuple(str(x) for x in parts)
        self.ensure_calls.append(values)
        return f"{self.backend}:" + "/".join(values)

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
            sha256_checksum=None,
        )

    async def file_metadata(self, file_id):
        data = self.files[file_id]
        return {
            "id": file_id,
            "name": file_id.rsplit("/", 1)[-1],
            "size": len(data),
            "mimeType": "text/csv",
        }

    async def download_named(self, parent, name):
        item = await self.find_child(parent, name)
        if item is None:
            return None, None
        return item, self.files[item.id]

    async def download_bytes(self, file_id):
        return self.files[file_id]

    async def upload_bytes(self, parent, name, data, *, mime_type="text/csv"):
        del mime_type
        self.order.append(self.backend)
        if self.fail_upload:
            raise RuntimeError(f"{self.backend} upload failed")
        key = f"{parent}/{name}"
        self.files[key] = bytes(data)
        return SimpleNamespace(
            id=key,
            name=name,
            size=len(data),
            mime_type="text/csv",
            sha256_checksum=__import__("hashlib").sha256(data).hexdigest(),
        )

    async def status(self):
        return {
            "configured": True,
            "reachable": True,
            "backend": self.backend,
            "root_folder_id": "drive-root" if self.backend != "local" else None,
            "root_name": "MCP архив базы данных" if self.backend != "local" else None,
            "read_only": False,
        }


def _store():
    order: list[str] = []
    reader = FakeStore("direct", order)
    writer = FakeStore("writer", order)
    local = FakeStore("local", order)
    return RemoteDurableArchiveStore(reader, writer, local), reader, writer, local, order


def test_job_state_uses_local_durable_only():
    store, reader, writer, local, _ = _store()
    parent = asyncio.run(store.ensure_folder_path(["app", "jobs", "ozon"]))
    asyncio.run(store.upload_bytes(parent, "job.json", b"{}"))

    assert reader.ensure_calls == []
    assert writer.ensure_calls == []
    assert local.ensure_calls == [("app", "jobs", "ozon")]
    assert local.files


def test_canonical_write_is_drive_first_then_local_backup():
    store, _reader, writer, local, order = _store()
    parent = asyncio.run(store.ensure_folder_path(["База данных", "Ozon", "shop"]))
    item = asyncio.run(store.upload_bytes(parent, "current.csv", b"rows"))

    assert order == ["writer", "local"]
    assert item.id == "writer:База данных/Ozon/shop/current.csv"
    assert writer.files["writer:База данных/Ozon/shop/current.csv"] == b"rows"
    assert local.files["local:База данных/Ozon/shop/current.csv"] == b"rows"


def test_backup_failure_fails_closed_after_canonical_write():
    store, _reader, writer, local, _ = _store()
    local.fail_upload = True
    parent = asyncio.run(store.ensure_folder_path(["База данных", "Ozon", "shop"]))

    with pytest.raises(ArchiveStorageError) as caught:
        asyncio.run(store.upload_bytes(parent, "current.csv", b"rows"))

    assert caught.value.code == "LOCAL_DURABLE_BACKUP_FAILED"
    assert writer.files["writer:База данных/Ozon/shop/current.csv"] == b"rows"


def test_canonical_read_prefers_direct_reader():
    store, reader, writer, local, _ = _store()
    parent = asyncio.run(store.ensure_folder_path(["База данных", "Ozon", "shop"]))
    reader.files["direct:База данных/Ozon/shop/current.csv"] = b"canonical"
    writer.files["writer:База данных/Ozon/shop/current.csv"] = b"writer-copy"
    local.files["local:База данных/Ozon/shop/current.csv"] = b"backup"

    item, data = asyncio.run(store.download_named(parent, "current.csv"))
    assert item.id.startswith("direct:")
    assert data == b"canonical"


def test_missing_canonical_can_be_restored_from_local_backup():
    store, reader, writer, local, order = _store()
    parent = asyncio.run(store.ensure_folder_path(["База данных", "Ozon", "shop"]))
    local.files["local:База данных/Ozon/shop/current.csv"] = b"backup"

    item, data = asyncio.run(store.download_named(parent, "current.csv"))
    assert data == b"backup"
    assert item.id == "writer:База данных/Ozon/shop/current.csv"
    assert writer.files[item.id] == b"backup"
    assert order == ["writer"]
    assert not reader.files


def test_remote_durable_status_is_write_capable():
    store, *_ = _store()
    status = asyncio.run(store.status())
    assert status["read_only"] is False
    assert status["reachable"] is True
    assert status["backend"] == "google_drive_primary_remote_local_durable"
    assert status["canonical_read"]["backend"] == "direct"
    assert status["canonical_write"]["backend"] == "writer"
    assert status["queue_and_staging"]["backend"] == "local"


def test_builder_selects_remote_durable_only_when_all_parts_exist(monkeypatch):
    import core.archive_hybrid as hybrid

    reader = FakeStore("direct")
    writer = FakeStore("writer")
    local = FakeStore("local")

    monkeypatch.setenv("MARKETPLACE_MCP_ARCHIVE_DIRECT_GOOGLE", "1")
    monkeypatch.setattr(hybrid, "build_direct_google_archive_store_from_env", lambda: reader)
    monkeypatch.setattr(hybrid, "build_google_archive_store_from_env", lambda: writer)
    monkeypatch.setattr(hybrid, "build_local_archive_store_from_env", lambda: local)

    store = hybrid.build_hybrid_archive_store_from_env()
    assert isinstance(store, hybrid.RemoteDurableArchiveStore)
    assert store.reader is reader
    assert store.writer is writer
    assert store.durable is local
