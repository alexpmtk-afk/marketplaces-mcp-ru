from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

import core.wb_advertising_archive_queue as queue_module
from core.archive_coverage import parse_registry
from core.wb_advertising_archive import encode_csv, parse_csv
from core.wb_advertising_archive_worker import (
    WBAdvertisingArchiveWorker,
    _dataset_lock_key,
    _empty_csv,
)


@dataclass
class _Item:
    id: str
    name: str = ""
    size: int | None = None
    mime_type: str = "text/csv"
    sha256_checksum: str | None = None


class _MemoryStore:
    def __init__(self):
        self.data: dict[tuple[str, str], bytes] = {}
        self.drive = self
        self.yandex = self

    async def ensure_folder_path(self, parts):
        return "/".join(str(part) for part in parts)

    async def download_named(self, parent, name):
        raw = self.data.get((parent, name))
        if raw is None:
            return None, None
        return _Item(id=f"{parent}/{name}", name=name, size=len(raw), sha256_checksum=hashlib.sha256(raw).hexdigest()), raw

    async def upload_bytes(self, parent, name, data, *, mime_type="text/csv"):
        raw = bytes(data)
        self.data[(parent, name)] = raw
        return _Item(id=f"{parent}/{name}", name=name, size=len(raw), mime_type=mime_type, sha256_checksum=hashlib.sha256(raw).hexdigest())

    async def download_bytes(self, file_id):
        parent, name = file_id.rsplit("/", 1)
        return self.data[(parent, name)]


class _Queue:
    def __init__(self, store, state=None):
        self.store = store
        self.state = state
        self.scheduled = []
        self.unscheduled = []

    async def _load(self, job_id):
        return self.state

    async def _save(self, state):
        self.state = state

    async def _schedule(self, job_id, delay_seconds=0):
        self.scheduled.append((job_id, delay_seconds))

    async def _unschedule(self, job_id):
        self.unscheduled.append(job_id)

    async def _next_due(self):
        return (self.state["job_id"], 0) if self.state else (None, 0)

    async def _stage_location(self, job_id, dataset):
        parent = await self.store.ensure_folder_path(["app", "jobs", "wb-advertising", job_id, "staging"])
        return parent, f"{dataset}.csv"

    async def worker_step(self, job_id=""):
        return {"ok": True, "action": "ingestion-delegate"}


def _state(**extra):
    base = {
        "job_id": "wb-advertising-wb_novokshenov-2026",
        "cabinet": "wb_novokshenov",
        "year": 2026,
        "phase": "COMMIT",
        "status": "READY_TO_COMMIT",
        "staged_datasets": {"ads_campaign_daily": {"total_rows": 1}},
        "completed_requests": [{
            "operation_id": "wb_get_adv_fullstats",
            "datasets": ["ads_campaign_daily"],
            "date_from": "2026-01-01",
            "date_to": "2026-01-31",
            "scope": {"campaign_ids": [10]},
        }],
    }
    base.update(extra)
    return base


def test_empty_dataset_has_schema_header_instead_of_missing_file():
    raw = _empty_csv("ads_campaign_daily")
    fields, rows = parse_csv(raw)
    assert rows == []
    assert fields[:2] == ["date", "campaign_id"]
    assert "spend" in fields


def test_commit_lock_is_scoped_to_cabinet_dataset_and_year():
    a = _dataset_lock_key("wb_novokshenov", 2026, "ads_campaign_daily")
    b = _dataset_lock_key("wb_novokshenov", 2026, "ads_expenses")
    c = _dataset_lock_key("wb_dmitrieva", 2026, "ads_campaign_daily")
    assert a != b != c
    assert "wb_novokshenov" in a
    assert "ads_campaign_daily" in a


def test_current_year_planner_never_requests_future_dates(monkeypatch):
    monkeypatch.setattr(queue_module, "_moscow_today", lambda: __import__("datetime").date(2026, 9, 14))
    assert queue_module.closed_history_period(2026) == ("2026-01-01", "2026-09-13")
    assert queue_module.closed_history_period(2025) == ("2025-01-01", "2025-12-31")


