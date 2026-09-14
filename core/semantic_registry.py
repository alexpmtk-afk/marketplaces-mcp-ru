from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


REGISTRY_PATH = Path(__file__).with_name("semantic_registry.yaml")
AVAILABLE_SOURCE_STATUS = "AVAILABLE_IN_CANONICAL_ARCHIVE"
AVAILABLE_CAPABILITY_STATUSES = {"AVAILABLE", "AVAILABLE_WITH_LIMITATION"}


class SemanticRegistryError(RuntimeError):
    """Raised when the semantic registry is inconsistent or unsafe to execute."""


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticRegistryError(f"{name} must be a mapping")
    return value


def _require_string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise SemanticRegistryError(f"{name} must be a non-empty string list")
    return value


def validate_semantic_registry(registry: dict[str, Any]) -> None:
    policy = _require_mapping(registry.get("policy"), "policy")
    if policy.get("fail_closed") is not True:
        raise SemanticRegistryError("semantic registry must fail closed")
    if policy.get("database_presence_required_for_archive_route") is not True:
        raise SemanticRegistryError("database presence must be required for archive routing")
    if policy.get("field_semantics_required_for_query") is not True:
        raise SemanticRegistryError("field semantics must be required for semantic queries")

    sources = _require_mapping(registry.get("sources"), "sources")
    datasets = _require_mapping(registry.get("datasets"), "datasets")
    capabilities = _require_mapping(registry.get("capabilities"), "capabilities")
    not_covered = _require_mapping(registry.get("not_covered"), "not_covered")

    for source_id, source_value in sources.items():
        source = _require_mapping(source_value, f"source {source_id}")
        presence = source.get("database_presence")
        if presence not in {AVAILABLE_SOURCE_STATUS, "NOT_IN_DATABASE"}:
            raise SemanticRegistryError(
                f"source {source_id} has unsupported database_presence {presence!r}"
            )
        if presence == "NOT_IN_DATABASE" and source.get("execution_status") != "REFERENCE_ONLY":
            raise SemanticRegistryError(
                f"source {source_id} is not in database and must be REFERENCE_ONLY"
            )
        if source.get("kind") == "archive_dataset":
            dataset_id = source.get("dataset_id")
            if dataset_id not in datasets:
                raise SemanticRegistryError(
                    f"archive source {source_id} references unknown dataset {dataset_id!r}"
                )

    for dataset_id, dataset_value in datasets.items():
        dataset = _require_mapping(dataset_value, f"dataset {dataset_id}")
        fields = _require_string_list(dataset.get("fields"), f"dataset {dataset_id} fields")
        if dataset.get("field_count") != len(fields):
            raise SemanticRegistryError(
                f"dataset {dataset_id} field_count does not match physical fields"
            )
        if len(set(fields)) != len(fields):
            raise SemanticRegistryError(f"dataset {dataset_id} fields must be unique")

        field_catalog = _require_mapping(
            dataset.get("field_catalog"), f"dataset {dataset_id} field_catalog"
        )
        if set(field_catalog) != set(fields):
            missing = sorted(set(fields) - set(field_catalog))
            extra = sorted(set(field_catalog) - set(fields))
            raise SemanticRegistryError(
                f"dataset {dataset_id} field_catalog must match physical schema; "
                f"missing={missing}, extra={extra}"
            )
        for field_name, field_value in field_catalog.items():
            field = _require_mapping(
                field_value, f"dataset {dataset_id} field {field_name}"
            )
            if not field.get("meaning_ru"):
                raise SemanticRegistryError(
                    f"dataset {dataset_id} field {field_name} must define meaning_ru"
                )
            if field.get("role") not in {
                "identifier",
                "date",
                "dimension",
                "measure",
                "operation",
                "flag",
                "legacy",
            }:
                raise SemanticRegistryError(
                    f"dataset {dataset_id} field {field_name} has unsupported role"
                )
            _require_string_list(
                field.get("safe_uses"),
                f"dataset {dataset_id} field {field_name} safe_uses",
            )

        dedup_key = _require_string_list(
            dataset.get("row_dedup_key"), f"dataset {dataset_id} row_dedup_key"
        )
        missing_dedup = [field for field in dedup_key if field not in fields]
        if missing_dedup:
            raise SemanticRegistryError(
                f"dataset {dataset_id} dedup fields missing from schema: {missing_dedup}"
            )

        coverage = _require_mapping(
            dataset.get("coverage"), f"dataset {dataset_id} coverage"
        )
        if coverage.get("archive_route_requirement") != "FULL_COVERAGE":
            raise SemanticRegistryError(
                f"dataset {dataset_id} archive route must require FULL_COVERAGE"
            )

    for capability_id, capability_value in capabilities.items():
        capability = _require_mapping(
            capability_value, f"capability {capability_id}"
        )
        source_id = capability.get("source_id")
        if source_id not in sources:
            raise SemanticRegistryError(
                f"capability {capability_id} references unknown source {source_id!r}"
            )
        if capability.get("status") not in AVAILABLE_CAPABILITY_STATUSES:
            raise SemanticRegistryError(
                f"capability {capability_id} has unsupported available status"
            )
        source = sources[source_id]
        if source.get("database_presence") != AVAILABLE_SOURCE_STATUS:
            raise SemanticRegistryError(
                f"available capability {capability_id} cannot use source not present in database"
            )
        if source.get("kind") != "archive_dataset":
            raise SemanticRegistryError(
                f"available capability {capability_id} must currently resolve to an archive dataset"
            )
        dataset = datasets[source["dataset_id"]]
        capability_fields = _require_string_list(
            capability.get("fields"), f"capability {capability_id} fields"
        )
        unknown_fields = [
            field for field in capability_fields if field not in dataset["field_catalog"]
        ]
        if unknown_fields:
            raise SemanticRegistryError(
                f"capability {capability_id} references unknown fields {unknown_fields}"
            )

    for concept_id, concept_value in not_covered.items():
        concept = _require_mapping(concept_value, f"not_covered {concept_id}")
        if concept.get("status") != "REQUIRES_OTHER_SOURCE":
            raise SemanticRegistryError(
                f"not_covered {concept_id} must require another source"
            )
        required_source_id = concept.get("required_source_id")
        if required_source_id:
            if required_source_id not in sources:
                raise SemanticRegistryError(
                    f"not_covered {concept_id} references unknown source {required_source_id!r}"
                )
            if sources[required_source_id].get("database_presence") != "NOT_IN_DATABASE":
                raise SemanticRegistryError(
                    f"not_covered {concept_id} must point to a source absent from database"
                )

    weekly = datasets.get("wb_weekly_finance_main")
    if weekly:
        order_dt = weekly["field_catalog"].get("orderDt") or {}
        limitations = " ".join(order_dt.get("limitations") or [])
        if "полного потока заказов" not in limitations:
            raise SemanticRegistryError(
                "orderDt must explicitly forbid use as the complete marketplace order flow"
            )
        fulfillment = capabilities.get("observed_fulfillment_method") or {}
        guardrail = fulfillment.get("guardrail", "")
        if "current" not in guardrail.lower() and "текущ" not in guardrail.lower():
            raise SemanticRegistryError(
                "observed fulfillment capability must forbid inference of current configuration"
            )


