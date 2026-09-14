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
    def __init__(self, candidate: bytes, events: list[str]):
        self.candidate = candidate
        self.backups: dict[str, bytes] = {}
        self.range_reads: list[tuple[int, int]] = []
        self.full_reads = 0
        self.events = events

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
        self.events.append("backup")
        key = f"{parent}/{name}"
        self.backups[key] = bytes(data)
        return FakeObject(key, bytes(data))


class FakeDrive:
    def __init__(self, candidate: bytes, events: list[str]):
        self.starts = []
        self.events = events
        self.staged_file_id = "drive-staged-1"
        self.previous_file_id = "drive-old-1"
        self.canonical_file_id = self.previous_file_id
        self.previous_trashed = False
        self.candidate = candidate
        self.promotions = []

    async def ensure_folder_path(self, parts):
        return "/".join(parts)

    async def start_resumable_session(self, **kwargs):
        self.starts.append(kwargs)
        return {
            "session_uri": (
                "https://www.googleapis.com/upload/drive/v3/files"
                "?uploadType=resumable&upload_id=test-session"
            ),
            "file_id": self.staged_file_id,
        }

    async def file_metadata(self, file_id):
        if file_id == self.staged_file_id:
            return {
                "id": file_id,
                "name": "staging.tmp",
                "size": str(len(self.candidate)),
                "sha256Checksum": hashlib.sha256(self.candidate).hexdigest(),
                "md5Checksum": hashlib.md5(self.candidate, usedforsecurity=False).hexdigest(),
            }
        if file_id == self.canonical_file_id and self.canonical_file_id == self.staged_file_id:
            return {
                "id": file_id,
                "name": "wb_laser_master__weekly_main__2026.csv",
                "size": str(len(self.candidate)),
                "sha256Checksum": hashlib.sha256(self.candidate).hexdigest(),
            }
        if file_id == self.previous_file_id:
            return {
                "id": file_id,
                "name": "wb_laser_master__weekly_main__2026.csv",
                "size": "3",
                "sha256Checksum": "f" * 64,
            }
        raise AssertionError(file_id)

    async def find_child(self, parent_id, name, **kwargs):
        del parent_id, kwargs
        if name == "wb_laser_master__weekly_main__2026.csv" and not self.previous_trashed:
            fid = self.canonical_file_id
            meta = await self.file_metadata(fid)
            return SimpleNamespace(
                id=fid,
                name=name,
                size=int(meta["size"]),
                md5_checksum=meta.get("md5Checksum"),
                sha256_checksum=meta.get("sha256Checksum"),
                mime_type="text/csv",
                modified_time=None,
            )
        return None

    async def promote_verified_file(self, **kwargs):
        assert kwargs["file_id"] == self.staged_file_id
        assert kwargs["previous_file_id"] == self.previous_file_id
        assert kwargs["expected_sha256"] == hashlib.sha256(self.candidate).hexdigest()
        self.events.append("promote")
        self.promotions.append(kwargs)
        self.previous_trashed = True
        self.canonical_file_id = self.staged_file_id
        return SimpleNamespace(
            id=self.staged_file_id,
            name=kwargs["canonical_name"],
            size=len(self.candidate),
            sha256_checksum=kwargs["expected_sha256"],
        )


