from __future__ import annotations

import asyncio

import pytest

from core.archive_tools import _query_year
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
