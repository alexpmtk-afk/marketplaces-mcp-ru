from pathlib import Path

from core.registry import Catalog


CATALOG_PATH = Path("ozon_mcp/endpoints.yaml")


def test_shutdown_ozon_routes_are_not_runtime_preferred_routes():
    raw = CATALOG_PATH.read_text(encoding="utf-8")
    for path in (
        "/v3/posting/fbs/unfulfilled/list",
        "/v3/posting/fbs/list",
        "/v2/posting/fbo/list",
        "/v3/finance/transaction/list",
        "/v3/finance/transaction/totals",
    ):
        assert path not in raw


def test_current_ozon_posting_and_finance_contracts_are_routed():
    catalog = Catalog.from_yaml(CATALOG_PATH)
    expected = {
        "ozon_fbs_unfulfilled": ("/v4/posting/fbs/unfulfilled/list", "cursor", "postings"),
        "ozon_fbs_list": ("/v4/posting/fbs/list", "cursor", "postings"),
        "ozon_fbo_list": ("/v3/posting/fbo/list", "cursor", "postings"),
        "ozon_finance_accrual_postings": ("/v1/finance/accrual/postings", "none", "posting_accruals"),
        "ozon_finance_accrual_types": ("/v1/finance/accrual/types", "none", "accrual_types"),
        "ozon_finance_accrual_by_day": ("/v1/finance/accrual/by-day", "last_id_no_limit", "accruals"),
    }
    for operation_id, wanted in expected.items():
        spec = catalog.get(operation_id)
        assert spec is not None, operation_id
        assert (spec.path, spec.pagination, spec.items_path) == wanted

    assert catalog.get("ozon_finance_transactions") is None
    assert catalog.get("ozon_finance_totals") is None


def test_unit_economics_no_longer_references_retired_finance_operation():
    raw = Path("ozon_mcp/workflows.yaml").read_text(encoding="utf-8")
    assert "operation_id: ozon_finance_transactions" not in raw
    assert "operation_id: ozon_finance_accrual_by_day" in raw
    assert "operation_id: ozon_finance_accrual_types" in raw
