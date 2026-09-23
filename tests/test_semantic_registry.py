from copy import deepcopy

import pytest

from core.semantic_registry import (
    SemanticRegistryError,
    get_capability,
    get_dataset,
    get_field,
    load_semantic_registry,
    require_available_capability,
    validate_semantic_registry,
)


def test_weekly_finance_has_semantics_for_every_physical_field():
    dataset = get_dataset("wb_weekly_finance_main")
    assert dataset["schema_status"] == "OFFICIAL_HELP_AUDITED_92_FIELDS_2026_09_14"
    assert dataset["field_count"] == 92
    assert len(dataset["fields"]) == 92
    assert len(dataset["field_catalog"]) == 92
    assert set(dataset["fields"]) == set(dataset["field_catalog"])
    assert dataset["row_dedup_key"] == ["reportId", "rrdId"]
    assert dataset["coverage"]["archive_route_requirement"] == "FULL_COVERAGE"
    assert "agencyVat" not in dataset["fields"]
    assert "agencyVat" not in dataset["field_catalog"]


def test_official_audit_corrects_fixed_coefficient_semantics():
    field = get_field("wb_weekly_finance_main", "dlvPrc")
    assert "момент планирования" in field["meaning_ru"]
    limitations = " ".join(field["limitations"])
    assert "окончания срока фиксации" in limitations
    assert "Не считать dlvPrc фактически применённым" in limitations

    capability = get_capability("warehouse_tariff_context")
    guardrail = capability["guardrail"]
    assert "fixed when the supply was planned" in guardrail
    assert "not proof" in guardrail
    assert "current live tariff" in guardrail


def test_official_audit_corrects_legacy_promo_and_payout_service_fields():
    legacy = get_field("wb_weekly_finance_main", "isKgvpV2")
    assert legacy["role"] == "legacy"
    assert "изменения коэффициента" in legacy["meaning_ru"]
    assert "акции со сниженной комиссией" in legacy["meaning_ru"]
    assert any("булев" in item.lower() for item in legacy["limitations"])

    payout = get_field("wb_weekly_finance_main", "paymentSchedule")
    assert payout["role"] == "measure"
    assert "Вывести сейчас" in payout["meaning_ru"]
    assert any("график выплат" in item.lower() for item in payout["limitations"])


def test_cashback_money_fields_remain_distinct_after_official_audit():
    cashback = get_field("wb_weekly_finance_main", "cashbackAmount")
    compensation = get_field("wb_weekly_finance_main", "cashbackDiscount")
    participation = get_field("wb_weekly_finance_main", "cashbackCommissionChange")
    assert "начисленные покупателю баллы" in cashback["meaning_ru"]
    assert "потратил на оплату товара" in compensation["meaning_ru"]
    assert "Стоимость участия продавца" in participation["meaning_ru"]


def test_delivery_method_answers_only_observed_historical_fulfillment():
    capability = get_capability("observed_fulfillment_method")
    assert "deliveryMethod" in capability["fields"]
    assert capability["status"] == "AVAILABLE_WITH_LIMITATION"
    assert "current" in capability["guardrail"].lower()

    field = get_field("wb_weekly_finance_main", "deliveryMethod")
    assert field["role"] == "dimension"
    assert any("истор" in item.lower() for item in field["limitations"])


def test_order_date_cannot_be_used_as_complete_orders_metric():
    field = get_field("wb_weekly_finance_main", "orderDt")
    assert any("полного потока заказов" in item for item in field["limitations"])

    registry = load_semantic_registry()
    missing = registry["not_covered"]["all_orders_placed"]
    assert missing["required_source_id"] == "wb_order_feed"
    assert registry["sources"]["wb_order_feed"]["database_presence"] == "NOT_IN_DATABASE"

    with pytest.raises(SemanticRegistryError):
        require_available_capability("all_orders_placed", registry)


def test_penalty_semantics_include_amount_and_reason():
    capability = require_available_capability("penalties")
    assert "penalty" in capability["fields"]
    assert "bonusTypeName" in capability["fields"]
    assert "sellerOperName" in capability["fields"]


def test_weekly_report_can_answer_charge_but_not_detailed_storage_driver():
    storage = require_available_capability("storage_charge")
    assert "paidStorage" in storage["fields"]
    assert storage["status"] == "AVAILABLE_WITH_LIMITATION"

    registry = load_semantic_registry()
    detailed = registry["not_covered"]["detailed_storage_calculation"]
    assert detailed["required_source_id"] == "wb_paid_storage_report"
    assert (
        registry["sources"]["wb_paid_storage_report"]["database_presence"]
        == "NOT_IN_DATABASE"
    )


def test_missing_wb_and_ozon_sources_remain_reference_only():
    registry = load_semantic_registry()
    for source_id in (
        "wb_order_feed",
        "wb_paid_storage_report",
        "wb_acceptance_operations_report",
        "wb_stock_report",
        "ozon_reports",
    ):
        source = registry["sources"][source_id]
        assert source["database_presence"] == "NOT_IN_DATABASE"
        assert source["execution_status"] == "REFERENCE_ONLY"


def test_wb_advertising_source_is_now_canonical_archive_capability():
    registry = load_semantic_registry()
    source = registry["sources"]["wb_ads_data"]
    assert source["database_presence"] == "AVAILABLE_IN_CANONICAL_ARCHIVE"
    assert source["kind"] == "archive_dataset"
    assert source["dataset_id"] == "ads_campaign_daily"
    capability = require_available_capability("advertising_performance", registry)
    assert capability["status"] == "AVAILABLE_WITH_LIMITATION"


def test_validator_rejects_available_capability_backed_by_missing_source():
    registry = load_semantic_registry()
    unsafe = deepcopy(registry)
    unsafe["capabilities"]["penalties"]["source_id"] = "wb_stock_report"
    with pytest.raises(SemanticRegistryError):
        validate_semantic_registry(unsafe)


def test_validator_rejects_missing_field_semantics():
    registry = load_semantic_registry()
    unsafe = deepcopy(registry)
    del unsafe["datasets"]["wb_weekly_finance_main"]["field_catalog"]["penalty"]
    with pytest.raises(SemanticRegistryError):
        validate_semantic_registry(unsafe)


def test_all_92_weekly_fields_belong_to_at_least_one_semantic_capability():
    registry = load_semantic_registry()
    dataset = registry["datasets"]["wb_weekly_finance_main"]
    used = set()
    for capability in registry["capabilities"].values():
        if capability.get("source_id") == "wb_weekly_finance_main":
            used.update(capability.get("fields") or [])
    assert used == set(dataset["fields"])


def test_final_seven_weekly_fields_have_explicit_safe_semantics():
    legacy = require_available_capability("legacy_finance_diagnostics")
    assert set(legacy["fields"]) == {
        "salePercent",
        "productDiscountForReport",
        "sellerPromo",
        "supRatingUp",
        "isKgvpV2",
    }
    assert "historical" in legacy["guardrail"].lower()
    assert "current" in legacy["guardrail"].lower()

    payout = require_available_capability("payout_term_change_fee")
    assert "paymentSchedule" in payout["fields"]
    assert "Вывести сейчас" in payout["guardrail"]

    social = require_available_capability("social_certificate_payment")
    assert "paidWithSocialCertificate" in social["fields"]
    assert "flag" in social["guardrail"].lower()
