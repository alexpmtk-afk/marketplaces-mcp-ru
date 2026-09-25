from __future__ import annotations

import asyncio
import csv
import io
from datetime import date
from types import SimpleNamespace

import pytest

import core.ozon_current_archive as current
from core.ozon_current_archive import (
    OzonCurrentArchiveJobQueue,
    canonical_location,
    current_period,
    parse_snapshot,
    provider_utc_window,
    serialize_snapshot,
    stable_key_quality,
)


def test_current_period_is_full_open_month_through_today():
    start, end = current_period(date(2026, 9, 23))
    assert start == date(2026, 9, 1)
    assert end == date(2026, 9, 23)


def test_provider_utc_window_preserves_moscow_calendar_days():
    since, to = provider_utc_window(date(2026, 9, 1), date(2026, 9, 23))
    assert since == "2026-08-31T21:00:00Z"
    assert to == "2026-09-23T20:59:59Z"


def test_current_canonical_names_are_separate_by_dataset():
    parts, fbo = canonical_location(
        "ozon_laser_master", 2026, 9, "ozon_current_orders_fbo"
    )
    _, fbs = canonical_location(
        "ozon_laser_master", 2026, 9, "ozon_current_orders_fbs"
    )
    _, accruals = canonical_location(
        "ozon_laser_master", 2026, 9, "ozon_current_accruals"
    )
    assert parts == [
        "База данных", "Ozon", "ozon_laser_master", "2026", "CURRENT", "2026-09"
    ]
    assert fbo == "ozon_laser_master__orders_fbo__2026-09.csv"
    assert fbs == "ozon_laser_master__orders_fbs__2026-09.csv"
    assert accruals == "ozon_laser_master__accruals__2026-09.csv"


def test_snapshot_preserves_nested_provider_data_and_raw_json():
    rows = [{
        "posting_number": "P-1",
        "status": "delivered",
        "products": [{"sku": 123, "offer_id": "A"}],
        "financial_data": {
            "cluster_from": "A",
            "products": [{"product_id": 1, "commission_amount": 2.5}],
        },
    }]
    raw = serialize_snapshot(rows, stable_key=("posting_number",))
    fields, parsed = parse_snapshot(raw)

    assert raw.startswith(b"\xef\xbb\xbf")
    assert fields[0] == "posting_number"
    assert "_raw_json" in fields
    assert parsed[0]["posting_number"] == "P-1"
    assert '"sku":123' in parsed[0]["products"]
    assert '"cluster_from":"A"' in parsed[0]["financial_data"]
    assert '"posting_number":"P-1"' in parsed[0]["_raw_json"]


def test_snapshot_rejects_duplicate_or_incomplete_stable_key():
    with pytest.raises(ValueError, match="duplicate stable key"):
        serialize_snapshot(
            [{"posting_number": "P-1"}, {"posting_number": "P-1"}],
            stable_key=("posting_number",),
        )
    with pytest.raises(ValueError, match="incomplete stable key"):
        serialize_snapshot(
            [{"posting_number": ""}],
            stable_key=("posting_number",),
        )


def test_accrual_key_quality_uses_accrual_id():
    quality = stable_key_quality(
        [{"accrual_id": "1"}, {"accrual_id": "2"}],
        ("accrual_id",),
    )
    assert quality == {
        "rows": 2,
        "unique_stable_keys": 2,
        "duplicate_stable_key_rows": 0,
        "incomplete_stable_key_rows": 0,
    }


def test_enqueue_reopens_same_month_as_fresh_full_snapshot(monkeypatch):
    queue = OzonCurrentArchiveJobQueue(SimpleNamespace(), SimpleNamespace())
    existing = {
        "job_id": "ozon-current-ozon_laser_master-2026-09",
        "status": "COMPLETE",
        "created_at_utc": "old",
        "refresh_generation": 4,
    }
    saved = []
    scheduled = []

    async def load(_job_id):
        return existing

    async def save(state):
        saved.append(dict(state))

    async def schedule(job_id, delay_seconds=0):
        scheduled.append((job_id, delay_seconds))

    monkeypatch.setattr(current, "current_period", lambda today=None: (date(2026, 9, 1), date(2026, 9, 23)))
    monkeypatch.setattr(queue, "_load", load)
    monkeypatch.setattr(queue, "_save", save)
    monkeypatch.setattr(queue, "_schedule", schedule)

    result = asyncio.run(queue.enqueue(year=2026, seller="ozon_laser_master"))

    assert result["refresh_action"] == "reopened_complete"
    assert result["refresh_generation"] == 5
    assert saved[-1]["date_from"] == "2026-09-01"
    assert saved[-1]["date_to"] == "2026-09-23"
    assert saved[-1]["phase"] == "FETCH_FBO"
    assert saved[-1]["datasets"] == {}
    assert scheduled == [("ozon-current-ozon_laser_master-2026-09", 0)]


