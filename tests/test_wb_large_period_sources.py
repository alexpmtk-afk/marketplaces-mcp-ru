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


def test_runtime_catalog_marks_sales_funnel_as_proven_large_period_source():
    spec = wb.catalog.get("wb_analytics_funnel")
    assert spec is not None
    assert spec.method == "POST"
    assert spec.host == "seller-analytics-api.wildberries.ru"
    assert spec.path == "/api/analytics/v3/sales-funnel/products"
    assert spec.rate_limit == "3 req/min"
    assert spec.quota_proven is True
    assert spec.items_path == "data.products"
