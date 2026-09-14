from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


REGISTRY_PATH = Path(__file__).with_name("semantic_registry.yaml")


class SemanticRegistryError(RuntimeError):
    """Raised when the semantic registry is inconsistent or unsafe to execute."""


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticRegistryError(f"{name} must be a mapping")
    return value


def validate_semantic_registry(registry: dict[str, Any]) -> None:
    policy = _require_mapping(registry.get("policy"), "policy")
    if policy.get("fail_closed") is not True:
        raise SemanticRegistryError("semantic registry must fail closed")

    metrics = _require_mapping(registry.get("metrics"), "metrics")
    sources = _require_mapping(registry.get("sources"), "sources")
    datasets = _require_mapping(registry.get("datasets"), "datasets")
    bindings = _require_mapping(registry.get("bindings"), "bindings")

    for metric_id, metric in metrics.items():
        metric = _require_mapping(metric, f"metric {metric_id}")
        source_id = metric.get("source_id")
        if not source_id or source_id not in sources:
            raise SemanticRegistryError(f"metric {metric_id} references unknown source {source_id!r}")
        if metric.get("status") == "APPROVED" and sources[source_id].get("status") != "APPROVED":
            raise SemanticRegistryError(
                f"approved metric {metric_id} cannot use non-approved source {source_id}"
            )

    for dataset_id, dataset in datasets.items():
        dataset = _require_mapping(dataset, f"dataset {dataset_id}")
        fields = dataset.get("fields")
        if not isinstance(fields, list) or not fields:
            raise SemanticRegistryError(f"dataset {dataset_id} must define fields")
        if dataset.get("field_count") != len(fields):
            raise SemanticRegistryError(
                f"dataset {dataset_id} field_count does not match observed schema"
            )
        dedup_key = dataset.get("row_dedup_key")
        if not isinstance(dedup_key, list) or not dedup_key:
            raise SemanticRegistryError(f"dataset {dataset_id} must define row_dedup_key")
        missing_dedup_fields = [field for field in dedup_key if field not in fields]
        if missing_dedup_fields:
            raise SemanticRegistryError(
                f"dataset {dataset_id} dedup fields missing from schema: {missing_dedup_fields}"
            )

    executable_status = policy.get("executable_binding_status", "APPROVED")
    for binding_id, binding in bindings.items():
        binding = _require_mapping(binding, f"binding {binding_id}")
        metric_id = binding.get("metric_id")
        source_id = binding.get("source_id")
        if metric_id not in metrics:
            raise SemanticRegistryError(f"binding {binding_id} references unknown metric {metric_id!r}")
        if source_id not in sources:
            raise SemanticRegistryError(f"binding {binding_id} references unknown source {source_id!r}")
        if binding.get("execution_allowed") is True and binding.get("status") != executable_status:
            raise SemanticRegistryError(
                f"binding {binding_id} cannot execute until status is {executable_status}"
            )
        source = sources[source_id]
        if source.get("kind") == "archive_dataset":
            dataset_id = source.get("dataset_id")
            if dataset_id not in datasets:
                raise SemanticRegistryError(
                    f"archive source {source_id} references unknown dataset {dataset_id!r}"
                )
            semantics = datasets[dataset_id].get("business_semantics") or {}
            if binding.get("execution_allowed") is True and semantics.get("status") != "APPROVED":
                raise SemanticRegistryError(
                    f"archive binding {binding_id} cannot execute with unproven business semantics"
                )


def load_semantic_registry(path: str | Path | None = None) -> dict[str, Any]:
    registry_path = Path(path) if path is not None else REGISTRY_PATH
    with registry_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    registry = _require_mapping(raw, "registry")
    validate_semantic_registry(registry)
    return registry


def get_metric(metric_id: str, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    data = registry if registry is not None else load_semantic_registry()
    metric = data["metrics"].get(metric_id)
    if metric is None:
        raise SemanticRegistryError(f"unknown metric {metric_id}")
    return deepcopy(metric)


def get_dataset(dataset_id: str, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    data = registry if registry is not None else load_semantic_registry()
    dataset = data["datasets"].get(dataset_id)
    if dataset is None:
        raise SemanticRegistryError(f"unknown dataset {dataset_id}")
    return deepcopy(dataset)


def require_approved_binding(
    metric_id: str,
    source_id: str,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = registry if registry is not None else load_semantic_registry()
    for binding in data["bindings"].values():
        if binding.get("metric_id") == metric_id and binding.get("source_id") == source_id:
            if binding.get("status") != data["policy"]["executable_binding_status"]:
                raise SemanticRegistryError(
                    f"source {source_id} is not approved for metric {metric_id}"
                )
            if binding.get("execution_allowed") is not True:
                raise SemanticRegistryError(
                    f"source {source_id} is not executable for metric {metric_id}"
                )
            return deepcopy(binding)
    raise SemanticRegistryError(f"no binding for metric {metric_id} and source {source_id}")
