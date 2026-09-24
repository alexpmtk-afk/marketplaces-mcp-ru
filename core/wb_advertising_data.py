"""Canonical WB Promotion data-layer contract.

This module defines source-of-truth routing, archive grains, provider limits and
semantic/quality boundaries before Advertising Archive V1 persists data.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

DATA_CONTRACT_VERSION = "wb_ads_data_v1.2"
ARCHIVE_STATUS = "archive_primitives_in_progress"

DATASETS: dict[str, dict[str, Any]] = {
    "ads_campaign_roster_snapshots": {
        "layer": "discovery_snapshot",
        "provider_operation": "wb_get_adv_promotion_count",
        "provider_path": "/adv/v1/promotion/count",
        "grain": ["observed_at", "campaign_id"],
        "archive": True,
        "rate_limit": "5 req/sec per seller account",
        "meaning": "all seller campaign IDs grouped by provider type/status with last change time",
        "fullstats_eligible_statuses": [7, 9, 11],
        "quality_rule": (
            "campaign discovery and fullstats availability are different contracts; "
            "fullstats covers only provider-addressable statuses 7,9,11"
        ),
    },
    "ads_campaign_daily": {
        "layer": "historical",
        "provider_operation": "wb_get_adv_fullstats",
        "provider_path": "/adv/v3/fullstats",
        "grain": ["date", "campaign_id"],
        "archive": True,
        "max_days_per_request": 31,
        "max_campaign_ids_per_request": 50,
        "rate_limit": "3 req/min per seller account",
        "primary_measures": [
            "views", "clicks", "cart_adds", "ad_orders", "advertised_items",
            "canceled", "spend", "attributed_order_amount",
        ],
        "derived_measures": [
            "accepted_orders_derived",
            "ctr_pct", "cpc", "click_to_order_cr_pct", "cpo",
            "drr_order_pct", "roas",
        ],
        "semantic_class": "advertising_attribution_operational",
        "limitations": [
            "ad_orders are WB advertising-attributed orders, not complete seller orders",
            "attributed_order_amount is not actual realized seller revenue",
            "financial spend should be reconciled against ads_expenses before financial conclusions",
        ],
    },
    "ads_product_daily": {
        "layer": "historical",
        "provider_operation": "wb_get_adv_fullstats",
        "provider_path": "/adv/v3/fullstats",
        "provider_extract": "days[].apps[].nms[]",
        "grain": ["date", "campaign_id", "app_type", "nm_id"],
        "archive": True,
        "max_days_per_request": 31,
        "max_campaign_ids_per_request": 50,
        "semantic_class": "advertising_attribution_operational",
        "additional_measures": [
            "accepted_orders_derived",
            "avg_position",
            "multicard_id_current",
            "conversion_type_current",
        ],
        "enrichment_sources": [
            "wb_get_api_advert_adverts",
            "wb_content_cards_list",
        ],
        "limitations": [
            "same attribution boundary as campaign statistics",
            "app_type is a provider platform/app dimension and is not relabeled as ad placement",
            "rows from different app_type values must not be silently collapsed before aggregation rules are applied",
            "conversion_type_current and multicard_id_current are based on a current campaign/content snapshot and are not historical event-time truth",
            "accepted_orders_derived is max(ad_orders-canceled,0), reconciled to the seller XLS Evidence Gate but remains an explicit derived measure",
        ],
    },
    "ads_product_identity_snapshots": {
        "layer": "current_state_snapshot",
        "provider_operation": "wb_content_cards_list",
        "provider_path": "/content/v2/get/cards/list",
        "grain": ["observed_at", "nm_id"],
        "archive": True,
        "rate_limit": "100 req/min per seller account",
        "meaning": "current nm_id to imtID/product-card identity used to reproduce multicard attribution with explicit observation time",
        "quality_rule": "current card grouping is observation-time context and must not be presented as historical event-time truth",
    },
    "ads_search_cluster_daily": {
        "layer": "historical",
        "provider_operation": "wb_post_adv_normquery_stats_v1",
        "provider_path": "/adv/v1/normquery/stats",
        "grain": ["date", "campaign_id", "nm_id", "norm_query"],
        "archive": True,
        "max_items_per_request": 100,
        "rate_limit": "10 req/min per seller account",
        "primary_measures": [
            "views", "clicks", "cart_adds", "orders", "ordered_items",
            "spend", "avg_position", "ctr", "cpc", "cpm",
        ],
        "nullable_by_payment_model": {"cpc": ["views", "ctr", "cpm"]},
        "quality_rule": "provider-unavailable CPC fields are NULL/not_available, never synthetic zero",
    },
    "ads_expenses": {
        "layer": "financial_history",
        "provider_operation": "wb_get_adv_upd",
        "provider_path": "/adv/v1/upd",
        "grain": ["expense_event_fingerprint"],
        "archive": True,
        "max_days_per_request": 31,
        "rate_limit": "1 req/sec per seller account",
        "meaning": "provider history of actual Promotion campaign costs",
        "financial_role": "spend_reconciliation_source",
    },
    "ads_payments": {
        "layer": "financial_history",
        "provider_operation": "wb_get_adv_payments",
        "provider_path": "/adv/v1/payments",
        "grain": ["payment_id_or_event_fingerprint"],
        "archive": True,
        "max_days_per_request": 31,
        "rate_limit": "1 req/sec per seller account",
        "meaning": "Promotion account top-up history",
        "financial_role": "funding_history_not_performance",
    },
    "ads_campaign_snapshots": {
        "layer": "current_state_snapshot",
        "provider_operation": "wb_get_api_advert_adverts",
        "provider_path": "/api/advert/v2/adverts",
        "grain": ["observed_at", "campaign_id"],
        "archive": True,
        "provider_sync": {
            "database": "about 3 minutes",
            "campaign_status": "about 1 minute",
            "bids": "about 30 seconds",
        },
        "quality_rule": "snapshot observed_at is mandatory; snapshot is observation, not event-time truth",
    },
    "ads_account_balance_current": {
        "layer": "live_state",
        "provider_operation": "wb_get_adv_balance",
        "provider_path": "/adv/v1/balance",
        "grain": ["cabinet"],
        "archive": False,
        "meaning": "current Promotion account balance/net/bonus state",
    },
    "ads_campaign_budget_current": {
        "layer": "live_state",
        "provider_operation": "wb_adv_budget",
        "provider_path": "/adv/v1/budget",
        "grain": ["campaign_id"],
        "archive": False,
        "meaning": "current campaign budget",
    },
    "ads_search_cluster_bids_current": {
        "layer": "live_config",
        "provider_operation": "wb_post_adv_normquery_get_bids",
        "provider_path": "/adv/v0/normquery/get-bids",
        "grain": ["campaign_id", "nm_id", "norm_query"],
        "archive": False,
        "history_policy": "history exists only from MCP-owned snapshots/audit log; provider endpoint is current state",
    },
    "ads_minus_phrases_current": {
        "layer": "live_config",
        "provider_operation": "wb_post_adv_normquery_get_minus",
        "provider_path": "/adv/v0/normquery/get-minus",
        "grain": ["campaign_id", "nm_id", "norm_query"],
        "archive": False,
        "history_policy": "history exists only from MCP-owned snapshots/audit log; provider endpoint is current state",
    },
}

ROUTING_RULES = {
    "campaign_discovery": "use /adv/v1/promotion/count; do not infer the complete campaign roster from active/current filters",
    "fullstats_population": "only discovered campaigns in provider-addressable statuses 7,9,11 are eligible for /adv/v3/fullstats coverage",
    "current_campaign_state": "live Promotion API only",
    "current_budget_balance_bids_minus_phrases": "live Promotion API only",
    "closed_historical_advertising": "archive-first only after FULL_COVERAGE is proven; otherwise explicit live fallback or coverage gap",
    "advertising_efficiency": "use advertising attribution datasets; label results as provider-attributed",
    "actual_business_profitability": (
        "requires joins to actual sales/buyouts, returns, marketplace finance and unit economics; "
        "advertising attribution alone is insufficient"
    ),
    "financial_spend": "reconcile campaign-stat spend with ads_expenses; do not assume identical semantics",
    "missing_provider_rows": "never silently coerce missing campaign/stat rows to zero",
    "xls_reconciliation": (
        "seller XLS is supplemental reconciliation evidence, not the canonical automated history source; "
        "the 2026-09-18..24 Evidence Gate matched core orders/cart/order-amount semantics, exposed current multicard conversion labels, "
        "and showed small timing drift in clicks/spend plus extra zero-rich keyword rows"
    ),
}

QUALITY_GATES = {
    "coverage": "FULL_COVERAGE required before archive-only historical calculations",
    "provenance": "every answer must retain marketplace, cabinet, dataset/source operation and covered period",
    "nullability": "provider-unavailable metrics remain NULL/not_available",
    "currency": "never combine different currencies without explicit conversion contract",
    "attribution": "AD_ORDERS != REAL_ORDERS and AD_ORDER_AMOUNT != REAL_SALES_AMOUNT",
    "zero_anomaly": (
        "zero-rich provider statistics must not be treated as proven business zero when other provider sources conflict; "
        "surface a quality flag and reconcile where an independent provider source exists"
    ),
}

LAYER_MODEL = [
    "provider_source",
    "normalization",
    "archive_and_coverage",
    "semantic_routing",
    "business_join",
    "quality_and_provenance",
    "action_gate",
]


def advertising_data_map() -> dict[str, Any]:
    return {
        "version": DATA_CONTRACT_VERSION,
        "status": ARCHIVE_STATUS,
        "layers": list(LAYER_MODEL),
        "datasets": deepcopy(DATASETS),
        "routing_rules": dict(ROUTING_RULES),
        "quality_gates": dict(QUALITY_GATES),
    }


def required_provider_operations(*, archive_only: bool = False) -> set[str]:
    rows = DATASETS.values()
    if archive_only:
        rows = [row for row in rows if row.get("archive") is True]
    return {str(row["provider_operation"]) for row in rows if row.get("provider_operation")}