class FakeStore:
    def __init__(self, candidate: bytes):
        self.events: list[str] = []
        self.yandex = FakeYandex(candidate, self.events)
        self.drive = FakeDrive(candidate, self.events)


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

    def __init__(self, drive: FakeDrive, candidate: bytes):
        self.drive = drive
        self.candidate = candidate
        self.session_uri = "https://www.googleapis.com/upload/drive/v3/files?upload_id=test-session"
        self.started = 0
        self.started_names = []
        self.uploads = []
        self.status_offset = 0
        self.file_id = drive.staged_file_id
        self.total = 0
        self.complete = False

    async def start_session(self, *, parent_id, name, total_bytes, mime_type):
        del parent_id, mime_type
        self.started += 1
        self.started_names.append(name)
        self.total = total_bytes
        return UploadSession(uri=self.session_uri, file_id=self.file_id, offset=0)

    async def query_status(self, session_uri, total_bytes):
        assert session_uri == self.session_uri
        assert total_bytes == self.total
        if self.complete:
            return UploadProgress("complete", total_bytes, {"id": self.file_id})
        return UploadProgress("incomplete", self.status_offset, None)

    async def upload_chunk(self, *, session_uri, offset, total_bytes, data):
        assert session_uri == self.session_uri
        assert total_bytes == self.total
        self.uploads.append((offset, bytes(data)))
        self.status_offset = offset + len(data)
        if self.status_offset >= total_bytes:
            self.complete = True
            return UploadProgress("complete", total_bytes, {"id": self.file_id})
        return UploadProgress("incomplete", self.status_offset, None)

    async def file_metadata(self, file_id):
        return await self.drive.file_metadata(file_id)

    async def find_named_file(self, parent_id, name):
        item = await self.drive.find_child(parent_id, name)
        if item is None:
            return None
        return {
            "id": item.id,
            "name": item.name,
            "size": item.size,
            "sha256Checksum": item.sha256_checksum,
            "md5Checksum": item.md5_checksum,
        }


def _state(candidate: bytes):
    return {
        "job_id": "wb-finance-wb_laser_master-2026",
        "status": "QUEUED",
        "phase": "DOWNLOAD",
        "cabinet": "wb_laser_master",
        "year": 2026,
        "provider_calls": 123,
        "completed_count": 23,
        "finalize": {
            "report_id": 743994450,
            "phase": "UPLOAD_ANNUAL",
            "annual_bytes": len(candidate),
            "annual_sha256": hashlib.sha256(candidate).hexdigest(),
        },
    }


def _worker(candidate=b"abcdefghijkl"):
    state = _state(candidate)
    queue = FakeQueue(state)
    store = FakeStore(candidate)
    uploader = FakeUploader(store.drive, candidate)
    return state, queue, store, uploader, archive_resumable_worker.WBFinanceResumableWorker(queue, store, uploader)


def test_existing_laser_starts_noncanonical_staging_session(monkeypatch):
    monkeypatch.setattr(archive_resumable_worker, "ArchiveLock", DummyLock)
    state, queue, store, uploader, worker = _worker()

    result = asyncio.run(worker.worker_step(state["job_id"]))

    assert result["action"] == "drive_resumable_session_started"
    assert result["canonical_untouched"] is True
    assert uploader.started == 1
    assert uploader.started_names[0].startswith(".wb_laser_master__weekly_main__2026.csv.upload-report-743994450-")
    assert uploader.started_names[0].endswith(".tmp")
    assert "session_uri" not in result
    assert queue.delegate_calls == []
    assert queue.state["completed_count"] == 23
    assert queue.state["finalize"]["phase"] == "UPLOAD_ANNUAL"
    assert queue.state["finalize"]["resumable_upload"]["previous_canonical_file_id"] == store.drive.previous_file_id


def test_resumable_worker_uploads_one_chunk_per_step_and_uses_server_offset(monkeypatch):
    monkeypatch.setattr(archive_resumable_worker, "ArchiveLock", DummyLock)
    state, queue, store, uploader, worker = _worker()

    asyncio.run(worker.worker_step(state["job_id"]))
    second = asyncio.run(worker.worker_step(state["job_id"]))
    third = asyncio.run(worker.worker_step(state["job_id"]))

    assert second["action"] == "drive_resumable_chunk_uploaded"
    assert second["confirmed_bytes"] == 4
    assert third["confirmed_bytes"] == 8
    assert uploader.uploads == [(0, b"abcd"), (4, b"efgh")]
    assert store.yandex.range_reads == [(0, 4), (4, 8)]
    assert queue.state["finalize"]["resumable_upload"]["offset"] == 8
    assert store.drive.promotions == []


