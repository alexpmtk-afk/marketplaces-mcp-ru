from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.archive_refresh_verify import verify_advertising_cabinet, verify_finance_cabinet
from core.archive_coverage import coverage_record, encode_registry
from core.wb_finance_archive import encode_csv, merge_registry


class FakeObject:
    def __init__(self, key: str, data: bytes):
        self.id = key
        self.name = key.rsplit("/", 1)[-1]
        self.size = len(data)


class FakeStore:
    def __init__(self):
        self.files: dict[str, bytes] = {}

    async def ensure_folder_path(self, parts):
        return "/".join(parts)

    async def download_named(self, folder, name):
        key = f"{folder}/{name}"
        data = self.files.get(key)
        if data is None:
            return None, None
        return FakeObject(key, data), data


def _finance_path(cabinet="wb_demo", year=2026):
    folder = f"База данных/WB/{cabinet}/{year}/finance/weekly/main"
    return f"{folder}/{cabinet}__weekly_main__{year}.csv"


def _registry_path():
    return "app/registry/reports_registry.csv"


def _finance_registry(report_id=101, rows=1, date_to="2026-09-13", cabinet="wb_demo"):
    record = {
        "marketplace": "wb",
        "cabinet": cabinet,
        "dataset": "wb_weekly_finance_main",
        "report_type": 1,
        "report_id": report_id,
        "date_from": "2026-09-07",
        "date_to": date_to,
        "logical_week_from": "2026-09-07",
        "logical_week_to": "2026-09-13",
        "create_date": "2026-09-14",
        "year": 2026,
        "annual_file": f"{cabinet}__weekly_main__2026.csv",
        "storage_object_key": "obj",
        "rows": rows,
        "bytes": 100,
        "sha256": "a" * 64,
        "status": "COMPLETE",
        "ingested_at_utc": "2026-09-14T00:00:00+00:00",
    }
    return merge_registry(None, [record])


def test_finance_verifier_reports_max_dates_and_no_duplicate_keys():
    store = FakeStore()
    store.files[_finance_path()] = encode_csv(
        ["reportId", "rrdId", "dateTo", "rrDate"],
        [{"reportId": 101, "rrdId": 1, "dateTo": "2026-09-13", "rrDate": "2026-09-13"}],
    )
    store.files[_registry_path()] = _finance_registry()

    result = asyncio.run(verify_finance_cabinet(store, cabinet="wb_demo", year=2026))
    assert result["ok"] is True
    assert result["initialized"] is True
    assert result["max_canonical_date"] == "2026-09-13"
    assert result["max_registry_coverage_date"] == "2026-09-13"
    assert result["duplicate_stable_key_rows"] == 0
    assert result["incomplete_stable_key_rows"] == 0
    assert result["missing_registry_report_ids_with_rows"] == []


def test_finance_verifier_fails_on_duplicate_stable_key():
    store = FakeStore()
    store.files[_finance_path()] = encode_csv(
        ["reportId", "rrdId", "dateTo"],
        [
            {"reportId": 101, "rrdId": 1, "dateTo": "2026-09-13"},
            {"reportId": 101, "rrdId": 1, "dateTo": "2026-09-13"},
        ],
    )
    store.files[_registry_path()] = _finance_registry(rows=2)

    result = asyncio.run(verify_finance_cabinet(store, cabinet="wb_demo", year=2026))
    assert result["ok"] is False
    assert result["duplicate_stable_key_rows"] == 1


def test_finance_verifier_fails_when_registry_nonempty_report_is_missing_from_csv():
    store = FakeStore()
    store.files[_finance_path()] = encode_csv(
        ["reportId", "rrdId", "dateTo"],
        [{"reportId": 100, "rrdId": 1, "dateTo": "2026-09-06"}],
    )
    store.files[_registry_path()] = _finance_registry(report_id=101, rows=10)

    result = asyncio.run(verify_finance_cabinet(store, cabinet="wb_demo", year=2026))
    assert result["ok"] is False
    assert result["missing_registry_report_ids_with_rows"] == [101]


def test_advertising_verifier_fails_closed_when_archive_was_never_initialized():
    store = FakeStore()
    result = asyncio.run(verify_advertising_cabinet(store, cabinet="wb_dmitrieva", year=2026))
    assert result["ok"] is False
    assert result["initialized"] is False


def test_advertising_verifier_accepts_initialized_roster_with_unique_keys():
    store = FakeStore()
    folder = "База данных/WB/wb_dmitrieva/2026/advertising/state/campaign_roster"
    name = "wb_dmitrieva__ads_campaign_roster_snapshots__2026.csv"
    store.files[f"{folder}/{name}"] = (
        "\ufeffobserved_at;campaign_id;campaign_type;status;change_time;fullstats_eligible\r\n"
        "2026-09-16T10:00:00+00:00;123;8;9;;true\r\n"
    ).encode("utf-8")
    record = coverage_record(
        marketplace="wb",
        cabinet="wb_dmitrieva",
        dataset="ads_campaign_roster_snapshots",
        operation_id="wb_get_adv_promotion_count",
        date_from="2026-01-01",
        date_to="2026-09-15",
        scope={},
        annual_file=name,
        rows=1,
        bytes_count=len(store.files[f"{folder}/{name}"]),
        sha256="b" * 64,
    )
    store.files["app/registry/dataset_coverage_registry.csv"] = encode_registry([record])

    result = asyncio.run(verify_advertising_cabinet(store, cabinet="wb_dmitrieva", year=2026))
    assert result["initialized"] is True
    assert result["datasets"]["ads_campaign_roster_snapshots"]["duplicate_stable_key_rows"] == 0
