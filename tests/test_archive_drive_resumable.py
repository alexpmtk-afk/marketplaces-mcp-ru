from __future__ import annotations

import asyncio

import httpx
import pytest

from core import archive_drive_resumable
from core.archive_drive_resumable import (
    CHUNK_GRANULARITY,
    GoogleDriveResumableUploader,
    ResumableUploadError,
)
from core.archive_google import ArchiveStorageNotConfigured


class FakeBroker:
    def __init__(self):
        self.starts = []

    async def start_resumable_session(self, **kwargs):
        self.starts.append(kwargs)
        return {
            "session_uri": (
                "https://www.googleapis.com/upload/drive/v3/files"
                "?uploadType=resumable&upload_id=test-session"
            ),
            "file_id": "drive-file-1",
        }

    async def file_metadata(self, file_id):
        return {
            "id": file_id,
            "size": "123",
            "md5Checksum": "0" * 32,
            "sha256Checksum": "1" * 64,
        }

    async def find_child(self, parent_id, name):
        del parent_id, name
        return None


def _response(status: int, *, headers=None, json_body=None) -> httpx.Response:
    request = httpx.Request("PUT", "https://www.googleapis.com/upload/drive/v3/files?upload_id=x")
    if json_body is None:
        return httpx.Response(status, headers=headers or {}, request=request)
    return httpx.Response(status, headers=headers or {}, json=json_body, request=request)


def test_chunk_size_must_be_multiple_of_256_kib():
    with pytest.raises(ArchiveStorageNotConfigured):
        GoogleDriveResumableUploader(
            session_broker=FakeBroker(),
            chunk_size=CHUNK_GRANULARITY + 1,
        )


def test_session_start_is_brokered_by_apps_script_without_oauth():
    broker = FakeBroker()
    uploader = GoogleDriveResumableUploader(
        session_broker=broker,
        chunk_size=CHUNK_GRANULARITY,
    )
    session = asyncio.run(uploader.start_session(
        parent_id="База данных/WB/shop/2026/finance/weekly/main",
        name=".shop__weekly_main__2026.csv.upload.tmp",
        total_bytes=123,
        mime_type="text/csv",
    ))
    assert session.file_id == "drive-file-1"
    assert session.uri.startswith("https://www.googleapis.com/upload/drive/")
    assert broker.starts[0]["total_bytes"] == 123


def test_session_uri_is_restricted_to_google_drive_upload_endpoint():
    with pytest.raises(ResumableUploadError):
        GoogleDriveResumableUploader._validate_session_uri("https://example.com/upload/drive/session")


