from __future__ import annotations

from datetime import date

from core.ozon_current_archive import parse_snapshot, stable_key_quality
from core.ozon_final_archive import (
    STABLE_KEY,
    annual_location,
    closed_months,
    coverage_location,
    enrich_realization_row,
    month_period,
    monthly_location,
    parse_final_coverage,
    serialize_final_coverage,
)
from core.ozon_current_archive import serialize_snapshot


def test_closed_months_excludes_open_month():
    assert closed_months(2026, date(2026, 9, 24)) == tuple(range(1, 9))
    assert closed_months(2025, date(2026, 9, 24)) == tuple(range(1, 13))


def test_realization_row_is_enriched_with_archive_identity_and_join_fields():
    row = {
        "row_number": 7,
        "item": {"sku": 123, "offer_id": "ABC"},
        "order": {"posting_number": "P-1", "created_date": "2026-08-12"},
        "seller_price_per_instance": 999,
    }
    value = enrich_realization_row(row, year=2026, month=8)
    assert value["report_year"] == 2026
    assert value["report_month"] == "2026-08"
    assert value["row_number"] == 7
    assert value["posting_number"] == "P-1"
    assert value["order_created_date"] == "2026-08-12"
    assert value["sku"] == 123
    assert value["offer_id"] == "ABC"


def test_final_stable_key_allows_same_provider_row_number_in_different_months():
    rows = [
        enrich_realization_row({"row_number": 1}, year=2026, month=7),
        enrich_realization_row({"row_number": 1}, year=2026, month=8),
    ]
    quality = stable_key_quality(rows, STABLE_KEY)
    assert quality == {
        "rows": 2,
        "unique_stable_keys": 2,
        "duplicate_stable_key_rows": 0,
        "incomplete_stable_key_rows": 0,
    }


def test_final_snapshot_roundtrip_preserves_raw_provider_row():
    rows = [
        enrich_realization_row(
            {
                "row_number": 1,
                "item": {"sku": 321, "offer_id": "OFFER"},
                "order": {"posting_number": "POST-1", "created_date": "2026-08-01"},
                "delivery_commission": {"quantity": 1, "amount": 100.5},
            },
            year=2026,
            month=8,
        )
    ]
    raw = serialize_snapshot(rows, stable_key=STABLE_KEY)
    fields, parsed = parse_snapshot(raw)
    assert fields[:3] == list(STABLE_KEY)
    assert len(parsed) == 1
    assert parsed[0]["report_month"] == "2026-08"
    assert parsed[0]["posting_number"] == "POST-1"
    assert '"delivery_commission"' in parsed[0]["_raw_json"]


def test_final_coverage_roundtrip_and_paths():
    payload = serialize_final_coverage([
        {
            "marketplace": "ozon",
            "cabinet": "ozon_laser_master",
            "dataset": "ozon_final_realization",
            "period": "2026-08",
            "report_year": 2026,
            "report_month": "2026-08",
            "stable_key": "report_year+report_month+row_number",
            "rows": 670,
            "monthly_file": "ozon_laser_master__realization__2026-08.csv",
            "monthly_file_id": "file-id",
            "bytes": 1234,
            "sha256": "a" * 64,
            "status": "COMPLETE",
            "refreshed_at_utc": "2026-09-24T00:00:00+00:00",
        }
    ])
    rows = parse_final_coverage(payload)
    assert rows[0]["period"] == "2026-08"
    assert rows[0]["rows"] == "670"

    monthly_parts, monthly_name = monthly_location("ozon_laser_master", 2026, 8)
    assert monthly_parts[-2:] == ["FINAL", "monthly_source"]
    assert monthly_name == "ozon_laser_master__realization__2026-08.csv"

    annual_parts, annual_name = annual_location("ozon_laser_master", 2026)
    assert annual_parts[-2:] == ["FINAL", "annual"]
    assert annual_name == "ozon_laser_master__realization__2026.csv"

    coverage_parts, coverage_name = coverage_location("ozon_laser_master", 2026)
    assert coverage_parts[-1] == "FINAL"
    assert coverage_name == "final_coverage_registry.csv"
