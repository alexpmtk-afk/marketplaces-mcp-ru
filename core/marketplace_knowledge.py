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


def _semantic_metric_id(knowledge_id: str, metric: dict[str, Any]) -> str:
    value = metric.get("metric_id", knowledge_id)
    if not isinstance(value, str) or not value.strip():
        raise MarketplaceKnowledgeError(
            f"knowledge metric {knowledge_id} metric_id must be a non-empty string"
        )
    return value.strip()


def _binding_field_paths(binding: dict[str, Any], knowledge_id: str) -> list[str]:
    single = binding.get("field_path")
    multiple = binding.get("field_paths")
    if single is not None and multiple is not None:
        raise MarketplaceKnowledgeError(
            f"knowledge metric {knowledge_id} provider_binding must use field_path or field_paths, not both"
        )
    if isinstance(single, str) and single.strip():
        return [single.strip()]
    if multiple is not None:
        return _require_string_list(
            multiple,
            f"knowledge metric {knowledge_id} provider_binding field_paths",
        )
    raise MarketplaceKnowledgeError(
        f"knowledge metric {knowledge_id} provider_binding must define field_path or field_paths"
    )


def validate_marketplace_knowledge_catalog(data: dict[str, Any]) -> None:
    policy = _require_mapping(data.get("policy"), "knowledge policy")
    if policy.get("verified_requires_official_sources") is not True:
        raise MarketplaceKnowledgeError("verified knowledge must require official sources")
    if policy.get("provider_field_name_is_not_business_definition") is not True:
        raise MarketplaceKnowledgeError("provider fields must not define business meaning")
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

    binding_owners: dict[tuple[str, str, str], str] = {}
    for knowledge_id, raw_metric in metrics.items():
        metric = _require_mapping(raw_metric, f"knowledge metric {knowledge_id}")
        semantic_metric_id = _semantic_metric_id(knowledge_id, metric)
        for field in ("marketplace", "label_ru", "definition_ru", "unit", "source_checked_at"):
            if not isinstance(metric.get(field), str) or not metric[field].strip():
                raise MarketplaceKnowledgeError(f"knowledge metric {knowledge_id} must define {field}")

        status = metric.get("semantic_status")
        if status not in _ALLOWED_STATUSES:
            raise MarketplaceKnowledgeError(
                f"knowledge metric {knowledge_id} has unsupported semantic_status {status!r}"
            )

        _require_string_list(metric.get("dimensions"), f"knowledge metric {knowledge_id} dimensions")
        _require_string_list(metric.get("aliases_ru"), f"knowledge metric {knowledge_id} aliases_ru", allow_empty=True)
        _require_string_list(metric.get("aliases_en"), f"knowledge metric {knowledge_id} aliases_en", allow_empty=True)
        _require_string_list(
            metric.get("historical_names"),
            f"knowledge metric {knowledge_id} historical_names",
            allow_empty=True,
        )

        binding = _require_mapping(
            metric.get("provider_binding"), f"knowledge metric {knowledge_id} provider_binding"
        )
        if not isinstance(binding.get("source_id"), str) or not binding["source_id"].strip():
            raise MarketplaceKnowledgeError(
                f"knowledge metric {knowledge_id} provider_binding must define source_id"
            )
        binding_fields = _binding_field_paths(binding, knowledge_id)

        marketplace = str(metric["marketplace"]).strip().lower()
        for field_path in binding_fields:
            binding_key = (semantic_metric_id, marketplace, field_path)
            previous = binding_owners.get(binding_key)
            if previous is not None:
                raise MarketplaceKnowledgeError(
                    f"duplicate knowledge binding {binding_key!r}: {previous} and {knowledge_id}"
                )
            binding_owners[binding_key] = knowledge_id

        source_refs = _require_string_list(
            metric.get("source_refs"), f"knowledge metric {knowledge_id} source_refs"
        )
        missing = [source_id for source_id in source_refs if source_id not in sources]
        if missing:
            raise MarketplaceKnowledgeError(
                f"knowledge metric {knowledge_id} references unknown sources: {missing}"
            )
        if status == "verified":
            non_official = [
                source_id for source_id in source_refs
                if sources[source_id].get("official") is not True
            ]
            if non_official:
                raise MarketplaceKnowledgeError(
                    f"verified knowledge metric {knowledge_id} has non-official sources: {non_official}"
                )

        cabinet_binding = metric.get("cabinet_binding")
        if cabinet_binding is not None:
            cabinet_binding = _require_mapping(
                cabinet_binding, f"knowledge metric {knowledge_id} cabinet_binding"
            )
            for field in ("surface_ru", "label_ru", "scope_ru", "equivalence_status"):
                if not isinstance(cabinet_binding.get(field), str) or not cabinet_binding[field].strip():
                    raise MarketplaceKnowledgeError(
                        f"knowledge metric {knowledge_id} cabinet_binding must define {field}"
                    )
            if cabinet_binding["equivalence_status"] not in {"verified", "provisional", "partial"}:
                raise MarketplaceKnowledgeError(
                    f"knowledge metric {knowledge_id} cabinet_binding has unsupported equivalence_status "
                    f"{cabinet_binding['equivalence_status']!r}"
                )
            cabinet_refs = _require_string_list(
                cabinet_binding.get("source_refs"),
                f"knowledge metric {knowledge_id} cabinet_binding source_refs",
            )
            cabinet_missing = [source_id for source_id in cabinet_refs if source_id not in sources]
            if cabinet_missing:
                raise MarketplaceKnowledgeError(
                    f"knowledge metric {knowledge_id} cabinet_binding references unknown sources: "
                    f"{cabinet_missing}"
                )
            if cabinet_binding["equivalence_status"] == "verified":
                cabinet_non_official = [
                    source_id for source_id in cabinet_refs
                    if sources[source_id].get("official") is not True
                ]
                if cabinet_non_official:
                    raise MarketplaceKnowledgeError(
                        f"verified cabinet binding {knowledge_id} has non-official sources: "
                        f"{cabinet_non_official}"
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
    *,
    marketplace: str = "",
    source_field: str = "",
) -> dict[str, Any] | None:
    """Resolve one provider-specific knowledge binding without cross-marketplace guessing.

    Catalog keys are knowledge-record IDs. Legacy records may use metric_id as
    their key; provider-specific records can instead declare metric_id explicitly.
    When several bindings exist for the same semantic metric, marketplace and,
    when needed, source_field must disambiguate them.
    """
    data = catalog if catalog is not None else load_marketplace_knowledge_catalog()
    wanted_metric = str(metric_id or "").strip()
    wanted_marketplace = str(marketplace or "").strip().lower()
    wanted_field = str(source_field or "").strip()
    if not wanted_metric:
        return None

    candidates: list[tuple[str, dict[str, Any]]] = []
    for knowledge_id, raw_metric in (data.get("metrics") or {}).items():
        if not isinstance(raw_metric, dict):
            continue
        semantic_metric_id = str(raw_metric.get("metric_id") or knowledge_id)
        if semantic_metric_id != wanted_metric:
            continue
        if wanted_marketplace and str(raw_metric.get("marketplace") or "").lower() != wanted_marketplace:
            continue
        binding = raw_metric.get("provider_binding") or {}
        binding_fields = _binding_field_paths(binding, knowledge_id)
        if wanted_field and wanted_field not in binding_fields:
            continue
        candidates.append((knowledge_id, raw_metric))

    if len(candidates) != 1:
        return None

    knowledge_id, metric = candidates[0]
    resolved = deepcopy(metric)
    resolved["knowledge_id"] = knowledge_id
    resolved["metric_id"] = str(metric.get("metric_id") or knowledge_id)
    return resolved


