from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from core.business_query_parser import parse_business_query_dimensions
from core.metric_registry import load_metric_registry, resolve_metric_terms
from core.semantic_registry import get_dataset, load_semantic_registry


INTENTS_PATH = Path(__file__).with_name("semantic_intents.yaml")


class SemanticResolutionError(RuntimeError):
    """Raised when semantic intent routing is inconsistent or ambiguous."""


def _normalize(text: str) -> str:
    value = text.casefold().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9_]+", " ", value)
    return " ".join(value.split())


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticResolutionError(f"{name} must be a mapping")
    return value


def _require_string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise SemanticResolutionError(f"{name} must be a non-empty string list")
    return value


def load_semantic_intents(path: str | Path | None = None) -> dict[str, Any]:
    intents_path = Path(path) if path is not None else INTENTS_PATH
    with intents_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    intents = _require_mapping(raw, "semantic intents")
    validate_semantic_intents(intents)
    return intents


def validate_semantic_intents(
    intents: dict[str, Any],
    registry: dict[str, Any] | None = None,
) -> None:
    registry_data = registry if registry is not None else load_semantic_registry()
    policy = _require_mapping(intents.get("policy"), "semantic intent policy")
    if policy.get("fail_closed_on_unknown") is not True:
        raise SemanticResolutionError("semantic intent routing must fail closed on unknown requests")

    business_metrics = intents.get("business_metrics") or {}
    if not isinstance(business_metrics, dict):
        raise SemanticResolutionError("business_metrics must be a mapping when present")
    for metric_id, metric_value in business_metrics.items():
        metric = _require_mapping(metric_value, f"business metric {metric_id}")
        if metric.get("status") not in {"AVAILABLE", "AVAILABLE_WITH_LIMITATION"}:
            raise SemanticResolutionError(f"business metric {metric_id} has unsupported status")
        _require_string_list(metric.get("allowed_measures"), f"business metric {metric_id} allowed_measures")
        _require_string_list(metric.get("allowed_groupings"), f"business metric {metric_id} allowed_groupings")
        if metric.get("default_measure") not in metric["allowed_measures"]:
            raise SemanticResolutionError(f"business metric {metric_id} default_measure is not allowed")
        if not isinstance(metric.get("source_id"), str) or not metric["source_id"]:
            raise SemanticResolutionError(f"business metric {metric_id} must define source_id")

    routes = intents.get("routes")
    if not isinstance(routes, list) or not routes:
        raise SemanticResolutionError("semantic intent routes must be a non-empty list")

    seen_ids: set[str] = set()
    for route_value in routes:
        route = _require_mapping(route_value, "semantic intent route")
        route_id = route.get("id")
        if not isinstance(route_id, str) or not route_id:
            raise SemanticResolutionError("every semantic route must define a non-empty id")
        if route_id in seen_ids:
            raise SemanticResolutionError(f"duplicate semantic route id {route_id}")
        seen_ids.add(route_id)

        priority = route.get("priority")
        if not isinstance(priority, int):
            raise SemanticResolutionError(f"semantic route {route_id} must define integer priority")

        _require_string_list(route.get("match_any"), f"semantic route {route_id} match_any")
        target_type = route.get("target_type")
        target_id = route.get("target_id")

        if target_type == "capability":
            if target_id not in registry_data["capabilities"]:
                raise SemanticResolutionError(
                    f"semantic route {route_id} references unknown capability {target_id!r}"
                )
        elif target_type == "business_metric":
            if target_id not in business_metrics:
                raise SemanticResolutionError(
                    f"semantic route {route_id} references unknown business metric {target_id!r}"
                )
        elif target_type == "not_covered":
            if target_id not in registry_data["not_covered"]:
                raise SemanticResolutionError(
                    f"semantic route {route_id} references unknown not_covered concept {target_id!r}"
                )
        elif target_type == "inline_not_covered":
            if not isinstance(route.get("reason"), str) or not route["reason"].strip():
                raise SemanticResolutionError(
                    f"semantic route {route_id} must define a reason for inline_not_covered"
                )
        else:
            raise SemanticResolutionError(
                f"semantic route {route_id} has unsupported target_type {target_type!r}"
            )


def _resolve_exact_field_reference(
    question: str,
    registry: dict[str, Any],
) -> dict[str, Any] | None:
    normalized = _normalize(question)
    dataset = get_dataset("wb_weekly_finance_main", registry)
    field_map = {field.casefold(): field for field in dataset["fields"]}
    tokens = set(normalized.split())

    for folded, field_name in field_map.items():
        if folded in tokens:
            field = deepcopy(dataset["field_catalog"][field_name])
            limitations = field.get("limitations") or []
            return {
                "status": "AVAILABLE_WITH_LIMITATION" if limitations else "AVAILABLE",
                "resolution_type": "FIELD",
                "dataset_id": "wb_weekly_finance_main",
                "source_id": "wb_weekly_finance_main",
                "field_name": field_name,
                "field": field,
                "fields": [field_name],
                "guardrail": " ".join(limitations) if limitations else None,
                "execution_allowed": False,
                "next_action": "BUILD_QUERY_PLAN_AFTER_COVERAGE_CHECK",
            }
    return None


