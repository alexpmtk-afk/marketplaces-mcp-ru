"""Fail-closed checks for quota contracts loaded by the production runtime.

This module protects the REMOTE service from a subtle deployment failure mode:
the process can be healthy at the HTTP/MCP layer while importing a stale
catalog or stale Python package whose generic quota gate refuses known-safe
business reads with RATE_LIMIT_RULE_UNPROVEN.

The checks below intentionally cover only business-critical operations whose
quota contract has been reviewed. Unknown/unproven catalog operations remain
fail-closed in core.tools.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from .tools import has_proven_quota


CRITICAL_QUOTA_CONTRACTS: tuple[dict[str, str], ...] = (
    {
        "service": "wb",
        "operation_id": "wb_prices_list",
        "method": "GET",
        "host": "discounts-prices-api.wildberries.ru",
        "path": "/api/v2/list/goods/filter",
        "proof": "method",
        "rate_limit": "10 req/6s",
    },
    {
        "service": "wb",
        "operation_id": "wb_analytics_stocks_wb_warehouses",
        "method": "POST",
        "host": "seller-analytics-api.wildberries.ru",
        "path": "/api/analytics/v1/stocks-report/wb-warehouses",
        "proof": "method",
        "rate_limit": "3 req/min",
    },
    {
        "service": "ozon",
        "operation_id": "ozon_prices_get",
        "method": "POST",
        "host": "api-seller.ozon.ru",
        "path": "/v5/product/info/prices",
        "proof": "service",
        "rate_limit": "",
    },
    {
        "service": "ozon",
        "operation_id": "ozon_stocks_info",
        "method": "POST",
        "host": "api-seller.ozon.ru",
        "path": "/v4/product/info/stocks",
        "proof": "service",
        "rate_limit": "",
    },
)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _file_state(path: object) -> dict[str, str | None]:
    resolved = Path(str(path)).resolve()
    return {
        "path": str(resolved),
        "sha256": _sha256(resolved),
    }


def _module_provenance(module: Any) -> dict[str, Any]:
    module_file = getattr(module, "__file__", "")
    catalog_path = getattr(module, "CATALOG_PATH", "")
    result: dict[str, Any] = {
        "module": _file_state(module_file) if module_file else None,
        "catalog": _file_state(catalog_path) if catalog_path else None,
    }
    if catalog_path:
        override = Path(str(catalog_path)).with_name("runtime_overrides.yaml")
        result["runtime_overrides"] = _file_state(override) if override.exists() else None
    return result


def audit_runtime_contracts(
    modules: Mapping[str, Any], *, required_services: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Audit the exact in-memory catalogs used by one or more running services.

    ``required_services`` narrows the audit for standalone WB/Ozon entrypoints.
    When omitted, the combined server still requires every critical contract.
    """
    errors: list[str] = []
    contracts: list[dict[str, Any]] = []
    required = set(required_services) if required_services is not None else {"wb", "ozon"}

    for expected in CRITICAL_QUOTA_CONTRACTS:
        if expected["service"] not in required:
            continue
        service = expected["service"]
        operation_id = expected["operation_id"]
        module = modules.get(service)
        if module is None:
            errors.append(f"{service}:{operation_id}: service module is not loaded")
            continue

        spec = module.catalog.get(operation_id)
        if spec is None:
            errors.append(f"{service}:{operation_id}: operation is missing from loaded catalog")
            continue

        row = {
            "service": service,
            "operation_id": operation_id,
            "method": spec.method,
            "host": spec.host,
            "path": spec.path,
            "rate_limit": spec.rate_limit,
            "quota_proven": bool(getattr(spec, "quota_proven", False)),
            "service_quota_proven": bool(getattr(spec, "service_quota_proven", False)),
            "executable": has_proven_quota(spec),
        }
        contracts.append(row)

        for field in ("method", "host", "path", "rate_limit"):
            if str(row[field]) != expected[field]:
                errors.append(
                    f"{service}:{operation_id}: {field}={row[field]!r}, "
                    f"expected {expected[field]!r}"
                )

        if expected["proof"] == "method" and not row["quota_proven"]:
            errors.append(f"{service}:{operation_id}: quota_proven is not true")
        if expected["proof"] == "service" and not row["service_quota_proven"]:
            errors.append(f"{service}:{operation_id}: service_quota_proven is not true")
        if not row["executable"]:
            errors.append(
                f"{service}:{operation_id}: loaded contract would return "
                "RATE_LIMIT_RULE_UNPROVEN"
            )

    provenance = {
        service: _module_provenance(module)
        for service, module in modules.items()
        if service in {"wb", "ozon"}
    }
    return {
        "ok": not errors,
        "python": sys.executable,
        "contracts": contracts,
        "errors": errors,
        "provenance": provenance,
    }


def assert_runtime_contracts(
    modules: Mapping[str, Any], *, required_services: Iterable[str] | None = None,
) -> None:
    """Refuse to start when critical loaded contracts are stale/incompatible."""
    report = audit_runtime_contracts(modules, required_services=required_services)
    if report["ok"]:
        return
    details = "; ".join(report["errors"])
    raise RuntimeError(f"critical marketplace runtime contract preflight failed: {details}")


def assert_service_runtime_contract(service: str, module: Any) -> None:
    """Fail-fast guard for standalone marketplace service entrypoints.

    Without this guard a stale standalone WB/Ozon process can complete the MCP
    handshake and only reveal its bad quota catalog on the first provider call.
    """
    normalized = str(service).strip().lower()
    if normalized not in {"wb", "ozon"}:
        return
    assert_runtime_contracts(
        {normalized: module}, required_services={normalized},
    )
