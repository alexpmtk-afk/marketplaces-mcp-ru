from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

from core import archive_resumable_worker
from core.archive_drive_resumable import UploadProgress, UploadSession


class DummyLock:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class FakeObject:
    def __init__(self, key: str, data: bytes):
        self.id = key
        self.name = key.rsplit("/", 1)[-1]
        self.size = len(data)
        self.etag = hashlib.md5(data, usedforsecurity=False).hexdigest()
        self.mime_type = "text/csv"


class FakeYandex:
    def __init__(self, candidate: bytes):
        self.candidate = candidate
        self.backups: dict[str, bytes] = {}
        self.range_reads: list[tuple[int, int]] = []
        self.full_reads = 0

    async def ensure_folder_path(self, parts):
        return "/".join(parts)

    async def find_child(self, parent, name, **kwargs):
        del kwargs
        if name == "annual-candidate.csv":
            return FakeObject(f"{parent}/{name}", self.candidate)
        key = f"{parent}/{name}"
        data = self.backups.get(key)
        return None if data is None else FakeObject(key, data)

    async def download_range(self, file_id, start, end):
        del file_id
        self.range_reads.append((start, end))
        return self.candidate[start:end]

    async def download_bytes(self, file_id):
        del file_id
        self.full_reads += 1
        return self.candidate

    async def upload_bytes(self, parent, name, data, **kwargs):
        del kwargs
        key = f"{parent}/{name}"
        self.backups[key] = bytes(data)
        return FakeObject(key, bytes(data))


class FakeDrive:
    async def ensure_folder_path(self, parts):
        return "drive:" + "/".join(parts)


class FakeStore:
    def __init__(self, candidate: bytes):
        self.yandex = FakeYandex(candidate)
        self.drive = FakeDrive()


class FakeQueue:
    def __init__(self, state):
        self.state = state
        self.saved = []
        self.scheduled = {}
        self.unscheduled = []
        self.delegate_calls = []

    async def _next_due(self):
        return self.state["job_id"], 0

    async def _load(self, job_id):
        return self.state if job_id == self.state["job_id"] else None

    async def _save(self, state):
        self.state = state
        self.saved.append(state.copy())

    async def _schedule(self, job_id, delay_seconds=0):
        self.scheduled[job_id] = float(delay_seconds)

    async def _unschedule(self, job_id):
        self.unscheduled.append(job_id)
        self.scheduled.pop(job_id, None)

    async def worker_step(self, job_id):
        self.delegate_calls.append(job_id)
        return {"ok": True, "action": "delegated", "job_id": job_id}


class FakeUploader:
    chunk_size = 4

    def __init__(self):
        self.session_uri = "https://upload.example/session-secret"
        self.started = 0
        self.uploads = []
        self.status_offset = 0
        self.file_id = "drive-file-1"
        self.total = 0
        self.complete = False

    async def start_session(self, *, parent_id, name, total_bytes, mime_type):
        del parent_id, name, mime_type
        self.started += 1
        self.total = total_bytes
        return UploadSession(uri=self.session_uri, file_id=self.file_id, offset=0)

    async def query_status(self, session_uri, total_bytes):
        assert session_uri == self.session_uri
        assert total_bytes == self.total
        if self.complete:
            return UploadProgress(
                "complete",
                total_bytes,
                {"id": self.file_id, "size": str(total_bytes), "md5Checksum": self.md5},
            )
        return UploadProgress("incomplete", self.status_offset, None)

    async def upload_chunk(self, *, session_uri, offset, total_bytes, data):
        assert session_uri == self.session_uri
        assert total_bytes == self.total
        self.uploads.append((offset, bytes(data)))
        self.status_offset = offset + len(data)
        if self.status_offset >= total_bytes:
            self.complete = True
            return UploadProgress(
                "complete",
                total_bytes,
                {"id": self.file_id, "size": str(total_bytes), "md5Checksum": self.md5},
            )
        return UploadProgress("incomplete", self.status_offset, None)

    async def file_metadata(self, file_id):
        assert file_id == self.file_id
        return {"id": file_id, "size": str(self.total), "md5Checksum": self.md5}

    async def find_named_file(self, parent_id, name):
        del parent_id, name
        return {"id": self.file_id}

    @property
    def md5(self):
        return self._md5

    @md5.setter
    def md5(self, value):
        self._md5 = value


def _state(candidate: bytes):
    return {
        "job_id": "wb-finance-wb_laser_master-2026",
        "status": "QUEUED",
        "phase": "DOWNLOAD",
        "cabinet": "wb_laser_master",
        "year": 2026,
        "provider_calls": 123,
        "finalize": {
            "report_id": 743994450,
            "phase": "UPLOAD_ANNUAL",
            "annual_bytes": len(candidate),
            "annual_sha256": hashlib.sha256(candidate).hexdigest(),
        },
    }