def test_direct_session_requests_never_follow_redirects(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            del args
            captured.update(kwargs)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def request(self, method, url, **kwargs):
            del method, url, kwargs
            return _response(308)

    monkeypatch.setattr(archive_drive_resumable.httpx, "AsyncClient", FakeClient)
    uploader = GoogleDriveResumableUploader(session_broker=FakeBroker(), chunk_size=CHUNK_GRANULARITY)
    asyncio.run(uploader.query_status(
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=x",
        CHUNK_GRANULARITY,
    ))
    assert captured["follow_redirects"] is False


def test_range_header_becomes_next_confirmed_offset():
    response = _response(308, headers={"Range": "bytes=0-4194303"})
    assert GoogleDriveResumableUploader._confirmed_offset(response) == 4 * 1024 * 1024


def test_missing_range_on_308_means_zero_confirmed_bytes():
    assert GoogleDriveResumableUploader._confirmed_offset(_response(308)) == 0


def test_upload_chunk_uses_drive_confirmed_range(monkeypatch):
    uploader = GoogleDriveResumableUploader(
        session_broker=FakeBroker(),
        chunk_size=CHUNK_GRANULARITY,
    )

    async def fake_request(method, url, **kwargs):
        assert method == "PUT"
        assert kwargs["headers"]["Content-Range"] == "bytes 0-262143/524288"
        return _response(308, headers={"Range": "bytes=0-262143"})

    monkeypatch.setattr(uploader, "_request", fake_request)
    progress = asyncio.run(uploader.upload_chunk(
        session_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=x",
        offset=0,
        total_bytes=2 * CHUNK_GRANULARITY,
        data=b"x" * CHUNK_GRANULARITY,
    ))
    assert progress.state == "incomplete"
    assert progress.offset == CHUNK_GRANULARITY


def test_documented_404_expires_session(monkeypatch):
    uploader = GoogleDriveResumableUploader(session_broker=FakeBroker(), chunk_size=CHUNK_GRANULARITY)

    async def fake_request(method, url, **kwargs):
        del method, url, kwargs
        return _response(404)

    monkeypatch.setattr(uploader, "_request", fake_request)
    progress = asyncio.run(uploader.query_status(
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=x",
        10 * CHUNK_GRANULARITY,
    ))
    assert progress.state == "expired"
    assert progress.offset == 0


@pytest.mark.parametrize("status", [400, 401, 410])
def test_non_rate_limit_4xx_restarts_resumable_session(monkeypatch, status):
    uploader = GoogleDriveResumableUploader(session_broker=FakeBroker(), chunk_size=CHUNK_GRANULARITY)

    async def fake_request(method, url, **kwargs):
        del method, url, kwargs
        return _response(status)

    monkeypatch.setattr(uploader, "_request", fake_request)
    progress = asyncio.run(uploader.query_status(
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=x",
        10 * CHUNK_GRANULARITY,
    ))
    assert progress.state == "expired"
    assert progress.offset == 0


def test_plain_permission_403_restarts_resumable_session(monkeypatch):
    uploader = GoogleDriveResumableUploader(session_broker=FakeBroker(), chunk_size=CHUNK_GRANULARITY)

    async def fake_request(method, url, **kwargs):
        del method, url, kwargs
        return _response(403, json_body={"error": {"errors": [{"reason": "forbidden"}]}})

    monkeypatch.setattr(uploader, "_request", fake_request)
    progress = asyncio.run(uploader.query_status(
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=x",
        10 * CHUNK_GRANULARITY,
    ))
    assert progress.state == "expired"
    assert progress.offset == 0


def test_rate_limit_403_is_retryable(monkeypatch):
    uploader = GoogleDriveResumableUploader(session_broker=FakeBroker(), chunk_size=CHUNK_GRANULARITY)

    async def fake_request(method, url, **kwargs):
        del method, url, kwargs
        return _response(403, json_body={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})

    monkeypatch.setattr(uploader, "_request", fake_request)
    with pytest.raises(ResumableUploadError) as exc:
        asyncio.run(uploader.query_status(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=x",
            10 * CHUNK_GRANULARITY,
        ))
    assert exc.value.retryable is True


def test_retry_after_header_is_preserved_on_throttle(monkeypatch):
    uploader = GoogleDriveResumableUploader(session_broker=FakeBroker(), chunk_size=CHUNK_GRANULARITY)

    async def fake_request(method, url, **kwargs):
        del method, url, kwargs
        return _response(429, headers={"Retry-After": "17"})

    monkeypatch.setattr(uploader, "_request", fake_request)
    with pytest.raises(ResumableUploadError) as exc:
        asyncio.run(uploader.query_status(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=x",
            10 * CHUNK_GRANULARITY,
        ))
    assert exc.value.retryable is True
    assert exc.value.retry_after_seconds == 17


def test_transient_5xx_status_is_retryable_without_exposing_session(monkeypatch):
    uploader = GoogleDriveResumableUploader(
        session_broker=FakeBroker(),
        chunk_size=CHUNK_GRANULARITY,
    )

    async def fake_request(method, url, **kwargs):
        del method, url, kwargs
        return _response(503)

    monkeypatch.setattr(uploader, "_request", fake_request)
    secret_uri = "https://www.googleapis.com/upload/drive/v3/files?upload_id=TOP_SECRET"
    with pytest.raises(ResumableUploadError) as exc:
        asyncio.run(uploader.query_status(secret_uri, 10 * CHUNK_GRANULARITY))
    assert exc.value.retryable is True
    assert "TOP_SECRET" not in str(exc.value)


def test_nonfinal_chunk_must_be_multiple_of_256_kib():
    uploader = GoogleDriveResumableUploader(
        session_broker=FakeBroker(),
        chunk_size=CHUNK_GRANULARITY,
    )
    with pytest.raises(ResumableUploadError):
        asyncio.run(uploader.upload_chunk(
            session_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=x",
            offset=0,
            total_bytes=2 * CHUNK_GRANULARITY,
            data=b"x" * (CHUNK_GRANULARITY - 1),
        ))


def test_final_chunk_may_be_smaller_than_256_kib(monkeypatch):
    uploader = GoogleDriveResumableUploader(
        session_broker=FakeBroker(),
        chunk_size=CHUNK_GRANULARITY,
    )

    async def fake_request(method, url, **kwargs):
        del method, url, kwargs
        return _response(200, json_body={
            "id": "drive-file-1",
            "size": "3",
            "sha256Checksum": "1" * 64,
        })

    monkeypatch.setattr(uploader, "_request", fake_request)
    progress = asyncio.run(uploader.upload_chunk(
        session_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=x",
        offset=0,
        total_bytes=3,
        data=b"xyz",
    ))
    assert progress.state == "complete"
    assert progress.file["id"] == "drive-file-1"


def test_final_chunk_returns_drive_file_metadata(monkeypatch):
    uploader = GoogleDriveResumableUploader(
        session_broker=FakeBroker(),
        chunk_size=CHUNK_GRANULARITY,
    )

    async def fake_request(method, url, **kwargs):
        del method, url, kwargs
        return _response(200, json_body={
            "id": "drive-file-1",
            "size": str(CHUNK_GRANULARITY),
            "sha256Checksum": "1" * 64,
        })

    monkeypatch.setattr(uploader, "_request", fake_request)
    progress = asyncio.run(uploader.upload_chunk(
        session_uri="https://www.googleapis.com/upload/drive/v3/files?upload_id=x",
        offset=0,
        total_bytes=CHUNK_GRANULARITY,
        data=b"x" * CHUNK_GRANULARITY,
    ))
    assert progress.state == "complete"
    assert progress.offset == CHUNK_GRANULARITY
    assert progress.file["id"] == "drive-file-1"
