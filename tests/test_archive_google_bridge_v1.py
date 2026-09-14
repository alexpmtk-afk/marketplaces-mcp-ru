from __future__ import annotations

import asyncio
import hashlib
import uuid

import httpx
import pytest

import core.archive_google as archive_google
from core.archive_google import (
    ArchiveStorageError,
    GoogleDriveArchiveStore,
    MARKETPLACES_BRIDGE_PROJECT_ID,
)


V1_URL = "https://script.google.com/macros/s/marketplaces-v1/exec"
ROOT_ID = "marketplaces-root"


def _v1_store() -> GoogleDriveArchiveStore:
    return GoogleDriveArchiveStore(
        bridge_url=V1_URL,
        bridge_secret="marketplaces-secret",
        root_folder_id=ROOT_ID,
        protocol_version=1,
        project_id=MARKETPLACES_BRIDGE_PROJECT_ID,
    )


def _response_for(body: dict, result: dict | None = None, *, ok: bool = True, error: dict | None = None):
    payload = {
        "ok": ok,
        "protocol_version": 1,
        "bridge_release": "1.0.0",
        "project_id": "marketplaces",
        "request_id": body["request_id"],
        "action": body["action"],
    }
    if ok:
        payload["result"] = result or {}
    else:
        payload["error"] = error or {
            "code": "BRIDGE_REJECTED",
            "message": "rejected",
            "retryable": False,
        }
    return httpx.Response(
        200,
        request=httpx.Request("POST", V1_URL),
        json=payload,
    )


