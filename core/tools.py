"""Generic MCP tool layer, registered identically for every marketplace.

Given a FastMCP instance, a MarketplaceClient and a Catalog, this wires up the
schema-driven meta-tools that turn hundreds of endpoints into a handful of
high-leverage tools:

    {svc}_check_auth        verify credentials are present (no secrets echoed)
    {svc}_list_sections     browse the API by section
    {svc}_get_section       list endpoints in one section
    {svc}_search_methods    token search across the catalog (RU/EN)
    {svc}_describe_method    full spec for one operation_id
    {svc}_call_method       execute a catalog endpoint (safety-gated)
    {svc}_call_raw          fail closed until a quota contract is catalogued
    {svc}_fetch_all         auto-paginate a catalog endpoint

Typed convenience tools live in each service's server.py and call the same
client underneath.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .client import MarketplaceClient
from .paginate import fetch_all as _fetch_all
from .rate_limit import parse_rate_limit
from .registry import Catalog
from .safety import check_gate, infer_safety


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def has_proven_quota(spec: Any) -> bool:
    if bool(getattr(spec, "service_quota_proven", False)):
        return True
    return bool(getattr(spec, "quota_proven", False)) and parse_rate_limit(
        str(getattr(spec, "rate_limit", ""))
    ) is not None


LEGACY_READ_OPERATION_ALIASES: dict[tuple[str, str], str] = {
    ("wb", "wb_post_api_list_goods_filter"): "wb_prices_list",
    ("wb", "wb_post_api_analytics_stocks_report_wb_warehouses"): "wb_analytics_stocks_wb_warehouses",
}


def resolve_legacy_operation_id(service: str, operation_id: str) -> tuple[str, bool]:
    canonical = LEGACY_READ_OPERATION_ALIASES.get(
        (str(service).strip().lower(), str(operation_id).strip())
    )
    return (canonical or operation_id, canonical is not None)


def resolve_equivalent_proven_read_spec(
    catalog: Catalog, spec: Any,
) -> tuple[Any, bool]:
    """Resolve an unproven read duplicate to one exact proven endpoint contract.

    Generated catalogs can contain stale/duplicate operation IDs for the same
    provider endpoint while a reviewed canonical record carries the proven
    quota contract. For read-only requests, reuse that contract only when there
    is exactly one proven read candidate with the same host + path. Method is
    intentionally not part of the identity because some stale generated WB
    records carried the wrong verb; the canonical record supplies the reviewed
    verb. Unknown or ambiguous duplicates remain fail-closed.
    """
    if has_proven_quota(spec):
        return spec, False
    if infer_safety(spec.method, spec.safety) != "read":
        return spec, False

    host = str(getattr(spec, "host", "") or "").strip().lower()
    path = str(getattr(spec, "path", "") or "").strip()
    matches: list[Any] = []
    for candidate in catalog.all():
        if candidate.operation_id == spec.operation_id:
            continue
        if str(candidate.host or "").strip().lower() != host:
            continue
        if str(candidate.path or "").strip() != path:
            continue
        if infer_safety(candidate.method, candidate.safety) != "read":
            continue
        if not has_proven_quota(candidate):
            continue
        matches.append(candidate)

    unique = {candidate.operation_id: candidate for candidate in matches}
    if len(unique) != 1:
        return spec, False
    return next(iter(unique.values())), True


def generic_read_execution_info(
    service: str, catalog: Catalog, spec: Any,
) -> dict[str, Any]:
    """Describe whether a catalog record is executable through generic read tools.

    This is deliberately a capability contract, not a guess. A read is marked
    executable only when the exact record, a service-level proof, or one unique
    equivalent canonical record carries a proven quota contract. Everything
    else is explicitly classified as blocked before a model attempts execution.
    """
    safety = infer_safety(spec.method, spec.safety)
    if safety != "read":
        return {
            "generic_read_status": "NOT_READ",
            "generic_read_executable": False,
            "generic_read_reason": "operation_is_not_read_only",
            "resolved_operation_id": spec.operation_id,
        }

    resolved, equivalent = resolve_equivalent_proven_read_spec(catalog, spec)
    if has_proven_quota(resolved):
        if equivalent:
            proof = "equivalent_proven_contract"
        elif bool(getattr(resolved, "service_quota_proven", False)):
            proof = "service_quota"
        else:
            proof = "operation_quota"
        return {
            "generic_read_status": "EXECUTABLE",
            "generic_read_executable": True,
            "generic_read_reason": "quota_contract_proven",
            "quota_proof": proof,
            "resolved_operation_id": resolved.operation_id,
            "equivalent_proven_contract_resolution": equivalent,
        }

    rate_limit = str(getattr(spec, "rate_limit", "") or "").strip()
    if not rate_limit:
        reason = "quota_contract_missing"
    elif parse_rate_limit(rate_limit) is None:
        reason = "rate_limit_not_parseable"
    else:
        reason = "rate_limit_present_but_unproven"
    return {
        "generic_read_status": "BLOCKED_QUOTA_UNPROVEN",
        "generic_read_executable": False,
        "generic_read_reason": reason,
        "quota_proof": "none",
        "resolved_operation_id": spec.operation_id,
        "equivalent_proven_contract_resolution": False,
    }


def summary_with_execution(service: str, catalog: Catalog, spec: Any) -> dict[str, Any]:
    row = spec.to_summary_dict()
    row.update(generic_read_execution_info(service, catalog, spec))
    return row


def normalize_legacy_read_inputs(
    service: str,
    requested_operation_id: str,
    *,
    resolved_operation_id: str = "",
    query: Optional[dict] = None,
    body: Optional[dict] = None,
) -> tuple[Optional[dict], Optional[dict]]:
    """Translate stale generated read-call shapes to reviewed canonical ones."""
    key = (str(service).strip().lower(), str(requested_operation_id).strip())
    q = dict(query or {})
    b = dict(body or {})

    if key == ("wb", "wb_post_api_list_goods_filter") or (
        str(service).strip().lower() == "wb" and resolved_operation_id == "wb_prices_list"
    ):
        merged: dict[str, Any] = {}
        nested = b.get("filter")
        if isinstance(nested, dict):
            merged.update(nested)
        merged.update(b)
        merged.update(q)

        result: dict[str, Any] = {}
        for field in ("limit", "offset", "filterNmID"):
            if field in merged and merged[field] is not None:
                result[field] = merged[field]

        if "filterNmID" not in result:
            for field in ("nmID", "nmId", "nm_id"):
                if merged.get(field) is not None:
                    result["filterNmID"] = merged[field]
                    break

        if "filterNmID" not in result:
            for field in ("nmIDs", "nmIds", "nm_ids"):
                values = merged.get(field)
                if isinstance(values, (list, tuple)):
                    if len(values) != 1:
                        raise ValueError(
                            "Legacy WB price operation accepts only one nmID when "
                            "resolved to wb_prices_list."
                        )
                    result["filterNmID"] = values[0]
                    break

        return (result or None), None

    if key == ("wb", "wb_post_api_analytics_stocks_report_wb_warehouses") or (
        str(service).strip().lower() == "wb"
        and resolved_operation_id == "wb_analytics_stocks_wb_warehouses"
    ):
        merged = dict(q)
        merged.update(b)
        return None, (merged or None)

    return (dict(query) if query is not None else None,
            dict(body) if body is not None else None)

def resolve_catalog_raw_spec(
    catalog: Catalog, *, method: str, path: str, host: Optional[str] = None,
) -> tuple[Optional[Any], str]:
    """Resolve an exact raw request to one approved read-only catalog contract.

    Raw execution is never an independent trust path. It may only reuse an
    existing catalog operation when method/path (and host, when supplied) match
    exactly, the operation is read-only after verb-floor safety inference, and
    its quota proof is executable. Unknown, ambiguous, templated, mutating, or
    unproven requests remain fail-closed.
    """
    verb = str(method or "").strip().upper()
    raw_path = str(path or "").strip()
    raw_host = str(host or "").strip().lower()

    matches = [
        spec for spec in catalog.all()
        if not spec.path_params
        and spec.method.upper() == verb
        and spec.path == raw_path
        and (not raw_host or spec.host.lower() == raw_host)
    ]
    if not matches:
        return None, "not_catalogued"
    if len(matches) != 1:
        return None, "ambiguous"
    spec = matches[0]
    if infer_safety(spec.method, spec.safety) != "read":
        return None, "unsafe"
    if not has_proven_quota(spec):
        return None, "quota_unproven"
    return spec, "approved"


def resolve_named_cabinet(client: MarketplaceClient, cabinet: str) -> tuple[Optional[dict[str, str]], Optional[dict]]:
    """Resolve an explicitly named cabinet without touching shared active state.

    An empty name deliberately preserves legacy single-cabinet behaviour.  Callers
    serving a named shop should pass the cabinet name, so a concurrent
    `*_use_cabinet` call cannot change the credentials for their request.
    """
    if not cabinet.strip():
        return None, None
    config = client.config
    creds, resolved = config.store.resolve_named(
        config.name, config.fields, config.env_map, cabinet
    )
    missing = [field for field in config.fields if not creds.get(field)]
    if not resolved or missing:
        return None, {
            "ok": False,
            "error": "cabinet_not_configured",
            "code": "CABINET_NOT_CONFIGURED",
            "cabinet": cabinet,
            "missing_fields": missing,
            "retryable": False,
            "message": "The requested cabinet is absent or its credentials are incomplete; no provider request was sent.",
        }
    return creds, None

def register_generic_tools(
    mcp: FastMCP,
    *,
    svc: str,
    client: MarketplaceClient,
    catalog: Catalog,
    key_help: str = "",
    entities: Optional[Any] = None,
) -> None:
    def quota_error(operation_id: str, path: str = "") -> dict:
        return {
            "ok": False, "error": "rate_limit_rule_unproven",
            "code": "RATE_LIMIT_RULE_UNPROVEN", "operation_id": operation_id,
            "endpoint": path, "retryable": False,
            "message": "Execution is refused until a provider-documented, parseable quota rule is registered.",
            "generic_read_status": "BLOCKED_QUOTA_UNPROVEN",
            "provider_call_sent": False,
            "remediation": (
                f"Use {svc}_search_methods with executable_only=true to choose "
                "an executable catalog operation, or prove this operation's quota contract."
            ),
        }

    """Register the 8 generic tools under the `{svc}_` prefix.
    key_help: human note on where to obtain the API keys (shown by check_auth).
    entities: EntityIndex instance (optional); enables the *_map tool overview.
    """

    @mcp.tool(
        name=f"{svc}_check_auth",
        annotations={"title": f"{svc.upper()} check credentials",
                     "readOnlyHint": True, "openWorldHint": False},
    )
    async def check_auth() -> str:
        """Check whether the required credentials are present in the environment.

        Does NOT reveal secret values — only reports which variables are set.
        Returns JSON: {"ready": bool, "missing": [str], "required": [str]}.
        """
        creds, source = client.config.resolve_creds()
        missing = [f for f in client.config.fields if not creds.get(f)]
        cabinets = client.config.store.list_cabinets(client.config.name)
        return _j({
            "ready": not missing,
            "active_cabinet": cabinets["active"],
            "source": source,  # cabinet name, "env", or "none"
            "cabinets": cabinets["cabinets"],
            "missing_fields": missing,
            "hint": (f"Add a cabinet with {svc}_add_cabinet, switch with "
                     f"{svc}_use_cabinet, or run install.py."),
            "where_to_get_keys": key_help,
        })

    @mcp.tool(
        name=f"{svc}_list_sections",
        annotations={"title": f"{svc.upper()} list sections",
                     "readOnlyHint": True, "openWorldHint": False},
    )
    async def list_sections() -> str:
        """List API sections and how many catalog endpoints each contains."""
        return _j({"sections": catalog.sections(), "total_endpoints": len(catalog.all())})

    @mcp.tool(
        name=f"{svc}_get_section",
        annotations={"title": f"{svc.upper()} get section",
                     "readOnlyHint": True, "openWorldHint": False},
    )
    async def get_section(section: str) -> str:
        """List all endpoints in one section.

        Args:
            section: section name (see {svc}_list_sections), e.g. "statistics".
        Returns JSON list of {operation_id, method, path, safety, summary}.
        """
        specs = catalog.in_section(section)
        if not specs:
            return _j({"error": "not_found", "message": f"No section '{section}'.",
                       "available": list(catalog.sections().keys())})
        return _j({
            "section": section,
            "endpoints": [summary_with_execution(svc, catalog, s) for s in specs],
        })

    @mcp.tool(
        name=f"{svc}_search_methods",
        annotations={"title": f"{svc.upper()} search methods",
                     "readOnlyHint": True, "openWorldHint": False},
    )
    async def search_methods(
        query: str, limit: int = 15, executable_only: bool = True,
    ) -> str:
        """Search the endpoint catalog by keyword (works in Russian and English).

        By default only operations that are actually executable through generic
        read tools are returned. This prevents an agent from selecting a known
        fail-closed operation and discovering RATE_LIMIT_RULE_UNPROVEN only at
        execution time. Pass executable_only=false for inventory/audit work.

        Args:
            query: free text, e.g. "остатки", "stocks", "update price".
            limit: max results (1-50).
            executable_only: hide quota-unproven read operations by default.
        Returns JSON with execution-aware matching endpoints.
        """
        limit = max(1, min(50, limit))
        candidates = catalog.search(query, limit=50)
        rows = [summary_with_execution(svc, catalog, s) for s in candidates]
        executable = [r for r in rows if r["generic_read_executable"]]
        blocked = [
            r for r in rows
            if r["generic_read_status"] == "BLOCKED_QUOTA_UNPROVEN"
        ]
        selected = executable if executable_only else rows
        selected = selected[:limit]
        return _j({
            "query": query,
            "executable_only": executable_only,
            "count": len(selected),
            "results": selected,
            "blocked_match_count": len(blocked),
            "blocked_matches": blocked[:5] if executable_only else [],
            "selection_policy": (
                "Results are executable through generic read tools. "
                "Blocked matches are audit-only and must not be executed."
                if executable_only else
                "Inventory mode: inspect generic_read_status before execution."
            ),
        })

    @mcp.tool(
        name=f"{svc}_map",
        annotations={"title": f"{svc.upper()} capabilities map",
                     "readOnlyHint": True, "openWorldHint": False},
    )
    async def entity_map(entity: str = "") -> str:
        """The big picture: business entities this API covers and the go-to
        methods for each. Call with no args to see the whole map ("you are
        here"); pass entity="reviews" (or stocks/prices/orders/…) to list every
        method of one entity. Use this before guessing — it orients you fast.
        """
        ents = entities.entities if entities is not None else []
        by_key: dict[str, list] = {}
        for s in catalog.all():
            for k in (s.entity or ["other"]):
                by_key.setdefault(k, []).append(s)
        if entity:
            specs = by_key.get(entity, [])
            return _j({
                "entity": entity,
                "count": len(specs),
                "methods": [summary_with_execution(svc, catalog, s) for s in specs],
            })
        out = []
        for e in ents:
            specs = by_key.get(e["key"], [])
            if not specs:
                continue
            headline = [s for s in specs if s.operation_id in e.get("headline", [])]
            # Fallback: surface read methods first so the map answers "how do I
            # see X" before "how do I change/delete X".
            ordered = sorted(
                specs,
                key=lambda s: (
                    0 if generic_read_execution_info(svc, catalog, s)["generic_read_executable"] else
                    1 if infer_safety(s.method, s.safety) == "read" else 2
                ),
            )
            shown = headline or ordered[:5]
            out.append({
                "key": e["key"], "title_ru": e["title_ru"],
                "title_en": e["title_en"], "synonyms": e["synonyms"],
                "method_count": len(specs),
                "headline": [summary_with_execution(svc, catalog, s) for s in shown],
            })
        if by_key.get("other"):
            out.append({"key": "other", "title_ru": "Прочее", "title_en": "Other",
                        "synonyms": [], "method_count": len(by_key["other"]),
                        "headline": []})
        return _j({"service": svc, "entities": out})

    @mcp.tool(
        name=f"{svc}_describe_method",
        annotations={"title": f"{svc.upper()} describe method",
                     "readOnlyHint": True, "openWorldHint": False},
    )
    async def describe_method(operation_id: str) -> str:
        """Return the full catalog record for one endpoint: method, host, path,
        scope, safety level, pagination style, rate limit, params and doc URL."""
        requested_operation_id = operation_id
        operation_id, aliased = resolve_legacy_operation_id(svc, operation_id)
        spec = catalog.get(operation_id)
        if not spec:
            hits = catalog.search(requested_operation_id, limit=5)
            return _j({"error": "not_found", "operation_id": requested_operation_id,
                       "did_you_mean": [s.operation_id for s in hits]})
        return _j({
            "operation_id": spec.operation_id,
            "requested_operation_id": requested_operation_id if aliased else spec.operation_id,
            "resolved_from_legacy_alias": aliased,
            "section": spec.section,
            "entity": spec.entity,
            "method": spec.method, "host": spec.host, "path": spec.path,
            "path_params": spec.path_params, "scope": spec.scope,
            "safety": spec.safety, "pagination": spec.pagination,
            "rate_limit": spec.rate_limit,
            "quota_proven": bool(getattr(spec, "quota_proven", False)),
            "service_quota_proven": bool(getattr(spec, "service_quota_proven", False)),
            "summary": spec.summary,
            "params": spec.params, "doc": spec.doc,
            **generic_read_execution_info(svc, catalog, spec),
        })

    @mcp.tool(
        name=f"{svc}_call_method",
        annotations={"title": f"{svc.upper()} call catalog method",
                     "readOnlyHint": False, "destructiveHint": True,
                     "openWorldHint": True},
    )
    async def call_method(
        operation_id: str,
        path_values: Optional[dict] = None,
        query: Optional[dict] = None,
        body: Optional[dict] = None,
        confirm_write: bool = False,
        i_understand_this_modifies_data: bool = False,
        cabinet: str = "",
    ) -> str:
        """Execute one catalog endpoint by operation_id.

        Read endpoints run immediately. WRITE endpoints require confirm_write=true.
        DESTRUCTIVE endpoints require confirm_write=true AND
        i_understand_this_modifies_data=true (nothing is sent otherwise).

        Args:
            operation_id: id from the catalog (see {svc}_search_methods).
            path_values: values for {placeholders} in the path.
            query: query-string parameters.
            body: JSON request body.
            confirm_write: required for write/destructive operations.
            i_understand_this_modifies_data: required for destructive operations.
        Returns JSON: {"ok": true, "status", "data"} or the error envelope.
        """
        requested_operation_id = operation_id
        operation_id, aliased = resolve_legacy_operation_id(svc, operation_id)
        spec = catalog.get(operation_id)
        if not spec:
            hits = catalog.search(requested_operation_id, limit=5)
            return _j({"error": "not_found", "operation_id": requested_operation_id,
                       "did_you_mean": [s.operation_id for s in hits]})
        spec, equivalent_contract = resolve_equivalent_proven_read_spec(catalog, spec)
        try:
            query, body = normalize_legacy_read_inputs(
                svc, requested_operation_id,
                resolved_operation_id=spec.operation_id,
                query=query, body=body,
            )
        except ValueError as exc:
            return _j({"ok": False, "error": "invalid_params",
                       "message": str(exc), "operation_id": requested_operation_id})
        # Defense in depth: never let a catalog `read` weaken the gate below the
        # HTTP verb's floor (a mislabelled PUT/PATCH/DELETE must still be gated).
        gate = check_gate(
            infer_safety(spec.method, spec.safety), confirm_write=confirm_write,
            i_understand_this_modifies_data=i_understand_this_modifies_data,
            operation_id=spec.operation_id, endpoint=spec.path,
        )
        if gate:
            return _j(gate)
        if not has_proven_quota(spec):
            return _j(quota_error(spec.operation_id, spec.path))
        creds_override, cabinet_error = resolve_named_cabinet(client, cabinet)
        if cabinet_error:
            return _j(cabinet_error)
        resp = await client.call_spec(
            spec, path_values=path_values, query=query, json_body=body,
            creds_override=creds_override,
        )
        if (aliased or equivalent_contract) and isinstance(resp, dict):
            resp.setdefault("requested_operation_id", requested_operation_id)
            resp.setdefault("resolved_operation_id", spec.operation_id)
            if aliased:
                resp.setdefault("legacy_alias_resolution", True)
            if equivalent_contract:
                resp.setdefault("equivalent_proven_contract_resolution", True)
        return _j(resp)

    @mcp.tool(
        name=f"{svc}_call_raw",
        annotations={"title": f"{svc.upper()} call raw path",
                     "readOnlyHint": False, "destructiveHint": True,
                     "openWorldHint": True},
    )
    async def call_raw(
        method: str,
        path: str,
        host: Optional[str] = None,
        query: Optional[dict] = None,
        body: Optional[dict] = None,
        confirm_write: bool = False,
        i_understand_this_modifies_data: bool = False,
        cabinet: str = "",
    ) -> str:
        """Resolve exact known reads through the catalog; otherwise fail closed.

        Raw is not a second execution policy. If method + path (+ host when
        supplied) exactly identify one catalogued read operation with a proven
        quota contract, the request is executed through that operation and the
        normal rate controller. Unknown, ambiguous, templated, write/destructive,
        or quota-unproven raw requests are refused before provider I/O.

        Args:
            method: HTTP verb (GET/POST/PUT/PATCH/DELETE).
            path: exact provider path beginning with '/'.
            host: optional exact provider host.
            query: query-string parameters.
            body: JSON request body.
            confirm_write / i_understand_this_modifies_data: retained for API
                compatibility; raw never auto-approves mutations.
            cabinet: optional exact configured cabinet name.
        Returns JSON: {"ok": true, "status", "data"} or the error envelope.
        """
        spec, resolution = resolve_catalog_raw_spec(
            catalog, method=method, path=path, host=host,
        )
        if spec is None:
            error = quota_error("raw", path)
            error["raw_resolution"] = resolution
            return _j(error)

        creds_override, cabinet_error = resolve_named_cabinet(client, cabinet)
        if cabinet_error:
            return _j(cabinet_error)
        resp = await client.call_spec(
            spec, query=query, json_body=body, creds_override=creds_override,
        )
        if isinstance(resp, dict):
            resp.setdefault("resolved_operation_id", spec.operation_id)
            resp.setdefault("raw_resolution", "catalog_read")
        return _j(resp)

    @mcp.tool(
        name=f"{svc}_fetch_all",
        annotations={"title": f"{svc.upper()} fetch all pages",
                     "readOnlyHint": True, "openWorldHint": True},
    )
    async def fetch_all_tool(
        operation_id: str,
        query: Optional[dict] = None,
        body: Optional[dict] = None,
        path_values: Optional[dict] = None,
        items_path: Optional[str] = None,
        limit: int = 1000,
        max_items: int = 10000,
        cabinet: str = "",
    ) -> str:
        """Auto-paginate a read endpoint and return every row in one response.

        Handles offset, last_id, cursor (Ozon v4/v5), page and WB lastChangeDate
        styles. The array path is taken from the catalog automatically.

        Args:
            operation_id: a read endpoint from the catalog.
            query / body / path_values: base parameters (cursor fields are managed).
            items_path: override the array path (default: the endpoint's own).
            limit: page size to request.
            max_items: hard cap to protect context (default 10000).
        Returns JSON: {"ok", "items", "total_fetched", "pages_fetched", "truncated"}.
        """
        requested_operation_id = operation_id
        operation_id, aliased = resolve_legacy_operation_id(svc, operation_id)
        spec = catalog.get(operation_id)
        if not spec:
            return _j({"error": "not_found", "operation_id": requested_operation_id})
        spec, equivalent_contract = resolve_equivalent_proven_read_spec(catalog, spec)
        try:
            query, body = normalize_legacy_read_inputs(
                svc, requested_operation_id,
                resolved_operation_id=spec.operation_id,
                query=query, body=body,
            )
        except ValueError as exc:
            return _j({"ok": False, "error": "invalid_params",
                       "message": str(exc), "operation_id": requested_operation_id})
        # Verb-floor defense in depth (same as call_method): a mutating verb
        # mislabelled `read` in the catalog must not be looped over unconfirmed.
        if infer_safety(spec.method, spec.safety) != "read":
            return _j({"error": "invalid_params",
                       "message": "fetch_all only runs read endpoints; "
                                  f"{operation_id} is a {spec.method} write."})
        if not has_proven_quota(spec):
            return _j(quota_error(spec.operation_id, spec.path))
        creds_override, cabinet_error = resolve_named_cabinet(client, cabinet)
        if cabinet_error:
            return _j(cabinet_error)
        resp = await _fetch_all(
            client, spec, base_query=query, base_body=body, path_values=path_values,
            items_path=items_path, limit=limit, max_items=max_items,
            creds_override=creds_override,
        )
        if (aliased or equivalent_contract) and isinstance(resp, dict):
            resp.setdefault("requested_operation_id", requested_operation_id)
            resp.setdefault("resolved_operation_id", spec.operation_id)
            if aliased:
                resp.setdefault("legacy_alias_resolution", True)
            if equivalent_contract:
                resp.setdefault("equivalent_proven_contract_resolution", True)
        return _j(resp)


def _dig(data: Any, dotted: str) -> Any:
    """Follow a dotted path into nested dicts; return None if any hop misses."""
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


async def fetch_shop_name(catalog: Optional[Catalog], client: MarketplaceClient,
                          creds: dict) -> Optional[str]:
    """Best-effort shop name from the marketplace's seller-info endpoint, using
    the given (possibly not-yet-stored) creds. Returns None on any problem —
    never raises. A failure here never blocks saving the key (the token may
    simply lack the seller-info scope)."""
    whoami = getattr(client.config, "whoami", None)
    if not whoami or catalog is None:
        return None
    op_id, name_fields = whoami
    spec = catalog.get(op_id)
    if spec is None:
        return None
    try:
        body = None if spec.method.upper() in ("GET", "HEAD") else {}
        resp = await client.call_spec(spec, json_body=body, creds_override=creds)
    except Exception:  # noqa: BLE001 — naming is best-effort, never fatal
        return None
    if not isinstance(resp, dict) or not resp.get("ok"):
        return None
    data = resp.get("data")
    for f in name_fields:
        val = _dig(data, f)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _consent_block(svc: str, consent: bool) -> Optional[dict]:
    """Return an error dict if the chat-secret consent flag is not set, else None.

    Putting a key into a chat tool means it lands in the transcript. We require
    explicit acknowledgement and point at the safe door (the installer)."""
    if consent:
        return None
    return {
        "error": "consent_required",
        "message": (
            "This puts the API key into the chat transcript (the provider keeps "
            "chat history). If that's acceptable — use a scoped key and rotate it "
            "if exposed — call again with i_understand_key_goes_to_chat=true. "
            f"Safer alternative (key never enters chat): run the installer "
            f"(install.py / double-click)."),
        "safe_alternative": "installer",
    }


async def _store_key_core(*, config, catalog: Optional[Catalog],
                          client: MarketplaceClient, credentials: dict,
                          cabinet: str, consent: bool) -> dict:
    """Shared logic for the consented, auto-naming chat key tools. Testable
    without the MCP layer. Resolution order for the target cabinet:
    explicit name -> active cabinet -> shop name from API -> "main"."""
    block = _consent_block(config.name, consent)
    if block:
        return block
    missing = [f for f in config.fields if not credentials.get(f)]
    if missing:
        return {"error": "invalid_params",
                "message": f"Missing required field(s): {', '.join(missing)}.",
                "fields_needed": config.fields}
    clean = {f: str(credentials[f]) for f in config.fields}
    shop = await fetch_shop_name(catalog, client, clean)
    active = config.store.list_cabinets(config.name).get("active")
    target = cabinet or active or shop or "main"
    config.store.add_cabinet(config.name, target, clean, make_active=True)
    info = config.store.list_cabinets(config.name)
    return {
        "ok": True,
        "cabinet": target,
        "shop_name": shop,
        "validated": shop is not None,
        "note": (f"Shop confirmed: {shop}." if shop else
                 "Saved. Couldn't fetch the shop name (the key may lack that "
                 "scope, or the lookup isn't live-verified for this marketplace) "
                 "— the key is stored anyway."),
        "active": info["active"],
        "cabinets": info["cabinets"],
    }


def register_cabinet_tools(mcp: FastMCP, *, svc: str, client: MarketplaceClient,
                           catalog: Optional[Catalog] = None) -> None:
    """Register cabinet-management tools so students can add/switch credentials
    from chat without editing files. Keys are stored locally (chmod 600)."""
    config = client.config

    @mcp.tool(
        name=f"{svc}_list_cabinets",
        annotations={"title": f"{svc.upper()} list cabinets",
                     "readOnlyHint": True, "openWorldHint": False},
    )
    async def list_cabinets() -> str:
        """List configured cabinets for this marketplace and which one is active.

        Returns JSON: {"active": str|null, "cabinets": [names], "fields_needed": [...]}.
        Secret values are never returned.
        """
        info = config.store.list_cabinets(config.name)
        return _j({**info, "fields_needed": config.fields})

    @mcp.tool(
        name=f"{svc}_add_cabinet",
        annotations={"title": f"{svc.upper()} add cabinet",
                     "readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": False},
    )
    async def add_cabinet(credentials: dict, name: str = "",
                          i_understand_key_goes_to_chat: bool = False) -> str:
        """Add or update a cabinet (a named set of API credentials), from chat.

        ⚠️ This puts the key into the chat transcript — requires
        i_understand_key_goes_to_chat=true. The terminal-free safe alternative is
        the installer (install.py / double-click), where the key never enters chat.

        Args:
            credentials: dict with the required fields for this service
                ({fields}). For Ozon: {{"client_id": "...", "api_key": "..."}};
                for WB: {{"token": "..."}}.
            name: optional label. If omitted, the cabinet is named after the real
                shop name fetched from the marketplace (falls back to "main").
            i_understand_key_goes_to_chat: must be true to proceed.
        Saved to ~/.marketplace-mcp/cabinets.json (local, chmod 600), never echoed.
        """
        res = await _store_key_core(
            config=config, catalog=catalog, client=client,
            credentials=credentials, cabinet=name,
            consent=i_understand_key_goes_to_chat)
        return _j(res)

    @mcp.tool(
        name=f"{svc}_set_key",
        annotations={"title": f"{svc.upper()} set / rotate key",
                     "readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": False},
    )
    async def set_key(credentials: dict, cabinet: str = "",
                      i_understand_key_goes_to_chat: bool = False) -> str:
        """Change / rotate the API key from chat (e.g. the old one expired or
        leaked).

        ⚠️ The key goes into the chat transcript — requires
        i_understand_key_goes_to_chat=true. The safe, terminal-free alternative
        is the installer, where the key never enters chat. Use a scoped key and
        rotate it in the seller cabinet if it was exposed.

        Args:
            credentials: dict with the required fields ({fields}).
            cabinet: which cabinet to update. Default: the active one (so "my key
                expired" just works). If there is none, the cabinet is named from
                the marketplace's shop name, else "main".
            i_understand_key_goes_to_chat: must be true to proceed.
        On success the key is validated against the marketplace and the shop name
        is reported. Saved locally (chmod 600), never echoed back.
        """
        res = await _store_key_core(
            config=config, catalog=catalog, client=client,
            credentials=credentials, cabinet=cabinet,
            consent=i_understand_key_goes_to_chat)
        return _j(res)

    @mcp.tool(
        name=f"{svc}_use_cabinet",
        annotations={"title": f"{svc.upper()} switch cabinet",
                     "readOnlyHint": False, "idempotentHint": True,
                     "openWorldHint": False},
    )
    async def use_cabinet(name: str) -> str:
        """Switch the active cabinet. Subsequent API calls use its credentials.

        Args:
            name: the cabinet to activate (see {svc}_list_cabinets).
        """
        ok = config.store.set_active(config.name, name)
        if not ok:
            info = config.store.list_cabinets(config.name)
            return _j({"error": "not_found", "name": name,
                       "available": info["cabinets"]})
        return _j({"ok": True, "active": name})

    @mcp.tool(
        name=f"{svc}_remove_cabinet",
        annotations={"title": f"{svc.upper()} remove cabinet",
                     "readOnlyHint": False, "destructiveHint": True,
                     "openWorldHint": False},
    )
    async def remove_cabinet(name: str) -> str:
        """Delete a stored cabinet. If it was active, another becomes active.

        Args:
            name: the cabinet to remove.
        """
        ok = config.store.remove_cabinet(config.name, name)
        info = config.store.list_cabinets(config.name)
        return _j({"ok": ok, "removed": name if ok else None,
                   "active": info["active"], "cabinets": info["cabinets"]})
