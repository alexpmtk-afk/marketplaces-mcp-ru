from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

import core.semantic_advertising as advertising
from core.archive_coverage import coverage_record, encode_registry
from core.semantic_business_router import execute_business_query
from core.semantic_registry import load_semantic_registry
from core.semantic_resolver import resolve_semantic_question
from core.wb_advertising_archive import canonical_location, encode_csv


@dataclass
class _Item:
    id: str
    size: int


class _Store:
    def __init__(self):
        self.data: dict[tuple[str, str], bytes] = {}

    async def ensure_folder_path(self, parts):
        return "/".join(str(part) for part in parts)

    async def upload_bytes(self, parent, name, data, *, mime_type="text/csv"):
        raw = bytes(data)
        self.data[(parent, name)] = raw
        return _Item(f"{parent}/{name}", len(raw))

    async def download_named(self, parent, name):
        raw = self.data.get((parent, name))
        if raw is None:
            return None, None
        return _Item(f"{parent}/{name}", len(raw)), raw


def _seed_store(*, complete_campaign_coverage: bool = True) -> _Store:
    store = _Store()
    cabinet = "wb_novokshenov"
    year = 2026

    roster_parts, roster_name = canonical_location(cabinet, year, advertising.ROSTER_DATASET)
    roster_parent = asyncio.run(store.ensure_folder_path(roster_parts))
    roster = encode_csv(
        ["observed_at", "campaign_id", "campaign_type", "status", "change_time", "fullstats_eligible"],
        [{
            "observed_at": "2026-09-03T06:00:00+00:00",
            "campaign_id": 10,
            "campaign_type": 8,
            "status": 9,
            "change_time": "2026-09-01T00:00:00+03:00",
            "fullstats_eligible": True,
        }],
    )
    asyncio.run(store.upload_bytes(roster_parent, roster_name, roster))

    daily_parts, daily_name = canonical_location(cabinet, year, advertising.CAMPAIGN_DATASET)
    daily_parent = asyncio.run(store.ensure_folder_path(daily_parts))
    daily = encode_csv(
        [
            "date", "campaign_id", "views", "clicks", "cart_adds", "ad_orders",
            "advertised_items", "canceled", "spend", "attributed_order_amount",
        ],
        [
            {
                "date": "2026-09-01", "campaign_id": 10, "views": 100, "clicks": 10,
                "cart_adds": 4, "ad_orders": 2, "advertised_items": 2, "canceled": 0,
                "spend": "50", "attributed_order_amount": "200",
            },
            {
                "date": "2026-09-02", "campaign_id": 10, "views": 200, "clicks": 20,
                "cart_adds": 8, "ad_orders": 4, "advertised_items": 4, "canceled": 0,
                "spend": "100", "attributed_order_amount": "400",
            },
        ],
    )
    asyncio.run(store.upload_bytes(daily_parent, daily_name, daily))

    campaign_to = "2026-09-02" if complete_campaign_coverage else "2026-09-01"
    registry = encode_registry([
        coverage_record(
            marketplace="wb",
            cabinet=cabinet,
            dataset=advertising.ROSTER_DATASET,
            operation_id="wb_get_adv_promotion_count",
            date_from="2026-09-01",
            date_to="2026-09-02",
            scope={},
            annual_file=roster_name,
            rows=1,
            bytes_count=len(roster),
            sha256="a" * 64,
        ),
        coverage_record(
            marketplace="wb",
            cabinet=cabinet,
            dataset=advertising.CAMPAIGN_DATASET,
            operation_id="wb_get_adv_fullstats",
            date_from="2026-09-01",
            date_to=campaign_to,
            scope={"campaign_ids": [10]},
            annual_file=daily_name,
            rows=2,
            bytes_count=len(daily),
            sha256="b" * 64,
        ),
    ])
    registry_parent = asyncio.run(store.ensure_folder_path(advertising.COVERAGE_FOLDER))
    asyncio.run(store.upload_bytes(registry_parent, advertising.COVERAGE_FILE, registry))
    return store


