from __future__ import annotations

import asyncio
import hashlib

import pytest

import core.archive_google as archive_google
from core.archive_google import ArchiveStorageError, DriveFile, GoogleDriveArchiveStore


V1_URL = "https://script.google.com/macros/s/marketplaces-v1/exec"
ROOT_ID = "marketplaces-root"


def _store() -> GoogleDriveArchiveStore:
    return GoogleDriveArchiveStore(
        bridge_url=V1_URL,
        bridge_secret="marketplaces-secret",
        root_folder_id=ROOT_ID,
        protocol_version=1,
        project_id="marketplaces",
    )


class _StreamResponse:
    def __init__(self, chunks: list[bytes], status_code: int = 200) -> None:
        self._chunks = chunks
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _DirectClient:
    chunks: list[bytes] = []
    seen_urls: list[str] = []

    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb

    def stream(self, method, url, *, headers):
        del method, headers
        self.__class__.seen_urls.append(str(url))
        return _StreamResponse(list(self.__class__.chunks))


def _ready(file_id: str, raw: bytes, *, uri: str = "https://drive.usercontent.google.com/download?id=opaque") -> dict:
    return {
        "ready": True,
        "file_id": file_id,
        "total_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "mime_type": "text/csv",
        "modified_time": "2026-09-14T18:00:00Z",
        "resource_key": "",
        "partial_download_allowed": False,
        "download_uri": uri,
    }


def test_stable_large_download_ready_path_verifies_before_return(monkeypatch):
    store = _store()
    raw = b"annual-csv-large-payload"
    calls: list[str] = []

    async def fake_post(action, payload=None, **kwargs):
        del kwargs
        calls.append(action)
        assert payload == {"file_id": "drive-annual"}
        return _ready("drive-annual", raw)

    store._post_v1 = fake_post  # type: ignore[method-assign]
    _DirectClient.chunks = [raw[:7], raw[7:]]
    _DirectClient.seen_urls = []
    monkeypatch.setattr(archive_google.httpx, "AsyncClient", _DirectClient)

    meta, downloaded = asyncio.run(store.download_large_by_id("drive-annual"))

    assert calls == ["large_download_start"]
    assert downloaded == raw
    assert meta["size"] == len(raw)
    assert meta["sha256Checksum"] == hashlib.sha256(raw).hexdigest()
    assert len(_DirectClient.seen_urls) == 1


def test_stable_large_download_polls_opaque_ticket_only_in_memory(monkeypatch):
    store = _store()
    raw = b"poll-then-download"
    calls: list[tuple[str, dict]] = []

    async def fake_post(action, payload=None, **kwargs):
        del kwargs
        payload = dict(payload or {})
        calls.append((action, payload))
        if action == "large_download_start":
            return {
                "ready": False,
                "download_ticket": "opaque-ticket-123",
                "file_id": "drive-annual",
                "total_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        if action == "large_download_poll":
            assert payload == {"download_ticket": "opaque-ticket-123"}
            return _ready("drive-annual", raw)
        raise AssertionError(action)

    async def no_sleep(delay):
        del delay

    store._post_v1 = fake_post  # type: ignore[method-assign]
    _DirectClient.chunks = [raw]
    _DirectClient.seen_urls = []
    monkeypatch.setattr(archive_google.httpx, "AsyncClient", _DirectClient)
    monkeypatch.setattr(archive_google.asyncio, "sleep", no_sleep)

    meta, downloaded = asyncio.run(
        store.download_large_by_id("drive-annual", max_polls=2, poll_seconds=0.1)
    )

    assert downloaded == raw
    assert meta["id"] == "drive-annual"
    assert [action for action, _ in calls] == ["large_download_start", "large_download_poll"]


def test_stable_large_download_fails_closed_on_exact_size_mismatch(monkeypatch):
    store = _store()
    raw = b"expected"

    async def fake_post(action, payload=None, **kwargs):
        del action, payload, kwargs
        state = _ready("drive-annual", raw)
        state["total_bytes"] = len(raw) + 1
        return state

    store._post_v1 = fake_post  # type: ignore[method-assign]
    _DirectClient.chunks = [raw]
    monkeypatch.setattr(archive_google.httpx, "AsyncClient", _DirectClient)

    with pytest.raises(ArchiveStorageError) as exc:
        asyncio.run(store.download_large_by_id("drive-annual"))
    assert exc.value.code == "SIZE_MISMATCH"


def test_stable_large_download_fails_closed_on_sha256_mismatch(monkeypatch):
    store = _store()
    raw = b"expected"

    async def fake_post(action, payload=None, **kwargs):
        del action, payload, kwargs
        state = _ready("drive-annual", raw)
        state["sha256"] = "0" * 64
        return state

    store._post_v1 = fake_post  # type: ignore[method-assign]
    _DirectClient.chunks = [raw]
    monkeypatch.setattr(archive_google.httpx, "AsyncClient", _DirectClient)

    with pytest.raises(ArchiveStorageError) as exc:
        asyncio.run(store.download_large_by_id("drive-annual"))
    assert exc.value.code == "SHA256_MISMATCH"


def test_stable_rejects_non_google_uri_without_echoing_capability():
    store = _store()
    secret_uri = "https://evil.example/download?bearer=DO_NOT_ECHO"
    raw = b"payload"

    async def fake_post(action, payload=None, **kwargs):
        del action, payload, kwargs
        return _ready("drive-annual", raw, uri=secret_uri)

    store._post_v1 = fake_post  # type: ignore[method-assign]

    with pytest.raises(ArchiveStorageError) as exc:
        asyncio.run(store.download_large_by_id("drive-annual"))
    assert exc.value.code == "INVALID_DOWNLOAD_URI"
    assert "DO_NOT_ECHO" not in str(exc.value)
    assert secret_uri not in str(exc.value)


def test_stable_download_named_uses_verified_large_path_after_small_limit(monkeypatch):
    store = _store()
    raw = b"verified-annual"
    item = DriveFile(
        id="drive-annual",
        name="annual.csv",
        mime_type="text/csv",
        size=len(raw),
        sha256_checksum=hashlib.sha256(raw).hexdigest(),
    )

    async def fake_post(action, payload=None, **kwargs):
        del payload, kwargs
        if action == "read_small":
            raise ArchiveStorageError(
                "file exceeds bounded Apps Script read limit",
                code="LARGE_READ_REQUIRED",
            )
        raise AssertionError(action)

    async def fake_find(parent_id, name, *, mime_type=None):
        del parent_id, name, mime_type
        return item

    async def fake_large(file_id, **kwargs):
        del kwargs
        assert file_id == item.id
        return {
            "id": item.id,
            "size": len(raw),
            "sha256Checksum": hashlib.sha256(raw).hexdigest(),
        }, raw

    store._post_v1 = fake_post  # type: ignore[method-assign]
    store.find_child = fake_find  # type: ignore[method-assign]
    store.download_large_by_id = fake_large  # type: ignore[method-assign]

    returned, downloaded = asyncio.run(
        store.download_named("База данных/WB/test/2026", "annual.csv")
    )
    assert returned == item
    assert downloaded == raw