def _route_matches(normalized_question: str, route: dict[str, Any]) -> list[str]:
    matches: list[str] = []
    for phrase in route["match_any"]:
        normalized_phrase = _normalize(phrase)
        if normalized_phrase and normalized_phrase in normalized_question:
            matches.append(phrase)
    return matches


def _with_dimensions(result: dict[str, Any], dimensions: dict[str, Any]) -> dict[str, Any]:
    result["normalized_query"] = {
        "measure": dimensions.get("requested_measure"),
        "grouping": dimensions.get("grouping"),
        "period_hint": dimensions.get("period_hint"),
        "filter_hints": deepcopy(dimensions.get("filter_hints") or []),
        "complete_order_flow": bool(dimensions.get("complete_order_flow")),
    }
    return result


def _attach_metric_dictionary(
    result: dict[str, Any], metric_matches: list[dict[str, Any]],
) -> dict[str, Any]:
    result["canonical_metrics"] = deepcopy(metric_matches)
    if len(metric_matches) == 1:
        result["canonical_metric"] = deepcopy(metric_matches[0])
    return result


def _metric_fallback_route(metric_matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not metric_matches:
        return None
    targets = {
        (match["semantic_target"]["type"], match["semantic_target"]["id"])
        for match in metric_matches
    }
    if len(targets) != 1:
        return None
    target_type, target_id = next(iter(targets))
    route_type = "business_metric" if target_type == "BUSINESS_METRIC" else "capability"
    return {
        "id": "metric_dictionary:" + "+".join(match["metric_id"] for match in metric_matches),
        "target_type": route_type,
        "target_id": target_id,
    }


def _resolve_route(
    route: dict[str, Any],
    matched_terms: list[str],
    registry: dict[str, Any],
    intents: dict[str, Any],
    dimensions: dict[str, Any],
) -> dict[str, Any]:
    target_type = route["target_type"]
    route_id = route["id"]

    if target_type == "capability":
        capability_id = route["target_id"]
        capability = deepcopy(registry["capabilities"][capability_id])
        source_id = capability["source_id"]
        source = registry["sources"][source_id]
        dataset_id = source.get("dataset_id")
        return _with_dimensions({
            "status": capability["status"],
            "resolution_type": "CAPABILITY",
            "route_id": route_id,
            "capability_id": capability_id,
            "source_id": source_id,
            "dataset_id": dataset_id,
            "fields": deepcopy(capability["fields"]),
            "scope": capability.get("scope"),
            "guardrail": capability.get("guardrail"),
            "matched_terms": matched_terms,
            "execution_allowed": False,
            "next_action": "BUILD_QUERY_PLAN_AFTER_COVERAGE_CHECK",
        }, dimensions)

    if target_type == "business_metric":
        metric_id = route["target_id"]
        metric = deepcopy(intents["business_metrics"][metric_id])
        measure = dimensions.get("requested_measure") or metric["default_measure"]
        grouping = dimensions.get("grouping") or "TOTAL"
        execution_allowed = (
            measure in metric["allowed_measures"]
            and grouping in metric["allowed_groupings"]
        )
        status = metric["status"] if execution_allowed else "REQUIRES_OTHER_SOURCE"
        result = {
            "status": status,
            "resolution_type": "BUSINESS_METRIC",
            "route_id": route_id,
            "metric_id": metric_id,
            "source_id": metric["source_id"],
            "data_class": metric.get("data_class"),
            "matched_terms": matched_terms,
            "guardrail": metric.get("guardrail"),
            "execution_allowed": execution_allowed,
            "next_action": "EXECUTE_APPROVED_BUSINESS_METRIC" if execution_allowed else "REQUIRE_APPROVED_GROUPING_OR_FILTER_CONTRACT",
        }
        result = _with_dimensions(result, dimensions)
        result["normalized_query"]["metric"] = metric_id
        result["normalized_query"]["measure"] = measure
        result["normalized_query"]["grouping"] = grouping
        return result

    if target_type == "not_covered":
        concept_id = route["target_id"]
        concept = deepcopy(registry["not_covered"][concept_id])
        required_source_id = concept.get("required_source_id")
        source = registry["sources"].get(required_source_id) if required_source_id else None
        return _with_dimensions({
            "status": "REQUIRES_OTHER_SOURCE",
            "resolution_type": "NOT_COVERED",
            "route_id": route_id,
            "concept_id": concept_id,
            "required_source_id": required_source_id,
            "required_source_database_presence": source.get("database_presence") if source else None,
            "reason": concept.get("reason"),
            "matched_terms": matched_terms,
            "execution_allowed": False,
            "next_action": "DO_NOT_QUERY_WEEKLY_ARCHIVE",
        }, dimensions)

    return _with_dimensions({
        "status": "REQUIRES_OTHER_SOURCE",
        "resolution_type": "NOT_COVERED",
        "route_id": route_id,
        "concept_id": route_id,
        "required_source_id": None,
        "required_source_database_presence": None,
        "reason": route["reason"],
        "matched_terms": matched_terms,
        "execution_allowed": False,
        "next_action": "DO_NOT_QUERY_WEEKLY_ARCHIVE",
    }, dimensions)


def resolve_semantic_question(
    question: str,
    registry: dict[str, Any] | None = None,
    intents: dict[str, Any] | None = None,
    metric_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(question, str) or not question.strip():
        raise SemanticResolutionError("question must be a non-empty string")

    registry_data = registry if registry is not None else load_semantic_registry()
    intents_data = intents if intents is not None else load_semantic_intents()
    metric_registry_data = metric_registry if metric_registry is not None else load_metric_registry()
    validate_semantic_intents(intents_data, registry_data)
    dimensions = parse_business_query_dimensions(question)
    metric_matches = resolve_metric_terms(question, metric_registry_data)

    if intents_data["policy"].get("prefer_exact_field_reference") is True:
        direct_field = _resolve_exact_field_reference(question, registry_data)
        if direct_field is not None:
            return _attach_metric_dictionary(_with_dimensions(direct_field, dimensions), metric_matches)

    if dimensions.get("complete_order_flow"):
        complete_route = next(
            (
                route
                for route in intents_data["routes"]
                if route.get("id") == "complete_order_flow"
            ),
            None,
        )
        if complete_route is None:
            raise SemanticResolutionError("complete_order_flow route is missing")
        result = _resolve_route(
            complete_route,
            ["complete_order_flow"],
            registry_data,
            intents_data,
            dimensions,
        )
        return _attach_metric_dictionary(result, metric_matches)

    normalized = _normalize(question)
    candidates: list[tuple[int, int, dict[str, Any], list[str]]] = []
    for route in intents_data["routes"]:
        matches = _route_matches(normalized, route)
        if matches:
            longest = max(len(_normalize(item)) for item in matches)
            candidates.append((route["priority"], longest, route, matches))

    if not candidates and metric_matches:
        fallback_route = _metric_fallback_route(metric_matches)
        if fallback_route is None:
            return _attach_metric_dictionary(_with_dimensions({
                "status": "AMBIGUOUS",
                "resolution_type": "AMBIGUOUS",
                "reason": "Запрос содержит несколько канонических метрик с несовместимыми семантическими целями.",
                "candidate_metrics": [match["metric_id"] for match in metric_matches],
                "execution_allowed": False,
                "next_action": "CLARIFY_OR_NORMALIZE_INTENT",
            }, dimensions), metric_matches)
        matched_terms = [
            alias
            for match in metric_matches
            for alias in match.get("matched_aliases", [])
        ]
        result = _resolve_route(
            fallback_route, matched_terms, registry_data, intents_data, dimensions,
        )
        return _attach_metric_dictionary(result, metric_matches)

    if not candidates:
        return _attach_metric_dictionary(_with_dimensions({
            "status": "UNKNOWN",
            "resolution_type": "UNKNOWN",
            "reason": "Запрос не сопоставлен ни с одной подтверждённой семантикой текущей базы.",
            "execution_allowed": False,
            "next_action": "DO_NOT_GUESS_OR_QUERY_ARCHIVE",
        }, dimensions), metric_matches)

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    top_priority, top_length, top_route, top_matches = candidates[0]
    tied = [
        item
        for item in candidates
        if item[0] == top_priority and item[1] == top_length
    ]
    if len(tied) > 1:
        targets = {(item[2].get("target_type"), item[2].get("target_id")) for item in tied}
        if len(targets) > 1:
            return _attach_metric_dictionary(_with_dimensions({
                "status": "AMBIGUOUS",
                "resolution_type": "AMBIGUOUS",
                "reason": "Запрос одновременно соответствует нескольким несовместимым семантическим маршрутам.",
                "candidate_routes": [item[2]["id"] for item in tied],
                "execution_allowed": False,
                "next_action": "CLARIFY_OR_NORMALIZE_INTENT",
            }, dimensions), metric_matches)

    result = _resolve_route(top_route, top_matches, registry_data, intents_data, dimensions)
    return _attach_metric_dictionary(result, metric_matches)
