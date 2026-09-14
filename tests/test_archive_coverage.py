from __future__ import annotations

from core.archive_coverage import (
    coverage_record,
    evaluate_coverage,
    merge_coverage_registry,
    parse_registry,
    request_key,
)


def _record(*, campaign_ids=(1, 2), date_from="2026-09-01", date_to="2026-09-07"):
    return coverage_record(
        marketplace="wb",
        cabinet="wb_novokshenov",
        dataset="ads_campaign_daily",
        operation_id="wb_get_adv_fullstats",
        date_from=date_from,
        date_to=date_to,
        scope={"campaign_ids": list(campaign_ids)},
        annual_file="wb_novokshenov__ads_campaign_daily__2026.csv",
        rows=14,
        bytes_count=1200,
        sha256="a" * 64,
        completed_at_utc="2026-09-14T20:00:00+00:00",
    )


def test_request_key_is_deterministic_and_scope_order_independent():
    first = request_key(
        marketplace="wb",
        cabinet="wb_novokshenov",
        dataset="ads_campaign_daily",
        operation_id="wb_get_adv_fullstats",
        date_from="2026-09-01",
        date_to="2026-09-07",
        scope={"campaign_ids": [2, 1, 2]},
    )
    second = request_key(
        marketplace="wb",
        cabinet="wb_novokshenov",
        dataset="ads_campaign_daily",
        operation_id="wb_get_adv_fullstats",
        date_from="2026-09-01",
        date_to="2026-09-07",
        scope={"campaign_ids": [1, 2]},
    )
    assert first == second
    assert len(first) == 64


def test_coverage_record_preserves_bounded_scope_and_commit_metadata():
    record = _record()
    assert record["status"] == "COMPLETE"
    assert record["scope_kind"] == "campaign_ids"
    assert record["scope_count"] == 2
    assert record["annual_file"].endswith("__2026.csv")
    assert record["quality_status"] == "PASS"
    assert record["request_key"]


def test_registry_merge_is_idempotent_by_request_key():
    record = _record()
    first = merge_coverage_registry(None, [record])
    second = merge_coverage_registry(first, [record])
    rows = parse_registry(second)
    assert len(rows) == 1
    assert rows[0]["request_key"] == record["request_key"]


def test_registry_rejects_non_complete_commit():
    record = _record()
    record["status"] = "PARTIAL"
    try:
        merge_coverage_registry(None, [record])
    except ValueError as exc:
        assert "COMPLETE" in str(exc)
    else:
        raise AssertionError("non-COMPLETE registry record must fail")


def test_full_coverage_requires_all_planned_requests_and_canonical_file():
    one = _record(campaign_ids=(1, 2), date_from="2026-09-01", date_to="2026-09-07")
    two = _record(campaign_ids=(3,), date_from="2026-09-01", date_to="2026-09-07")
    registry = merge_coverage_registry(None, [one, two])
    result = evaluate_coverage(
        expected_request_keys=[one["request_key"], two["request_key"]],
        registry_data=registry,
        marketplace="wb",
        cabinet="wb_novokshenov",
        dataset="ads_campaign_daily",
        canonical_file_present=True,
    )
    assert result["status"] == "FULL_COVERAGE"
    assert result["safe_for_archive_only_query"] is True

    missing_file = evaluate_coverage(
        expected_request_keys=[one["request_key"], two["request_key"]],
        registry_data=registry,
        marketplace="wb",
        cabinet="wb_novokshenov",
        dataset="ads_campaign_daily",
        canonical_file_present=False,
    )
    assert missing_file["status"] == "ARCHIVE_FILE_MISSING"
    assert missing_file["safe_for_archive_only_query"] is False


def test_partial_coverage_fails_closed():
    one = _record(campaign_ids=(1, 2))
    two_key = request_key(
        marketplace="wb",
        cabinet="wb_novokshenov",
        dataset="ads_campaign_daily",
        operation_id="wb_get_adv_fullstats",
        date_from="2026-09-01",
        date_to="2026-09-07",
        scope={"campaign_ids": [3]},
    )
    registry = merge_coverage_registry(None, [one])
    result = evaluate_coverage(
        expected_request_keys=[one["request_key"], two_key],
        registry_data=registry,
        marketplace="wb",
        cabinet="wb_novokshenov",
        dataset="ads_campaign_daily",
        canonical_file_present=True,
    )
    assert result["status"] == "PARTIAL_COVERAGE"
    assert result["missing_requests"] == 1
    assert result["safe_for_archive_only_query"] is False


def test_quality_failure_does_not_count_as_complete_coverage():
    record = _record()
    record["quality_status"] = "FAIL"
    registry = merge_coverage_registry(None, [record])
    result = evaluate_coverage(
        expected_request_keys=[record["request_key"]],
        registry_data=registry,
        marketplace="wb",
        cabinet="wb_novokshenov",
        dataset="ads_campaign_daily",
        canonical_file_present=True,
    )
    assert result["status"] == "NO_COVERAGE"
    assert result["safe_for_archive_only_query"] is False
