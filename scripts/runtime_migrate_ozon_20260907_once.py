#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENDPOINTS = ROOT / "ozon_mcp" / "endpoints.yaml"
SERVER = ROOT / "ozon_mcp" / "server.py"
WORKFLOWS = ROOT / "ozon_mcp" / "workflows.yaml"
PAGINATE = ROOT / "core" / "paginate.py"
TEST_PAGINATE = ROOT / "tests" / "test_paginate.py"
TEST_RUNTIME = ROOT / "tests" / "test_ozon_runtime_migrations.py"


def replace_endpoint_block(text: str, operation_id: str, replacement: str) -> str:
    pattern = rf"(?ms)^- operation_id: {re.escape(operation_id)}\n.*?(?=^- operation_id: |\Z)"
    new_text, count = re.subn(pattern, replacement.rstrip() + ("\n" if replacement else ""), text)
    if count != 1:
        raise RuntimeError(f"expected exactly one endpoint block for {operation_id}, got {count}")
    return new_text


endpoints = ENDPOINTS.read_text(encoding="utf-8")

endpoints = replace_endpoint_block(endpoints, "ozon_fbs_unfulfilled", r'''- operation_id: ozon_fbs_unfulfilled
  section: orders_fbs
  method: POST
  host: api-seller.ozon.ru
  path: /v4/posting/fbs/unfulfilled/list
  scope: seller
  safety: read
  pagination: cursor
  summary: New/unprocessed FBS shipments awaiting assembly. Current v4 cursor contract.
  params:
    body: '{filter:{cutoff_from, cutoff_to, delivering_date_from, delivering_date_to, delivery_method_ids, provider_ids, statuses, warehouse_ids, last_changed_status_date}, limit<=100, cursor, sort_dir, translit, with:{}}'
  doc: https://docs.ozon.ru/api/seller/
  keywords:
  - новые заказы
  - необработанные
  - сборка
  - фбс
  items_path: postings
''')

endpoints = replace_endpoint_block(endpoints, "ozon_fbs_list", r'''- operation_id: ozon_fbs_list
  section: orders_fbs
  method: POST
  host: api-seller.ozon.ru
  path: /v4/posting/fbs/list
  scope: seller
  safety: read
  pagination: cursor
  summary: FBS shipments for a period. Current v4 cursor contract; filter requires since/to.
  params:
    body: '{filter:{since, to, statuses, delivery_method_ids, provider_ids, warehouse_ids, order_id, order_numbers, last_changed_status_date}, limit<=100, cursor, sort_dir, translit, with:{}}'
  doc: https://docs.ozon.ru/api/seller/
  keywords:
  - заказы фбс
  - отправления
  items_path: postings
''')

endpoints = replace_endpoint_block(endpoints, "ozon_fbo_list", r'''- operation_id: ozon_fbo_list
  section: orders_fbo
  method: POST
  host: api-seller.ozon.ru
  path: /v3/posting/fbo/list
  scope: seller
  safety: read
  pagination: cursor
  summary: FBO shipments for a period. Current v3 cursor contract.
  params:
    body: '{filter:{since, to, statuses, posting_numbers, order_numbers}, limit<=100, cursor, sort_dir, translit, with:{analytics_data, financial_data, legal_info}}'
  doc: https://docs.ozon.ru/api/seller/
  keywords:
  - заказы фбо
  - отправления фбо
  items_path: postings
''')

endpoints = replace_endpoint_block(endpoints, "ozon_finance_transactions", r'''- operation_id: ozon_finance_accrual_postings
  section: finance
  method: POST
  host: api-seller.ozon.ru
  path: /v1/finance/accrual/postings
  scope: seller
  safety: read
  pagination: none
  summary: Current Ozon accruals for 1-200 posting numbers; replaces the retired v3 transaction API for posting-scoped detail.
  params:
    body: '{posting_numbers:[1..200]}'
  doc: https://docs.ozon.ru/api/seller/
  keywords:
  - финансы
  - начисления
  - отправления
  - posting
  items_path: posting_accruals
- operation_id: ozon_finance_accrual_types
  section: finance
  method: POST
  host: api-seller.ozon.ru
  path: /v1/finance/accrual/types
  scope: seller
  safety: read
  pagination: none
  summary: Current Ozon accrual type dictionary; use IDs to interpret accrual records.
  params: {}
  doc: https://docs.ozon.ru/api/seller/
  keywords:
  - финансы
  - начисления
  - типы начислений
  - справочник
  items_path: accrual_types
- operation_id: ozon_finance_accrual_by_day
  section: finance
  method: POST
  host: api-seller.ozon.ru
  path: /v1/finance/accrual/by-day
  scope: seller
  safety: read
  pagination: last_id_no_limit
  summary: Current Ozon accruals for one calendar day. Page with last_id; the API has no limit field.
  params:
    body: '{date:YYYY-MM-DD, last_id:""}'
  doc: https://docs.ozon.ru/api/seller/
  keywords:
  - финансы
  - начисления
  - день
  - транзакции
  items_path: accruals
''')
endpoints = replace_endpoint_block(endpoints, "ozon_finance_totals", "")

