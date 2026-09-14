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
                "deliveryAmount",
                "returnAmount",
                "deliveryService",
                "rebillLogisticCost",
                "deduction",
                "additionalPayment",
                "vw",
                "vwNds",
                "acquiringFee",
                "paymentProcessing",
                "acquiringBank",
                "deliveryMethod",
                "officeName",
                "dlvPrc",
                "fixTariffDateFrom",
                "fixTariffDateTo",
                "warehouseLogisticsCoeff",
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
            "vw": "100.00",
            "vwNds": "20.00",
            "acquiringFee": "10.00",
            "paymentProcessing": "Компенсация платёжных услуг",
            "acquiringBank": "Вайлдберриз Банк",
            "deliveryMethod": "FBS, (МГТ)",
            "officeName": "Воронеж МП МП",
            "dlvPrc": "1.25",
            "fixTariffDateFrom": "2026-07-01",
            "fixTariffDateTo": "2026-09-28",
            "warehouseLogisticsCoeff": "1.15",
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
            "vw": "30.00",
            "vwNds": "6.00",
            "acquiringFee": "3.00",
            "paymentProcessing": "Компенсация платёжных услуг",
            "acquiringBank": "Вайлдберриз Банк",
            "deliveryMethod": "FBS, (МГТ)",
            "officeName": "Воронеж МП МП",
            "dlvPrc": "1.25",
            "fixTariffDateFrom": "2026-07-01",
            "fixTariffDateTo": "2026-09-28",
            "warehouseLogisticsCoeff": "1.15",
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
            "deliveryMethod": "FBW, (МГТ, коробка)",
            "officeName": "Электросталь",
            "dlvPrc": "1.10",
            "fixTariffDateFrom": "2026-07-15",
            "fixTariffDateTo": "2026-09-12",
            "warehouseLogisticsCoeff": "1.05",
            "nmId": "111",
        },
        {
            "reportId": 101,
            "rrdId": 8,
            "reportType": 1,
            "rrDate": "2026-08-06",
            "saleDt": "2026-08-06T09:00:00Z",
            "currency": "RUB",
            "docTypeName": "",
            "quantity": "0",
            "retailAmount": "0",
            "bonusTypeName": "К клиенту при продаже",
            "sellerOperName": "Логистика",
            "deliveryAmount": "1",
            "returnAmount": "0",
            "deliveryService": "50.00",
            "rebillLogisticCost": "0",
            "deduction": "0",
            "additionalPayment": "0",
            "nmId": "111",
        },
        {
            "reportId": 101,
            "rrdId": 9,
            "reportType": 1,
            "rrDate": "2026-08-07",
            "saleDt": "2026-08-07T09:00:00Z",
            "currency": "RUB",
            "docTypeName": "",
            "quantity": "0",
            "retailAmount": "0",
            "bonusTypeName": "Корректировка",
            "sellerOperName": "Коррекция логистики",
            "deliveryAmount": "0",
            "returnAmount": "1",
            "deliveryService": "-10.00",
            "rebillLogisticCost": "0",
            "deduction": "0",
            "additionalPayment": "0",
            "nmId": "111",
        },
        {
            "reportId": 102,
            "rrdId": 10,
            "reportType": 1,
            "rrDate": "2026-08-11",
            "saleDt": "2026-08-11T00:00:00Z",
            "currency": "RUB",
            "docTypeName": "",
            "quantity": "2",
            "retailAmount": "0",
            "bonusTypeName": "",
            "sellerOperName": "Возмещение издержек по перевозке/по складским операциям с товаром",
            "deliveryAmount": "0",
            "returnAmount": "0",
            "deliveryService": "0",
            "rebillLogisticCost": "7.00",
            "deduction": "0",
            "additionalPayment": "0",
            "nmId": "0",
        },
        {
            "reportId": 102,
            "rrdId": 11,
            "reportType": 1,
            "rrDate": "2026-08-11",
            "saleDt": "2026-08-11T10:00:00Z",
            "currency": "RUB",
            "docTypeName": "",
            "quantity": "0",
            "retailAmount": "0",
            "bonusTypeName": "Оказание услуг «WB Продвижение»",
            "sellerOperName": "Удержание",
            "deliveryAmount": "0",
            "returnAmount": "0",
            "deliveryService": "0",
            "rebillLogisticCost": "0",
            "deduction": "25.00",
            "additionalPayment": "0",
            "nmId": "0",
        },
        {
            "reportId": 102,
            "rrdId": 12,
            "reportType": 1,
            "rrDate": "2026-08-12",
            "saleDt": "2026-08-12T10:00:00Z",
            "currency": "RUB",
            "docTypeName": "",
            "quantity": "0",
            "retailAmount": "0",
            "bonusTypeName": "Корректировка вознаграждения",
            "sellerOperName": "Корректировка",
            "deliveryAmount": "0",
            "returnAmount": "0",
            "deliveryService": "0",
            "rebillLogisticCost": "0",
            "deduction": "0",
            "additionalPayment": "5.00",
            "nmId": "0",
        },
    ]


