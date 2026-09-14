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
                "saleDt",
                "currency",
                "docTypeName",
                "quantity",
                "retailAmount",
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
            "saleDt": "",
            "currency": "RUB",
            "docTypeName": "",
            "quantity": "0",
            "retailAmount": "0",
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
            "saleDt": "",
            "currency": "RUB",
            "docTypeName": "",
            "quantity": "0",
            "retailAmount": "0",
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
            "saleDt": "",
            "currency": "RUB",
            "docTypeName": "",
            "quantity": "0",
            "retailAmount": "0",
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
            "saleDt": "",
            "currency": "USD",
            "docTypeName": "",
            "quantity": "0",
            "retailAmount": "0",
            "penalty": "3.00",
            "bonusTypeName": "Штраф C",
            "sellerOperName": "Штраф",
            "paidStorage": "1.00",
            "paidAcceptance": "0",
            "nmId": "111",
        },
        {
            "reportId": 101,
            "rrdId": 5,
            "reportType": 1,
            "rrDate": "2026-08-07",
            "saleDt": "2026-08-07T12:00:00Z",
            "currency": "RUB",
            "docTypeName": "Продажа",
            "quantity": "1",
            "retailAmount": "100.00",
            "penalty": "0",
            "bonusTypeName": "",
            "sellerOperName": "Продажа",
            "paidStorage": "0",
            "paidAcceptance": "0",
            "nmId": "111",
        },
        {
            "reportId": 101,
            "rrdId": 6,
            "reportType": 1,
            "rrDate": "2026-08-08",
            "saleDt": "2026-08-08T13:00:00Z",
            "currency": "RUB",
            "docTypeName": "Возврат",
            "quantity": "1",
            "retailAmount": "30.00",
            "penalty": "0",
            "bonusTypeName": "",
            "sellerOperName": "Возврат",
            "paidStorage": "0",
            "paidAcceptance": "0",
            "nmId": "111",
        },
        {
            "reportId": 101,
            "rrdId": 7,
            "reportType": 1,
            "rrDate": "2026-08-08",
            "saleDt": "2026-08-08T14:00:00Z",
            "currency": "RUB",
            "docTypeName": "Продажа",
            "quantity": "0",
            "retailAmount": "0",
            "penalty": "0",
            "bonusTypeName": "",
            "sellerOperName": "Компенсация скидки по программе лояльности",
            "paidStorage": "0",
            "paidAcceptance": "0",
            "nmId": "111",
        },
    ]


def test_execution_registry_approves_four_safe_capabilities():
    execution = load_semantic_execution()
    assert set(execution["executors"]) == {
        "penalties",
        "storage_charge",
        "acceptance_charge",
        "sale_and_return_operations",
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


def test_sales_and_returns_use_explicit_doc_type_subtraction():
    store = FakeStore(_registry_rows(full=True), _annual_rows())
    result = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Сколько было продаж за этот период?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
            nm_ids=[111],
        )
    )
    assert result["ok"] is True
    assert result["capability_id"] == "sale_and_return_operations"
    assert result["provenance"]["date_field"] == "saleDt"
    assert result["provenance"]["sum_rule"] == "sale_minus_return_by_doc_type"
    assert result["calculation"]["cross_currency_total"] is None
    assert result["calculation"]["sales_and_returns_by_currency"] == [
        {
            "currency": "RUB",
            "sale_amount": 100.0,
            "return_amount": 30.0,
            "net_sales_amount": 70.0,
            "sale_units": 1.0,
            "return_units": 1.0,
            "net_sales_units": 0.0,
            "sale_rows": 2,
            "return_rows": 1,
        }
    ]


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


def test_sales_sql_uses_sale_date_and_explicit_operation_buckets():
    execution = load_semantic_execution()
    sql = build_semantic_archive_sql(
        cabinet="wb_novokshenov",
        executor=execution["executors"]["sale_and_return_operations"],
        date_from="2026-08-05",
        date_to="2026-08-12",
        nm_ids=[111],
    )
    assert '"saleDt"' in sql
    assert '"docTypeName"' in sql
    assert '"retailAmount"' in sql
    assert '"quantity"' in sql
    assert "'Продажа'" in sql
    assert "'Возврат'" in sql