for old_path in (
    "/v3/posting/fbs/unfulfilled/list",
    "/v3/posting/fbs/list",
    "/v2/posting/fbo/list",
    "/v3/finance/transaction/list",
    "/v3/finance/transaction/totals",
):
    if old_path in endpoints:
        raise RuntimeError(f"shutdown path still present in runtime catalog: {old_path}")
for new_path in (
    "/v4/posting/fbs/unfulfilled/list",
    "/v4/posting/fbs/list",
    "/v3/posting/fbo/list",
    "/v1/finance/accrual/postings",
    "/v1/finance/accrual/types",
    "/v1/finance/accrual/by-day",
):
    if new_path not in endpoints:
        raise RuntimeError(f"replacement path missing from runtime catalog: {new_path}")
ENDPOINTS.write_text(endpoints, encoding="utf-8")

# Typed convenience tool: v4 uses cursor, not offset.
server = SERVER.read_text(encoding="utf-8")
old_server = '''async def ozon_get_fbs_unfulfilled(cutoff_from: str, cutoff_to: str,\n                                   limit: int = 100, offset: int = 0) -> str:\n    """List new/unprocessed FBS shipments awaiting assembly.\n\n    Args:\n        cutoff_from: ISO datetime lower bound, e.g. "2026-06-01T00:00:00Z".\n        cutoff_to: ISO datetime upper bound.\n        limit: page size.\n        offset: pagination offset.\n    Returns JSON: {"ok": true, "data": {"result": {"postings": [...]}}}.\n    """\n    body = {"filter": {"cutoff_from": cutoff_from, "cutoff_to": cutoff_to},\n            "limit": limit, "offset": offset}\n    spec = catalog.get("ozon_fbs_unfulfilled")\n    return _j(await client.call_spec(spec, json_body=body))\n'''
new_server = '''async def ozon_get_fbs_unfulfilled(cutoff_from: str, cutoff_to: str,\n                                   limit: int = 100, cursor: str = "") -> str:\n    """List new/unprocessed FBS shipments awaiting assembly (v4).\n\n    Args:\n        cutoff_from: ISO datetime lower bound, e.g. "2026-06-01T00:00:00Z".\n        cutoff_to: ISO datetime upper bound.\n        limit: page size (1..100).\n        cursor: pagination cursor from a previous v4 response.\n    Returns JSON with root-level postings, cursor and has_next.\n    """\n    body = {"filter": {"cutoff_from": cutoff_from, "cutoff_to": cutoff_to},\n            "limit": max(1, min(limit, 100))}\n    if cursor:\n        body["cursor"] = cursor\n    spec = catalog.get("ozon_fbs_unfulfilled")\n    return _j(await client.call_spec(spec, json_body=body))\n'''
if old_server not in server:
    raise RuntimeError("typed FBS unfulfilled function no longer matches expected pre-migration text")
server = server.replace(old_server, new_server, 1)
SERVER.write_text(server, encoding="utf-8")

