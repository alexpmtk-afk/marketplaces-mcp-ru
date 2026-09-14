from __future__ import annotations

import asyncio
from datetime import date

from core.semantic_archive import (
    build_semantic_archive_sql,
    evaluate_registry_coverage,
    execute_semantic_archive_question,
    load_semantic_execution,
)
from core.wb_finance_archive import REGISTRY_FIELDS, encode_csv


class FakeFile:
    def __init__(self, file_id: str, name: str, size: int):
        self.id = file_id
        self.name = name
        self.size = size


class FakeStore:
    def __init__(self, registry_rows, annual_rows):
        self.registry_data = encode_csv(REGISTRY_FIELDS, registry_rows)
        self.annual_data = encode_csv(
            [
                "reportId",
                "rrdId",
                "reportType",
                "rrDate",
                "currency",
                "penalty",
                "bonusTypeName",
                "sellerOperName",
                "paidStorage",
                "paidAcceptance",
                "nmId",
            ],
            annual_rows,
        )

    async def ensure_folder_path(self, parts):
        return "/".join(parts)

    async def download_named(self, folder, name):
        if name == "reports_registry.csv":
            return FakeFile("registry", name, len(self.registry_data)), self.registry_data
        if name == "wb_novokshenov__weekly_main__2026.csv":
            return FakeFile("annual", name, len(self.annual_data)), self.annual_data
        return None, None


def _registry_rows(full: bool = True):
    rows = [
        {
            "marketplace": "wb",
            "cabinet": "wb_novokshenov",
            "dataset": "wb_weekly_finance_main",
            "report_type": 1,
            "report_id": 101,
            "date_from": "2026-08-03",
            "date_to": "2026-08-09",
            "logical_week_from": "2026-08-03",
            "logical_week_to": "2026-08-09",
            "create_date": "2026-08-10",
            "year": 2026,
            "annual_file": "wb_novokshenov__weekly_main__2026.csv",
            "storage_object_key": "x",
            "rows": 10,
            "bytes": 100,
            "sha256": "a",
            "status": "COMPLETE",
            "ingested_at_utc": "2026-09-14T00:00:00Z",
        }
    ]
    if full:
        rows.append(
            {
                "marketplace": "wb",
                "cabinet": "wb_novokshenov",
                "dataset": "wb_weekly_finance_main",
                "report_type": 1,
                "report_id": 102,
                "date_from": "2026-08-10",
                "date_to": "2026-08-16",
                "logical_week_from": "2026-08-10",
                "logical_week_to": "2026-08-16",
                "create_date": "2026-08-17",
                "year": 2026,
                "annual_file": "wb_novokshenov__weekly_main__2026.csv",
                "storage_object_key": "x",
                "rows": 10,
                "bytes": 100,
                "sha256": "b",
                "status": "COMPLETE",
                "ingested_at_utc": "2026-09-14T00:00:00Z",
            }
        )
    return rows


def _annual_rows():
    return [
        {
            "reportId": 101,
            "rrdId": 1,
            "reportType": 1,
            "rrDate": "2026-08-05",
            "currency": "RUB",
            "penalty": "100.50",
            "bonusTypeName": "Штраф A",
            "sellerOperName": "Штраф",
            "paidStorage": "10.00",
            "paidAcceptance": "2.50",
            "nmId": "111",
        },
        {
            "reportId": 101,
            "rrdId": 2,
            "reportType": 1,
            "rrDate": "2026-08-06",
            "currency": "RUB",
            "penalty": "49.50",
            "bonusTypeName": "Штраф B",
            "sellerOperName": "Штраф",
            "paidStorage": "5.25",
            "paidAcceptance": "0",
            "nmId": "222",
        },
        {
            "reportId": 102,
            "rrdId": 3,
            "reportType": 1,
            "rrDate": "2026-08-12",
            "currency": "RUB",
            "penalty": "0",
            "bonusTypeName": "",
            "sellerOperName": "Хранение",
            "paidStorage": "20.00",
            "paidAcceptance": "7.50",
            "nmId": "111",
        },
        {
            "reportId": 102,
            "rrdId": 4,
            "reportType": 1,
            "rrDate": "2026-08-15",
            "currency": "USD",
            "penalty": "3.00",
            "bonusTypeName": "Штраф C",
            "sellerOperName": "Штраф",
            "paidStorage": "1.00",
            "paidAcceptance": "0",
            "nmId": "111",
        },
    ]


