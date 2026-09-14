from __future__ import annotations

from pathlib import Path

from core.registry import Catalog
from core.wb_advertising_data import (
    ARCHIVE_STATUS,
    DATASETS,
    DATA_CONTRACT_VERSION,
    QUALITY_GATES,
    ROUTING_RULES,
    advertising_data_map,
    required_provider_operations,
)

ROOT = Path(__file__).resolve().parent.parent


def test_contract_exposes_required_layers_and_status():
    data = advertising_data_map()
    assert data["version"] == DATA_CONTRACT_VERSION == "wb_ads_data_v1.0"
    assert data["status"] == ARCHIVE_STATUS == "contract_ready_ingestion_pending"
    assert data["layers"] == [
        "provider_source",
        "normalization",
        "archive_and_coverage",
        "semantic_routing",
        "business_join",
        "quality_and_provenance",
        "action_gate",
    ]


def test_historical_contract_preserves_provider_limits():
    campaign = DATASETS["ads_campaign_daily"]
    assert campaign["provider_path"] == "/adv/v3/fullstats"
    assert campaign["max_days_per_request"] == 31
    assert campaign["max_campaign_ids_per_request"] == 50
    assert campaign["archive"] is True

    expenses = DATASETS["ads_expenses"]
    payments = DATASETS["ads_payments"]
    assert expenses["max_days_per_request"] == 31
    assert payments["max_days_per_request"] == 31


def test_cpc_cluster_metrics_are_nullable_not_zero_filled():
    clusters = DATASETS["ads_search_cluster_daily"]
    assert clusters["nullable_by_payment_model"]["cpc"] == ["views", "ctr", "cpm"]
    assert "never synthetic zero" in clusters["quality_rule"]


def test_live_state_is_not_misrepresented_as_historical_truth():
    assert DATASETS["ads_account_balance_current"]["archive"] is False
    assert DATASETS["ads_campaign_budget_current"]["archive"] is False
    assert DATASETS["ads_search_cluster_bids_current"]["archive"] is False
    assert DATASETS["ads_minus_phrases_current"]["archive"] is False
    assert "snapshots" in DATASETS["ads_search_cluster_bids_current"]["history_policy"]


def test_profitability_and_spend_boundaries_are_explicit():
    assert "actual sales/buyouts" in ROUTING_RULES["actual_business_profitability"]
    assert "reconcile" in ROUTING_RULES["financial_spend"]
    assert QUALITY_GATES["attribution"] == "AD_ORDERS != REAL_ORDERS and AD_ORDER_AMOUNT != REAL_SALES_AMOUNT"
    assert "FULL_COVERAGE" in QUALITY_GATES["coverage"]


def test_required_provider_operations_cover_archive_v1_sources():
    ops = required_provider_operations(archive_only=True)
    assert {
        "wb_get_adv_fullstats",
        "wb_post_adv_normquery_stats_v1",
        "wb_get_adv_upd",
        "wb_get_adv_payments",
        "wb_get_api_advert_adverts",
    } <= ops


def test_runtime_catalog_contains_read_only_advertising_sources():
    catalog = Catalog.from_yaml(ROOT / "wb_mcp" / "endpoints.yaml")
    expected = {
        "wb_get_api_advert_adverts": "/api/advert/v2/adverts",
        "wb_get_adv_fullstats": "/adv/v3/fullstats",
        "wb_post_adv_normquery_stats_v1": "/adv/v1/normquery/stats",
        "wb_get_adv_upd": "/adv/v1/upd",
        "wb_get_adv_payments": "/adv/v1/payments",
        "wb_get_adv_balance": "/adv/v1/balance",
        "wb_post_adv_normquery_get_bids": "/adv/v0/normquery/get-bids",
        "wb_post_adv_normquery_get_minus": "/adv/v0/normquery/get-minus",
        "wb_adv_budget": "/adv/v1/budget",
    }
    for operation_id, path in expected.items():
        spec = catalog.get(operation_id)
        assert spec is not None, operation_id
        assert spec.path == path
        assert spec.safety == "read"