def load_semantic_registry(path: str | Path | None = None) -> dict[str, Any]:
    registry_path = Path(path) if path is not None else REGISTRY_PATH
    with registry_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    registry = _require_mapping(raw, "registry")
    validate_semantic_registry(registry)
    return registry


def get_source(source_id: str, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    data = registry if registry is not None else load_semantic_registry()
    source = data["sources"].get(source_id)
    if source is None:
        raise SemanticRegistryError(f"unknown source {source_id}")
    return deepcopy(source)


def get_dataset(dataset_id: str, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    data = registry if registry is not None else load_semantic_registry()
    dataset = data["datasets"].get(dataset_id)
    if dataset is None:
        raise SemanticRegistryError(f"unknown dataset {dataset_id}")
    return deepcopy(dataset)


def get_field(
    dataset_id: str,
    field_name: str,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dataset = get_dataset(dataset_id, registry)
    field = dataset["field_catalog"].get(field_name)
    if field is None:
        raise SemanticRegistryError(f"unknown field {field_name} in dataset {dataset_id}")
    return deepcopy(field)


def get_capability(
    capability_id: str,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = registry if registry is not None else load_semantic_registry()
    capability = data["capabilities"].get(capability_id)
    if capability is None:
        raise SemanticRegistryError(f"unknown capability {capability_id}")
    return deepcopy(capability)


def require_available_capability(
    capability_id: str,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = registry if registry is not None else load_semantic_registry()
    capability = data["capabilities"].get(capability_id)
    if capability is None:
        missing = data["not_covered"].get(capability_id)
        if missing is not None:
            raise SemanticRegistryError(
                f"capability {capability_id} requires another source: {missing.get('reason')}"
            )
        raise SemanticRegistryError(f"unknown capability {capability_id}")
    if capability.get("status") not in AVAILABLE_CAPABILITY_STATUSES:
        raise SemanticRegistryError(f"capability {capability_id} is not available")
    source = data["sources"][capability["source_id"]]
    if source.get("database_presence") != AVAILABLE_SOURCE_STATUS:
        raise SemanticRegistryError(
            f"source {capability['source_id']} is not present in canonical archive"
        )
    return deepcopy(capability)
