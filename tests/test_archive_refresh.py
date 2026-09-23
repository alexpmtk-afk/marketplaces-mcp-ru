from __future__ import annotations

import asyncio

from core.archive_refresh import (
    enqueue_refresh_cycle,
    normalize_refresh_family,
    refresh_catalog,
)


class FakeQueue:
    def __init__(self, state=None):
        self.state = state
        self.saved = []
        self.scheduled = []
        self.created = []

    @staticmethod
    def normalize_cabinet(seller):
        return str(seller)

    @staticmethod
    def job_id(cabinet, year):
        return f"job-{cabinet}-{year}"

    async def _load(self, job_id):
        return None if self.state is None else dict(self.state)

    async def _save(self, state):
        self.state = dict(state)
        self.saved.append(dict(state))

    async def _schedule(self, job_id, delay_seconds=0):
        self.scheduled.append((job_id, float(delay_seconds)))

    async def enqueue(self, *, year, seller):
        self.created.append((year, seller))
        return {
            "ok": True,
            "job_id": self.job_id(seller, year),
            "created": True,
            "status": "QUEUED",
            "phase": "DISCOVER",
        }


def test_catalog_registers_refresh_invariants_and_stable_keys():
    catalog = {item["dataset_family"]: item for item in refresh_catalog()}
    assert set(catalog) == {"advertising", "finance", "current"}
    assert catalog["finance"]["stable_keys"]["wb_weekly_finance_main"] == ["reportId", "rrdId"]
    assert catalog["finance"]["coverage_model"] == "provider_report_registry"
    assert catalog["advertising"]["stable_keys"]["ads_campaign_daily"] == ["date", "campaign_id"]
    assert catalog["advertising"]["coverage_model"] == "bounded_request_coverage_registry"
    assert catalog["current"]["marketplace"] == "ozon"
    assert catalog["current"]["stable_keys"]["ozon_current_orders_fbo"] == ["posting_number"]
    assert catalog["current"]["stable_keys"]["ozon_current_accruals"] == ["accrual_id"]


def test_normalize_refresh_family_aliases():
    assert normalize_refresh_family("all") == ("advertising", "finance")
    assert normalize_refresh_family("wb_weekly_finance_main") == ("finance",)
    assert normalize_refresh_family("ads") == ("advertising",)
    assert normalize_refresh_family("all", marketplace="ozon") == ("ozon_current",)
    assert normalize_refresh_family("current", marketplace="ozon") == ("ozon_current",)


def test_new_job_is_created_and_scheduled_by_native_queue():
    queue = FakeQueue()
    result = asyncio.run(enqueue_refresh_cycle(
        queue, family="finance", year=2026, seller="wb_demo"
    ))
    assert result["refresh_action"] == "created"
    assert result["scheduled"] is True
    assert queue.created == [(2026, "wb_demo")]


def test_running_job_is_rescheduled_without_resetting_progress():
    state = {
        "job_id": "job-wb_demo-2026",
        "status": "DOWNLOAD",
        "phase": "DOWNLOAD",
        "report_index": 12,
        "provider_calls": 42,
        "refresh_generation": 3,
    }
    queue = FakeQueue(state)
    result = asyncio.run(enqueue_refresh_cycle(
        queue, family="finance", year=2026, seller="wb_demo"
    ))
    assert result["refresh_action"] == "resumed_existing"
    assert result["refresh_generation"] == 3
    assert queue.state == state
    assert queue.scheduled == [("job-wb_demo-2026", 0.0)]


def test_complete_finance_job_reopens_at_discover_and_preserves_lifetime_calls():
    queue = FakeQueue({
        "job_id": "job-wb_demo-2026",
        "status": "COMPLETE",
        "phase": "DOWNLOAD",
        "discovery_offset": 1000,
        "fragments": [{"report_id": 1}],
        "report_index": 1,
        "current_rrd_id": 999,
        "completed_report_ids": [1],
        "provider_calls": 77,
        "finalize": {"report_id": 1, "phase": "COMMIT"},
        "finalize_telemetry": {"events": ["old"]},
        "last_error": "old error",
        "last_retry_after_seconds": 59,
        "refresh_generation": 4,
    })
    result = asyncio.run(enqueue_refresh_cycle(
        queue, family="finance", year=2026, seller="wb_demo"
    ))
    assert result["refresh_action"] == "reopened_complete"
    assert result["refresh_generation"] == 5
    assert queue.state["status"] == "QUEUED"
    assert queue.state["phase"] == "DISCOVER"
    assert queue.state["discovery_offset"] == 0
    assert queue.state["fragments"] == []
    assert queue.state["report_index"] == 0
    assert queue.state["current_rrd_id"] == 0
    assert queue.state["completed_report_ids"] == []
    assert queue.state["provider_calls"] == 77
    assert queue.state["last_error"] is None
    assert "finalize" not in queue.state
    assert "finalize_telemetry" not in queue.state
    assert queue.scheduled == [("job-wb_demo-2026", 0.0)]


def test_complete_advertising_job_reopens_without_stale_plan_or_commit_state():
    queue = FakeQueue({
        "job_id": "job-wb_demo-2026",
        "status": "COMPLETE",
        "phase": "COMPLETE",
        "provider_calls": 120,
        "fetch_plan": [{"old": True}],
        "fetch_index": 1,
        "cluster_plan": [{"old": True}],
        "cluster_index": 1,
        "completed_requests": [{"old": True}],
        "staged_datasets": {"ads_campaign_daily": {"rows": 10}},
        "campaign_ids": [1, 2],
        "fullstats_campaign_ids": [1],
        "commit": {"phase": "COMPLETE"},
        "refresh_generation": 1,
    })
    result = asyncio.run(enqueue_refresh_cycle(
        queue, family="advertising", year=2026, seller="wb_demo"
    ))
    assert result["refresh_action"] == "reopened_complete"
    assert result["refresh_generation"] == 2
    assert queue.state["status"] == "QUEUED"
    assert queue.state["phase"] == "DISCOVER"
    assert queue.state["provider_calls"] == 120
    assert queue.state["fetch_plan"] == []
    assert queue.state["cluster_plan"] == []
    assert queue.state["completed_requests"] == []
    assert queue.state["staged_datasets"] == {}
    assert queue.state["campaign_ids"] == []
    assert queue.state["fullstats_campaign_ids"] == []
    assert "commit" not in queue.state
