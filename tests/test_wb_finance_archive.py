from __future__ import annotations

import asyncio

from core import wb_finance_archive as archive


def test_month_boundary_fragments_map_to_same_logical_week():
    assert archive.logical_week("2026-08-31") == ("2026-08-31", "2026-09-06")
    assert archive.logical_week("2026-09-01") == ("2026-08-31", "2026-09-06")
    assert archive.logical_week("2026-09-06") == ("2026-08-31", "2026-09-06")


def test_normalize_main_fragments_rejects_buyout():
    got = archive.normalize_main_fragments([
        {"reportId": 1, "dateFrom": "2026-08-31", "dateTo": "2026-08-31", "createDate": "2026-09-01", "reportType": 1},
        {"reportId": 2, "dateFrom": "2026-08-31", "dateTo": "2026-08-31", "createDate": "2026-09-01", "reportType": 2},
        {"reportId": 3, "dateFrom": "2026-09-01", "dateTo": "2026-09-06", "createDate": "2026-09-07", "reportType": 1},
        {"reportId": 4, "dateFrom": "2026-09-01", "dateTo": "2026-09-06", "createDate": "2026-09-07", "reportType": 2},
    ])
    assert [x.report_id for x in got] == [1, 3]
    assert {(x.logical_week_from, x.logical_week_to) for x in got} == {("2026-08-31", "2026-09-06")}


def test_annual_csv_is_excel_friendly_and_idempotent_with_schema_union():
    rows = [
        {"reportId": 10, "rrdId": 100, "reportType": 1, "subjectName": "Кобуры", "forPay": "10.50"},
        {"reportId": 10, "rrdId": 101, "reportType": 1, "subjectName": "Чехлы", "forPay": "20.00"},
    ]
    first, stats1 = archive.merge_annual_csv(None, rows)
    assert first.startswith(b"\xef\xbb\xbf")
    assert b";" in first.splitlines()[0]
    assert stats1["added_rows"] == 2
    assert stats1["total_rows"] == 2

    second, stats2 = archive.merge_annual_csv(first, [rows[0], {**rows[1], "newField": "new"}])
    fields, decoded = archive.parse_csv_bytes(second)
    assert stats2["added_rows"] == 0
    assert len(decoded) == 2
    assert "newField" in fields
    assert decoded[0]["subjectName"] == "Кобуры"


def test_registry_tracks_complete_report_ids_per_cabinet():
    payload = archive.merge_registry(None, [{
        "marketplace": "wb",
        "cabinet": "wb_novokshenov",
        "dataset": archive.DATASET,
        "report_type": 1,
        "report_id": 123,
        "date_from": "2026-09-01",
        "date_to": "2026-09-06",
        "logical_week_from": "2026-08-31",
        "logical_week_to": "2026-09-06",
        "create_date": "2026-09-07",
        "year": 2026,
        "annual_file": "x.csv",
        "drive_file_id": "drive-1",
        "rows": 5,
        "bytes": 10,
        "sha256": "abc",
        "status": "COMPLETE",
        "ingested_at_utc": "2026-09-13T00:00:00+00:00",
    }])
    assert archive.registry_complete_ids(payload, "wb_novokshenov") == {123}
    assert archive.registry_complete_ids(payload, "wb_dmitrieva") == set()


class DummyLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeDriveFile:
    def __init__(self, file_id, name, size):
        self.id = file_id
        self.name = name
        self.size = size


class FakeStore:
    def __init__(self):
        self.files = {}

    async def ensure_folder_path(self, parts):
        return "/".join(parts)

    async def find_child(self, parent, name, **kwargs):
        value = self.files.get((parent, name))
        if value is None:
            return None
        return FakeDriveFile("id:" + parent + "/" + name, name, len(value))

    async def download_named(self, parent, name):
        value = self.files.get((parent, name))
        if value is None:
            return None, None
        return FakeDriveFile("id:" + parent + "/" + name, name, len(value)), value

    async def upload_bytes(self, parent, name, data, **kwargs):
        self.files[(parent, name)] = data
        return FakeDriveFile("id:" + parent + "/" + name, name, len(data))

    async def status(self):
        return {"configured": True, "reachable": True}


class FakeManager(archive.WBFinanceArchiveManager):
    def __init__(self, store):
        super().__init__(object(), store)
        self.downloads = []

    async def _list_main_fragments(self, cabinet, year):
        return [
            archive.ReportFragment(101, "2026-08-31", "2026-08-31", "2026-09-01", 1, "2026-08-31", "2026-09-06"),
            archive.ReportFragment(102, "2026-09-01", "2026-09-06", "2026-09-07", 1, "2026-08-31", "2026-09-06"),
        ]

    async def _download_fragment(self, cabinet, report_id):
        self.downloads.append(report_id)
        return [{"reportId": report_id, "rrdId": report_id * 10, "reportType": 1, "subjectName": "Товар"}]


def test_update_builds_one_annual_file_and_second_run_is_noop(monkeypatch):
    monkeypatch.setattr(archive, "ArchiveLock", DummyLock)
    store = FakeStore()
    manager = FakeManager(store)

    first = asyncio.run(manager.update(year=2026, cabinets=("wb_novokshenov",), max_reports_per_cabinet=10))
    assert first["complete"] is True
    assert manager.downloads == [101, 102]
    assert first["cabinets"][0]["total_rows"] == 2
    assert first["cabinets"][0]["downloaded_report_ids"] == [101, 102]

    second = asyncio.run(manager.update(year=2026, cabinets=("wb_novokshenov",), max_reports_per_cabinet=10))
    assert second["complete"] is True
    assert manager.downloads == [101, 102]
    assert second["cabinets"][0]["downloaded_report_ids"] == []
    assert second["cabinets"][0]["total_rows"] == 2
