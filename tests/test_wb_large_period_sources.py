from wb_mcp import server as wb


def test_runtime_catalog_removes_retired_realization_endpoint():
    assert wb.catalog.get("wb_report_realization") is None


def test_runtime_catalog_has_current_finance_period_endpoint():
    spec = wb.catalog.get("wb_finance_sales_reports_detailed")
    assert spec is not None
    assert spec.method == "POST"
    assert spec.host == "finance-api.wildberries.ru"
    assert spec.path == "/api/finance/v1/sales-reports/detailed"
    assert spec.scope == "finance"
    assert spec.safety == "read"
    assert spec.rate_limit == "1 req/min"
    assert spec.quota_proven is True


def test_sales_funnel_is_not_promoted_to_proven_canonical_orders_source():
    spec = wb.catalog.get("wb_analytics_funnel")
    assert spec is not None
    assert spec.method == "POST"
    assert spec.host == "seller-analytics-api.wildberries.ru"
    assert spec.path == "/api/analytics/v3/sales-funnel/products"
    # Live parity on all three TEST WB cabinets proved that Sales Funnel
    # orderCount/orderSum semantics do not equal canonical Statistics Orders.
    # Its quota also varies by WB token type, so runtime_overrides must not
    # promote a universal 3/min rule as quota-proven.
    assert spec.quota_proven is False
