from __future__ import annotations

import asyncio
from dataclasses import dataclass

from core.wb_advertising_archive import merge_annual_dataset, parse_csv
from core.wb_advertising_archive_queue import (
    WBAdvertisingArchiveJobQueue,
    _request_lock_key,
)


@dataclass
class _Item:
    id: str = "memory"
    size: int | None = None
    mime_type: str | None = None


class _MemoryStore:
    def __init__(self):
        self.data: dict[tuple[str, str], bytes] = {}

    async def ensure_folder_path(self, parts):
        return "/".join(str(part) for part in parts)

    async def download_named(self, parent, name):
        raw = self.data.get((parent, name))
        return (_Item(size=len(raw)), raw) if raw is not None else (None, None)

    async def upload_bytes(self, parent, name, data, *, mime_type="text/csv"):
        self.data[(parent, name)] = bytes(data)
        return _Item(size=len(data), mime_type=mime_type)


class _Queue(WBAdvertisingArchiveJobQueue):
    def __init__(self, store, responses=None):
        super().__init__(object(), store)
        self.responses = responses or {}
        self.scheduled: list[tuple[str, float]] = []
        self.unscheduled: list[str] = []

    def _resolve_creds(self, cabinet):
        return {"token": "test"}

    async def _schedule(self, job_id, delay_seconds=0.0):
        self.scheduled.append((job_id, float(delay_seconds)))

    async def _unschedule(self, job_id):
        self.unscheduled.append(job_id)

    async def _provider_call(self, operation_id, creds, *, query=None, json_body=None):
        value = self.responses[operation_id]
        return value(query=query, json_body=json_body) if callable(value) else value


def _state(**extra):
    base = {
        "job_id": "wb-advertising-wb_novokshenov-2026",
        "cabinet": "wb_novokshenov",
        "year": 2026,
        "status": "QUEUED",
        "phase": "DISCOVER",
        "provider_calls": 0,
        "fetch_plan": [],
        "fetch_index": 0,
        "cluster_plan": [],
        "cluster_index": 0,
        "completed_requests": [],
        "staged_datasets": {},
        "last_retry_after_seconds": 0,
        "last_error": None,
    }
    base.update(extra)
    return base


def test_resource_lock_is_narrow_to_dataset_period_and_scope():
    first = _request_lock_key("wb_novokshenov", {
        "operation_id": "wb_get_adv_fullstats",
        "datasets": ["ads_campaign_daily", "ads_product_daily"],
        "date_from": "2026-01-01",
        "date_to": "2026-01-31",
        "scope": {"campaign_ids": [1, 2]},
    })
    second = _request_lock_key("wb_novokshenov", {
        "operation_id": "wb_get_adv_fullstats",
        "datasets": ["ads_campaign_daily", "ads_product_daily"],
        "date_from": "2026-02-01",
        "date_to": "2026-02-28",
        "scope": {"campaign_ids": [1, 2]},
    })
    other = _request_lock_key("wb_dmitrieva", {
        "operation_id": "wb_get_adv_fullstats",
        "datasets": ["ads_campaign_daily", "ads_product_daily"],
        "date_from": "2026-01-01",
        "date_to": "2026-01-31",
        "scope": {"campaign_ids": [1, 2]},
    })
    assert first != second
    assert first != other
    assert "wb_novokshenov" in first


def test_discovery_stages_campaign_roster_and_advances_to_plan():
    store = _MemoryStore()
    queue = _Queue(store, {
        "wb_get_adv_promotion_count": {
            "ok": True,
            "data": {
                "adverts": [
                    {"type": 9, "status": 9, "advert_list": [{"advertId": 10, "changeTime": "2026-09-01"}]},
                    {"type": 9, "status": 8, "advert_list": [{"advertId": 11, "changeTime": "2026-08-01"}]},
                ]
            },
        }
    })
    state = _state()
    result = asyncio.run(queue._discover_step(state))
    assert result["action"] == "campaign_roster_staged"
    assert state["campaign_ids"] == [10, 11]
    assert state["fullstats_campaign_ids"] == [10]
    assert state["phase"] == "PLAN"
    folder, name = asyncio.run(queue._stage_location(state["job_id"], "ads_campaign_roster_snapshots"))
    _, raw = asyncio.run(store.download_named(folder, name))
    _, rows = parse_csv(raw)
    assert len(rows) == 2