def validate_knowledge_against_metric_registry(
    catalog: dict[str, Any],
    metric_registry: dict[str, Any],
) -> None:
    """Cross-check human semantics against executable provider mappings."""
    registry_metrics = _require_mapping(metric_registry.get("metrics"), "metric registry metrics")
    for knowledge_id, metric in (catalog.get("metrics") or {}).items():
        semantic_metric_id = _semantic_metric_id(knowledge_id, metric)
        if semantic_metric_id not in registry_metrics:
            raise MarketplaceKnowledgeError(
                f"knowledge metric {knowledge_id} references absent registry metric {semantic_metric_id}"
            )
        marketplace = str(metric["marketplace"])
        binding = metric["provider_binding"]
        provider = (
            registry_metrics[semantic_metric_id].get("provider_mappings") or {}
        ).get(marketplace)
        if not isinstance(provider, dict):
            raise MarketplaceKnowledgeError(
                f"metric registry {semantic_metric_id} has no provider mapping for {marketplace}"
            )
        if provider.get("source_id") != binding.get("source_id"):
            raise MarketplaceKnowledgeError(
                f"knowledge/registry source mismatch for {knowledge_id}"
            )
        fields = list(provider.get("fields") or [])
        binding_fields = _binding_field_paths(binding, knowledge_id)
        missing_fields = [field for field in binding_fields if field not in fields]
        if missing_fields:
            raise MarketplaceKnowledgeError(
                f"knowledge/registry field mismatch for {knowledge_id}: {missing_fields}"
            )