def test_protocol_v1_is_the_only_runtime_env_contract(monkeypatch):
    for name in (
        "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_URL",
        "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_SECRET",
        "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_ROOT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    # Legacy variables must not configure the canonical client.
    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL", "https://script.google.com/macros/s/legacy/exec")
    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET", "legacy-secret")
    monkeypatch.setenv("MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID", "legacy-root")
    with pytest.raises(Exception, match="BRIDGE_V1"):
        GoogleDriveArchiveStore.from_env()

    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_URL", V1_URL)
    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_SECRET", "new-secret")
    monkeypatch.setenv("MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_ROOT_ID", ROOT_ID)
    v1 = GoogleDriveArchiveStore.from_env()
    assert v1.is_protocol_v1 is True
    assert v1.protocol_version == 1
    assert v1.project_id == "marketplaces"
    assert v1.bridge_url == V1_URL

def test_protocol_v1_rejects_non_marketplaces_project_id():
    with pytest.raises(Exception, match="project_id=marketplaces"):
        GoogleDriveArchiveStore(
            bridge_url=V1_URL,
            bridge_secret="secret",
            root_folder_id=ROOT_ID,
            protocol_version=1,
            project_id="birzha",
        )


def test_v1_write_uses_unique_request_ids_and_stable_idempotency(monkeypatch):
    store = _v1_store()
    seen: list[dict] = []
    raw = b"registry"
    sha = hashlib.sha256(raw).hexdigest()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb

        async def post(self, url, *, json, headers):
            del url, headers
            seen.append(dict(json))
            return _response_for(
                json,
                {
                    "sha256": sha,
                    "file": {
                        "id": "file-1",
                        "name": "reports_registry.csv",
                        "mime_type": "text/csv",
                        "size": len(raw),
                        "sha256_checksum": sha,
                    },
                },
            )

    monkeypatch.setattr(archive_google.httpx, "AsyncClient", FakeClient)

    asyncio.run(store.upload_bytes("app/registry", "reports_registry.csv", raw))
    asyncio.run(store.upload_bytes("app/registry", "reports_registry.csv", raw))

    assert len(seen) == 2
    assert all(item["project_id"] == "marketplaces" for item in seen)
    assert all(item["action"] == "write_small" for item in seen)
    assert all(item["payload"]["sha256"] == sha for item in seen)
    assert seen[0]["request_id"] != seen[1]["request_id"]
    uuid.UUID(seen[0]["request_id"])
    uuid.UUID(seen[1]["request_id"])
    assert seen[0]["idempotency_key"] == seen[1]["idempotency_key"]
    assert seen[0]["idempotency_key"].startswith("marketplaces:write_small:")


def test_v1_resumable_start_is_idempotent_and_returns_server_staging_name(monkeypatch):
    store = _v1_store()
    seen: list[dict] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb

        async def post(self, url, *, json, headers):
            del url, headers
            seen.append(dict(json))
            return _response_for(
                json,
                {
                    "session_uri": "https://www.googleapis.com/upload/drive/v3/files/file-1?upload_id=opaque",
                    "file_id": "file-1",
                    "staging_filename": ".bridge-upload-abc-annual.csv",
                },
            )

    monkeypatch.setattr(archive_google.httpx, "AsyncClient", FakeClient)

    one = asyncio.run(
        store.start_resumable_session(
            parent_id="База данных/WB/test/2026/finance/weekly/main",
            name=".annual.csv.upload-report-10-deadbeef.tmp",
            total_bytes=16 * 1024 * 1024,
            mime_type="text/csv",
        )
    )
    two = asyncio.run(
        store.start_resumable_session(
            parent_id="База данных/WB/test/2026/finance/weekly/main",
            name=".annual.csv.upload-report-10-deadbeef.tmp",
            total_bytes=16 * 1024 * 1024,
            mime_type="text/csv",
        )
    )

    assert one["staging_filename"] == ".bridge-upload-abc-annual.csv"
    assert two["file_id"] == "file-1"
    assert seen[0]["idempotency_key"] == seen[1]["idempotency_key"]
    assert seen[0]["request_id"] != seen[1]["request_id"]


def test_v1_deep_health_fails_closed_on_project_root_or_guard_mismatch(monkeypatch):
    store = _v1_store()
    modes = ["project", "root", "guard"]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb

        async def post(self, url, *, json, headers):
            del url, headers
            mode = modes.pop(0)
            result = {
                "protocol_version": 1,
                "bridge_release": "1.0.0",
                "project_id": "marketplaces",
                "root_id": ROOT_ID,
                "root_name": "MCP архив базы данных",
                "capabilities": {
                    "fixed_root_file_id_guard": True,
                    "idempotent_mutations": True,
                    "drive_large_download": False,
                },
            }
            envelope = _response_for(json, result)
            data = envelope.json()
            if mode == "project":
                data["project_id"] = "birzha"
            elif mode == "root":
                data["result"]["root_id"] = "foreign-root"
            else:
                data["result"]["capabilities"]["fixed_root_file_id_guard"] = False
            return httpx.Response(200, request=httpx.Request("POST", V1_URL), json=data)

    monkeypatch.setattr(archive_google.httpx, "AsyncClient", FakeClient)

    with pytest.raises(ArchiveStorageError) as project_exc:
        asyncio.run(store.status())
    assert project_exc.value.code == "PROJECT_MISMATCH"

    with pytest.raises(ArchiveStorageError) as root_exc:
        asyncio.run(store.status())
    assert root_exc.value.code == "ROOT_MISMATCH"

    with pytest.raises(ArchiveStorageError) as guard_exc:
        asyncio.run(store.status())
    assert guard_exc.value.code == "ROOT_GUARD_REQUIRED"


def test_v1_large_read_fails_closed_until_shared_transport_is_accepted(monkeypatch):
    store = _v1_store()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb

        async def post(self, url, *, json, headers):
            del url, headers
            if json["action"] == "read_small":
                return _response_for(
                    json,
                    ok=False,
                    error={
                        "code": "LARGE_READ_REQUIRED",
                        "message": "file exceeds bounded Apps Script read limit",
                        "retryable": False,
                    },
                )
            if json["action"] == "stat":
                return _response_for(
                    json,
                    {
                        "found": True,
                        "file": {
                            "id": "large-file",
                            "name": "annual.csv",
                            "mime_type": "text/csv",
                            "size": 20 * 1024 * 1024,
                        },
                    },
                )
            if json["action"] == "large_download_start":
                return _response_for(
                    json,
                    ok=False,
                    error={
                        "code": "NOT_IMPLEMENTED",
                        "message": "large direct download capability is not implemented in alpha.1",
                        "retryable": False,
                    },
                )
            raise AssertionError(json["action"])

    monkeypatch.setattr(archive_google.httpx, "AsyncClient", FakeClient)

    with pytest.raises(ArchiveStorageError) as exc:
        asyncio.run(store.download_named("База данных/WB/test/2026", "annual.csv"))
    assert exc.value.code == "NOT_IMPLEMENTED"


def test_v1_health_fails_closed_when_required_large_download_is_missing(monkeypatch):
    store = _v1_store()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb

        async def post(self, url, *, json, headers):
            del url, headers
            return _response_for(
                json,
                {
                    "protocol_version": 1,
                    "bridge_release": "1.0.0",
                    "project_id": "marketplaces",
                    "root_id": ROOT_ID,
                    "root_name": "MCP архив базы данных",
                    "capabilities": {
                        "drive_small_io": True,
                        "drive_resumable_upload": True,
                        "drive_large_download": False,
                        "fixed_root_file_id_guard": True,
                        "idempotent_mutations": True,
                        "global_script_lock": False,
                    },
                },
            )

    monkeypatch.setattr(archive_google.httpx, "AsyncClient", FakeClient)
    with pytest.raises(ArchiveStorageError) as exc:
        asyncio.run(store.status())
    assert exc.value.code == "LARGE_DOWNLOAD_REQUIRED"