def test_existing_laser_upload_phase_starts_session_without_provider_or_prepare(monkeypatch):
    monkeypatch.setattr(archive_resumable_worker, "ArchiveLock", DummyLock)
    candidate = b"abcdefghijkl"
    state = _state(candidate)
    queue = FakeQueue(state)
    store = FakeStore(candidate)
    uploader = FakeUploader()
    uploader.md5 = hashlib.md5(candidate, usedforsecurity=False).hexdigest()
    worker = archive_resumable_worker.WBFinanceResumableWorker(queue, store, uploader)

    result = asyncio.run(worker.worker_step(state["job_id"]))

    assert result["action"] == "drive_resumable_session_started"
    assert uploader.started == 1
    assert uploader.uploads == []
    assert queue.delegate_calls == []
    assert queue.state["finalize"]["phase"] == "UPLOAD_ANNUAL"
    assert queue.state["finalize"]["report_id"] == 743994450
    assert queue.state["finalize"]["resumable_upload"]["offset"] == 0


def test_resumable_worker_uploads_one_chunk_per_step_and_persists_offset(monkeypatch):
    monkeypatch.setattr(archive_resumable_worker, "ArchiveLock", DummyLock)
    candidate = b"abcdefghijkl"
    state = _state(candidate)
    queue = FakeQueue(state)
    store = FakeStore(candidate)
    uploader = FakeUploader()
    uploader.md5 = hashlib.md5(candidate, usedforsecurity=False).hexdigest()
    worker = archive_resumable_worker.WBFinanceResumableWorker(queue, store, uploader)

    first = asyncio.run(worker.worker_step(state["job_id"]))
    assert first["action"] == "drive_resumable_session_started"

    second = asyncio.run(worker.worker_step(state["job_id"]))
    assert second["action"] == "drive_resumable_chunk_uploaded"
    assert second["confirmed_bytes"] == 4
    assert uploader.uploads == [(0, b"abcd")]
    assert queue.state["finalize"]["resumable_upload"]["offset"] == 4
    assert store.yandex.range_reads == [(0, 4)]

    third = asyncio.run(worker.worker_step(state["job_id"]))
    assert third["action"] == "drive_resumable_chunk_uploaded"
    assert third["confirmed_bytes"] == 8
    assert uploader.uploads[-1] == (4, b"efgh")
    assert queue.state["finalize"]["resumable_upload"]["offset"] == 8


def test_resumable_worker_verifies_final_file_and_advances_only_to_commit(monkeypatch):
    monkeypatch.setattr(archive_resumable_worker, "ArchiveLock", DummyLock)
    candidate = b"abcdefghijkl"
    state = _state(candidate)
    queue = FakeQueue(state)
    store = FakeStore(candidate)
    uploader = FakeUploader()
    uploader.md5 = hashlib.md5(candidate, usedforsecurity=False).hexdigest()
    worker = archive_resumable_worker.WBFinanceResumableWorker(queue, store, uploader)

    asyncio.run(worker.worker_step(state["job_id"]))
    asyncio.run(worker.worker_step(state["job_id"]))
    asyncio.run(worker.worker_step(state["job_id"]))
    final = asyncio.run(worker.worker_step(state["job_id"]))

    assert final["action"] == "report_annual_uploaded_resumable"
    assert final["drive_verified"] is True
    assert final["backup_verified"] is True
    assert queue.state["finalize"]["phase"] == "COMMIT"
    assert queue.state["finalize"]["annual_object_id"] == uploader.file_id
    assert "resumable_upload" not in queue.state["finalize"]
    assert store.yandex.full_reads == 1
    backup_key = "База данных/WB/wb_laser_master/2026/finance/weekly/main/wb_laser_master__weekly_main__2026.csv"
    assert store.yandex.backups[backup_key] == candidate
    # COMMIT is deliberately left to the existing queue on the next step.
    assert queue.delegate_calls == []


def test_missing_oauth_fails_closed_and_preserves_candidate(monkeypatch):
    monkeypatch.setattr(archive_resumable_worker, "ArchiveLock", DummyLock)
    candidate = b"laser-candidate"
    state = _state(candidate)
    queue = FakeQueue(state)
    store = FakeStore(candidate)
    worker = archive_resumable_worker.WBFinanceResumableWorker(queue, store, None)
    worker.uploader = None

    result = asyncio.run(worker.worker_step(state["job_id"]))

    assert result["action"] == "drive_resumable_oauth_required"
    assert result["candidate_preserved"] is True
    assert queue.state["status"] == "WAITING_CONFIGURATION"
    assert queue.state["finalize"]["phase"] == "UPLOAD_ANNUAL"
    assert queue.state["finalize"]["report_id"] == 743994450
    assert store.yandex.candidate == candidate
    assert queue.delegate_calls == []
    assert state["job_id"] in queue.unscheduled


def test_non_upload_phase_delegates_to_existing_queue(monkeypatch):
    monkeypatch.setattr(archive_resumable_worker, "ArchiveLock", DummyLock)
    candidate = b"x"
    state = _state(candidate)
    state.pop("finalize")
    state["phase"] = "DOWNLOAD"
    queue = FakeQueue(state)
    worker = archive_resumable_worker.WBFinanceResumableWorker(queue, FakeStore(candidate), None)
    worker.uploader = None

    result = asyncio.run(worker.worker_step(state["job_id"]))

    assert result["action"] == "delegated"
    assert queue.delegate_calls == [state["job_id"]]
