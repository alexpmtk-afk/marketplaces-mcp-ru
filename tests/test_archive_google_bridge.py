from __future__ import annotations

import asyncio
import base64
import hashlib

import httpx
import pytest

import core.archive_google as archive_google
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


def test_v1_mode_is_fail_closed(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_PROTOCOL", "v1")
    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL", "https://script.google.com/macros/s/v3/exec")
    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET", "secret")
    monkeypatch.setenv("MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID", "root")
    with pytest.raises(ArchiveStorageNotConfigured, match="retired"):
        GoogleDriveArchiveStore.from_env()


def test_folder_locator_is_relative_to_fixed_root():
    store = _store()
    path = asyncio.run(store.ensure_folder_path([
        "База данных", "WB", "wb_novokshenov", "2026", "finance", "weekly", "main"
    ]))
    assert path == "База данных/WB/wb_novokshenov/2026/finance/weekly/main"


def test_upload_sends_base64_and_checksum_to_bridge_and_prechecks_replay():
    store = _store()
    captured = {}

    async def no_existing(parent_id, name):
        assert parent_id == "База данных/WB/test"
        assert name == "annual.csv"
        return None, None

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

    store.download_named = no_existing  # type: ignore[method-assign]
    store._post = fake_post  # type: ignore[method-assign]
    item = asyncio.run(store.upload_bytes("База данных/WB/test", "annual.csv", b"archive"))

    assert item.id == "drive-file-id"
    assert captured["action"] == "write"
    assert captured["path"] == "База данных/WB/test"
    assert captured["content_base64"] == base64.b64encode(b"archive").decode("ascii")
    assert captured["sha256"] == hashlib.sha256(b"archive").hexdigest()


def test_identical_small_write_replay_reuses_existing_file_without_write():
    store = _store()
    existing = archive_google.DriveFile(
        id="same-id",
        name="reports_registry.csv",
        mime_type="text/csv",
        size=7,
    )

    async def existing_read(parent_id, name):
        del parent_id, name
        return existing, b"archive"

    async def should_not_write(action, **payload):
        raise AssertionError((action, payload))

    store.download_named = existing_read  # type: ignore[method-assign]
    store._post = should_not_write  # type: ignore[method-assign]
    item = asyncio.run(store.upload_bytes("app/registry", "reports_registry.csv", b"archive"))
    assert item.id == "same-id"


def test_download_named_decodes_bridge_payload():
    store = _store()
    calls = []

    async def fake_post(action, **payload):
        calls.append(action)
        assert payload["path"] == "База данных/WB/test"
        file = {
            "id": "drive-file-id",
            "name": "annual.csv",
            "mime_type": "text/csv",
            "size": 7,
        }
        if action == "stat":
            return {"ok": True, "found": True, "file": file}
        assert action == "read"
        return {
            "ok": True,
            "found": True,
            "file": file,
            "content_base64": base64.b64encode(b"archive").decode("ascii"),
        }

    store._post = fake_post  # type: ignore[method-assign]
    item, data = asyncio.run(store.download_named("База данных/WB/test", "annual.csv"))
    assert item is not None and item.id == "drive-file-id"
    assert data == b"archive"
    assert calls == ["stat", "read"]


def test_promote_verified_file_sends_exact_integrity_and_identity_contract():
    store = _store()
    captured = {}
    sha = hashlib.sha256(b"archive").hexdigest()

    async def fake_post(action, **payload):
        captured["action"] = action
        captured.update(payload)
        return {
            "ok": True,
            "previous_file_trashed": True,
            "file": {
                "id": "staged-id",
                "name": "annual.csv",
                "mimeType": "text/csv",
                "size": "7",
                "sha256Checksum": sha,
            },
        }

    store._post = fake_post  # type: ignore[method-assign]
    item = asyncio.run(store.promote_verified_file(
        parent_id="База данных/WB/test/2026/finance/weekly/main",
        file_id="staged-id",
        staging_name=".annual.csv.upload-report-1-abcdef.tmp",
        canonical_name="annual.csv",
        expected_bytes=7,
        expected_sha256=sha,
        previous_file_id="old-id",
    ))

    assert item.id == "staged-id"
    assert item.sha256_checksum == sha
    assert captured["action"] == "promote_verified"
    assert captured["staging_filename"] == ".annual.csv.upload-report-1-abcdef.tmp"
    assert captured["canonical_filename"] == "annual.csv"
    assert captured["expected_bytes"] == 7
    assert captured["expected_sha256"] == sha
    assert captured["previous_file_id"] == "old-id"


def test_promote_verified_file_requires_previous_cleanup_confirmation():
    store = _store()
    sha = hashlib.sha256(b"archive").hexdigest()

    async def fake_post(action, **payload):
        del action, payload
        return {
            "ok": True,
            "previous_file_trashed": False,
            "file": {
                "id": "staged-id",
                "name": "annual.csv",
                "mimeType": "text/csv",
                "size": "7",
                "sha256Checksum": sha,
            },
        }

    store._post = fake_post  # type: ignore[method-assign]
    with pytest.raises(ArchiveStorageError) as exc:
        asyncio.run(store.promote_verified_file(
            parent_id="База данных/WB/test",
            file_id="staged-id",
            staging_name=".annual.csv.tmp",
            canonical_name="annual.csv",
            expected_bytes=7,
            expected_sha256=sha,
            previous_file_id="old-id",
        ))
    assert exc.value.retryable is True


def test_promote_verified_file_fails_closed_on_wrong_checksum_response():
    store = _store()
    sha = hashlib.sha256(b"archive").hexdigest()

    async def fake_post(action, **payload):
        del action, payload
        return {
            "ok": True,
            "previous_file_trashed": True,
            "file": {
                "id": "staged-id",
                "name": "annual.csv",
                "mimeType": "text/csv",
                "size": "7",
                "sha256Checksum": "0" * 64,
            },
        }

    store._post = fake_post  # type: ignore[method-assign]
    with pytest.raises(ArchiveStorageError, match="size/SHA256"):
        asyncio.run(store.promote_verified_file(
            parent_id="База данных/WB/test",
            file_id="staged-id",
            staging_name=".annual.csv.tmp",
            canonical_name="annual.csv",
            expected_bytes=7,
            expected_sha256=sha,
            previous_file_id="old-id",
        ))


def test_status_requires_bridge_v3_and_correct_drive_root():
    store = _store()
    responses = [
        {"ok": True, "version": 2, "root_id": "root-id", "root_name": "MCP архив базы данных"},
        {"ok": True, "version": 3, "root_id": "wrong-root", "root_name": "MCP архив базы данных"},
    ]

    async def fake_post(action, **payload):
        del payload
        assert action == "health"
        return responses.pop(0)

    store._post = fake_post  # type: ignore[method-assign]
    with pytest.raises(ArchiveStorageError, match="version mismatch"):
        asyncio.run(store.status())
    with pytest.raises(ArchiveStorageError, match="root mismatch"):
        asyncio.run(store.status())


def test_post_retries_transient_google_redirect_404_from_original_exec_url(monkeypatch):
    store = _store()
    calls: list[str] = []
    responses = [
        httpx.Response(
            404,
            request=httpx.Request(
                "POST", "https://script.googleusercontent.com/macros/echo?user_content_key=expired"
            ),
            text="Not Found",
        ),
        httpx.Response(
            200,
            request=httpx.Request("POST", store.bridge_url),
            json={
                "ok": True,
                "version": 3,
                "root_id": "root-id",
                "root_name": "MCP архив базы данных",
            },
        ),
    ]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
        async def post(self, url, **kwargs):
            del kwargs
            calls.append(str(url))
            return responses.pop(0)

    async def no_sleep(delay):
        del delay

    monkeypatch.setattr(archive_google.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(archive_google.asyncio, "sleep", no_sleep)

    result = asyncio.run(store.status())
    assert result["reachable"] is True
    assert result["bridge_version"] == 3
    assert calls == [store.bridge_url, store.bridge_url]


def test_post_honors_structured_bridge_retry_hint(monkeypatch):
    store = _store()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
        async def post(self, url, **kwargs):
            del kwargs
            return httpx.Response(
                200,
                request=httpx.Request("POST", str(url)),
                json={"ok": False, "error": "promotion_post_rename_retry", "retryable": True},
            )

    monkeypatch.setattr(archive_google.httpx, "AsyncClient", FakeClient)

    with pytest.raises(ArchiveStorageError) as exc:
        asyncio.run(store._post("promote_verified"))
    assert exc.value.retryable is True


def test_post_does_not_retry_non_transient_http_error(monkeypatch):
    store = _store()
    calls = 0

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
        async def post(self, url, **kwargs):
            nonlocal calls
            del kwargs
            calls += 1
            return httpx.Response(
                401,
                request=httpx.Request("POST", str(url)),
                text="Unauthorized",
            )

    monkeypatch.setattr(archive_google.httpx, "AsyncClient", FakeClient)

    with pytest.raises(ArchiveStorageError, match="HTTP 401"):
        asyncio.run(store.status())
    assert calls == 1


def test_large_download_named_uses_bounded_ranges_and_verifies_sha256(monkeypatch):
    store = _store()
    payload = b"A" * (archive_google._SMALL_READ_MAX_BYTES + 12345)
    sha = hashlib.sha256(payload).hexdigest()
    calls: list[tuple[str, dict]] = []

    async def fake_post(action, **body):
        calls.append((action, dict(body)))
        if action == "stat":
            return {
                "ok": True,
                "found": True,
                "file": {
                    "id": "large-id",
                    "name": "annual.csv",
                    "mime_type": "text/csv",
                    "size": len(payload),
                },
            }
        if action == "metadata_by_id":
            return {
                "ok": True,
                "found": True,
                "file": {
                    "id": "large-id",
                    "name": "annual.csv",
                    "mimeType": "text/csv",
                    "size": str(len(payload)),
                    "sha256Checksum": sha,
                },
            }
        assert action == "read_range_by_id"
        offset = int(body["offset"])
        length = int(body["length"])
        chunk = payload[offset:offset + length]
        return {
            "ok": True,
            "file_id": "large-id",
            "offset": offset,
            "next_offset": offset + len(chunk),
            "total_bytes": len(payload),
            "eof": offset + len(chunk) == len(payload),
            "sha256": sha,
            "content_base64": base64.b64encode(chunk).decode("ascii"),
        }

    store._post = fake_post  # type: ignore[method-assign]
    item, data = asyncio.run(store.download_named("База данных/WB/test", "annual.csv"))

    assert item is not None and item.id == "large-id"
    assert data == payload
    actions = [action for action, _ in calls]
    assert actions[:2] == ["stat", "metadata_by_id"]
    assert actions.count("read_range_by_id") == 2
    range_calls = [body for action, body in calls if action == "read_range_by_id"]
    assert range_calls[0]["length"] == archive_google._LARGE_READ_CHUNK_BYTES
    assert range_calls[1]["offset"] == archive_google._LARGE_READ_CHUNK_BYTES


def test_large_download_fails_closed_if_source_checksum_changes():
    store = _store()
    payload = b"B" * (archive_google._SMALL_READ_MAX_BYTES + 1)
    sha = hashlib.sha256(payload).hexdigest()

    async def fake_post(action, **body):
        if action == "stat":
            return {
                "ok": True,
                "found": True,
                "file": {
                    "id": "large-id",
                    "name": "annual.csv",
                    "mime_type": "text/csv",
                    "size": len(payload),
                },
            }
        if action == "metadata_by_id":
            return {
                "ok": True,
                "file": {
                    "id": "large-id",
                    "name": "annual.csv",
                    "mimeType": "text/csv",
                    "size": str(len(payload)),
                    "sha256Checksum": sha,
                },
            }
        assert action == "read_range_by_id"
        offset = int(body["offset"])
        length = int(body["length"])
        chunk = payload[offset:offset + length]
        return {
            "ok": True,
            "file_id": "large-id",
            "offset": offset,
            "next_offset": offset + len(chunk),
            "total_bytes": len(payload),
            "sha256": "0" * 64,
            "content_base64": base64.b64encode(chunk).decode("ascii"),
        }

    store._post = fake_post  # type: ignore[method-assign]
    with pytest.raises(ArchiveStorageError) as exc:
        asyncio.run(store.download_named("База данных/WB/test", "annual.csv"))
    assert exc.value.code == "DOWNLOAD_SOURCE_CHANGED"
    assert exc.value.retryable is True


def test_large_download_requires_provider_sha256():
    store = _store()
    size = archive_google._SMALL_READ_MAX_BYTES + 1

    async def fake_post(action, **body):
        del body
        if action == "stat":
            return {
                "ok": True,
                "found": True,
                "file": {
                    "id": "large-id",
                    "name": "annual.csv",
                    "mime_type": "text/csv",
                    "size": size,
                },
            }
        if action == "metadata_by_id":
            return {
                "ok": True,
                "file": {
                    "id": "large-id",
                    "name": "annual.csv",
                    "mimeType": "text/csv",
                    "size": str(size),
                },
            }
        raise AssertionError(action)

    store._post = fake_post  # type: ignore[method-assign]
    with pytest.raises(ArchiveStorageError) as exc:
        asyncio.run(store.download_named("База данных/WB/test", "annual.csv"))
    assert exc.value.code == "SHA256_UNAVAILABLE"