def test_execution_registry_only_approves_three_safe_capabilities():
    execution = load_semantic_execution()
    assert set(execution["executors"]) == {
        "penalties",
        "storage_charge",
        "acceptance_charge",
    }
    assert execution["policy"]["require_full_coverage"] is True
    assert execution["policy"]["cross_currency_sum_forbidden"] is True


def test_coverage_merges_complete_report_fragments():
    data = encode_csv(REGISTRY_FIELDS, _registry_rows(full=True))
    coverage = evaluate_registry_coverage(
        data,
        cabinet="wb_novokshenov",
        date_from="2026-08-05",
        date_to="2026-08-12",
    )
    assert coverage["status"] == "FULL_COVERAGE"
    assert coverage["gaps"] == []
    assert coverage["archive_years"] == [2026]


def test_coverage_gap_fails_closed():
    data = encode_csv(REGISTRY_FIELDS, _registry_rows(full=False))
    coverage = evaluate_registry_coverage(
        data,
        cabinet="wb_novokshenov",
        date_from="2026-08-05",
        date_to="2026-08-12",
    )
    assert coverage["status"] == "PARTIAL_COVERAGE"
    assert coverage["gaps"] == [["2026-08-10", "2026-08-12"]]


def test_penalties_execute_only_after_full_coverage_and_keep_currencies_separate():
    store = FakeStore(_registry_rows(full=True), _annual_rows())
    result = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Сколько штрафов и за что их начислили?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-15",
        )
    )
    assert result["ok"] is True
    assert result["capability_id"] == "penalties"
    assert result["coverage"]["status"] == "FULL_COVERAGE"
    assert result["calculation"]["cross_currency_total"] is None
    totals = {row["currency"]: row["amount"] for row in result["calculation"]["totals_by_currency"]}
    assert totals == {"RUB": 150.0, "USD": 3.0}
    assert len(result["calculation"]["breakdown"]) == 3


def test_storage_and_acceptance_sum_as_reported_with_product_filter():
    store = FakeStore(_registry_rows(full=True), _annual_rows())
    storage = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Сколько списали за хранение?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
            nm_ids=[111],
        )
    )
    assert storage["ok"] is True
    assert storage["capability_id"] == "storage_charge"
    assert storage["calculation"]["totals_by_currency"] == [
        {"currency": "RUB", "amount": 30.0, "operation_rows": 2}
    ]

    acceptance = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Сколько списали за приёмку?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
            nm_ids=[111],
        )
    )
    assert acceptance["ok"] is True
    assert acceptance["calculation"]["totals_by_currency"] == [
        {"currency": "RUB", "amount": 10.0, "operation_rows": 2}
    ]


def test_recognized_sales_remains_blocked_without_approved_formula():
    store = FakeStore(_registry_rows(full=True), _annual_rows())
    result = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Сколько было продаж за этот период?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
        )
    )
    assert result["ok"] is False
    assert result["error"] == "semantic_execution_not_approved"
    assert result["execution_allowed"] is False


def test_coverage_gap_prevents_archive_calculation():
    store = FakeStore(_registry_rows(full=False), _annual_rows())
    result = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Сколько списали за хранение?",
            seller="wb_novokshenov",
            date_from=date(2026, 8, 5),
            date_to=date(2026, 8, 12),
        )
    )
    assert result["ok"] is False
    assert result["error"] == "coverage_gap"
    assert result["coverage"]["status"] == "PARTIAL_COVERAGE"


def test_sql_builder_uses_only_registered_fields_and_date_axis():
    execution = load_semantic_execution()
    sql = build_semantic_archive_sql(
        cabinet="wb_novokshenov",
        executor=execution["executors"]["storage_charge"],
        date_from="2026-08-05",
        date_to="2026-08-12",
        nm_ids=[111, 222],
    )
    assert '"paidStorage"' in sql
    assert '"rrDate"' in sql
    assert '"currency"' in sql
    assert '"nmId"' in sql
    assert "2026-08-05" in sql and "2026-08-12" in sql
