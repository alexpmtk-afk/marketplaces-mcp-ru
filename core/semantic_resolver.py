from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from core.semantic_registry import (
    SemanticRegistryError,
    get_dataset,
    load_semantic_registry,
)


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


def _resolve_route(
    route: dict[str, Any],
    matched_terms: list[str],
    registry: dict[str, Any],
) -> dict[str, Any]:
    target_type = route["target_type"]
    route_id = route["id"]

    if target_type == "capability":
        capability_id = route["target_id"]
        capability = deepcopy(registry["capabilities"][capability_id])
        source_id = capability["source_id"]
        source = registry["sources"][source_id]
        dataset_id = source.get("dataset_id")
        return {
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
        }

    if target_type == "not_covered":
        concept_id = route["target_id"]
        concept = deepcopy(registry["not_covered"][concept_id])
        required_source_id = concept.get("required_source_id")
        source = registry["sources"].get(required_source_id) if required_source_id else None
        return {
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
        }

    return {
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
    }


def resolve_semantic_question(
    question: str,
    registry: dict[str, Any] | None = None,
    intents: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(question, str) or not question.strip():
        raise SemanticResolutionError("question must be a non-empty string")

    registry_data = registry if registry is not None else load_semantic_registry()
    intents_data = intents if intents is not None else load_semantic_intents()
    validate_semantic_intents(intents_data, registry_data)

    if intents_data["policy"].get("prefer_exact_field_reference") is True:
        direct_field = _resolve_exact_field_reference(question, registry_data)
        if direct_field is not None:
            return direct_field

    normalized = _normalize(question)
    candidates: list[tuple[int, int, dict[str, Any], list[str]]] = []
    for route in intents_data["routes"]:
        matches = _route_matches(normalized, route)
        if matches:
            longest = max(len(_normalize(item)) for item in matches)
            candidates.append((route["priority"], longest, route, matches))

    if not candidates:
        return {
            "status": "UNKNOWN",
            "resolution_type": "UNKNOWN",
            "reason": "Запрос не сопоставлен ни с одной подтверждённой семантикой текущей базы.",
            "execution_allowed": False,
            "next_action": "DO_NOT_GUESS_OR_QUERY_ARCHIVE",
        }

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
            return {
                "status": "AMBIGUOUS",
                "resolution_type": "AMBIGUOUS",
                "reason": "Запрос одновременно соответствует нескольким несовместимым семантическим маршрутам.",
                "candidate_routes": [item[2]["id"] for item in tied],
                "execution_allowed": False,
                "next_action": "CLARIFY_OR_NORMALIZE_INTENT",
            }

    return _resolve_route(top_route, top_matches, registry_data)
