from __future__ import annotations

from datetime import date
from pathlib import Path

from core.registry import Catalog, EndpointSpec
from core.request_source_router import (
    SOURCE_CANONICAL_ARCHIVE,
    SOURCE_LIVE_CABINET_API,
    plan_marketplace_request,
)
from core.tools import (
    generic_read_execution_info,
    resolve_equivalent_proven_read_spec,
)

ROOT = Path(__file__).resolve().parent.parent


def test_historical_buyout_routes_to_canonical_archive_not_live_api():
    plan = plan_marketplace_request(
        "сколько по Wildberries у Новокшенова была сумма выкупов в прошлом месяце",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
        today=date(2026, 9, 23),
    )
    assert plan["source_family"] == SOURCE_CANONICAL_ARCHIVE
    assert plan["source_status"] == "FULL_COVERAGE_REQUIRED"
    assert plan["downstream_handler"] == "marketplace_business_query"
    assert plan["execution_plan"]["can_start_execution"] is True


def test_wb_current_price_has_dedicated_live_business_route():
    plan = plan_marketplace_request(
        "какая цена товара 507763296",
        marketplace="wb",
        seller="wb_laser_master",
        today=date(2026, 9, 23),
    )
    assert plan["source_family"] == SOURCE_LIVE_CABINET_API
    assert plan["downstream_handler"] == "marketplace_business_query"
    assert plan["semantic_resolution"]["metric_id"] == "CURRENT_SELLING_PRICE"


def test_wb_fbs_stock_is_not_generic_wb_warehouse_stock():
    plan = plan_marketplace_request(
        "проверь остаток на складе FBS по товару 507763296",
        marketplace="wb",
        seller="wb_laser_master",
        today=date(2026, 9, 23),
    )
    assert plan["source_family"] == SOURCE_LIVE_CABINET_API
    assert plan["semantic_resolution"]["metric_id"] == "CURRENT_FBS_STOCK"
    assert "WB-warehouse stock substituted for seller FBS stock" in plan["forbidden_substitutes"]


def test_post_read_requires_explicit_semantics_proof_even_with_quota():
    spec = EndpointSpec(
        operation_id="demo_post_read",
        method="POST",
        host="example.invalid",
        path="/read",
        safety="read",
        rate_limit="300 req/min",
        quota_proven=True,
    )
    catalog = Catalog([spec])
    blocked = generic_read_execution_info("wb", catalog, spec)
    assert blocked["generic_read_status"] == "BLOCKED_READ_SEMANTICS_UNPROVEN"
    assert blocked["generic_read_executable"] is False

    spec.read_only_post_proven = True
    allowed = generic_read_execution_info("wb", catalog, spec)
    assert allowed["generic_read_status"] == "EXECUTABLE"
    assert allowed["generic_read_executable"] is True


def test_equivalent_read_contract_never_crosses_http_verbs():
    get_spec = EndpointSpec(
        operation_id="safe_get",
        method="GET",
        host="example.invalid",
        path="/same-path",
        safety="read",
        rate_limit="300 req/min",
        quota_proven=True,
    )
    post_spec = EndpointSpec(
        operation_id="ambiguous_post",
        method="POST",
        host="example.invalid",
        path="/same-path",
        safety="read",
        read_only_post_proven=True,
    )
    catalog = Catalog([get_spec, post_spec])
    resolved, equivalent = resolve_equivalent_proven_read_spec(catalog, post_spec)
    assert resolved.operation_id == "ambiguous_post"
    assert equivalent is False


def test_wb_fbs_catalog_contracts_are_read_only_and_quota_proven():
    catalog = Catalog.from_yaml(ROOT / "wb_mcp" / "endpoints.yaml")
    warehouses = catalog.get("wb_fbs_warehouses")
    stock = catalog.get("wb_post_api_stocks_warehouseid")
    create = catalog.get("wb_post_api_warehouses")

    assert warehouses is not None
    assert warehouses.method == "GET"
    assert warehouses.safety == "read"
    assert warehouses.rate_limit == "300 req/min"
    assert warehouses.quota_proven is True

    assert stock is not None
    assert stock.method == "POST"
    assert stock.safety == "read"
    assert stock.read_only_post_proven is True
    assert stock.rate_limit == "300 req/min"
    assert stock.quota_proven is True

    assert create is not None
    assert create.method == "POST"
    assert create.safety == "write"


def test_ozon_primary_snapshot_posts_have_explicit_read_semantics_proof():
    catalog = Catalog.from_yaml(ROOT / "ozon_mcp" / "endpoints.yaml")
    for operation_id in (
        "ozon_product_info_list",
        "ozon_prices_get",
        "ozon_stocks_info",
    ):
        spec = catalog.get(operation_id)
        assert spec is not None
        assert spec.method == "POST"
        assert spec.safety == "read"
        assert spec.read_only_post_proven is True


def test_wb_price_tool_publishes_major_currency_unit_contract():
    source = (ROOT / "wb_mcp" / "server.py").read_text(encoding="utf-8")
    assert '"rub_values_are_rubles": True' in source
    assert '"divide_by_100": False' in source
    assert '"price_rub"' in source
    assert "Clients must never divide these values by 100" in source
