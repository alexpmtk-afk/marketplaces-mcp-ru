from __future__ import annotations

import pytest

from core.wb_advertising_normalize import (
    campaign_product_ids,
    enrich_product_attribution,
    normalize_fullstats,
    normalize_product_identity_snapshot,
    normalize_search_cluster_daily,
    plan_fullstats_requests,
    plan_period_requests,
    split_date_range,
)


def test_split_date_range_respects_31_day_provider_limit():
    assert split_date_range("2026-01-01", "2026-01-31") == [("2026-01-01", "2026-01-31")]
    assert split_date_range("2026-01-01", "2026-02-01") == [
        ("2026-01-01", "2026-01-31"),
        ("2026-02-01", "2026-02-01"),
    ]
    chunks = split_date_range("2026-01-01", "2026-12-31")
    assert chunks[0] == ("2026-01-01", "2026-01-31")
    assert chunks[-1][1] == "2026-12-31"
    assert all((__import__("datetime").date.fromisoformat(b) - __import__("datetime").date.fromisoformat(a)).days < 31 for a, b in chunks)


def test_split_date_range_rejects_reversed_period():
    with pytest.raises(ValueError, match="date_from"):
        split_date_range("2026-09-02", "2026-09-01")


def test_fullstats_planner_batches_dates_and_campaigns_without_gaps():
    plan = plan_fullstats_requests(range(1, 52), "2026-01-01", "2026-02-01")
    assert len(plan) == 4
    assert plan[0] == {
        "operation_id": "wb_get_adv_fullstats",
        "campaign_ids": list(range(1, 51)),
        "date_from": "2026-01-01",
        "date_to": "2026-01-31",
    }
    assert plan[1]["campaign_ids"] == [51]
    assert plan[2]["date_from"] == "2026-02-01"
    assert plan[3]["campaign_ids"] == [51]


def test_period_planner_chunks_finance_history():
    plan = plan_period_requests("wb_get_adv_upd", "2026-01-01", "2026-02-01")
    assert plan == [
        {"operation_id": "wb_get_adv_upd", "date_from": "2026-01-01", "date_to": "2026-01-31"},
        {"operation_id": "wb_get_adv_upd", "date_from": "2026-02-01", "date_to": "2026-02-01"},
    ]


