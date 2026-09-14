from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

from core.wb_advertising_archive_verified_worker import VerifiedWBAdvertisingArchiveWorker


@dataclass
class _Item:
    id: str
    size: int


class _Yandex:
    def __init__(self, candidate: bytes):
        self.objects = {"candidate": candidate}

    async def download_bytes(self, file_id):
        return self.objects[file_id]

    async def ensure_folder_path(self, parts):
        return "/".join(parts)

    async def upload_bytes(self, parent, name, data, *, mime_type="text/csv"):
        key = f"{parent}/{name}"
        self.objects[key] = bytes(data)
        return _Item(key, len(data))


class _Drive:
    async def ensure_folder_path(self, parts):
        return "/".join(parts)


class _Store:
    def __init__(self, candidate):
        self.drive = _Drive()
        self.yandex = _Yandex(candidate)


class _Queue:
    def __init__(self, state):
        self.state = state
        self.scheduled = []

    async def _save(self, state):
        self.state = state

    async def _schedule(self, job_id, delay=0):
        self.scheduled.append((job_id, delay))


class _Uploader:
    def __init__(self, file_id, size, sha):
        self.file_id = file_id
        self.meta = {"size": size, "sha256Checksum": sha}

    async def find_named_file(self, parent, name):
        return {"id": self.file_id, "name": name}

    async def file_metadata(self, file_id):
        return self.meta


def test_recovery_recreates_and_verifies_yandex_backup_before_coverage():
    candidate = b"date;campaign_id\r\n2026-09-01;10\r\n"
    sha = hashlib.sha256(candidate).hexdigest()
    state = {
        "job_id": "wb-advertising-wb_novokshenov-2026",
        "status": "PROMOTION_PENDING",
        "phase": "COMMIT",
    }
    commit = {
        "annual_bytes": len(candidate),
        "annual_sha256": sha,
        "candidate_id": "candidate",
        "annual_parts": ["База данных", "WB", "wb_novokshenov", "2026", "advertising", "stats", "campaign_daily"],
        "annual_name": "wb_novokshenov__ads_campaign_daily__2026.csv",
        "dataset_phase": "PROMOTION_PENDING",
    }
    state["commit"] = commit
    store = _Store(candidate)
    queue = _Queue(state)
    worker = VerifiedWBAdvertisingArchiveWorker(
        queue,
        store,
        uploader=_Uploader("drive-canonical", len(candidate), sha),
    )

    result = asyncio.run(worker._upload_dataset(state, commit, "ads_campaign_daily"))
    assert result["action"] == "canonical_and_backup_recovered"
    assert result["backup_verified"] is True
    assert queue.state["commit"]["dataset_phase"] == "COVERAGE"
    backup_key = "/".join(commit["annual_parts"]) + "/" + commit["annual_name"]
    assert store.yandex.objects[backup_key] == candidate
