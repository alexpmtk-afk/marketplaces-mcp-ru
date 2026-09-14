from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core import archive_queue


def test_2026_boundary_starts_on_previous_monday():
    assert archive_queue._boundary_start(2026).isoformat() == "2025-12-29"
    fragment = SimpleNamespace(
        logical_week_from="2025-12-29",
        logical_week_to="2026-01-04",
    )
    assert archive_queue._fragment_overlaps_year(fragment, 2026) is True


class FakeObject:
    def __init__(self, key: str, data: bytes):
        self.id = key
        self.name = key.rsplit("/", 1)[-1]
        self.size = len(data)


class FakeStore:
    def __init__(self):
        self.files = {}

    async def ensure_folder_path(self, parts):
        return "/".join(parts)

    async def download_named(self, folder, name):
        key = f"{folder}/{name}"
        data = self.files.get(key)
        return (None, None) if data is None else (FakeObject(key, data), data)

    async def upload_bytes(self, folder, name, data, **kwargs):
        key = f"{folder}/{name}"
        self.files[key] = data
        return FakeObject(key, data)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call_spec(self, spec, **kwargs):
        self.calls.append((spec, kwargs))
        return self.responses.pop(0)


class FakeQueue(archive_queue.WBFinanceArchiveJobQueue):
    def __init__(self, wb, store):
        super().__init__(wb, store)
        self.scheduled = {}

    def _resolve_creds(self, cabinet):
        return {"token": "not-a-jwt"}

    async def _schedule(self, job_id, delay_seconds=0):
        self.scheduled[job_id] = float(delay_seconds)

    async def _unschedule(self, job_id):
        self.scheduled.pop(job_id, None)


def _patch_lock(monkeypatch):
    class DummyLock:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
    monkeypatch.setattr(archive_queue, "ArchiveLock", DummyLock)


def test_worker_step_is_bounded_to_one_provider_call(monkeypatch):
    _patch_lock(monkeypatch)
    discovery = {
        "ok": True,
        "status": 200,
        "data": [{
            "reportId": 101,
            "dateFrom": "2025-12-29",
            "dateTo": "2026-01-04",
            "createDate": "2026-01-05",
            "reportType": 1,
        }],
    }
    detail = {
        "ok": True,
        "status": 200,
        "data": [{
            "reportId": 101,
            "rrdId": 10,
            "reportType": 1,
            "dateFrom": "2025-12-29",
            "dateTo": "2026-01-04",
        }],
    }
    finish = {"ok": True, "status": 204, "data": None}
    client = FakeClient([discovery, detail, finish])
    wb = SimpleNamespace(
        client=client,
        catalog={
            "wb_finance_sales_reports_list": "list-spec",
            "wb_finance_sales_reports_detailed_by_id": "detail-spec",
        },
    )
    queue = FakeQueue(wb, FakeStore())
    job = asyncio.run(queue.enqueue(year=2026, seller="wb_novokshenov"))
    job_id = job["job_id"]

    before = len(client.calls)
    first = asyncio.run(queue.worker_step(job_id))
    assert len(client.calls) - before == 1
    assert first["action"] == "discovery_complete"
    state = asyncio.run(queue.status(job_id))
    assert state["reports_total"] == 1

    before = len(client.calls)
    second = asyncio.run(queue.worker_step(job_id))
    assert len(client.calls) - before == 1
    assert second["action"] == "report_page_staged"

    before = len(client.calls)
    third = asyncio.run(queue.worker_step(job_id))
    assert len(client.calls) - before == 1
    assert third["action"] == "report_finalized"
    assert third["status"] == "COMPLETE"


def test_rate_limit_reschedules_without_sleep(monkeypatch):
    _patch_lock(monkeypatch)
    client = FakeClient([{
        "ok": False,
        "error_type": "rate_limit",
        "retry_after_seconds": 59.2,
        "retryable": True,
    }])
    wb = SimpleNamespace(
        client=client,
        catalog={"wb_finance_sales_reports_list": "list-spec"},
    )
    queue = FakeQueue(wb, FakeStore())
    job = asyncio.run(queue.enqueue(year=2026, seller="wb_novokshenov"))
    result = asyncio.run(queue.worker_step(job["job_id"]))
    assert result["action"] == "waiting_rate_limit"
    assert result["retry_after_seconds"] == 60
    assert len(client.calls) == 1
    assert queue.scheduled[job["job_id"]] == 59.2
