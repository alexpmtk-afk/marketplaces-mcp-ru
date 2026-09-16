import pytest

from core.archive_coverage import coverage_record, encode_registry, request_key
from core.archive_refresh import REFRESH_CONTRACTS
from core.archive_refresh_advertising import _filter_plan, request_fully_covered
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


@pytest.mark.asyncio
async def test_filter_plan_keeps_new_tail_but_skips_exact_committed_history():
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


def test_archive_refresh_import_installs_coverage_aware_queue_handlers():
    assert REFRESH_CONTRACTS["advertising"].coverage_model == "bounded_request_coverage_registry"
    assert WBAdvertisingArchiveJobQueue._plan_step.__module__ == "core.archive_refresh_advertising"
    assert WBAdvertisingArchiveJobQueue._plan_clusters_step.__module__ == "core.archive_refresh_advertising"