def _provider_metric_coverage(
    catalog: dict[str, Any],
    registry: dict[str, Any],
) -> dict[str, Any]:
    """Measure semantic progress against the canonical composed metric registry."""
    registry_metrics = _require_mapping(registry.get("metrics"), "metric registry metrics")
    knowledge_rows = [
        (knowledge_id, metric)
        for knowledge_id, metric in (catalog.get("metrics") or {}).items()
        if isinstance(metric, dict)
    ]
    out: dict[str, Any] = {}
    for marketplace in ("wb", "ozon"):
        status_counts: dict[str, int] = {}
        unresolved: list[str] = []
        applicable: list[str] = []
        for metric_id, metric in registry_metrics.items():
            provider = ((metric or {}).get("provider_mappings") or {}).get(marketplace)
            status = str(provider.get("status") or "ABSENT") if isinstance(provider, dict) else "ABSENT"
            status_counts[status] = status_counts.get(status, 0) + 1
            if status in {"NOT_MAPPED", "ABSENT"}:
                unresolved.append(str(metric_id))
            elif status != "NOT_APPLICABLE":
                applicable.append(str(metric_id))

        knowledge_status_counts: dict[str, int] = {}
        bound_metric_ids: set[str] = set()
        verified_metric_ids: set[str] = set()
        provisional_metric_ids: set[str] = set()
        for knowledge_id, metric in knowledge_rows:
            if str(metric.get("marketplace") or "").lower() != marketplace:
                continue
            semantic_metric_id = str(metric.get("metric_id") or knowledge_id)
            semantic_status = str(metric.get("semantic_status") or "source_field")
            knowledge_status_counts[semantic_status] = knowledge_status_counts.get(semantic_status, 0) + 1
            if semantic_status not in {"deprecated", "broken"}:
                bound_metric_ids.add(semantic_metric_id)
            if semantic_status == "verified":
                verified_metric_ids.add(semantic_metric_id)
            elif semantic_status == "provisional":
                provisional_metric_ids.add(semantic_metric_id)

        missing_knowledge = sorted(set(applicable) - bound_metric_ids)
        out[marketplace] = {
            "registry_mapping_status_counts": dict(sorted(status_counts.items())),
            "applicable_metric_count": len(set(applicable)),
            "registry_unresolved_metric_ids": sorted(unresolved),
            "knowledge_status_counts": dict(sorted(knowledge_status_counts.items())),
            "knowledge_bound_metric_ids": sorted(bound_metric_ids),
            "verified_metric_ids": sorted(verified_metric_ids),
            "provisional_metric_ids": sorted(provisional_metric_ids),
            "missing_knowledge_metric_ids": missing_knowledge,
            "metric_coverage_complete": (
                not unresolved
                and not missing_knowledge
                and not provisional_metric_ids
            ),
        }
    return out


def _weekly_report_field_coverage() -> dict[str, Any]:
    """Expose the audited physical WB weekly-report field layer without duplicating it."""
    from .semantic_registry import load_semantic_registry

    semantic = load_semantic_registry()
    dataset_id = "wb_weekly_finance_main"
    dataset = (semantic.get("datasets") or {}).get(dataset_id) or {}
    fields = list(dataset.get("fields") or [])
    catalog = dataset.get("field_catalog") or {}

    role_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    missing_meaning: list[str] = []
    missing_safe_uses: list[str] = []
    unreviewed: list[str] = []
    fields_with_limitations: list[str] = []

    for field_name in fields:
        item = catalog.get(field_name) or {}
        role = str(item.get("role") or "UNKNOWN")
        status = str(item.get("semantic_status") or "UNKNOWN")
        role_counts[role] = role_counts.get(role, 0) + 1
        status_counts[status] = status_counts.get(status, 0) + 1
        if not str(item.get("meaning_ru") or "").strip():
            missing_meaning.append(field_name)
        if not list(item.get("safe_uses") or []):
            missing_safe_uses.append(field_name)
        if status != "REVIEWED":
            unreviewed.append(field_name)
        if list(item.get("limitations") or []):
            fields_with_limitations.append(field_name)

    physical_match = (
        int(dataset.get("field_count") or 0) == len(fields)
        and len(catalog) == len(fields)
        and set(catalog) == set(fields)
    )
    complete = (
        physical_match
        and not missing_meaning
        and not missing_safe_uses
        and not unreviewed
    )

    capability_usage: dict[str, list[str]] = {field_name: [] for field_name in fields}
    for capability_id, capability in (semantic.get("capabilities") or {}).items():
        if capability.get("source_id") != dataset_id:
            continue
        for field_name in capability.get("fields") or []:
            if field_name in capability_usage:
                capability_usage[field_name].append(str(capability_id))

    capability_linked_fields = sorted(
        field_name for field_name, owners in capability_usage.items() if owners
    )
    capability_unlinked_fields = sorted(
        field_name for field_name, owners in capability_usage.items() if not owners
    )

    references = semantic.get("references") or {}
    return {
        "dataset_id": dataset_id,
        "report_name_ru": dataset.get("report_name_ru"),
        "schema_status": dataset.get("schema_status"),
        "physical_field_count": int(dataset.get("field_count") or 0),
        "catalogued_field_count": len(catalog),
        "reviewed_field_count": sum(
            1 for field_name in fields
            if str((catalog.get(field_name) or {}).get("semantic_status") or "") == "REVIEWED"
        ),
        "role_counts": dict(sorted(role_counts.items())),
        "semantic_status_counts": dict(sorted(status_counts.items())),
        "fields_with_limitations_count": len(fields_with_limitations),
        "fields_with_limitations": sorted(fields_with_limitations),
        "capability_linked_field_count": len(capability_linked_fields),
        "capability_unlinked_field_count": len(capability_unlinked_fields),
        "capability_unlinked_fields": capability_unlinked_fields,
        "capabilities_by_field": {
            field_name: sorted(owners)
            for field_name, owners in sorted(capability_usage.items())
        },
        "missing_meaning_fields": sorted(missing_meaning),
        "missing_safe_uses_fields": sorted(missing_safe_uses),
        "unreviewed_fields": sorted(unreviewed),
        "physical_schema_matches_catalog": physical_match,
        "field_coverage_complete": complete and not capability_unlinked_fields,
        "official_audit": references.get("official_audit"),
        "archive_observation": references.get("archive_observation"),
        "blocked_unapproved_provider_fields": ["agencyVat"]
        if "agencyVat" not in fields else [],
        "rule": (
            "Each physical weekly-report column must have reviewed human meaning and safe uses. "
            "Provider fields absent from the approved archive schema stay non-executable until review."
        ),
    }