def test_fullstats_normalization_preserves_daily_and_product_grains():
    result = normalize_fullstats([
        {
            "advertId": 101,
            "boosterStats": [{"date": "2026-09-01", "nm": 777, "avg_position": 63}],
            "days": [
                {
                    "date": "2026-09-01T00:00:00Z",
                    "views": 1000,
                    "clicks": 100,
                    "atbs": 20,
                    "orders": 10,
                    "shks": 9,
                    "canceled": 1,
                    "sum": "500.20",
                    "sum_price": "5000.10",
                    "apps": [
                        {
                            "appType": 32,
                            "nms": [
                                {
                                    "nmId": 777,
                                    "name": "Товар",
                                    "views": 400,
                                    "clicks": 40,
                                    "atbs": 8,
                                    "orders": 4,
                                    "shks": 4,
                                    "canceled": 0,
                                    "sum": "200.08",
                                    "sum_price": "1900.03",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ])
    campaign = result["ads_campaign_daily"]
    product = result["ads_product_daily"]
    assert campaign == [{
        "date": "2026-09-01",
        "campaign_id": 101,
        "views": 1000,
        "clicks": 100,
        "cart_adds": 20,
        "ad_orders": 10,
        "accepted_orders_derived": 9,
        "advertised_items": 9,
        "canceled": 1,
        "spend": "500.20",
        "attributed_order_amount": "5000.10",
    }]
    assert product[0]["date"] == "2026-09-01"
    assert product[0]["campaign_id"] == 101
    assert product[0]["app_type"] == 32
    assert product[0]["nm_id"] == 777
    assert product[0]["spend"] == "200.08"
    assert product[0]["attributed_order_amount"] == "1900.03"
    assert product[0]["accepted_orders_derived"] == 4
    assert product[0]["avg_position"] == 63.0


def test_fullstats_normalizer_never_invents_missing_rows():
    result = normalize_fullstats([{"advertId": 101, "days": []}])
    assert result == {"ads_campaign_daily": [], "ads_product_daily": []}



def test_campaign_product_ids_reads_current_nm_settings():
    mapping = campaign_product_ids({
        "adverts": [{
            "id": 33650945,
            "nm_settings": [
                {"nm_id": 404071811, "bids_kopecks": {"search": 173}},
            ],
        }]
    })
    assert mapping == {33650945: {404071811}}


def test_product_identity_snapshot_preserves_multicard_id():
    rows = normalize_product_identity_snapshot({
        "cards": [{
            "nmID": 615105045,
            "imtID": 631725304,
            "title": "Вертикальная полка",
            "vendorCode": "ABC",
            "subjectID": 2532,
        }]
    }, observed_at="2026-09-24T10:28:38Z")
    assert rows[0]["nm_id"] == 615105045
    assert rows[0]["imt_id"] == 631725304
    assert rows[0]["subject_id"] == 2532
    assert rows[0]["resolution_status"] == "resolved_current"
    assert rows[0]["observed_at"] == "2026-09-24T10:28:38Z"


def test_xls_evidence_conversion_types_can_be_recovered_from_current_imt_id():
    rows = [
        {"campaign_id": 33650945, "nm_id": 404071811},
        {"campaign_id": 33650945, "nm_id": 615105045},
        {"campaign_id": 33650945, "nm_id": 713641223},
        {"campaign_id": 33650945, "nm_id": 1465123096},
    ]
    enriched = enrich_product_attribution(
        rows,
        advertised_nm_ids_by_campaign={33650945: {404071811}},
        imt_id_by_nm={
            404071811: 631725304,
            615105045: 631725304,
            713641223: 631725304,
            1465123096: 3956687619,
        },
        observed_at="2026-09-24T10:28:38Z",
    )
    assert [row["conversion_type_current"] for row in enriched] == [
        "direct", "multicard", "multicard", "associated",
    ]
    assert enriched[1]["multicard_id_current"] == 631725304
    assert enriched[3]["multicard_id_current"] == 3956687619
    assert enriched[0]["conversion_type_quality_flags"] == ["current_snapshot_not_event_time"]


def test_search_cluster_cpm_preserves_provider_metrics():
    rows = normalize_search_cluster_daily({
        "items": [{
            "advertId": 101,
            "nmId": 777,
            "dailyStats": [{
                "date": "2026-09-01",
                "stat": {
                    "normQuery": "лазер",
                    "views": 100,
                    "clicks": 20,
                    "atbs": 5,
                    "orders": 2,
                    "shks": 2,
                    "spend": 50.5,
                    "avgPos": 3.2,
                    "ctr": 20,
                    "cpc": 2.525,
                    "cpm": 505,
                },
            }],
        }]
    }, payment_type_by_campaign={101: "cpm"})
    row = rows[0]
    assert row["views"] == 100
    assert row["ctr"] == 20.0
    assert row["cpm"] == 505.0
    assert row["quality_flags"] == []


def test_search_cluster_cpc_keeps_unavailable_metrics_null_not_zero():
    rows = normalize_search_cluster_daily({
        "items": [{
            "advertId": 101,
            "nmId": 777,
            "dailyStats": [{
                "date": "2026-09-01",
                "stat": {
                    "normQuery": "лазер",
                    "views": 999,
                    "clicks": 20,
                    "atbs": 5,
                    "orders": 2,
                    "shks": 2,
                    "spend": 50.5,
                    "avgPos": 3.2,
                    "ctr": 99,
                    "cpc": 2.525,
                    "cpm": 999,
                },
            }],
        }]
    }, payment_type_by_campaign={101: "cpc"})
    row = rows[0]
    assert row["views"] is None
    assert row["ctr"] is None
    assert row["cpm"] is None
    assert row["cpc"] == 2.525
    assert row["quality_flags"] == ["cpc_views_ctr_cpm_not_available"]


def test_unresolved_identity_never_becomes_associated_by_guess():
    enriched = enrich_product_attribution(
        [{"campaign_id": 1, "nm_id": 222}],
        advertised_nm_ids_by_campaign={1: {111}},
        imt_id_by_nm={111: 1000, 222: None},
        observed_at="2026-09-24T10:28:38Z",
    )
    assert enriched[0]["conversion_type_current"] == "unknown"
    assert enriched[0]["multicard_id_current"] is None
