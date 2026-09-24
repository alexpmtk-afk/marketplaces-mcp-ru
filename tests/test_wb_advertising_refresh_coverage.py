import asyncio
from datetime import date

import core.archive_refresh_advertising as refresh_ads
from core.archive_coverage import coverage_record, encode_registry, request_key
from core.archive_refresh import REFRESH_CONTRACTS
from core.archive_refresh_advertising import (
    _filter_plan,
    request_fully_covered,
    request_requires_correction_refresh,
    split_refresh_period,
)
from core.wb_advertising_archive_queue import WBAdvertisingArchiveJobQueue


FULLSTATS = {
    "kind": "fullstats",
    "operation_id": "wb_get_adv_fullstats",
    "datasets": ["ads_campaign_daily", "ads_product_daily"],
    "date_from": "2026-08-01",
    "date_to": "2026-08-31",
    "scope": {"campaign_ids": [10, 20]},
}


def _key(dataset: str, request: dict) -> str:
    return request_key(
        marketplace="wb",
        cabinet="wb_laser_master",
        dataset=dataset,
        operation_id=request["operation_id"],
        date_from=request["date_from"],
        date_to=request["date_to"],
        scope=request["scope"],
    )


def test_multi_dataset_request_is_skipped_only_when_every_output_is_covered():
    campaign_key = _key("ads_campaign_daily", FULLSTATS)
    product_key = _key("ads_product_daily", FULLSTATS)

    assert not request_fully_covered(
        cabinet="wb_laser_master",
        request=FULLSTATS,
        complete_by_dataset={
            "ads_campaign_daily": {campaign_key},
            "ads_product_daily": set(),
        },
    )
    assert request_fully_covered(
        cabinet="wb_laser_master",
        request=FULLSTATS,
        complete_by_dataset={
            "ads_campaign_daily": {campaign_key},
            "ads_product_daily": {product_key},
        },
    )


def test_current_campaign_snapshot_is_never_suppressed_by_old_coverage():
    request = {
        "kind": "campaign_info",
        "operation_id": "wb_get_api_advert_adverts",
        "datasets": ["ads_campaign_snapshots"],
        "date_from": "2026-01-01",
        "date_to": "2026-09-15",
        "scope": {"campaign_ids": [10]},
    }
    key = _key("ads_campaign_snapshots", request)
    assert not request_fully_covered(
        cabinet="wb_laser_master",
        request=request,
        complete_by_dataset={"ads_campaign_snapshots": {key}},
    )


class _CoverageStore:
    def __init__(self, raw: bytes):
        self.raw = raw

    async def ensure_folder_path(self, parts):
        assert list(parts) == ["app", "registry"]
        return "registry-folder"

    async def download_named(self, folder, name):
        assert folder == "registry-folder"
        assert name == "dataset_coverage_registry.csv"
        return object(), self.raw


def test_filter_plan_keeps_new_tail_but_skips_exact_committed_history():
    async def run() -> None:
        records = [
            coverage_record(
                marketplace="wb",
                cabinet="wb_laser_master",
                dataset=dataset,
                operation_id=FULLSTATS["operation_id"],
                date_from=FULLSTATS["date_from"],
                date_to=FULLSTATS["date_to"],
                scope=FULLSTATS["scope"],
                annual_file=f"{dataset}.csv",
                rows=100,
                bytes_count=1000,
                sha256="a" * 64,
            )
            for dataset in FULLSTATS["datasets"]
        ]
        queue = object.__new__(WBAdvertisingArchiveJobQueue)
        queue.store = _CoverageStore(encode_registry(records))

        new_tail = {
            **FULLSTATS,
            "date_from": "2026-09-01",
            "date_to": "2026-09-15",
        }
        pending, skipped = await _filter_plan(
            queue,
            cabinet="wb_laser_master",
            plan=[FULLSTATS, new_tail],
        )

        assert skipped == 1
        assert pending == [new_tail]

    asyncio.run(run())


def test_archive_refresh_import_installs_coverage_aware_queue_handlers():
    assert REFRESH_CONTRACTS["advertising"].coverage_model == "bounded_request_coverage_registry"
    assert WBAdvertisingArchiveJobQueue._plan_step.__module__ == "core.archive_refresh_advertising"
    assert WBAdvertisingArchiveJobQueue._plan_clusters_step.__module__ == "core.archive_refresh_advertising"



def test_recent_closed_request_is_forced_into_correction_refresh_window():
    request = {
        **FULLSTATS,
        "date_from": "2026-09-17",
        "date_to": "2026-09-23",
    }
    assert request_requires_correction_refresh(
        request,
        yesterday=date(2026, 9, 23),
        window_days=7,
    )


def test_old_request_is_not_forced_into_correction_refresh_window():
    assert not request_requires_correction_refresh(
        FULLSTATS,
        yesterday=date(2026, 9, 23),
        window_days=7,
    )


def test_filter_plan_replays_recent_covered_window_for_late_provider_corrections(monkeypatch):
    async def run() -> None:
        request = {
            **FULLSTATS,
            "date_from": "2026-09-17",
            "date_to": "2026-09-23",
        }
        records = [
            coverage_record(
                marketplace="wb",
                cabinet="wb_laser_master",
                dataset=dataset,
                operation_id=request["operation_id"],
                date_from=request["date_from"],
                date_to=request["date_to"],
                scope=request["scope"],
                annual_file=f"{dataset}.csv",
                rows=100,
                bytes_count=1000,
                sha256="a" * 64,
            )
            for dataset in request["datasets"]
        ]
        queue = object.__new__(WBAdvertisingArchiveJobQueue)
        queue.store = _CoverageStore(encode_registry(records))
        monkeypatch.setattr(refresh_ads, "_moscow_yesterday", lambda: date(2026, 9, 23))

        pending, skipped = await _filter_plan(
            queue,
            cabinet="wb_laser_master",
            plan=[request],
        )

        assert skipped == 0
        assert pending == [request]

    asyncio.run(run())



def test_split_refresh_period_makes_exact_seven_day_current_tail():
    stable, recent = split_refresh_period(
        "2026-01-01",
        "2026-09-23",
        yesterday=date(2026, 9, 23),
        window_days=7,
    )
    assert stable == ("2026-01-01", "2026-09-16")
    assert recent == ("2026-09-17", "2026-09-23")


def test_split_refresh_period_does_not_resegment_old_closed_year():
    stable, recent = split_refresh_period(
        "2025-01-01",
        "2025-12-31",
        yesterday=date(2026, 9, 23),
        window_days=7,
    )
    assert stable == ("2025-01-01", "2025-12-31")
    assert recent is None
