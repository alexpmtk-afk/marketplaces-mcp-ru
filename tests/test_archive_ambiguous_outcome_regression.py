from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

from core import archive_resumable_worker


class DummyLock:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class CandidateObject:
    def __init__(self, file_id: str, data: bytes):
        self.id = file_id
        self.size = len(data)


class FakeYandex:
    def __init__(self, candidate: bytes):
        self.candidate = candidate
        self.backups: dict[str, bytes] = {}

    async def ensure_folder_path(self, parts):
        return "/".join(parts)

    async def find_child(self, parent, name, **kwargs):
        del kwargs
        if name == "annual-candidate.csv":
            return CandidateObject(f"{parent}/{name}", self.candidate)
        key = f"{parent}/{name}"
        data = self.backups.get(key)
        return None if data is None else CandidateObject(key, data)

    async def download_bytes(self, file_id):
        assert file_id.endswith("/annual-candidate.csv")
        return self.candidate

    async def upload_bytes(self, parent, name, data, **kwargs):
        del kwargs
        key = f"{parent}/{name}"
        self.backups[key] = bytes(data)
        return CandidateObject(key, bytes(data))


class FakeDrive:
    async def ensure_folder_path(self, parts):
        return "/".join(parts)


class FakeStore:
    def __init__(self, candidate: bytes):
        self.yandex = FakeYandex(candidate)
        self.drive = FakeDrive()


class FakeQueue:
    def __init__(self, state):
        self.state = state
        self.delegate_calls = 0
        self.scheduled = []

    async def _load(self, job_id):
        return self.state if job_id == self.state["job_id"] else None

    async def _save(self, state):
        self.state = state

    async def _schedule(self, job_id, delay_seconds=0):
        self.scheduled.append((job_id, float(delay_seconds)))

    async def _unschedule(self, job_id):
        del job_id

    async def worker_step(self, job_id):
        self.delegate_calls += 1
        raise AssertionError(f"WB/base queue must not run during ambiguous upload recovery: {job_id}")


class AlreadyPromotedUploader:
    """Drive already contains the exact canonical file, but durable state missed the response."""

    chunk_size = 4 * 1024 * 1024

    def __init__(self, candidate: bytes):
        self.candidate = candidate
        self.file_id = "drive-canonical-after-lost-response"
        self.start_calls = 0
        self.chunk_calls = 0
        self.status_calls = 0

    async def find_named_file(self, parent_id, name):
        del parent_id
        assert name == "wb_laser_master__weekly_main__2026.csv"
        return {
            "id": self.file_id,
            "name": name,
            "size": len(self.candidate),
            "sha256Checksum": hashlib.sha256(self.candidate).hexdigest(),
        }

    async def file_metadata(self, file_id):
        assert file_id == self.file_id
        return {
            "id": file_id,
            "name": "wb_laser_master__weekly_main__2026.csv",
            "size": str(len(self.candidate)),
            "sha256Checksum": hashlib.sha256(self.candidate).hexdigest(),
        }

    async def start_session(self, **kwargs):
        self.start_calls += 1
        raise AssertionError(f"recovery must not start another Drive upload: {kwargs}")

    async def upload_chunk(self, **kwargs):
        self.chunk_calls += 1
        raise AssertionError(f"recovery must not upload another chunk: {kwargs}")

    async def query_status(self, *args, **kwargs):
        self.status_calls += 1
        raise AssertionError(f"recovery must not need a resumable session probe: {args} {kwargs}")


def test_lost_upload_response_reconciles_existing_canonical_without_reupload_or_wb(monkeypatch):
    """Regression for Laser report 743994450 (2026-09-14 ambiguous outcome).

    The remote Drive side effect succeeded, but the caller lost the response and
    durable state therefore still says UPLOAD_ANNUAL with no resumable session.
    The next worker must reconcile the exact canonical file before attempting any
    new upload or delegating back to provider/WB work.
    """

    monkeypatch.setattr(archive_resumable_worker, "ArchiveLock", DummyLock)

    candidate = b"laser-report-24-annual-candidate"
    state = {
        "job_id": "wb-finance-wb_laser_master-2026",
        "status": "QUEUED",
        "phase": "DOWNLOAD",
        "cabinet": "wb_laser_master",
        "year": 2026,
        "provider_calls": 49,
        "completed_count": 23,
        "finalize": {
            "report_id": 743994450,
            "phase": "UPLOAD_ANNUAL",
            "annual_bytes": len(candidate),
            "annual_sha256": hashlib.sha256(candidate).hexdigest(),
        },
    }
    queue = FakeQueue(state)
    store = FakeStore(candidate)
    uploader = AlreadyPromotedUploader(candidate)
    worker = archive_resumable_worker.WBFinanceResumableWorker(queue, store, uploader)

    provider_calls_before = queue.state["provider_calls"]
    result = asyncio.run(worker.worker_step(state["job_id"]))

    assert result["ok"] is True
    assert result["action"] == "report_annual_already_promoted"
    assert result["report_id"] == 743994450
    assert result["drive_verified"] is True
    assert result["backup_verified"] is True
    assert queue.state["finalize"]["phase"] == "COMMIT"
    assert queue.state["finalize"]["annual_object_id"] == uploader.file_id
    assert queue.state["completed_count"] == 23
    assert queue.state["provider_calls"] == provider_calls_before == 49
    assert queue.delegate_calls == 0
    assert uploader.start_calls == 0
    assert uploader.chunk_calls == 0
    assert uploader.status_calls == 0

    backup_key = (
        "База данных/WB/wb_laser_master/2026/finance/weekly/main/"
        "wb_laser_master__weekly_main__2026.csv"
    )
    assert store.yandex.backups[backup_key] == candidate
