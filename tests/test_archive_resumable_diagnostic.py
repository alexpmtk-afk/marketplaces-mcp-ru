from __future__ import annotations

import asyncio
import hashlib

from core import archive_resumable_diagnostic
from core.archive_drive_resumable import UploadProgress, UploadSession


class DummyLock:
    def __init__(self, *args, **kwargs):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None


class Obj:
    def __init__(self, key: str, data: bytes):
        self.id = key
        self.size = len(data)


class FakeYandex:
    def __init__(self, candidate: bytes):
        self.candidate = candidate
    async def ensure_folder_path(self, parts):
        return "/".join(parts)
    async def find_child(self, parent, name):
        if name == "annual-candidate.csv":
            return Obj(f"{parent}/{name}", self.candidate)
        return None
    async def download_range(self, file_id, start, end):
        del file_id
        return self.candidate[start:end]


class FakeDrive:
    def __init__(self):
        self.trashed = []
    async def ensure_folder_path(self, parts):
        return "/".join(parts)
    async def trash_file(self, file_id):
        self.trashed.append(file_id)


class FakeStore:
    def __init__(self, candidate: bytes):
        self.yandex = FakeYandex(candidate)
        self.drive = FakeDrive()


class FakeQueue:
    def __init__(self, state):
        self.state = state
        self.saves = 0
    async def _load(self, job_id):
        return self.state if job_id == self.state["job_id"] else None
    async def _save(self, state):
        self.state = state
        self.saves += 1


class FakeUploader:
    chunk_size = 4
    def __init__(self, candidate: bytes):
        self.candidate = candidate
        self.offset = 0
        self.file_id = "diagnostic-drive-file"
        self.uri = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=diag"
        self.sha256 = hashlib.sha256(candidate).hexdigest()
    async def start_session(self, **kwargs):
        assert ".diagnostic-report-743994450.csv" in kwargs["name"]
        return UploadSession(self.uri, self.file_id, 0)
    async def query_status(self, session_uri, total_bytes):
        del session_uri
        if self.offset >= total_bytes:
            return UploadProgress("complete", total_bytes, {"id": self.file_id})
        return UploadProgress("incomplete", self.offset, None)
    async def upload_chunk(self, *, session_uri, offset, total_bytes, data):
        del session_uri
        self.offset = offset + len(data)
        if self.offset >= total_bytes:
            return UploadProgress("complete", total_bytes, {"id": self.file_id})
        return UploadProgress("incomplete", self.offset, None)
    async def file_metadata(self, file_id):
        assert file_id == self.file_id
        return {
            "id": file_id,
            "size": str(len(self.candidate)),
            "sha256Checksum": self.sha256,
        }


def _state(candidate: bytes):
    return {
        "job_id": "wb-finance-wb_laser_master-2026",
        "status": "QUEUED",
        "phase": "DOWNLOAD",
        "cabinet": "wb_laser_master",
        "year": 2026,
        "completed_count": 23,
        "finalize": {
            "report_id": 743994450,
            "phase": "UPLOAD_ANNUAL",
            "annual_bytes": len(candidate),
            "annual_sha256": hashlib.sha256(candidate).hexdigest(),
        },
    }


def test_laser_diagnostic_copy_verifies_sha256_trashes_temp_and_preserves_23_of_38(monkeypatch):
    monkeypatch.setattr(archive_resumable_diagnostic, "ArchiveLock", DummyLock)
    candidate = b"abcdefghijkl"
    state = _state(candidate)
    queue = FakeQueue(state)
    store = FakeStore(candidate)
    uploader = FakeUploader(candidate)
    diagnostic = archive_resumable_diagnostic.WBFinanceResumableDiagnostic(queue, store, uploader)

    results = [asyncio.run(diagnostic.step(state["job_id"])) for _ in range(4)]

    assert results[-1]["action"] == "diagnostic_pass"
    assert results[-1]["verified_sha256"] == hashlib.sha256(candidate).hexdigest()
    assert results[-1]["trashed"] is True
    assert store.drive.trashed == ["diagnostic-drive-file"]
    assert queue.state["completed_count"] == 23
    assert queue.state["finalize"]["report_id"] == 743994450
    assert queue.state["finalize"]["phase"] == "UPLOAD_ANNUAL"
    assert queue.state["finalize"]["resumable_diagnostic"]["status"] == "PASS"
