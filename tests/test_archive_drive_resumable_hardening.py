from __future__ import annotations

import asyncio

import httpx
import pytest

from core.archive_drive_resumable import (
    CHUNK_GRANULARITY,
    MAX_CHUNK_SIZE,
    GoogleDriveResumableUploader,
    ResumableUploadError,
)
from core.archive_google import ArchiveStorageNotConfigured


class Broker:
    async def start_resumable_session(self, **kwargs):
        del kwargs
        return {
            "session_uri": "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=x",
            "file_id": None,
        }


def response(status: int, *, headers=None, json_body=None) -> httpx.Response:
    request = httpx.Request("PUT", "https://www.googleapis.com/upload/drive/v3/files?upload_id=x")
    if json_body is None:
        return httpx.Response(status, headers=headers or {}, request=request)
    return httpx.Response(status, headers=headers or {}, json=json_body, request=request)


def uploader() -> GoogleDriveResumableUploader:
    return GoogleDriveResumableUploader(session_broker=Broker(), chunk_size=CHUNK_GRANULARITY)


def test_chunk_size_has_safe_upper_bound():
    with pytest.raises(ArchiveStorageNotConfigured):
        GoogleDriveResumableUploader(
            session_broker=Broker(),
            chunk_size=MAX_CHUNK_SIZE + CHUNK_GRANULARITY,
        )


def test_session_uri_rejects_non_google_port_and_userinfo():
    with pytest.raises(ResumableUploadError):
        GoogleDriveResumableUploader._validate_session_uri(
            "https://user@www.googleapis.com/upload/drive/v3/files?upload_id=x"
        )
    with pytest.raises(ResumableUploadError):
        GoogleDriveResumableUploader._validate_session_uri(
            "https://www.googleapis.com:444/upload/drive/v3/files?upload_id=x"
        )


def test_308_without_range_means_zero_confirmed_bytes():
    assert GoogleDriveResumableUploader._confirmed_offset(response(308)) == 0


def test_malformed_range_is_retryable_protocol_error():
    with pytest.raises(ResumableUploadError) as caught:
        GoogleDriveResumableUploader._confirmed_offset(
            response(308, headers={"Range": "bytes=10-20"})
        )
    assert caught.value.retryable is True


def test_non_rate_limit_403_restarts_session(monkeypatch):
    client = uploader()

    async def fake_request(*args, **kwargs):
        del args, kwargs
        return response(403, json_body={"error": {"errors": [{"reason": "forbidden"}]}})

    monkeypatch.setattr(client, "_request", fake_request)
    progress = asyncio.run(client.query_status(
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=x",
        2 * CHUNK_GRANULARITY,
    ))
    assert progress.state == "expired"
    assert progress.offset == 0


def test_rate_limit_403_uses_retry_after_instead_of_restarting(monkeypatch):
    client = uploader()

    async def fake_request(*args, **kwargs):
        del args, kwargs
        return response(
            403,
            headers={"Retry-After": "17"},
            json_body={"error": {"errors": [{"reason": "rateLimitExceeded"}]}},
        )

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(ResumableUploadError) as caught:
        asyncio.run(client.query_status(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=x",
            2 * CHUNK_GRANULARITY,
        ))
    assert caught.value.retryable is True
    assert caught.value.retry_after_seconds == 17


def test_generic_400_restarts_resumable_session(monkeypatch):
    client = uploader()

    async def fake_request(*args, **kwargs):
        del args, kwargs
        return response(400)

    monkeypatch.setattr(client, "_request", fake_request)
    progress = asyncio.run(client.query_status(
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=x",
        2 * CHUNK_GRANULARITY,
    ))
    assert progress.state == "expired"