def test_registry_extension_promotes_advertising_to_available_capability():
    registry = load_semantic_registry()
    assert "advertising_performance" in registry["capabilities"]
    assert "advertising_performance" not in registry["not_covered"]
    assert registry["sources"]["wb_ads_data"]["database_presence"] == "AVAILABLE_IN_CANONICAL_ARCHIVE"
    assert registry["sources"]["wb_ads_data"]["dataset_id"] == advertising.CAMPAIGN_DATASET


def test_resolver_routes_advertising_question_to_capability():
    resolved = resolve_semantic_question("Какой ДРР и рекламные расходы были за период?")
    assert resolved["resolution_type"] == "CAPABILITY"
    assert resolved["capability_id"] == "advertising_performance"
    assert resolved["dataset_id"] == advertising.CAMPAIGN_DATASET


def test_advertising_executor_uses_approved_metric_contract(monkeypatch):
    monkeypatch.setattr(advertising, "_moscow_today", lambda: __import__("datetime").date(2026, 9, 15))
    store = _seed_store()
    result = asyncio.run(advertising.execute_semantic_advertising_question(
        store,
        question="Какой ДРР по рекламе за 1-2 сентября?",
        seller="wb_novokshenov",
        date_from="2026-09-01",
        date_to="2026-09-02",
    ))
    assert result["ok"] is True
    assert result["route"] == "semantic_advertising_archive"
    assert result["coverage"]["status"] == "FULL_COVERAGE"
    assert result["metric_contract_version"] == "wb_ads_m0.v1"
    assert result["data_class"] == "ADVERTISING_ATTRIBUTION_OPERATIONAL"
    metrics = result["metrics"]
    assert metrics["views"] == 300
    assert metrics["clicks"] == 30
    assert metrics["spend"] == 150.0
    assert metrics["attributed_order_amount"] == 600.0
    assert metrics["ctr_pct"] == 10.0
    assert metrics["cpc"] == 5.0
    assert metrics["cpo"] == 25.0
    assert metrics["drr_order_pct"] == 25.0
    assert metrics["roas"] == 4.0


def test_advertising_executor_fails_closed_on_partial_campaign_coverage(monkeypatch):
    monkeypatch.setattr(advertising, "_moscow_today", lambda: __import__("datetime").date(2026, 9, 15))
    store = _seed_store(complete_campaign_coverage=False)
    with pytest.raises(advertising.SemanticAdvertisingExecutionError, match="FULL_COVERAGE"):
        asyncio.run(advertising.execute_semantic_advertising_question(
            store,
            question="Покажи рекламные расходы",
            seller="wb_novokshenov",
            date_from="2026-09-01",
            date_to="2026-09-02",
        ))


def test_advertising_executor_refuses_product_substitution(monkeypatch):
    monkeypatch.setattr(advertising, "_moscow_today", lambda: __import__("datetime").date(2026, 9, 15))
    store = _seed_store()
    with pytest.raises(advertising.SemanticAdvertisingExecutionError, match="ads_product_daily"):
        asyncio.run(advertising.execute_semantic_advertising_question(
            store,
            question="Какой ДРР по этому товару?",
            seller="wb_novokshenov",
            date_from="2026-09-01",
            date_to="2026-09-02",
            nm_ids=[12345],
        ))


def test_business_router_dispatches_advertising_before_finance_executor(monkeypatch):
    monkeypatch.setattr(advertising, "_moscow_today", lambda: __import__("datetime").date(2026, 9, 15))
    store = _seed_store()
    result = asyncio.run(execute_business_query(
        {"_archive_store": store},
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-09-01",
        date_to="2026-09-02",
        question="Сколько потратили на рекламу и какой был ROAS?",
    ))
    assert result["ok"] is True
    assert result["metric"] == "ADVERTISING_PERFORMANCE"
    assert result["semantic_resolution"]["capability_id"] == "advertising_performance"
