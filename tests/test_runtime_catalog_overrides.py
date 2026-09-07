"""Runtime catalog overlays keep live MCP routing ahead of registry refreshes."""
from pathlib import Path

from core.registry import Catalog
from core.safety import infer_safety


def test_wb_runtime_catalog_replaces_retired_stocks_endpoint():
    path = Path(__file__).parents[1] / "wb_mcp" / "endpoints.yaml"
    catalog = Catalog.from_yaml(path)

    assert catalog.get("wb_stats_stocks") is None
    spec = catalog.get("wb_analytics_stocks_wb_warehouses")
    assert spec is not None
    assert spec.method == "POST"
    assert spec.host == "seller-analytics-api.wildberries.ru"
    assert spec.path == "/api/analytics/v1/stocks-report/wb-warehouses"
    assert spec.scope == "analytics"
    assert spec.safety == "read"
    assert spec.pagination == "offset"
    assert spec.items_path == "data.items"
    assert spec.rate_limit == "3 req/min"


def test_wb_inventory_search_cannot_surface_retired_path():
    path = Path(__file__).parents[1] / "wb_mcp" / "endpoints.yaml"
    catalog = Catalog.from_yaml(path)
    hits = catalog.search("остатки склад наличие", limit=20)

    paths = {spec.path for spec in hits}
    ids = {spec.operation_id for spec in hits}
    assert "/api/v1/supplier/stocks" not in paths
    assert "wb_stats_stocks" not in ids
    assert "wb_analytics_stocks_wb_warehouses" in ids


def test_read_only_post_override_does_not_require_write_confirmation():
    path = Path(__file__).parents[1] / "wb_mcp" / "endpoints.yaml"
    spec = Catalog.from_yaml(path).get("wb_analytics_stocks_wb_warehouses")
    assert spec is not None
    assert infer_safety(spec.method, spec.safety) == "read"
