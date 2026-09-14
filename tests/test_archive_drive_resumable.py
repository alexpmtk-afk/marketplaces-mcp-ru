from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from core.archive_drive_resumable import (
    CHUNK_GRANULARITY,
    GoogleDriveResumableUploader,
)
from core.archive_google import ArchiveStorageNotConfigured


def _oauth_json() -> str:
    return json.dumps({
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
    })


def _response(status: int, *, headers=None, json_body=None) -> httpx.Response:
    request = httpx.Request("PUT", "https://upload.example/session")
    if json_body is None:
        return httpx.Response(status, headers=headers or {}, request=request)
    return httpx.Response(status, headers=headers or {}, json=json_body, request=request)


def test_chunk_size_must_be_multiple_of_256_kib():
    with pytest.raises(ArchiveStorageNotConfigured):
        GoogleDriveResumableUploader(
            oauth_json=_oauth_json(),
            chunk_size=CHUNK_GRANULARITY + 1,
        )


def test_range_header_becomes_next_confirmed_offset():
    response = _response(308, headers={"Range": "bytes=0-4194303"})
    assert GoogleDriveResumableUploader._confirmed_offset(response) == 4 * 1024 * 1024


def test_upload_chunk_uses_drive_confirmed_range(monkeypatch):
    uploader = GoogleDriveResumableUploader(
        oauth_json=_oauth_json(),
        chunk_size=CHUNK_GRANULARITY,
    )

    async def fake_request(method, url, **kwargs):
        assert method == "PUT"
        assert url == "https://upload.example/session"
        assert kwargs["headers"]["Content-Range"] == "bytes 0-262143/524288"
        return _response(308, headers={"Range": "bytes=0-262143"})

    monkeypatch.setattr(uploader, "_request", fake_request)
    progress = asyncio.run(uploader.upload_chunk(
        session_uri="https://upload.example/session",
        offset=0,
        total_bytes=2 * CHUNK_GRANULARITY,
        data=b"x" * CHUNK_GRANULARITY,
    ))
    assert progress.state == "incomplete"
    assert progress.offset == CHUNK_GRANULARITY


def test_status_404_marks_session_expired(monkeypatch):
    uploader = GoogleDriveResumableUploader(
        oauth_json=_oauth_json(),
        chunk_size=CHUNK_GRANULARITY,
    )

    async def fake_request(method, url, **kwargs):
        del method, url, kwargs
        return _response(404)

    monkeypatch.setattr(uploader, "_request", fake_request)
    progress = asyncio.run(uploader.query_status(
        "https://upload.example/session",
        10 * CHUNK_GRANULARITY,
    ))
    assert progress.state == "expired"
    assert progress.offset == 0


def test_final_chunk_returns_drive_file_metadata(monkeypatch):
    uploader = GoogleDriveResumableUploader(
        oauth_json=_oauth_json(),
        chunk_size=CHUNK_GRANULARITY,
    )

    async def fake_request(method, url, **kwargs):
        del method, url, kwargs
        return _response(200, json_body={
            "id": "drive-file-1",
            "size": str(CHUNK_GRANULARITY),
            "md5Checksum": "0" * 32,
        })

    monkeypatch.setattr(uploader, "_request", fake_request)
    progress = asyncio.run(uploader.upload_chunk(
        session_uri="https://upload.example/session",
        offset=0,
        total_bytes=CHUNK_GRANULARITY,
        data=b"x" * CHUNK_GRANULARITY,
    ))
    assert progress.state == "complete"
    assert progress.offset == CHUNK_GRANULARITY
    assert progress.file["id"] == "drive-file-1"
