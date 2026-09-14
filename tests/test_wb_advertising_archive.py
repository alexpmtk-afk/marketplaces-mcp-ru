from __future__ import annotations

from core.wb_advertising_archive import (
    canonical_location,
    coverage_registry_location,
    eligible_fullstats_campaign_ids,
    merge_annual_dataset,
    normalize_campaign_info_snapshot,
    normalize_campaign_roster,
    normalize_expenses,
    normalize_payments,
    parse_csv,
)


def test_campaign_roster_flattens_groups_and_filters_fullstats_eligibility():
    rows = normalize_campaign_roster({
        "adverts": [
            {
                "type": 9,
                "status": 8,
                "count": 2,
                "advert_list": [
                    {"advertId": 10, "changeTime": "2026-09-01T10:00:00+03:00"},
                    {"advertId": 11, "changeTime": "2026-09-01T11:00:00+03:00"},
                ],
            },
            {
                "type": 8,
                "status": 9,
                "count": 1,
                "advert_list": [
                    {"advertId": 12, "changeTime": "2026-09-01T12:00:00+03:00"},
                ],
            },
            {
                "type": 8,
                "status": 11,
                "count": 1,
                "advert_list": [{"advertId": 13, "changeTime": None}],
            },
            {
                "type": 8,
                "status": 7,
                "count": 1,
                "advert_list": [{"advertId": 14, "changeTime": "2026-08-01T12:00:00+03:00"}],
            },
        ],
        "all": 5,
    }, observed_at="2026-09-14T20:10:00Z")
    assert len(rows) == 5
    assert eligible_fullstats_campaign_ids(rows) == [12, 13, 14]
    rejected = next(row for row in rows if row["campaign_id"] == 10)
    assert rejected["fullstats_eligible"] is False
    paused = next(row for row in rows if row["campaign_id"] == 13)
    assert paused["change_time"] is None


def test_expenses_keep_null_time_and_do_not_assume_upd_num_unique():
    rows = normalize_expenses([
        {
            "updNum": 0,
            "updTime": "2026-09-01T10:00:00+03:00",
            "updSum": 24.10,
            "advertId": 100,
            "campName": "A",
            "advertType": 8,
            "paymentType": "Баланс",
            "advertStatus": 9,
        },
        {
            "updNum": 0,
            "updTime": None,
            "updSum": "107.00",
            "advertId": 200,
            "campName": "B",
            "advertType": 9,
            "paymentType": "Счет",
            "advertStatus": 11,
        },
    ], request_date_from="2026-09-01", request_date_to="2026-09-02")
    assert rows[0]["upd_sum"] == "24.1"
    assert rows[1]["upd_sum"] == "107.00"
    assert rows[1]["upd_time"] is None
    assert rows[0]["event_fingerprint"] != rows[1]["event_fingerprint"]


def test_payments_use_provider_id_and_exact_money_string():
    rows = normalize_payments([
        {
            "id": 1036666,
            "date": "2026-09-01T09:06:47Z",
            "sum": "600.00",
            "type": 0,
            "statusId": 1,
            "cardStatus": "",
        }
    ], request_date_from="2026-09-01", request_date_to="2026-09-01")
    assert rows[0]["event_key"] == "id:1036666"
    assert rows[0]["sum"] == "600.00"
    assert rows[0]["card_status"] is None


def test_payments_without_provider_id_get_stable_fingerprint():
    source = [{"date": "2026-09-01", "sum": 10, "type": 3, "statusId": 1, "cardStatus": "succeeded"}]
    first = normalize_payments(source, request_date_from="2026-09-01", request_date_to="2026-09-01")
    second = normalize_payments(source, request_date_from="2026-09-01", request_date_to="2026-09-01")
    assert first[0]["event_key"].startswith("fp:")
    assert first[0]["event_key"] == second[0]["event_key"]


def test_campaign_snapshot_requires_observation_time_and_preserves_raw_provider_row():
    rows = normalize_campaign_info_snapshot([
        {"advertId": 77, "status": 9, "payment_type": "cpm", "name": "Campaign", "type": 8, "extra": {"x": 1}}
    ], observed_at="2026-09-14T20:15:00Z")
    assert rows[0]["campaign_id"] == 77
    assert rows[0]["observed_at"] == "2026-09-14T20:15:00Z"
    assert '"extra":{"x":1}' in rows[0]["raw_json"]


def test_canonical_locations_match_drive_scaffold_and_filename_contract():
    folder, name = canonical_location("wb_novokshenov", 2026, "ads_campaign_daily")
    assert folder == [
        "База данных", "WB", "wb_novokshenov", "2026",
        "advertising", "stats", "campaign_daily",
    ]
    assert name == "wb_novokshenov__ads_campaign_daily__2026.csv"
    assert coverage_registry_location() == (["app", "registry"], "dataset_coverage_registry.csv")


def test_campaign_daily_merge_is_idempotent_by_date_campaign():
    rows = [
        {"date": "2026-09-01", "campaign_id": 10, "spend": "1.00"},
        {"date": "2026-09-02", "campaign_id": 10, "spend": "2.00"},
    ]
    first, stats1 = merge_annual_dataset("ads_campaign_daily", None, rows)
    second, stats2 = merge_annual_dataset("ads_campaign_daily", first, rows)
    _, parsed = parse_csv(second)
    assert stats1["total_rows"] == 2
    assert stats2["added_rows"] == 0
    assert len(parsed) == 2


def test_expense_retry_is_deduped_but_distinct_same_upd_num_survives():
    rows = normalize_expenses([
        {"updNum": 0, "updTime": "2026-09-01T10:00:00Z", "updSum": 10, "advertId": 1, "campName": "A", "advertType": 8, "paymentType": "Баланс", "advertStatus": 9},
        {"updNum": 0, "updTime": None, "updSum": 20, "advertId": 2, "campName": "B", "advertType": 8, "paymentType": "Счет", "advertStatus": 11},
    ], request_date_from="2026-09-01", request_date_to="2026-09-01")
    first, _ = merge_annual_dataset("ads_expenses", None, rows)
    second, stats = merge_annual_dataset("ads_expenses", first, rows)
    _, parsed = parse_csv(second)
    assert stats["added_rows"] == 0
    assert len(parsed) == 2


def test_roster_snapshot_merge_keeps_same_campaign_at_different_observation_times():
    row1 = normalize_campaign_roster({
        "adverts": [{"type": 8, "status": 9, "advert_list": [{"advertId": 10, "changeTime": "2026-09-01"}]}]
    }, observed_at="2026-09-14T10:00:00Z")
    row2 = normalize_campaign_roster({
        "adverts": [{"type": 8, "status": 11, "advert_list": [{"advertId": 10, "changeTime": "2026-09-14"}]}]
    }, observed_at="2026-09-14T11:00:00Z")
    first, _ = merge_annual_dataset("ads_campaign_roster_snapshots", None, row1)
    second, stats = merge_annual_dataset("ads_campaign_roster_snapshots", first, row2)
    _, parsed = parse_csv(second)
    assert stats["added_rows"] == 1
    assert len(parsed) == 2
