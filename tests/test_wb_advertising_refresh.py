from core.wb_advertising_archive import merge_annual_dataset, parse_csv


def test_repeat_provider_fetch_refreshes_same_campaign_day_without_duplicate():
    first, _ = merge_annual_dataset(
        "ads_campaign_daily",
        None,
        [{"date": "2026-09-01", "campaign_id": 10, "clicks": 5, "spend": "100"}],
    )
    refreshed, stats = merge_annual_dataset(
        "ads_campaign_daily",
        first,
        [{"date": "2026-09-01", "campaign_id": 10, "clicks": 7, "spend": "120"}],
    )
    _, rows = parse_csv(refreshed)
    assert len(rows) == 1
    assert stats["added_rows"] == 0
    assert rows[0]["clicks"] == "7"
    assert rows[0]["spend"] == "120"


def test_replaying_identical_provider_row_remains_idempotent():
    source = [{"date": "2026-09-01", "campaign_id": 10, "clicks": 7, "spend": "120"}]
    first, _ = merge_annual_dataset("ads_campaign_daily", None, source)
    second, stats = merge_annual_dataset("ads_campaign_daily", first, source)
    _, rows = parse_csv(second)
    assert len(rows) == 1
    assert stats["added_rows"] == 0
    assert first == second
