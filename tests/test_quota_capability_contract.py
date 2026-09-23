from __future__ import annotations

import asyncio
import json

from core.tools import generic_read_execution_info
from ozon_mcp import server as ozon
from wb_mcp import server as wb


def _call(server, name: str, args: dict) -> dict:
    result = asyncio.run(server.mcp.call_tool(name, args))
    return json.loads(result[0][0].text)


def test_proven_wb_read_is_executable():
    spec = wb.catalog.get("wb_prices_list")
    info = generic_read_execution_info("wb", wb.catalog, spec)
    assert info["generic_read_status"] == "EXECUTABLE"
    assert info["generic_read_executable"] is True
    assert info["quota_proof"] == "operation_quota"


def test_generated_wb_duplicate_resolves_to_proven_canonical_contract():
    spec = wb.catalog.get("wb_post_api_list_goods_filter")
    info = generic_read_execution_info("wb", wb.catalog, spec)
    assert info["generic_read_status"] == "EXECUTABLE"
    assert info["resolved_operation_id"] == "wb_prices_list"
    assert info["quota_proof"] == "equivalent_proven_contract"


def test_parseable_but_unproven_rule_is_explicitly_blocked():
    spec = wb.catalog.get("wb_analytics_funnel")
    info = generic_read_execution_info("wb", wb.catalog, spec)
    assert info["generic_read_status"] == "BLOCKED_QUOTA_UNPROVEN"
    assert info["generic_read_executable"] is False
    assert info["generic_read_reason"] == "rate_limit_present_but_unproven"


def test_missing_quota_contract_is_explicitly_blocked():
    spec = wb.catalog.get("wb_content_categories")
    info = generic_read_execution_info("wb", wb.catalog, spec)
    assert info["generic_read_status"] == "BLOCKED_QUOTA_UNPROVEN"
    assert info["generic_read_reason"] == "quota_contract_missing"


def test_ozon_service_proof_is_visible():
    spec = ozon.catalog.get("ozon_prices_get")
    info = generic_read_execution_info("ozon", ozon.catalog, spec)
    assert info["generic_read_status"] == "EXECUTABLE"
    assert info["quota_proof"] == "service_quota"


def test_search_hides_quota_blocked_reads_by_default():
    payload = _call(wb, "wb_search_methods", {"query": "воронка", "limit": 20})
    assert payload["executable_only"] is True
    assert all(
        row["generic_read_status"] != "BLOCKED_QUOTA_UNPROVEN"
        for row in payload["results"]
    )
    assert payload["blocked_match_count"] >= 1
    assert any(
        row["operation_id"] == "wb_analytics_funnel"
        for row in payload["blocked_matches"]
    )


def test_search_inventory_mode_exposes_status_instead_of_hiding_it():
    payload = _call(
        wb, "wb_search_methods",
        {"query": "воронка", "limit": 20, "executable_only": False},
    )
    row = next(
        row for row in payload["results"]
        if row["operation_id"] == "wb_analytics_funnel"
    )
    assert row["generic_read_status"] == "BLOCKED_QUOTA_UNPROVEN"
    assert row["generic_read_reason"] == "rate_limit_present_but_unproven"


def test_describe_exposes_quota_block_before_execution():
    payload = _call(
        wb, "wb_describe_method", {"operation_id": "wb_content_categories"},
    )
    assert payload["generic_read_status"] == "BLOCKED_QUOTA_UNPROVEN"
    assert payload["generic_read_reason"] == "quota_contract_missing"
    assert payload["generic_read_executable"] is False


def test_every_read_only_catalog_operation_has_explicit_quota_capability_status():
    for service, server in (("wb", wb), ("ozon", ozon)):
        for spec in server.catalog.all():
            info = generic_read_execution_info(service, server.catalog, spec)
            if info["generic_read_status"] == "NOT_READ":
                continue
            assert info["generic_read_status"] in {
                "EXECUTABLE", "BLOCKED_QUOTA_UNPROVEN",
            }, (service, spec.operation_id, info)
            assert isinstance(info.get("generic_read_reason"), str)
            assert info["generic_read_reason"], (service, spec.operation_id, info)
            if info["generic_read_status"] == "EXECUTABLE":
                assert info["generic_read_executable"] is True
                assert info.get("quota_proof") in {
                    "operation_quota", "service_quota",
                    "equivalent_proven_contract",
                }
            else:
                assert info["generic_read_executable"] is False
                assert info.get("quota_proof") == "none"
