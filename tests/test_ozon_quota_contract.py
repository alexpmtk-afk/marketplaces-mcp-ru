"""Regression coverage for Ozon Seller quotas that are explicitly documented."""
from pathlib import Path

from core.registry import Catalog
from core.tools import has_proven_quota

CATALOG_PATH = Path("ozon_mcp/endpoints.yaml")


def test_documented_ozon_analytics_quotas_are_executable():
    catalog = Catalog.from_yaml(CATALOG_PATH)
    for operation_id in ("ozon_analytics_data", "ozon_turnover_stocks"):
        spec = catalog.get(operation_id)
        assert spec is not None
        assert spec.rate_limit == "1 req/min"
        assert has_proven_quota(spec)


def test_unproven_ozon_catalog_operation_remains_closed():
    catalog = Catalog.from_yaml(CATALOG_PATH)
    assert not has_proven_quota(catalog.get("ozon_product_list"))