def test_prepare_merges_staging_with_existing_canonical_and_persists_candidate():
    store = _MemoryStore()
    state = _state()
    queue = _Queue(store, state)
    worker = WBAdvertisingArchiveWorker(queue, store, uploader=object())

    staged = encode_csv(
        ["date", "campaign_id", "spend"],
        [{"date": "2026-01-02", "campaign_id": 10, "spend": "2.00"}],
    )
    stage_parent, stage_name = asyncio.run(queue._stage_location(state["job_id"], "ads_campaign_daily"))
    asyncio.run(store.upload_bytes(stage_parent, stage_name, staged))

    canonical_parent = asyncio.run(store.ensure_folder_path([
        "База данных", "WB", "wb_novokshenov", "2026", "advertising", "stats", "campaign_daily"
    ]))
    canonical_name = "wb_novokshenov__ads_campaign_daily__2026.csv"
    existing = encode_csv(
        ["date", "campaign_id", "spend"],
        [{"date": "2026-01-01", "campaign_id": 10, "spend": "1.00"}],
    )
    asyncio.run(store.upload_bytes(canonical_parent, canonical_name, existing))

    result = asyncio.run(worker._prepare_dataset(state, {}, "ads_campaign_daily"))
    assert result["action"] == "advertising_candidate_prepared"
    assert result["rows"] == 2
    assert queue.state["commit"]["dataset_phase"] == "UPLOAD"
    candidate_id = queue.state["commit"]["candidate_id"]
    candidate = asyncio.run(store.download_bytes(candidate_id))
    _, rows = parse_csv(candidate)
    assert {(row["date"], row["campaign_id"]) for row in rows} == {
        ("2026-01-01", "10"), ("2026-01-02", "10")
    }


def test_coverage_is_written_only_after_canonical_phase_and_is_idempotent():
    store = _MemoryStore()
    state = _state(status="COMMITTING")
    queue = _Queue(store, state)
    worker = WBAdvertisingArchiveWorker(queue, store, uploader=object())
    commit = {
        "datasets": ["ads_campaign_daily"],
        "index": 0,
        "dataset": "ads_campaign_daily",
        "dataset_phase": "COVERAGE",
        "annual_name": "wb_novokshenov__ads_campaign_daily__2026.csv",
        "annual_bytes": 123,
        "annual_sha256": "a" * 64,
        "annual_rows": 9,
        "canonical_file_id": "drive-file-1",
    }
    state["commit"] = commit

    first = asyncio.run(worker._coverage_dataset(state, commit, "ads_campaign_daily"))
    assert first["action"] == "advertising_coverage_committed"
    registry_parent = asyncio.run(store.ensure_folder_path(["app", "registry"]))
    _, raw1 = asyncio.run(store.download_named(registry_parent, "dataset_coverage_registry.csv"))
    records1 = parse_registry(raw1)
    assert len(records1) == 1
    assert records1[0]["status"] == "COMPLETE"
    assert records1[0]["dataset"] == "ads_campaign_daily"

    replay = _state(status="COMMITTING")
    replay_commit = dict(commit)
    replay["commit"] = replay_commit
    queue.state = replay
    asyncio.run(worker._coverage_dataset(replay, replay_commit, "ads_campaign_daily"))
    _, raw2 = asyncio.run(store.download_named(registry_parent, "dataset_coverage_registry.csv"))
    assert len(parse_registry(raw2)) == 1


def test_worker_marks_job_complete_only_after_all_dataset_commits():
    store = _MemoryStore()
    state = _state(
        status="COMMITTING",
        commit={
            "datasets": ["ads_campaign_daily"],
            "index": 1,
            "dataset_phase": "PREPARE",
            "committed_datasets": ["ads_campaign_daily"],
        },
    )
    queue = _Queue(store, state)
    worker = WBAdvertisingArchiveWorker(queue, store, uploader=object())
    result = asyncio.run(worker._commit_step(state))
    assert result["status"] == "COMPLETE"
    assert queue.state["status"] == "COMPLETE"
    assert queue.state["phase"] == "COMPLETE"
