from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


METRIC_REGISTRY_PATH = Path(__file__).with_name("metric_registry.yaml")
_ALLOWED_KINDS = {"RAW", "DERIVED", "BUSINESS"}
_ALLOWED_TARGET_TYPES = {"BUSINESS_METRIC", "CAPABILITY"}


class MetricRegistryError(RuntimeError):
    """Raised when the canonical metric dictionary is invalid."""


def _normalize(text: str) -> str:
    value = str(text or "").casefold().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9_]+", " ", value)
    return " ".join(value.split())


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MetricRegistryError(f"{name} must be a mapping")
    return value


def _require_string_list(value: Any, name: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise MetricRegistryError(f"{name} must be a {'possibly empty ' if allow_empty else 'non-empty '}string list")
    if not all(isinstance(item, str) and item for item in value):
        raise MetricRegistryError(f"{name} must contain only non-empty strings")
    return value


def validate_metric_registry(data: dict[str, Any]) -> None:
    policy = _require_mapping(data.get("policy"), "metric registry policy")
    if policy.get("provider_field_is_not_business_name") is not True:
        raise MetricRegistryError("provider fields must not be treated as canonical business names")
    if policy.get("metric_dictionary_grants_execution") is not False:
        raise MetricRegistryError("metric dictionary must not grant execution permission")
    if policy.get("unknown_provider_mapping_must_not_be_inferred") is not True:
        raise MetricRegistryError("unknown provider mappings must fail closed")

    metrics = _require_mapping(data.get("metrics"), "metrics")
    if not metrics:
        raise MetricRegistryError("metrics must not be empty")

    normalized_alias_owners: dict[str, str] = {}
    for metric_id, raw_metric in metrics.items():
        if not isinstance(metric_id, str) or not metric_id:
            raise MetricRegistryError("metric ids must be non-empty strings")
        metric = _require_mapping(raw_metric, f"metric {metric_id}")
        canonical_name = metric.get("canonical_name_ru")
        if not isinstance(canonical_name, str) or not canonical_name.strip():
            raise MetricRegistryError(f"metric {metric_id} must define canonical_name_ru")
        if metric.get("kind") not in _ALLOWED_KINDS:
            raise MetricRegistryError(f"metric {metric_id} has unsupported kind")
        _require_string_list(metric.get("units"), f"metric {metric_id} units")
        aliases_ru = _require_string_list(metric.get("aliases_ru"), f"metric {metric_id} aliases_ru")
        aliases_en = _require_string_list(metric.get("aliases_en"), f"metric {metric_id} aliases_en", allow_empty=True)
        abbreviation = metric.get("abbreviation")
        if abbreviation is not None and (not isinstance(abbreviation, str) or not abbreviation.strip()):
            raise MetricRegistryError(f"metric {metric_id} abbreviation must be a non-empty string or null")

        target = _require_mapping(metric.get("semantic_target"), f"metric {metric_id} semantic_target")
        if target.get("type") not in _ALLOWED_TARGET_TYPES:
            raise MetricRegistryError(f"metric {metric_id} has unsupported semantic target type")
        if not isinstance(target.get("id"), str) or not target["id"]:
            raise MetricRegistryError(f"metric {metric_id} semantic target id must be non-empty")

        providers = _require_mapping(metric.get("provider_mappings"), f"metric {metric_id} provider_mappings")
        for marketplace in ("wb", "ozon"):
            provider = _require_mapping(providers.get(marketplace), f"metric {metric_id} provider {marketplace}")
            if not isinstance(provider.get("status"), str) or not provider["status"]:
                raise MetricRegistryError(f"metric {metric_id} provider {marketplace} must define status")
            _require_string_list(provider.get("fields", []), f"metric {metric_id} provider {marketplace} fields", allow_empty=True)
            if provider.get("provider_aliases") is not None:
                _require_string_list(
                    provider.get("provider_aliases"),
                    f"metric {metric_id} provider {marketplace} provider_aliases",
                    allow_empty=True,
                )

        if metric.get("kind") == "DERIVED":
            formula_ref = metric.get("formula_ref")
            if not isinstance(formula_ref, str) or not formula_ref.strip():
                raise MetricRegistryError(f"derived metric {metric_id} must define formula_ref")

        aliases = [canonical_name, *aliases_ru, *aliases_en]
        if abbreviation:
            aliases.append(abbreviation)
        for alias in aliases:
            normalized = _normalize(alias)
            if not normalized:
                continue
            owner = normalized_alias_owners.get(normalized)
            if owner is not None and owner != metric_id:
                # Shared generic aliases (for example 'orders') are dangerous because
                # they can silently map one user phrase to two distinct business meanings.
                raise MetricRegistryError(
                    f"alias {alias!r} is shared by metrics {owner} and {metric_id}"
                )
            normalized_alias_owners[normalized] = metric_id


def load_metric_registry(path: str | Path | None = None) -> dict[str, Any]:
    registry_path = Path(path) if path is not None else METRIC_REGISTRY_PATH
    with registry_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    data = _require_mapping(raw, "metric registry")
    validate_metric_registry(data)
    return data


def _metric_aliases(metric: dict[str, Any]) -> list[str]:
    aliases = [metric["canonical_name_ru"], *metric.get("aliases_ru", []), *metric.get("aliases_en", [])]
    abbreviation = metric.get("abbreviation")
    if abbreviation:
        aliases.append(abbreviation)
    seen: set[str] = set()
    result: list[str] = []
    for alias in aliases:
        normalized = _normalize(alias)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(alias)
    return result


def resolve_metric_terms(text: str, registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Resolve user wording to canonical metric dictionary entries.

    This is semantic recognition only. It never grants execution or arithmetic.
    Shorter aliases swallowed by a longer matched alias are suppressed so
    'доля рекламных расходов' resolves to ДРР rather than both ДРР and Spend.
    """
    data = registry if registry is not None else load_metric_registry()
    normalized_text = _normalize(text)
    raw_matches: list[tuple[str, str, str]] = []
    for metric_id, metric in data["metrics"].items():
        for alias in _metric_aliases(metric):
            normalized_alias = _normalize(alias)
            if normalized_alias and normalized_alias in normalized_text:
                raw_matches.append((metric_id, alias, normalized_alias))

    if not raw_matches:
        return []

    maximal_phrases = {
        phrase
        for _, _, phrase in raw_matches
        if not any(phrase != other and phrase in other for _, _, other in raw_matches)
    }
    by_metric: dict[str, list[str]] = {}
    for metric_id, alias, phrase in raw_matches:
        if phrase in maximal_phrases:
            by_metric.setdefault(metric_id, []).append(alias)

    results: list[dict[str, Any]] = []
    for metric_id, matched_aliases in by_metric.items():
        metric = deepcopy(data["metrics"][metric_id])
        results.append({
            "metric_id": metric_id,
            "canonical_name_ru": metric["canonical_name_ru"],
            "abbreviation": metric.get("abbreviation"),
            "kind": metric["kind"],
            "units": deepcopy(metric["units"]),
            "matched_aliases": matched_aliases,
            "semantic_target": deepcopy(metric["semantic_target"]),
            "provider_mappings": deepcopy(metric["provider_mappings"]),
            "formula_ref": metric.get("formula_ref"),
            "guardrail": metric.get("guardrail"),
        })
    results.sort(
        key=lambda item: max(len(_normalize(alias)) for alias in item["matched_aliases"]),
        reverse=True,
    )
    return results