def test_execution_registry_approves_ten_safe_capabilities():
    execution = load_semantic_execution()
    assert set(execution["executors"]) == {
        "penalties",
        "storage_charge",
        "acceptance_charge",
        "sale_and_return_operations",
        "logistics",
        "deductions_and_adjustments",
        "commission_and_wb_reward",
        "acquiring_and_payment_processing",
        "observed_fulfillment_method",
        "warehouse_tariff_context",
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


def test_logistics_keeps_delivery_rebill_and_counts_separate():
    store = FakeStore(_registry_rows(full=True), _annual_rows())
    result = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Сколько стоила логистика за период?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
        )
    )
    assert result["ok"] is True
    assert result["capability_id"] == "logistics"
    assert result["provenance"]["sum_rule"] == "separate_components_as_reported_no_cross_component_netting"
    assert result["calculation"]["cross_component_total"] is None
    assert result["calculation"]["components_by_currency"] == [
        {
            "currency": "RUB",
            "delivery_service_amount": 40.0,
            "rebilled_transport_warehouse_cost": 7.0,
            "delivery_count": 1.0,
            "return_logistics_count": 1.0,
            "operation_rows": 3,
        }
    ]
    operations = {row["operation"] for row in result["calculation"]["breakdown"]}
    assert "Логистика" in operations
    assert "Коррекция логистики" in operations
    assert "Возмещение издержек по перевозке/по складским операциям с товаром" in operations


def test_deductions_never_net_with_wb_reward_adjustments():
    store = FakeStore(_registry_rows(full=True), _annual_rows())
    result = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Какие удержания были за период?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
        )
    )
    assert result["ok"] is True
    assert result["capability_id"] == "deductions_and_adjustments"
    assert result["calculation"]["cross_component_total"] is None
    assert result["calculation"]["components_by_currency"] == [
        {
            "currency": "RUB",
            "deduction_amount": 25.0,
            "wb_reward_adjustment_amount": 5.0,
            "operation_rows": 2,
        }
    ]
    assert "seller payout" not in result["provenance"]["semantics"].lower()


def test_wb_reward_uses_reported_money_fields_not_percentages():
    store = FakeStore(_registry_rows(full=True), _annual_rows())
    result = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Какая сумма комиссии WB за период?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
            nm_ids=[111],
        )
    )
    assert result["ok"] is True
    assert result["capability_id"] == "commission_and_wb_reward"
    assert result["provenance"]["sum_rule"] == "sale_minus_return_by_doc_type_for_registered_components"
    assert result["calculation"]["components_by_currency"] == [
        {
            "currency": "RUB",
            "sale_wb_reward_without_vat": 100.0,
            "return_wb_reward_without_vat": 30.0,
            "net_wb_reward_without_vat": 70.0,
            "sale_wb_reward_vat": 20.0,
            "return_wb_reward_vat": 6.0,
            "net_wb_reward_vat": 14.0,
            "sale_wb_reward_including_vat": 120.0,
            "return_wb_reward_including_vat": 36.0,
            "net_wb_reward_including_vat": 84.0,
            "sale_rows": 1,
            "return_rows": 1,
        }
    ]
    assert "commissionPercent" not in result["provenance"]["component_fields"].values()
    assert "kvw" not in result["provenance"]["component_fields"].values()