# Pagination: Ozon posting v4/v3 exposes has_next; finance by-day has last_id but no limit.
paginate = PAGINATE.read_text(encoding="utf-8")
paginate = paginate.replace(
    '- last_id         : Ozon — body filter, response result.last_id\n',
    '- last_id         : Ozon — body filter, response result.last_id\n- last_id_no_limit: Ozon finance by-day — last_id cursor without a limit field\n',
    1,
)
paginate = paginate.replace(
    '''        elif style == "last_id":\n            loc.setdefault("limit", limit)\n            if seen_cursor:\n                loc["last_id"] = seen_cursor\n''',
    '''        elif style in ("last_id", "last_id_no_limit"):\n            if style == "last_id":\n                loc.setdefault("limit", limit)\n            if seen_cursor:\n                loc["last_id"] = seen_cursor\n''',
    1,
)
paginate = paginate.replace(
    '''        elif style == "cursor":\n            cur = _dig(data, "cursor")\n            total = _to_int(_dig(data, "total"))\n            if total is not None and len(items) >= total:\n                return _result(items, pages, truncated=False)\n            if not cur or cur == seen_cursor:\n                return _result(items, pages, truncated=False)\n            seen_cursor = cur\n        elif style == "last_id":\n''',
    '''        elif style == "cursor":\n            # New Ozon posting APIs expose has_next instead of total. Honour it\n            # before advancing the cursor so we do not send a needless final request.\n            if _dig(data, "has_next") is False:\n                return _result(items, pages, truncated=False)\n            cur = _dig(data, "cursor")\n            total = _to_int(_dig(data, "total"))\n            if total is not None and len(items) >= total:\n                return _result(items, pages, truncated=False)\n            if not cur or cur == seen_cursor:\n                return _result(items, pages, truncated=False)\n            seen_cursor = cur\n        elif style in ("last_id", "last_id_no_limit"):\n''',
    1,
)
if "last_id_no_limit" not in paginate or 'if _dig(data, "has_next") is False' not in paginate:
    raise RuntimeError("pagination migration did not apply")
PAGINATE.write_text(paginate, encoding="utf-8")

# Unit-economics recipe must no longer call the retired transaction operation.
workflows = WORKFLOWS.read_text(encoding="utf-8")
pattern = r'(?ms)^  - name: unit_economics\n.*?(?=^  - name: catalog_sync\n)'
replacement = '''  - name: unit_economics\n    category: finance\n    when_to_use: Understand Ozon accruals and per-order economics using the current finance API.\n    steps:\n      - operation_id: ozon_finance_accrual_by_day\n        why: Current daily accrual feed; use ozon_fetch_all with date + last_id="" for every page of one day.\n      - operation_id: ozon_finance_accrual_types\n        why: Decodes accrual type identifiers without guessing their business meaning.\n      - operation_id: ozon_prices_get\n        why: Current prices and commission reference per SKU.\n    interpret: >\n      Pull each required calendar day separately. Sum total_amount using its currency,\n      retain unknown accrual identifiers instead of dropping them, and group posting-linked\n      rows by unit_number/SKU. Join type IDs through ozon_finance_accrual_types before\n      assigning business categories. The retired v3 transaction list/totals shapes are not\n      assumed to be equivalent to the accrual API.\n    common_mistakes:\n      - Calling the retired /v3/finance/transaction/list or /totals routes.\n      - Treating the three accrual endpoints as one-for-one field-compatible replacements.\n      - Dropping unknown accrual IDs or summing amounts across currencies.\n\n'''
workflows, count = re.subn(pattern, replacement, workflows)
if count != 1:
    raise RuntimeError(f"expected one unit_economics workflow, got {count}")
WORKFLOWS.write_text(workflows, encoding="utf-8")

# Extend offline paginator tests.
tests = TEST_PAGINATE.read_text(encoding="utf-8")
addition = r'''


def test_cursor_honours_has_next_false_without_extra_request():
    spec = EndpointSpec(operation_id="op", method="POST", host="h", path="/x",
                        pagination="cursor", items_path="postings")
    calls = []

    def responder(loc):
        calls.append(dict(loc))
        return {"postings": [1, 2], "cursor": "would-be-next", "has_next": False}

    out = asyncio.run(fetch_all(_FakeClient(responder), spec, limit=100))
    assert out["total_fetched"] == 2
    assert len(calls) == 1


def test_last_id_no_limit_never_injects_limit():
    spec = EndpointSpec(operation_id="op", method="POST", host="h", path="/x",
                        pagination="last_id_no_limit", items_path="accruals")
    calls = []

    def responder(loc):
        calls.append(dict(loc))
        if loc.get("last_id") == "next":
            return {"accruals": [2], "last_id": ""}
        return {"accruals": [1], "last_id": "next"}

    out = asyncio.run(fetch_all(
        _FakeClient(responder), spec,
        base_body={"date": "2026-09-07", "last_id": ""}, limit=100,
    ))
    assert out["total_fetched"] == 2
    assert len(calls) == 2
    assert all("limit" not in call for call in calls)
    assert calls[1]["last_id"] == "next"
'''
if "test_last_id_no_limit_never_injects_limit" not in tests:
    tests += addition
TEST_PAGINATE.write_text(tests, encoding="utf-8")

TEST_RUNTIME.write_text(r'''from pathlib import Path

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
''', encoding="utf-8")

print("Ozon runtime migration staged successfully")
