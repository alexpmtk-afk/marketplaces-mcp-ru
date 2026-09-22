"""Regression guard for stale quota catalogs on the REMOTE runtime."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.registry import Catalog
from core.runtime_contracts import audit_runtime_contracts
from ozon_mcp import server as ozon
from wb_mcp import server as wb


ROOT = Path(__file__).parents[1]


def test_current_loaded_critical_quota_contracts_pass():
    report = audit_runtime_contracts({"wb": wb, "ozon": ozon})
    assert report["ok"] is True, report["errors"]
    assert {row["operation_id"] for row in report["contracts"]} == {
        "wb_prices_list",
        "wb_analytics_stocks_wb_warehouses",
        "ozon_prices_get",
        "ozon_stocks_info",
    }
    assert all(row["executable"] is True for row in report["contracts"])


def test_stale_wb_price_catalog_is_rejected_before_runtime_start():
    wb_catalog = Catalog.from_yaml(ROOT / "wb_mcp" / "endpoints.yaml")
    ozon_catalog = Catalog.from_yaml(ROOT / "ozon_mcp" / "endpoints.yaml")
    wb_catalog.get("wb_prices_list").quota_proven = False

    report = audit_runtime_contracts({
        "wb": SimpleNamespace(catalog=wb_catalog),
        "ozon": SimpleNamespace(catalog=ozon_catalog),
    })

    assert report["ok"] is False
    assert any(
        "wb:wb_prices_list" in error and "quota_proven" in error
        for error in report["errors"]
    )
    assert any(
        "wb:wb_prices_list" in error and "RATE_LIMIT_RULE_UNPROVEN" in error
        for error in report["errors"]
    )


def test_wrong_wb_price_endpoint_is_rejected_even_with_quota_flag():
    wb_catalog = Catalog.from_yaml(ROOT / "wb_mcp" / "endpoints.yaml")
    ozon_catalog = Catalog.from_yaml(ROOT / "ozon_mcp" / "endpoints.yaml")
    wb_catalog.get("wb_prices_list").path = "/stale/path"

    report = audit_runtime_contracts({
        "wb": SimpleNamespace(catalog=wb_catalog),
        "ozon": SimpleNamespace(catalog=ozon_catalog),
    })

    assert report["ok"] is False
    assert any(
        "wb:wb_prices_list" in error and "path=" in error
        for error in report["errors"]
    )


def test_provenance_identifies_loaded_catalog_files():
    report = audit_runtime_contracts({"wb": wb, "ozon": ozon})
    for service in ("wb", "ozon"):
        provenance = report["provenance"][service]
        assert provenance["module"]["path"]
        assert provenance["module"]["sha256"]
        assert provenance["catalog"]["path"]
        assert provenance["catalog"]["sha256"]
