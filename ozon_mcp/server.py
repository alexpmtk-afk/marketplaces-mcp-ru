#!/usr/bin/env python3
"""ozon_mcp — MCP server for the Ozon Seller API.

Exposes the whole Ozon Seller API through schema-driven meta-tools (search /
describe / call / fetch_all) plus typed convenience tools for everyday tasks.
Single host (api-seller.ozon.ru). Credentials come from the environment.

Auth: two flat headers — Client-Id and Api-Key.

Run:
    OZON_CLIENT_ID=... OZON_API_KEY=... python -m ozon_mcp.server
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from core.client import MarketplaceClient, ServiceConfig
from core.entities import EntityIndex
from core.registry import Catalog
from core.safety import check_gate
from core.tools import register_cabinet_tools, register_generic_tools, resolve_named_cabinet
from core.workflows import Workflows, register_workflow_tools

CATALOG_PATH = Path(__file__).with_name("endpoints.yaml")
WORKFLOWS_PATH = Path(__file__).with_name("workflows.yaml")


def _build_headers(creds: dict[str, str]) -> dict[str, str]:
    return {
        "Client-Id": creds.get("client_id", ""),
        "Api-Key": creds.get("api_key", ""),
        "Content-Type": "application/json",
    }


OZON_CONFIG = ServiceConfig(
    name="ozon",
    scheme="https",
    fields=["client_id", "api_key"],
    env_map={"client_id": "OZON_CLIENT_ID", "api_key": "OZON_API_KEY"},
    build_headers=_build_headers,
    # POST /v1/seller/info — exact name field not yet live-verified; we try a few
    # candidates and fall back gracefully if none match.
    whoami=("ozon_post_v1_seller_info",
            ["name", "company_name", "result.name", "result.company_name"]),
    # Client-Id / Api-Key may only be sent to Ozon hosts.
    allowed_host_suffixes=[".ozon.ru"],
)

mcp = FastMCP("ozon_mcp")
entities = EntityIndex.load()
catalog = Catalog.from_yaml(CATALOG_PATH, entities=entities)
client = MarketplaceClient(OZON_CONFIG)

register_generic_tools(
    mcp, svc="ozon", client=client, catalog=catalog, entities=entities,
    key_help="seller.ozon.ru → Settings → API keys (Client-Id + Api-Key).",
)
register_cabinet_tools(mcp, svc="ozon", client=client, catalog=catalog)
register_workflow_tools(mcp, svc="ozon", workflows=Workflows.from_yaml(WORKFLOWS_PATH))


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


# --------------------------------------------------------------------------
# Named-cabinet analytics — deterministic across concurrent MCP requests.
# --------------------------------------------------------------------------
@mcp.tool(
    name="ozon_get_revenue_summary",
    annotations={"title": "Ozon ordered revenue for one cabinet", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def ozon_get_revenue_summary(cabinet: str, date_from: str, date_to: str = "") -> str:
    """Return Ozon ordered revenue for one explicitly named cabinet and date range.

    This tool never changes the shared active cabinet. It is therefore safe when
    several chats query different Ozon shops at the same time.

    Args:
        cabinet: exact cabinet name from ozon_list_cabinets.
        date_from: first calendar day, YYYY-MM-DD.
        date_to: last calendar day, YYYY-MM-DD (defaults to date_from).
    Returns JSON with revenue in RUB or the standard provider error envelope.
    """
    creds_override, cabinet_error = resolve_named_cabinet(client, cabinet)
    if cabinet_error:
        return _j(cabinet_error)
    assert creds_override is not None
    end = date_to or date_from
    spec = catalog.get("ozon_analytics_data")
    if spec is None:
        return _j({
            "ok": False, "error": "rate_limit_rule_unproven",
            "code": "RATE_LIMIT_RULE_UNPROVEN", "retryable": False,
            "message": "The Ozon analytics quota contract is absent from the runtime catalog.",
        })
    body = {
        "date_from": date_from, "date_to": end,
        "metrics": ["revenue"], "dimension": ["day"],
        "filters": [], "sort": [], "limit": 100, "offset": 0,
    }
    response = await client.call_spec(spec, json_body=body, creds_override=creds_override)
    if not response.get("ok"):
        return _j(response)
    result = ((response.get("data") or {}).get("result") or {})
    totals = result.get("totals") or []
    revenue = totals[0] if totals else 0
    return _j({
        "ok": True,
        "cabinet": cabinet,
        "date_from": date_from,
        "date_to": end,
        "revenue": revenue,
        "currency": "RUB",
        "source": "ozon_analytics_data",
    })


@mcp.tool(
    name="ozon_quota_scope_status",
    annotations={"title": "Ozon cabinet quota scope status", "readOnlyHint": True,
                 "openWorldHint": False},
)
async def ozon_quota_scope_status() -> str:
    """Show which configured Ozon cabinets share a quota owner, without IDs or keys.

    Cabinet names in one group use the same provider quota identity. Raw
    Client-Ids, API keys and internal hashes are never returned.
    """
    info = client.config.store.list_cabinets(client.config.name)
    groups: dict[str, list[str]] = {}
    incomplete: list[str] = []
    for cabinet in info["cabinets"]:
        creds, resolved = client.config.store.resolve_named(
            client.config.name, client.config.fields, client.config.env_map, cabinet
        )
        if not resolved or any(not creds.get(field) for field in client.config.fields):
            incomplete.append(cabinet)
            continue
        try:
            scope = client._quota_key(client.config, creds)
        except ValueError:
            incomplete.append(cabinet)
            continue
        groups.setdefault(scope, []).append(cabinet)
    return _j({
        "ok": not incomplete,
        "quota_scope_groups": sorted(sorted(group) for group in groups.values()),
        "incomplete_cabinets": sorted(incomplete),
    })


# --------------------------------------------------------------------------
# Typed convenience tools — everyday workflows.
# --------------------------------------------------------------------------
@mcp.tool(
    name="ozon_get_products",
    annotations={"title": "Ozon product list", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def ozon_get_products(visibility: str = "ALL", limit: int = 100,
                            last_id: str = "") -> str:
    """List Ozon products (one page).

    Args:
        visibility: ALL | VISIBLE | INVISIBLE | ARCHIVED | IN_SALE ...
        limit: page size (<=1000).
        last_id: cursor from a previous page (empty for first page).
    Returns JSON: {"ok": true, "data": {"result": {"items": [...], "last_id": "..."}}}.
    For every product across pages use ozon_fetch_all with ozon_product_list.
    """
    body = {"filter": {"visibility": visibility}, "limit": min(limit, 1000)}
    if last_id:
        body["last_id"] = last_id
    spec = catalog.get("ozon_product_list")
    return _j(await client.call_spec(spec, json_body=body))


@mcp.tool(
    name="ozon_get_stocks",
    annotations={"title": "Ozon stocks", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def ozon_get_stocks(visibility: str = "ALL", limit: int = 100,
                          last_id: str = "") -> str:
    """Get available + reserved stock per product (v4/product/info/stocks).

    Args:
        visibility: product visibility filter (default ALL).
        limit: page size (<=1000).
        last_id: cursor for pagination.
    Returns JSON with stock per product (present, reserved) per warehouse type.
    """
    body = {"filter": {"visibility": visibility}, "limit": min(limit, 1000)}
    if last_id:
        body["last_id"] = last_id
    spec = catalog.get("ozon_stocks_info")
    return _j(await client.call_spec(spec, json_body=body))


@mcp.tool(
    name="ozon_get_prices",
    annotations={"title": "Ozon prices", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def ozon_get_prices(visibility: str = "ALL", limit: int = 100,
                          cursor: str = "") -> str:
    """Get prices, commissions and price indexes per product (v5/product/info/prices).

    Args:
        visibility: product visibility filter (default ALL).
        limit: page size (<=1000).
        cursor: pagination cursor from a previous response.
    Returns JSON with price, marketing_seller_price, min_price, commissions, price_indexes.
    """
    body = {"filter": {"visibility": visibility}, "limit": min(limit, 1000)}
    if cursor:
        body["cursor"] = cursor
    spec = catalog.get("ozon_prices_get")
    return _j(await client.call_spec(spec, json_body=body))


@mcp.tool(
    name="ozon_get_fbs_unfulfilled",
    annotations={"title": "Ozon unfulfilled FBS orders", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def ozon_get_fbs_unfulfilled(cutoff_from: str, cutoff_to: str,
                                   limit: int = 100, cursor: str = "") -> str:
    """List new/unprocessed FBS shipments awaiting assembly (v4).

    Args:
        cutoff_from: ISO datetime lower bound, e.g. "2026-06-01T00:00:00Z".
        cutoff_to: ISO datetime upper bound.
        limit: page size (1..100).
        cursor: pagination cursor from a previous v4 response.
    Returns JSON with root-level postings, cursor and has_next.
    """
    body = {"filter": {"cutoff_from": cutoff_from, "cutoff_to": cutoff_to},
            "limit": max(1, min(limit, 100))}
    if cursor:
        body["cursor"] = cursor
    spec = catalog.get("ozon_fbs_unfulfilled")
    return _j(await client.call_spec(spec, json_body=body))


@mcp.tool(
    name="ozon_set_price",
    annotations={"title": "Ozon set price", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": True},
)
async def ozon_set_price(offer_id: str, price: str, old_price: str = "0",
                         min_price: str = "0", currency_code: str = "RUB",
                         confirm_write: bool = False) -> str:
    """Set the price for ONE product by offer_id (v1/product/import/prices). WRITE.

    Requires confirm_write=true. Ozon limits price updates to ~10/product/hour.
    Prices are strings. old_price="0" clears the strikethrough old price.

    Args:
        offer_id: seller's article (offer_id).
        price: new price as a string, e.g. "1499".
        old_price: pre-discount price as string, or "0" to clear.
        min_price: minimum price as string, or "0".
        currency_code: default "RUB".
        confirm_write: must be true to send.
    Returns JSON: {"ok": true, "data": {"result": [{"offer_id", "updated", "errors"}]}}.
    """
    gate = check_gate("write", confirm_write=confirm_write,
                      i_understand_this_modifies_data=True,
                      operation_id="ozon_set_price",
                      endpoint="/v1/product/import/prices")
    if gate:
        return _j(gate)
    body = {"prices": [{
        "offer_id": offer_id, "price": price, "old_price": old_price,
        "min_price": min_price, "currency_code": currency_code,
    }]}
    spec = catalog.get("ozon_prices_update")
    product_hash = hashlib.sha256(offer_id.encode("utf-8")).hexdigest()
    product_scope = f"seller:price-product:{product_hash}"
    return _j(await client.request(
        "POST", spec.host, spec.path, json_body=body, operation_id=spec.operation_id,
        rate_limit="10 req/hour", rate_scope=product_scope,
    ))


def main() -> None:
    """Console entry point (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