def test_final_upload_sha256_backup_then_promote_then_commit(monkeypatch):
    monkeypatch.setattr(archive_resumable_worker, "ArchiveLock", DummyLock)
    state, queue, store, uploader, worker = _worker()

    asyncio.run(worker.worker_step(state["job_id"]))
    asyncio.run(worker.worker_step(state["job_id"]))
    asyncio.run(worker.worker_step(state["job_id"]))
    final = asyncio.run(worker.worker_step(state["job_id"]))

    assert final["action"] == "report_annual_uploaded_resumable"
    assert final["drive_verified"] is True
    assert final["backup_verified"] is True
    assert final["canonical_promoted"] is True
    assert store.events == ["backup", "promote"]
    assert queue.state["finalize"]["phase"] == "COMMIT"
    assert queue.state["finalize"]["annual_object_id"] == uploader.file_id
    assert queue.state["finalize"]["resumable_upload_verified"]["sha256"] == hashlib.sha256(store.yandex.candidate).hexdigest()
    assert "resumable_upload" not in queue.state["finalize"]
    assert queue.state["completed_count"] == 23
    backup_key = "База данных/WB/wb_laser_master/2026/finance/weekly/main/wb_laser_master__weekly_main__2026.csv"
    assert store.yandex.backups[backup_key] == store.yandex.candidate


def test_crash_recovery_adopts_matching_already_promoted_canonical(monkeypatch):
    monkeypatch.setattr(archive_resumable_worker, "ArchiveLock", DummyLock)
    candidate = b"abcdefghijkl"
    state, queue, store, uploader, worker = _worker(candidate)
    store.drive.canonical_file_id = store.drive.staged_file_id
    store.drive.previous_trashed = False
    store.drive.previous_file_id = "unused-old"

    async def exact_find(parent_id, name):
        del parent_id, name
        return {
            "id": store.drive.staged_file_id,
            "name": "wb_laser_master__weekly_main__2026.csv",
            "size": len(candidate),
            "sha256Checksum": hashlib.sha256(candidate).hexdigest(),
        }

    async def exact_meta(file_id):
        assert file_id == store.drive.staged_file_id
        return {
            "id": file_id,
            "name": "wb_laser_master__weekly_main__2026.csv",
            "size": str(len(candidate)),
            "sha256Checksum": hashlib.sha256(candidate).hexdigest(),
        }

    uploader.find_named_file = exact_find
    uploader.file_metadata = exact_meta

    result = asyncio.run(worker.worker_step(state["job_id"]))

    assert result["action"] == "report_annual_already_promoted"
    assert uploader.started == 0
    assert store.events == ["backup"]
    assert queue.state["finalize"]["phase"] == "COMMIT"
    assert queue.state["completed_count"] == 23


def test_retry_delay_is_exponential_bounded_and_has_small_jitter():
    job = "wb-finance-wb_laser_master-2026"
    delays = [archive_resumable_worker.WBFinanceResumableWorker._retry_delay(job, i) for i in range(1, 10)]
    assert 5 <= delays[0] <= 10
    assert delays[1] >= 10
    assert delays[2] >= 20
    assert delays[-1] <= 300


def test_default_worker_uses_apps_script_session_broker_without_oauth(monkeypatch):
    monkeypatch.setattr(archive_resumable_worker, "ArchiveLock", DummyLock)
    monkeypatch.delenv("MARKETPLACE_MCP_GOOGLE_DRIVE_OAUTH_JSON", raising=False)
    candidate = b"laser-candidate"
    state = _state(candidate)
    queue = FakeQueue(state)
    store = FakeStore(candidate)
    worker = archive_resumable_worker.WBFinanceResumableWorker(queue, store, None)

    result = asyncio.run(worker.worker_step(state["job_id"]))

    assert result["action"] == "drive_resumable_session_started"
    assert store.drive.starts
    assert store.drive.starts[0]["name"].startswith(".wb_laser_master__weekly_main__2026.csv.upload-report-")
    assert queue.state["finalize"]["phase"] == "UPLOAD_ANNUAL"
    assert queue.state["finalize"]["report_id"] == 743994450
    assert store.yandex.candidate == candidate
    assert queue.delegate_calls == []


def test_non_upload_phase_delegates_to_existing_queue(monkeypatch):
    monkeypatch.setattr(archive_resumable_worker, "ArchiveLock", DummyLock)
    candidate = b"x"
    state = _state(candidate)
    state.pop("finalize")
    state["phase"] = "DOWNLOAD"
    queue = FakeQueue(state)
    worker = archive_resumable_worker.WBFinanceResumableWorker(queue, FakeStore(candidate), None)

    result = asyncio.run(worker.worker_step(state["job_id"]))

    assert result["action"] == "delegated"
    assert queue.delegate_calls == [state["job_id"]]
