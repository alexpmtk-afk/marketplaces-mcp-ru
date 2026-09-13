"""Regression tests for the July 2026 WB Finance API migration."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import yaml

from core.paginate import fetch_all
from core.registry import Catalog, EndpointSpec
from core.tools import has_proven_quota


ROOT = Path(__file__).parents[1]


def _wb_catalog() -> Catalog:
    return Catalog.from_yaml(ROOT / "wb_mcp" / "endpoints.yaml")


def test_runtime_removes_retired_realization_endpoint():
    catalog = _wb_catalog()

    assert catalog.get("wb_report_realization") is None
    assert all(
        spec.path != "/api/v5/supplier/reportDetailByPeriod"
        for spec in catalog.all()
    )


def test_runtime_exposes_current_sales_report_list():
    spec = _wb_catalog().get("wb_finance_sales_reports_list")

    assert spec is not None
    assert spec.method == "POST"
    assert spec.host == "finance-api.wildberries.ru"
    assert spec.path == "/api/finance/v1/sales-reports/list"
    assert spec.scope == "finance"
    assert spec.safety == "read"
    assert spec.pagination == "offset"
    assert spec.rate_limit == "1 req/min"
    assert spec.quota_proven is True
    assert has_proven_quota(spec) is True


def test_runtime_exposes_current_sales_report_details_by_id():
    spec = _wb_catalog().get("wb_finance_sales_reports_detailed_by_id")

    assert spec is not None
    assert spec.method == "POST"
    assert spec.host == "finance-api.wildberries.ru"
    assert spec.path == "/api/finance/v1/sales-reports/detailed/{reportId}"
    assert spec.path_params == ["reportId"]
    assert spec.scope == "finance"
    assert spec.safety == "read"
    assert spec.pagination == "rrdid"
    assert spec.rate_limit == "1 req/min"
    assert spec.quota_proven is True
    assert has_proven_quota(spec) is True


def test_period_replacement_is_present_but_fails_closed_on_token_dependent_quota():
    spec = _wb_catalog().get("wb_finance_sales_reports_detailed_period")

    assert spec is not None
    assert spec.host == "finance-api.wildberries.ru"
    assert spec.path == "/api/finance/v1/sales-reports/detailed"
    assert spec.pagination == "rrdid"
    assert spec.quota_proven is False
    assert has_proven_quota(spec) is False


def test_balance_uses_current_finance_host_and_remains_fail_closed_until_token_aware_quota():
    spec = _wb_catalog().get("wb_finance_balance")

    assert spec is not None
    assert spec.host == "finance-api.wildberries.ru"
    assert spec.path == "/api/v1/account/balance"
    assert spec.quota_proven is False
    assert has_proven_quota(spec) is False


def test_lifecycle_registry_marks_old_realization_path_shutdown():
    data = yaml.safe_load((ROOT / "api_registry" / "status_overrides.yaml").read_text(encoding="utf-8"))
    rows = [
        row for row in data["entries"]
        if row.get("marketplace") == "wildberries"
        and row.get("path") == "/api/v5/supplier/reportDetailByPeriod"
    ]

    assert len(rows) == 1
    assert rows[0]["status"] == "SHUTDOWN"
    assert str(rows[0]["shutdown_date"]) == "2026-07-15"


class _FakeClient:
    def __init__(self):
        self.calls: list[dict] = []

    async def call_spec(self, spec, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if len(self.calls) == 1:
            return {
                "ok": True,
                "status": 200,
                "data": [
                    {"rrdId": 101, "nmId": 1},
                    {"rrdId": 202, "nmId": 2},
                ],
            }
        return {"ok": True, "status": 204, "data": ""}


def test_rrdid_pagination_uses_camel_case_body_cursor_for_current_finance_api():
    client = _FakeClient()
    spec = EndpointSpec(
        operation_id="wb_finance_sales_reports_detailed_by_id",
        method="POST",
        host="finance-api.wildberries.ru",
        path="/api/finance/v1/sales-reports/detailed/{reportId}",
        pagination="rrdid",
        items_path="result.items",
    )

    result = asyncio.run(fetch_all(
        client,
        spec,
        path_values={"reportId": 123456789},
        base_body={},
        limit=100000,
        max_items=200000,
    ))

    assert result == {
        "ok": True,
        "items": [
            {"rrdId": 101, "nmId": 1},
            {"rrdId": 202, "nmId": 2},
        ],
        "total_fetched": 2,
        "pages_fetched": 2,
        "truncated": False,
    }
    assert client.calls[0]["json_body"] == {"limit": 100000, "rrdId": 0}
    assert client.calls[1]["json_body"] == {"limit": 100000, "rrdId": 202}
    assert client.calls[0]["path_values"] == {"reportId": 123456789}
