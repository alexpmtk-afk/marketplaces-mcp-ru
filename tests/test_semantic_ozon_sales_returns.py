import asyncio
import hashlib

import pytest

from core.ozon_current_archive import serialize_snapshot
from core.ozon_final_archive import STABLE_KEY, serialize_final_coverage
from core.semantic_ozon_sales_returns import (
    SemanticOzonFinalSalesReturnsError,
    execute_ozon_final_sales_returns_question,
    requested_ozon_sales_returns,
)


class FakeItem:
    def __init__(self, file_id):
        self.id = file_id


class FakeStore:
    def __init__(self, files):
        self.files = dict(files)
        self.paths = []

    async def ensure_folder_path(self, parts):
        path = "/".join(parts)
        self.paths.append(path)
        return path

    async def download_named(self, parent, name):
        raw = self.files.get((parent, name))
        if raw is None:
            return None, None
        return FakeItem("id-" + name), raw


def build_store(*, corrupt_hash=False):
    rows = [
        {
            "report_year": 2026,
            "report_month": "2026-08",
            "row_number": 1,
            "delivery_commission": {"quantity": 2, "amount": 200, "total": 180},
            "return_commission": {},
            "item": {"sku": 1, "offer_id": "A"},
            "order": {"posting_number": "P1"},
            "posting_number": "P1",
            "seller_price_per_instance": 100,
        },
        {
            "report_year": 2026,
            "report_month": "2026-08",
            "row_number": 2,
            "delivery_commission": {"quantity": 1, "amount": 100, "total": 90},
            "return_commission": {"quantity": 1, "amount": 100, "total": 85},
            "item": {"sku": 2, "offer_id": "B"},
            "order": {"posting_number": "P2"},
            "posting_number": "P2",
            "seller_price_per_instance": 100,
        },
    ]
    raw = serialize_snapshot(rows, stable_key=STABLE_KEY)
    sha = hashlib.sha256(raw).hexdigest()
    if corrupt_hash:
        sha = "0" * 64
    coverage = serialize_final_coverage([
        {
            "marketplace": "ozon",
            "cabinet": "ozon_laser_master",
            "dataset": "ozon_final_realization",
            "period": "2026-08",
            "report_year": 2026,
            "report_month": "2026-08",
            "stable_key": "+".join(STABLE_KEY),
            "rows": 2,
            "monthly_file": "ozon_laser_master__realization__2026-08.csv",
            "monthly_file_id": "file-id",
            "bytes": len(raw),
            "sha256": sha,
            "status": "COMPLETE",
            "refreshed_at_utc": "2026-09-24T00:00:00+00:00",
        }
    ])
    return FakeStore({
        (
            "База данных/Ozon/ozon_laser_master/2026/FINAL",
            "final_coverage_registry.csv",
        ): coverage,
        (
            "База данных/Ozon/ozon_laser_master/2026/FINAL/monthly_source",
            "ozon_laser_master__realization__2026-08.csv",
        ): raw,
    })


def test_parser_recognizes_sales_and_returns_and_amount():
    request = requested_ozon_sales_returns(
        "Сколько продаж и возвратов Ozon за август?"
    )
    assert request["metrics"] == ["SALES", "RETURNS"]
    assert request["requested_measure"] == "UNITS"

    amount = requested_ozon_sales_returns(
        "На какую сумму были продажи Ozon за август?"
    )
    assert amount["metrics"] == ["SALES"]
    assert amount["requested_measure"] == "AMOUNT_RUB"


def test_price_wording_is_not_misclassified_as_sales():
    assert requested_ozon_sales_returns(
        "Какая текущая цена продажи Ozon?"
    ) is None


def test_closed_month_units_are_separate_and_net_is_explicit():
    result = asyncio.run(execute_ozon_final_sales_returns_question(
        build_store(),
        question="Сколько продаж и возвратов Ozon за август 2026?",
        seller="ozon_laser_master",
    ))

    assert result["ok"] is True
    assert result["complete"] is True
    assert result["requested_metrics"] == ["SALES", "RETURNS"]
    assert result["sales_units"] == 3
    assert result["return_units"] == 1
    assert result["net_units"] == 2
    assert result["coverage"]["status"] == "FULL_COVERAGE"
    observations = {item["metric_id"]: item for item in result["metric_observations"]}
    assert observations["SALES"]["source_field"] == "delivery_commission.quantity"
    assert observations["RETURNS"]["source_field"] == "return_commission.quantity"


def test_single_sales_metric_returns_value():
    result = asyncio.run(execute_ozon_final_sales_returns_question(
        build_store(),
        question="Сколько продаж Ozon за август 2026?",
        seller="LaserMaster",
    ))
    assert result["ok"] is True
    assert result["metric_id"] == "SALES"
    assert result["value"] == 3
    assert result["unit"] == "UNITS"


def test_money_fails_closed_before_archive_read():
    store = FakeStore({})
    result = asyncio.run(execute_ozon_final_sales_returns_question(
        store,
        question="На какую сумму были продажи Ozon за август 2026?",
        seller="ozon_laser_master",
    ))
    assert result["ok"] is False
    assert result["code"] == "OZON_SALES_RETURNS_AMOUNT_NOT_APPROVED"
    assert store.paths == []


def test_open_month_fails_closed_without_using_final(monkeypatch):
    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            from datetime import datetime
            return datetime(2026, 9, 25, 12, 0, tzinfo=tz)

    import core.semantic_ozon_sales_returns as module
    monkeypatch.setattr(module, "datetime", FixedDateTime)

    store = FakeStore({})
    result = asyncio.run(execute_ozon_final_sales_returns_question(
        store,
        question="Сколько продаж Ozon за сентябрь 2026?",
        seller="ozon_laser_master",
    ))
    assert result["ok"] is False
    assert result["code"] == "OZON_CURRENT_SALES_RETURNS_NOT_APPROVED"
    assert store.paths == []


def test_partial_month_fails_closed():
    store = FakeStore({})
    result = asyncio.run(execute_ozon_final_sales_returns_question(
        store,
        question="Сколько продаж Ozon?",
        seller="ozon_laser_master",
        date_from="2026-08-10",
        date_to="2026-08-20",
    ))
    assert result["ok"] is False
    assert result["code"] == "OZON_FINAL_REQUIRES_FULL_MONTH"
    assert store.paths == []


def test_coverage_hash_mismatch_is_rejected():
    with pytest.raises(
        SemanticOzonFinalSalesReturnsError,
        match="hash does not match",
    ):
        asyncio.run(execute_ozon_final_sales_returns_question(
            build_store(corrupt_hash=True),
            question="Сколько возвратов Ozon за август 2026?",
            seller="ozon_laser_master",
        ))