def test_base_plan_never_exceeds_fullstats_provider_bounds():
    store = _MemoryStore()
    queue = _Queue(store)
    state = _state(
        phase="PLAN",
        campaign_ids=list(range(1, 62)),
        fullstats_campaign_ids=list(range(1, 62)),
    )
    result = asyncio.run(queue._plan_step(state))
    assert result["action"] == "base_plan_ready"
    fullstats = [item for item in state["fetch_plan"] if item["kind"] == "fullstats"]
    assert fullstats
    for item in fullstats:
        assert len(item["scope"]["campaign_ids"]) <= 50
        start = __import__("datetime").date.fromisoformat(item["date_from"])
        end = __import__("datetime").date.fromisoformat(item["date_to"])
        assert (end - start).days < 31
    campaign_info = [item for item in state["fetch_plan"] if item["kind"] == "campaign_info"]
    assert all(len(item["scope"]["campaign_ids"]) <= 50 for item in campaign_info)


def test_fullstats_provider_step_stages_campaign_and_product_data():
    store = _MemoryStore()
    queue = _Queue(store, {
        "wb_get_adv_fullstats": {
            "ok": True,
            "data": [{
                "advertId": 10,
                "days": [{
                    "date": "2026-09-01",
                    "views": 100,
                    "clicks": 10,
                    "atbs": 3,
                    "orders": 2,
                    "shks": 2,
                    "canceled": 0,
                    "sum": 50,
                    "sum_price": 500,
                    "apps": [{"appType": 32, "nms": [{
                        "nmId": 777,
                        "name": "Item",
                        "views": 100,
                        "clicks": 10,
                        "atbs": 3,
                        "orders": 2,
                        "shks": 2,
                        "canceled": 0,
                        "sum": 50,
                        "sum_price": 500,
                    }]}],
                }],
            }],
        }
    })
    request = {
        "kind": "fullstats",
        "operation_id": "wb_get_adv_fullstats",
        "datasets": ["ads_campaign_daily", "ads_product_daily"],
        "date_from": "2026-09-01",
        "date_to": "2026-09-01",
        "scope": {"campaign_ids": [10]},
        "query": {"ids": "10", "beginDate": "2026-09-01", "endDate": "2026-09-01"},
    }
    state = _state(phase="FETCH", fetch_plan=[request])
    result = asyncio.run(queue._fetch_step(state, cluster=False))
    assert result["action"] == "provider_request_staged"
    assert set(result["datasets"]) == {"ads_campaign_daily", "ads_product_daily"}
    assert state["fetch_index"] == 1
    assert state["provider_calls"] == 1


def test_cluster_plan_uses_staged_product_pairs_and_conservative_period_chunks():
    store = _MemoryStore()
    queue = _Queue(store)
    state = _state(phase="PLAN_CLUSTERS")
    rows = [
        {
            "date": "2026-01-01",
            "campaign_id": 10,
            "app_type": 32,
            "nm_id": value,
            "product_role": "ad_traffic_product",
        }
        for value in range(1, 102)
    ]
    rows.append({
        "date": "2026-01-01",
        "campaign_id": 10,
        "app_type": 32,
        "nm_id": 999,
        "product_role": "associated_conversion_candidate",
    })
    raw, _ = merge_annual_dataset("ads_product_daily", None, rows)
    folder, name = asyncio.run(queue._stage_location(state["job_id"], "ads_product_daily"))
    asyncio.run(store.upload_bytes(folder, name, raw))

    result = asyncio.run(queue._plan_clusters_step(state))
    assert result["action"] == "cluster_plan_ready"
    assert result["campaign_product_pairs"] == 101
    planned_pairs = {
        (int(item["advertId"]), int(item["nmId"]))
        for request in state["cluster_plan"]
        for item in request["json_body"]["items"]
    }
    assert (10, 999) not in planned_pairs
    assert state["phase"] == "FETCH_CLUSTERS"
    assert state["cluster_plan"]
    for item in state["cluster_plan"]:
        assert len(item["json_body"]["items"]) <= 100
        start = __import__("datetime").date.fromisoformat(item["date_from"])
        end = __import__("datetime").date.fromisoformat(item["date_to"])
        assert (end - start).days < 31


def test_ingestion_finishes_staged_not_falsely_complete():
    store = _MemoryStore()
    queue = _Queue(store)
    state = _state(phase="FETCH_CLUSTERS", status="QUEUED", cluster_plan=[], cluster_index=0)
    result = asyncio.run(queue._mark_ready_to_commit(state))
    assert result["status"] == "READY_TO_COMMIT"
    assert state["phase"] == "COMMIT"
    assert state["status"] != "COMPLETE"
    assert result["next_required_phase"] == "verified canonical Drive commit + coverage COMMIT"