def test_weekly_acquiring_is_explicitly_preliminary_and_split_by_payment_context():
    store = FakeStore(_registry_rows(full=True), _annual_rows())
    result = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Сколько было эквайринга за период?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
            nm_ids=[111],
        )
    )
    assert result["ok"] is True
    assert result["capability_id"] == "acquiring_and_payment_processing"
    assert result["provenance"]["data_class"] == "PRELIMINARY_WEEKLY_PAYMENT_ACCEPTANCE_WITHHOLDING"
    assert result["calculation"]["components_by_currency"] == [
        {
            "currency": "RUB",
            "sale_weekly_payment_processing_fee": 10.0,
            "return_weekly_payment_processing_fee": 3.0,
            "net_weekly_payment_processing_fee": 7.0,
            "sale_rows": 1,
            "return_rows": 1,
        }
    ]
    assert result["calculation"]["breakdown"] == [
        {
            "currency": "RUB",
            "paymentProcessing": "Компенсация платёжных услуг",
            "acquiringBank": "Вайлдберриз Банк",
            "sale_weekly_payment_processing_fee": 10.0,
            "return_weekly_payment_processing_fee": 3.0,
            "net_weekly_payment_processing_fee": 7.0,
            "sale_rows": 1,
            "return_rows": 1,
        }
    ]


def test_historical_fulfillment_returns_observed_methods_and_warehouse_only():
    store = FakeStore(_registry_rows(full=True), _annual_rows())
    result = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="По какой схеме отгружался товар за период?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
            nm_ids=[111],
        )
    )
    assert result["ok"] is True
    assert result["capability_id"] == "observed_fulfillment_method"
    assert result["semantic_status"] == "AVAILABLE_WITH_LIMITATION"
    assert result["provenance"]["data_class"] == "HISTORICAL_OBSERVED_FULFILLMENT"
    assert result["provenance"]["current_state_inference_forbidden"] is True
    assert result["calculation"]["historical_only"] is True
    assert result["calculation"]["current_configuration_confirmed"] is False
    assert result["calculation"]["distinct_values"] == [
        "FBS, (МГТ)",
        "FBW, (МГТ, коробка)",
    ]
    assert result["calculation"]["observations"] == [
        {
            "value": "FBS, (МГТ)",
            "officeName": "Воронеж МП МП",
            "observation_rows": 2,
            "first_observed_date": "2026-08-07",
            "last_observed_date": "2026-08-08",
        },
        {
            "value": "FBW, (МГТ, коробка)",
            "officeName": "Электросталь",
            "observation_rows": 1,
            "first_observed_date": "2026-08-08",
            "last_observed_date": "2026-08-08",
        },
    ]


def test_current_fulfillment_never_falls_back_to_historical_observations():
    store = FakeStore(_registry_rows(full=True), _annual_rows())
    result = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Какая схема отгрузки сейчас у товара?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
            nm_ids=[111],
        )
    )
    assert result["ok"] is False
    assert result["error"] == "semantic_source_not_executable"
    assert result["resolution"]["concept_id"] == "current_fulfillment_configuration"


def test_historical_tariff_context_returns_applied_coefficients_only():
    store = FakeStore(_registry_rows(full=True), _annual_rows())
    result = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Какой коэффициент склада применялся к товару за период?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
            nm_ids=[111],
        )
    )
    assert result["ok"] is True
    assert result["capability_id"] == "warehouse_tariff_context"
    assert result["semantic_status"] == "AVAILABLE_WITH_LIMITATION"
    assert result["provenance"]["data_class"] == "HISTORICAL_APPLIED_WAREHOUSE_TARIFF_CONTEXT"
    assert result["provenance"]["current_state_inference_forbidden"] is True
    assert result["calculation"]["historical_only"] is True
    assert result["calculation"]["current_configuration_confirmed"] is False
    assert result["calculation"]["distinct_values"] == ["1.10", "1.25"]
    assert result["calculation"]["observations"] == [
        {
            "value": "1.25",
            "officeName": "Воронеж МП МП",
            "fixTariffDateFrom": "2026-07-01",
            "fixTariffDateTo": "2026-09-28",
            "warehouseLogisticsCoeff": "1.15",
            "observation_rows": 2,
            "first_observed_date": "2026-08-07",
            "last_observed_date": "2026-08-08",
        },
        {
            "value": "1.10",
            "officeName": "Электросталь",
            "fixTariffDateFrom": "2026-07-15",
            "fixTariffDateTo": "2026-09-12",
            "warehouseLogisticsCoeff": "1.05",
            "observation_rows": 1,
            "first_observed_date": "2026-08-08",
            "last_observed_date": "2026-08-08",
        },
    ]


