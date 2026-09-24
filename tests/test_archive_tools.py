from __future__ import annotations

import asyncio

import pytest

from core.archive_tools import _database_update_scope, _query_year
from core.wb_finance_archive import encode_csv


class FakeFile:
    def __init__(self, file_id, name, size):
        self.id = file_id
        self.name = name
        self.size = size


class FakeStore:
    async def ensure_folder_path(self, parts):
        return "/".join(parts)

    async def download_named(self, folder, name):
        cabinet = name.split("__", 1)[0]
        if cabinet not in {"wb_novokshenov", "wb_dmitrieva"}:
            return None, None
        data = encode_csv(
            ["reportId", "rrdId", "reportType", "forPay"],
            [{
                "reportId": 1 if cabinet == "wb_novokshenov" else 2,
                "rrdId": 10,
                "reportType": 1,
                "forPay": "12.50",
            }],
        )
        return FakeFile("x", name, len(data)), data


def test_archive_query_exposes_union_view():
    result = asyncio.run(_query_year(
        FakeStore(),
        2026,
        "SELECT archive_cabinet, count(*) AS n FROM wb_all GROUP BY 1 ORDER BY 1",
    ))
    assert result["ok"] is True
    assert result["row_count"] == 2
    assert [row[0] for row in result["rows"]] == ["wb_dmitrieva", "wb_novokshenov"]


def test_archive_query_blocks_external_readers():
    with pytest.raises(ValueError):
        asyncio.run(_query_year(FakeStore(), 2026, "SELECT * FROM read_csv('/etc/passwd')"))



def test_database_update_scope_requires_marketplace_seller_and_family():
    result = _database_update_scope()
    assert result["ok"] is False
    assert result["error"] == "clarification_required"
    assert result["missing_fields"] == ["marketplace", "seller", "dataset_family"]
    assert result["no_jobs_queued"] is True
    assert len(result["questions"]) == 3


def test_database_update_scope_is_precise_for_one_wb_advertising_cabinet():
    result = _database_update_scope(
        marketplace="wb",
        seller="wb_novokshenov",
        dataset_family="advertising",
    )
    assert result["ok"] is True
    assert result["marketplace"] == "wb"
    assert result["selected_sellers"] == ["wb_novokshenov"]
    assert result["dataset_families"] == ["advertising"]
    assert result["explicit_all_sellers"] is False
    assert result["explicit_all_families"] is False
    assert result["no_jobs_queued"] is True


def test_database_update_scope_expands_all_only_when_explicit():
    result = _database_update_scope(
        marketplace="wb",
        seller="all",
        dataset_family="all",
    )
    assert result["ok"] is True
    assert result["selected_sellers"] == [
        "wb_dmitrieva",
        "wb_laser_master",
        "wb_novokshenov",
    ]
    assert result["dataset_families"] == ["advertising", "finance"]
    assert result["explicit_all_sellers"] is True
    assert result["explicit_all_families"] is True


def test_database_update_scope_accepts_russian_advertising_alias():
    result = _database_update_scope(
        marketplace="wb",
        seller="wb_dmitrieva",
        dataset_family="реклама",
    )
    assert result["ok"] is True
    assert result["dataset_families"] == ["advertising"]


def test_database_update_scope_rejects_unknown_marketplace_without_queueing():
    result = _database_update_scope(
        marketplace="unknown",
        seller="all",
        dataset_family="all",
    )
    assert result["ok"] is False
    assert result["error"] == "unsupported_marketplace"
    assert result["no_jobs_queued"] is True
