from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

import yaml

KNOWLEDGE_CATALOG_PATH = Path(__file__).with_name("marketplace_knowledge_catalog.yaml")
_ALLOWED_STATUSES = {"verified", "provisional", "source_field", "deprecated", "broken"}


class MarketplaceKnowledgeError(RuntimeError):
    """Raised when marketplace human-semantic knowledge is invalid."""


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MarketplaceKnowledgeError(f"{name} must be a mapping")
    return value


def _require_string_list(value: Any, name: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise MarketplaceKnowledgeError(
            f"{name} must be a {'possibly empty ' if allow_empty else 'non-empty '}string list"
        )
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise MarketplaceKnowledgeError(f"{name} must contain only non-empty strings")
    return value


def validate_marketplace_knowledge_catalog(data: dict[str, Any]) -> None:
    policy = _require_mapping(data.get("policy"), "knowledge policy")
    if policy.get("verified_requires_official_sources") is not True:
        raise MarketplaceKnowledgeError("verified knowledge must require official sources")
    if policy.get("provider_field_name_is_not_business_definition") is not True:
        raise MarketplaceKnowledgeError("provider field names must not define business meaning")
    if policy.get("production_auto_update") is not False:
        raise MarketplaceKnowledgeError("knowledge updates must not silently auto-change production")

    sources = _require_mapping(data.get("sources"), "knowledge sources")
    metrics = _require_mapping(data.get("metrics"), "knowledge metrics")
    if not metrics:
        raise MarketplaceKnowledgeError("knowledge metrics must not be empty")

    for source_id, raw_source in sources.items():
        source = _require_mapping(raw_source, f"source {source_id}")
        for field in ("marketplace", "title", "url", "source_version", "checked_at"):
            if not isinstance(source.get(field), str) or not source[field].strip():
                raise MarketplaceKnowledgeError(f"source {source_id} must define {field}")

    for metric_id, raw_metric in metrics.items():
        metric = _require_mapping(raw_metric, f"knowledge metric {metric_id}")
        for field in ("marketplace", "label_ru", "definition_ru", "unit", "source_checked_at"):
            if not isinstance(metric.get(field), str) or not metric[field].strip():
                raise MarketplaceKnowledgeError(f"knowledge metric {metric_id} must define {field}")

        status = metric.get("semantic_status")
        if status not in _ALLOWED_STATUSES:
            raise MarketplaceKnowledgeError(
                f"knowledge metric {metric_id} has unsupported semantic_status {status!r}"
            )

        _require_string_list(metric.get("dimensions"), f"knowledge metric {metric_id} dimensions")
        _require_string_list(metric.get("aliases_ru"), f"knowledge metric {metric_id} aliases_ru", allow_empty=True)
        _require_string_list(metric.get("aliases_en"), f"knowledge metric {metric_id} aliases_en", allow_empty=True)
        _require_string_list(
            metric.get("historical_names"),
            f"knowledge metric {metric_id} historical_names",
            allow_empty=True,
        )

        binding = _require_mapping(metric.get("provider_binding"), f"knowledge metric {metric_id} provider_binding")
        for field in ("source_id", "field_path"):
            if not isinstance(binding.get(field), str) or not binding[field].strip():
                raise MarketplaceKnowledgeError(
                    f"knowledge metric {metric_id} provider_binding must define {field}"
                )

        source_refs = _require_string_list(
            metric.get("source_refs"), f"knowledge metric {metric_id} source_refs"
        )
        missing = [source_id for source_id in source_refs if source_id not in sources]
        if missing:
            raise MarketplaceKnowledgeError(
                f"knowledge metric {metric_id} references unknown sources: {missing}"
            )
        if status == "verified":
            non_official = [
                source_id for source_id in source_refs
                if sources[source_id].get("official") is not True
            ]
            if non_official:
                raise MarketplaceKnowledgeError(
                    f"verified knowledge metric {metric_id} has non-official sources: {non_official}"
                )


def load_marketplace_knowledge_catalog(
    path: str | Path | None = None,
) -> dict[str, Any]:
    catalog_path = Path(path) if path is not None else KNOWLEDGE_CATALOG_PATH
    with catalog_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    data = _require_mapping(raw, "marketplace knowledge catalog")
    validate_marketplace_knowledge_catalog(data)
    return data


def get_knowledge_metric(
    metric_id: str,
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    data = catalog if catalog is not None else load_marketplace_knowledge_catalog()
    metric = (data.get("metrics") or {}).get(str(metric_id or ""))
    return deepcopy(metric) if isinstance(metric, dict) else None


def validate_knowledge_against_metric_registry(
    catalog: dict[str, Any],
    metric_registry: dict[str, Any],
) -> None:
    """Cross-check verified human semantics against provider mappings.

    The knowledge layer owns names/definitions. The metric registry owns
    executable provider bindings. A verified entry is invalid if those layers
    disagree.
    """
    registry_metrics = _require_mapping(metric_registry.get("metrics"), "metric registry metrics")
    for metric_id, metric in (catalog.get("metrics") or {}).items():
        if metric_id not in registry_metrics:
            raise MarketplaceKnowledgeError(
                f"knowledge metric {metric_id} is absent from metric registry"
            )
        marketplace = str(metric["marketplace"])
        binding = metric["provider_binding"]
        provider = (registry_metrics[metric_id].get("provider_mappings") or {}).get(marketplace)
        if not isinstance(provider, dict):
            raise MarketplaceKnowledgeError(
                f"metric registry {metric_id} has no provider mapping for {marketplace}"
            )
        if provider.get("source_id") != binding.get("source_id"):
            raise MarketplaceKnowledgeError(
                f"knowledge/registry source mismatch for {metric_id}"
            )
        fields = list(provider.get("fields") or [])
        if binding.get("field_path") not in fields:
            raise MarketplaceKnowledgeError(
                f"knowledge/registry field mismatch for {metric_id}: {binding.get('field_path')}"
            )



def verify_marketplace_knowledge(
    *,
    metric_registry: dict[str, Any] | None = None,
    today: date | None = None,
    max_source_age_days: int = 30,
) -> dict[str, Any]:
    """Read-only integrity/freshness check for the human-semantic knowledge layer."""
    from .metric_registry import load_metric_registry

    checked_on = today or date.today()
    catalog = load_marketplace_knowledge_catalog()
    registry = metric_registry if metric_registry is not None else load_metric_registry()
    errors: list[str] = []
    try:
        validate_knowledge_against_metric_registry(catalog, registry)
    except Exception as exc:
        errors.append(str(exc))

    stale_sources: list[dict[str, Any]] = []
    for source_id, source in (catalog.get("sources") or {}).items():
        raw_checked = str(source.get("checked_at") or "")
        try:
            checked = date.fromisoformat(raw_checked)
            age_days = (checked_on - checked).days
        except ValueError:
            age_days = None
        if age_days is None or age_days > max_source_age_days:
            stale_sources.append({
                "source_id": source_id,
                "checked_at": raw_checked or None,
                "age_days": age_days,
                "url": source.get("url"),
            })

    if errors:
        status = "FAIL"
    elif stale_sources:
        status = "STALE_REVIEW_REQUIRED"
    else:
        status = "PASS"

    return {
        "ok": not errors,
        "status": status,
        "catalog_version": catalog.get("version"),
        "checked_on": checked_on.isoformat(),
        "max_source_age_days": int(max_source_age_days),
        "verified_metric_count": sum(
            1 for metric in (catalog.get("metrics") or {}).values()
            if metric.get("semantic_status") == "verified"
        ),
        "source_count": len(catalog.get("sources") or {}),
        "stale_sources": stale_sources,
        "errors": errors,
        "production_auto_update": False,
        "rule": "Detected semantic/source changes require review before production meaning is changed.",
    }


def register_marketplace_knowledge_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="marketplace_knowledge_verify",
        annotations={
            "title": "Verify marketplace business-meaning knowledge",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_knowledge_verify(max_source_age_days: int = 30) -> str:
        import json

        return json.dumps(
            verify_marketplace_knowledge(max_source_age_days=max_source_age_days),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