def test_current_tariff_never_falls_back_to_historical_coefficients():
    store = FakeStore(_registry_rows(full=True), _annual_rows())
    result = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Какой актуальный коэффициент склада у товара?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
            nm_ids=[111],
        )
    )
    assert result["ok"] is False
    assert result["error"] == "semantic_source_not_executable"
    assert result["resolution"]["route_id"] == "current_warehouse_tariff"


def test_rate_and_final_acquiring_questions_fail_closed_instead_of_using_money_executor():
    store = FakeStore(_registry_rows(full=True), _annual_rows())
    rate = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Какой процент комиссии WB был за период?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
        )
    )
    assert rate["ok"] is False
    assert rate["error"] == "semantic_source_not_executable"
    assert rate["resolution"]["route_id"] == "commission_rate_not_money"

    final_acquiring = asyncio.run(
        execute_semantic_archive_question(
            store,
            question="Какие окончательные издержки на приём платежей за период?",
            seller="wb_novokshenov",
            date_from="2026-08-05",
            date_to="2026-08-12",
        )
    )
    assert final_acquiring["ok"] is False
    assert final_acquiring["error"] == "semantic_source_not_executable"
    assert final_acquiring["resolution"]["route_id"] == "final_acquiring_expenses"


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


def test_component_sql_keeps_logistics_fields_separate():
    execution = load_semantic_execution()
    sql = build_semantic_archive_sql(
        cabinet="wb_novokshenov",
        executor=execution["executors"]["logistics"],
        date_from="2026-08-05",
        date_to="2026-08-12",
    )
    assert 'SUM(COALESCE(TRY_CAST(NULLIF(TRIM("deliveryService")' in sql
    assert 'SUM(COALESCE(TRY_CAST(NULLIF(TRIM("rebillLogisticCost")' in sql
    assert '"deliveryAmount"' in sql
    assert '"returnAmount"' in sql
    assert '"rrDate"' in sql


def test_reward_and_acquiring_sql_use_only_explicit_money_fields():
    execution = load_semantic_execution()
    reward_sql = build_semantic_archive_sql(
        cabinet="wb_novokshenov",
        executor=execution["executors"]["commission_and_wb_reward"],
        date_from="2026-08-05",
        date_to="2026-08-12",
    )
    assert '"vw"' in reward_sql
    assert '"vwNds"' in reward_sql
    assert '"commissionPercent"' not in reward_sql
    assert '"kvw"' not in reward_sql
    assert '"saleDt"' in reward_sql
    assert '"docTypeName"' in reward_sql

    acquiring_sql = build_semantic_archive_sql(
        cabinet="wb_novokshenov",
        executor=execution["executors"]["acquiring_and_payment_processing"],
        date_from="2026-08-05",
        date_to="2026-08-12",
    )
    assert '"acquiringFee"' in acquiring_sql
    assert '"paymentProcessing"' in acquiring_sql
    assert '"acquiringBank"' in acquiring_sql
    assert '"saleDt"' in acquiring_sql
    assert '"docTypeName"' in acquiring_sql


def test_fulfillment_sql_is_observation_only_and_uses_historical_date_axis():
    execution = load_semantic_execution()
    sql = build_semantic_archive_sql(
        cabinet="wb_novokshenov",
        executor=execution["executors"]["observed_fulfillment_method"],
        date_from="2026-08-05",
        date_to="2026-08-12",
        nm_ids=[111],
    )
    assert '"deliveryMethod"' in sql
    assert '"officeName"' in sql
    assert '"rrDate"' in sql
    assert '"nmId"' in sql
    assert "COUNT(*) AS observation_rows" in sql
    assert "MIN(" in sql and "MAX(" in sql
    assert '"currency"' not in sql
    assert '"retailAmount"' not in sql


def test_tariff_context_sql_is_observation_only_and_never_reads_money_fields():
    execution = load_semantic_execution()
    sql = build_semantic_archive_sql(
        cabinet="wb_novokshenov",
        executor=execution["executors"]["warehouse_tariff_context"],
        date_from="2026-08-05",
        date_to="2026-08-12",
        nm_ids=[111],
    )
    assert '"dlvPrc"' in sql
    assert '"fixTariffDateFrom"' in sql
    assert '"fixTariffDateTo"' in sql
    assert '"warehouseLogisticsCoeff"' in sql
    assert '"officeName"' in sql
    assert '"rrDate"' in sql
    assert '"nmId"' in sql
    assert "COUNT(*) AS observation_rows" in sql
    assert '"currency"' not in sql
    assert '"retailAmount"' not in sql
