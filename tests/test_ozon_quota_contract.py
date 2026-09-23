"""Regression coverage for Ozon Seller quotas that are explicitly documented."""
from pathlib import Path

from core.registry import Catalog
from core.tools import generic_read_execution_info, has_proven_quota, has_proven_read_semantics

CATALOG_PATH = Path("ozon_mcp/endpoints.yaml")


def test_documented_ozon_analytics_quotas_are_executable():
    catalog = Catalog.from_yaml(CATALOG_PATH)
    for operation_id in ("ozon_analytics_data", "ozon_turnover_stocks"):
        spec = catalog.get(operation_id)
        assert spec is not None
        assert spec.rate_limit == "1 req/min"
        assert has_proven_quota(spec)


def test_current_product_reads_use_proven_service_quota():
    catalog = Catalog.from_yaml(CATALOG_PATH)
    for operation_id in (
        "ozon_product_info_list",
        "ozon_prices_get",
        "ozon_stocks_info",
    ):
        spec = catalog.get(operation_id)
        assert spec is not None
        assert spec.service_quota_proven is True
        assert spec.rate_limit == ""
        assert has_proven_quota(spec)


def test_current_order_and_accrual_reads_are_proven_and_executable():
    catalog = Catalog.from_yaml(CATALOG_PATH)
    for operation_id in (
        "ozon_fbo_list",
        "ozon_fbs_list",
        "ozon_finance_accrual_by_day",
    ):
        spec = catalog.get(operation_id)
        assert spec is not None
        assert spec.read_only_post_proven is True
        assert spec.service_quota_proven is True
        assert has_proven_read_semantics(spec)
        assert has_proven_quota(spec)
        info = generic_read_execution_info("ozon", catalog, spec)
        assert info["generic_read_status"] == "EXECUTABLE"
        assert info["quota_proof"] == "service_quota"



def test_realization_reports_are_proven_read_only_and_executable():
    catalog = Catalog.from_yaml(CATALOG_PATH)
    for operation_id in (
        "ozon_finance_realization",
        "ozon_post_v1_finance_realization_posting",
    ):
        spec = catalog.get(operation_id)
        assert spec is not None
        assert spec.read_only_post_proven is True
        assert spec.service_quota_proven is True
        assert has_proven_read_semantics(spec)
        assert has_proven_quota(spec)
        info = generic_read_execution_info("ozon", catalog, spec)
        assert info["generic_read_status"] == "EXECUTABLE"
        assert info["quota_proof"] == "service_quota"


def test_unproven_ozon_catalog_operation_remains_closed():
    catalog = Catalog.from_yaml(CATALOG_PATH)
    assert not has_proven_quota(catalog.get("ozon_product_list"))
