from __future__ import annotations

import asyncio
import httpx
from datetime import date, timedelta
from pathlib import Path

from core.registry import Catalog, EndpointSpec
from core.client import _parse_body
import core.semantic_current_stock as semantic_current_stock
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


def test_current_stock_blank_dates_mean_current_snapshot(monkeypatch):
    async def fake_fetch_current_rows(wb, *, seller, nm_ids):
        assert seller == "wb_laser_master"
        assert nm_ids == [507763296]
        return seller, {
            "ok": True,
            "source": "test_current_stock",
            "items": [
                {
                    "nmId": 507763296,
                    "warehouseName": "Склад WB",
                    "quantity": 2,
                }
            ],
        }

    monkeypatch.setattr(
        semantic_current_stock,
        "_fetch_current_rows",
        fake_fetch_current_rows,
    )

    result = asyncio.run(
        semantic_current_stock.execute_current_stock_question(
            object(),
            seller="wb_laser_master",
            date_from="",
            date_to="",
            grouping="TOTAL",
            nm_ids=[507763296],
        )
    )

    assert result["ok"] is True
    assert result["metric"] == "CURRENT_STOCK"
    assert result["as_of_date"] == date.today().isoformat()
    assert result["stock_units"] == 2
    assert result["complete"] is True


def test_current_stock_rejects_half_specified_date_range():
    try:
        asyncio.run(
            semantic_current_stock.execute_current_stock_question(
                object(),
                seller="wb_laser_master",
                date_from=date.today().isoformat(),
                date_to="",
                grouping="TOTAL",
                nm_ids=[507763296],
            )
        )
    except semantic_current_stock.SemanticCurrentStockExecutionError as exc:
        assert "both be omitted" in str(exc)
    else:
        raise AssertionError("half-specified current-stock date range must fail")


def test_current_stock_historical_date_fails_before_provider(monkeypatch):
    called = False

    async def should_not_fetch(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("provider must not be called for historical CURRENT_STOCK")

    monkeypatch.setattr(
        semantic_current_stock,
        "_fetch_current_rows",
        should_not_fetch,
    )

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    result = asyncio.run(
        semantic_current_stock.execute_current_stock_question(
            object(),
            seller="wb_laser_master",
            date_from=yesterday,
            date_to=yesterday,
            grouping="TOTAL",
            nm_ids=[507763296],
        )
    )

    assert result["ok"] is False
    assert result["error"] == "source_not_suitable"
    assert called is False



def test_provider_json_with_text_plain_content_type_is_recovered_for_current_stock():
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/plain; charset=utf-8"},
        content=(
            b'{"data":{"items":[{"nmId":507763296,'
            b'"warehouseName":"Test WB warehouse","quantity":3}]}}'
        ),
    )

    provider = _parse_body(response)

    assert isinstance(provider, dict)
    items = semantic_current_stock._extract_fast_items({
        "ok": True,
        "status": 200,
        "data": provider,
    })
    assert items == [{
        "nmId": 507763296,
        "warehouseName": "Test WB warehouse",
        "quantity": 3,
    }]


def test_provider_plain_text_remains_plain_text():
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/plain"},
        content=b"provider is alive",
    )

    assert _parse_body(response) == "provider is alive"


def test_provider_malformed_json_text_remains_text():
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/plain"},
        content=b'{"data": broken',
    )

    assert _parse_body(response) == '{"data": broken'