def test_enqueue_resumes_incomplete_without_resetting_progress(monkeypatch):
    queue = OzonCurrentArchiveJobQueue(SimpleNamespace(), SimpleNamespace())
    existing = {
        "job_id": "ozon-current-ozon_laser_master-2026-09",
        "status": "QUEUED",
        "phase": "FETCH_ACCRUALS",
        "year": 2026,
        "period": "2026-09",
        "refresh_generation": 3,
    }
    scheduled = []

    async def load(_job_id):
        return dict(existing)

    async def schedule(job_id, delay_seconds=0):
        scheduled.append((job_id, delay_seconds))

    async def forbidden_save(_state):
        raise AssertionError("incomplete refresh must not be reset")

    monkeypatch.setattr(current, "current_period", lambda today=None: (date(2026, 9, 1), date(2026, 9, 23)))
    monkeypatch.setattr(queue, "_load", load)
    monkeypatch.setattr(queue, "_schedule", schedule)
    monkeypatch.setattr(queue, "_save", forbidden_save)

    result = asyncio.run(queue.enqueue(year=2026, seller="ozon_laser_master"))

    assert result["refresh_action"] == "resumed_existing"
    assert result["phase"] == "FETCH_ACCRUALS"
    assert result["refresh_generation"] == 3
    assert scheduled == [("ozon-current-ozon_laser_master-2026-09", 0)]


def test_enqueue_resumes_failed_job_from_saved_phase(monkeypatch):
    queue = OzonCurrentArchiveJobQueue(SimpleNamespace(), SimpleNamespace())
    existing = {
        "job_id": "ozon-current-ozon_laser_master-2026-09",
        "status": "FAILED",
        "phase": "PUBLISH",
        "year": 2026,
        "period": "2026-09",
        "refresh_generation": 1,
        "last_error": "temporary Drive verification failure",
        "last_retry_after_seconds": 0,
    }
    saved = []
    scheduled = []

    async def load(_job_id):
        return dict(existing)

    async def save(state):
        saved.append(dict(state))

    async def schedule(job_id, delay_seconds=0):
        scheduled.append((job_id, delay_seconds))

    monkeypatch.setattr(current, "current_period", lambda today=None: (date(2026, 9, 1), date(2026, 9, 23)))
    monkeypatch.setattr(queue, "_load", load)
    monkeypatch.setattr(queue, "_save", save)
    monkeypatch.setattr(queue, "_schedule", schedule)

    result = asyncio.run(queue.enqueue(year=2026, seller="ozon_laser_master"))

    assert result["refresh_action"] == "resumed_failed"
    assert result["status"] == "QUEUED"
    assert result["phase"] == "PUBLISH"
    assert result["refresh_generation"] == 1
    assert saved[-1]["status"] == "QUEUED"
    assert saved[-1]["phase"] == "PUBLISH"
    assert saved[-1]["last_error"] is None
    assert scheduled == [("ozon-current-ozon_laser_master-2026-09", 0)]


def test_enqueue_rejects_non_open_year(monkeypatch):
    queue = OzonCurrentArchiveJobQueue(SimpleNamespace(), SimpleNamespace())
    monkeypatch.setattr(current, "current_period", lambda today=None: (date(2026, 9, 1), date(2026, 9, 23)))
    with pytest.raises(ValueError, match="open month only"):
        asyncio.run(queue.enqueue(year=2025, seller="ozon_laser_master"))


class _VerifyStore:
    def __init__(self):
        self.files = {}
        self.reader = _VerifyReader(self)

    async def ensure_folder_path(self, parts):
        return "writer:" + "/".join(parts)

    async def upload_bytes(self, parent, name, data, *, mime_type="text/csv"):
        del mime_type
        key = f"{parent}/{name}"
        self.files[key] = bytes(data)
        direct_key = key.replace("writer:", "direct:", 1)
        self.files[direct_key] = bytes(data)
        return SimpleNamespace(id="drive-id", name=name, size=len(data))


class _VerifyReader:
    def __init__(self, owner):
        self.owner = owner
        self.reads = []

    async def ensure_folder_path(self, parts):
        return "direct:" + "/".join(parts)

    async def download_named(self, parent, name):
        key = f"{parent}/{name}"
        self.reads.append(key)
        data = self.owner.files.get(key)
        if data is None:
            return None, None
        return SimpleNamespace(id="direct-id", name=name, size=len(data)), data


def test_canonical_publish_verification_prefers_direct_drive_reader():
    store = _VerifyStore()
    queue = OzonCurrentArchiveJobQueue(SimpleNamespace(), store)
    payload = b"canonical-current"

    item, sha = asyncio.run(
        queue._write_canonical_verified(
            ["База данных", "Ozon", "ozon_laser_master", "2026", "CURRENT", "2026-09"],
            "sample.csv",
            payload,
        )
    )

    assert item.id == "drive-id"
    assert sha == __import__("hashlib").sha256(payload).hexdigest()
    assert store.reader.reads == [
        "direct:База данных/Ozon/ozon_laser_master/2026/CURRENT/2026-09/sample.csv"
    ]