def get_report_field_knowledge(
    field_name: str,
    *,
    dataset_id: str = "wb_weekly_finance_main",
) -> dict[str, Any]:
    """Return reviewed human meaning for one physical report column."""
    from .semantic_registry import get_dataset

    dataset = get_dataset(dataset_id)
    field = (dataset.get("field_catalog") or {}).get(field_name)
    if not isinstance(field, dict):
        raise MarketplaceKnowledgeError(
            f"unknown report field {field_name!r} in dataset {dataset_id!r}"
        )
    return {
        "dataset_id": dataset_id,
        "report_name_ru": dataset.get("report_name_ru"),
        "field_name": field_name,
        "meaning_ru": field.get("meaning_ru"),
        "role": field.get("role"),
        "semantic_status": field.get("semantic_status"),
        "safe_uses": list(field.get("safe_uses") or []),
        "limitations": list(field.get("limitations") or []),
        "schema_status": dataset.get("schema_status"),
    }


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

    metric_records = [
        metric for metric in (catalog.get("metrics") or {}).values()
        if isinstance(metric, dict)
    ]
    verified_metric_ids = {
        str(metric.get("metric_id") or knowledge_id)
        for knowledge_id, metric in (catalog.get("metrics") or {}).items()
        if isinstance(metric, dict) and metric.get("semantic_status") == "verified"
    }
    provisional_bindings = [
        {
            "knowledge_id": knowledge_id,
            "metric_id": str(metric.get("metric_id") or knowledge_id),
            "marketplace": metric.get("marketplace"),
            "field_paths": _binding_field_paths(
                metric.get("provider_binding") or {}, knowledge_id
            ),
        }
        for knowledge_id, metric in (catalog.get("metrics") or {}).items()
        if isinstance(metric, dict) and metric.get("semantic_status") == "provisional"
    ]

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
        "knowledge_record_count": len(metric_records),
        "semantic_metric_count": len({
            str(metric.get("metric_id") or knowledge_id)
            for knowledge_id, metric in (catalog.get("metrics") or {}).items()
            if isinstance(metric, dict)
        }),
        "verified_metric_count": len(verified_metric_ids),
        "verified_binding_count": sum(
            1 for metric in metric_records if metric.get("semantic_status") == "verified"
        ),
        "provisional_binding_count": len(provisional_bindings),
        "provisional_bindings": provisional_bindings,
        "provider_metric_coverage": _provider_metric_coverage(catalog, registry),
        "weekly_report_field_coverage": _weekly_report_field_coverage(),
        "source_count": len(catalog.get("sources") or {}),
        "stale_sources": stale_sources,
        "errors": errors,
        "production_auto_update": False,
        "rule": "Detected semantic/source changes require review before production meaning is changed.",
    }


def register_marketplace_knowledge_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="marketplace_report_field_explain",
        annotations={
            "title": "Explain one marketplace report field",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_report_field_explain(
        field_name: str,
        dataset_id: str = "wb_weekly_finance_main",
    ) -> str:
        import json

        try:
            result = get_report_field_knowledge(
                field_name,
                dataset_id=dataset_id,
            )
        except Exception as exc:
            result = {
                "ok": False,
                "dataset_id": dataset_id,
                "field_name": field_name,
                "error": str(exc),
            }
        else:
            result = {"ok": True, **result}
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)

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
