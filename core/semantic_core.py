"""Canonical composed Semantic Core for Marketplaces MCP.

The brain does not duplicate business rules. It composes and cross-validates
existing owner registries for meaning, routing, execution, joins, calculations,
coverage and known gaps. Owner registries remain the only writable sources of
truth; this module is the canonical read model of the whole business brain.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import request_join_controller as _join_controller
from . import system_map as _system_map
from .calculation_contract_registry import calculation_registry_summary
from .marketplace_knowledge import (
    load_marketplace_knowledge_catalog,
    validate_knowledge_against_metric_registry,
)
from .metric_registry import load_metric_registry
from .request_execution_controller import CONTROLLER_VERSION, LEG_CONTRACT_VERSION
from .request_source_router import (
    EXECUTION_BLOCKED,
    EXECUTION_NEEDS_CONTEXT,
    EXECUTION_READY,
    EXECUTION_READY_WITH_GATES,
    SOURCE_AVAILABILITY_FACTS,
    SOURCE_CANONICAL_ARCHIVE,
    SOURCE_HYBRID,
    SOURCE_LIVE_CABINET_API,
    SOURCE_PUBLIC_MARKETPLACE,
    SOURCE_SYSTEM_INTERNAL,
    SOURCE_UNAVAILABLE,
    WB_OPERATIONAL_ORDERS_RETENTION_DAYS,
)
from .semantic_archive import load_semantic_execution
from .semantic_registry import load_semantic_registry
from .semantic_resolver import load_semantic_intents

SEMANTIC_CORE_VERSION = "marketplace_semantic_core.v1"
SEMANTIC_CORE_STATUS = "CANONICAL_BRAIN"

PROCESS_OWNERSHIP: dict[str, dict[str, Any]] = {
    "UNDERSTAND": {
        "owners": ["core/business_query_parser.py", "core/semantic_intents.yaml", "core/metric_registry.yaml"],
        "purpose": "normalize natural-language business meaning and canonical metric terminology",
    },
    "RESOLVE": {
        "owners": [
            "core/metric_registry.py",
            "core/marketplace_knowledge.py",
            "core/marketplace_knowledge_catalog.yaml",
            "core/semantic_registry.yaml",
            "core/semantic_registry_extensions.yaml",
            "core/semantic_resolver.py",
        ],
        "purpose": "resolve user metric vocabulary to registered metrics/capabilities and then to sources/field semantics",
    },
    "PLAN_SOURCE": {
        "owners": ["core/request_source_router.py"],
        "purpose": "select source family before provider fields/endpoints",
    },
    "CLARIFY": {
        "owners": ["core/request_source_router.py", "core/request_source_system_map.py"],
        "purpose": "fail closed on missing context or ambiguity before reads",
    },
    "DISPATCH": {
        "owners": ["core/request_execution_controller.py"],
        "purpose": "build deterministic per-leg execution contracts",
    },
    "EXECUTE_ARCHIVE": {
        "owners": ["core/semantic_execution.yaml", "core/semantic_archive.py"],
        "purpose": "execute approved archive semantics after coverage proof",
    },
    "EXECUTE_ADVERTISING": {
        "owners": ["core/semantic_advertising.py", "core/semantic_registry_extensions.yaml"],
        "purpose": "execute approved historical advertising semantics",
    },
    "EXECUTE_OPERATIONAL": {
        "owners": ["core/semantic_business_router.py", "core/semantic_current_stock.py", "core/business_router.py"],
        "purpose": "execute approved live operational metrics",
    },
    "JOIN": {
        "owners": ["core/request_join_controller.py"],
        "purpose": "authorize only registered result joins",
    },
    "CALCULATE": {
        "owners": ["core/calculation_contract_registry.py"],
        "purpose": "authorize only registered server-owned derived formulas",
    },
    "COVERAGE": {
        "owners": ["core/archive_coverage.py", "core/archive_refresh_verify.py"],
        "purpose": "prove completeness/integrity before answerability",
    },
    "ARCHIVE_LIFECYCLE": {
        "owners": ["core/archive_refresh.py", "core/archive_tools.py", "core/archive_hybrid.py"],
        "purpose": "refresh and expose canonical history without owning business meaning",
    },
    "ENTITY_AND_CABINET": {
        "owners": ["core/entities.yaml", "core/entities.py", "core/business_registry.py"],
        "purpose": "resolve business entities and cabinet aliases",
    },
    "PROVIDER_API": {
        "owners": ["core/registry.py", "core/tools.py", "wb_mcp/endpoints.yaml", "ozon_mcp/endpoints.yaml"],
        "purpose": "execute provider endpoints only after Semantic Core routing",
    },
    "DATA_CATALOG_VIEW": {
        "owners": ["core/data_catalog.py"],
        "purpose": "backward-compatible read-only projection of Semantic Core; never an independent route registry",
    },
}

_REQUIRED_STAGES = {
    "UNDERSTAND", "RESOLVE", "PLAN_SOURCE", "CLARIFY", "DISPATCH",
    "EXECUTE_ARCHIVE", "EXECUTE_ADVERTISING", "EXECUTE_OPERATIONAL",
    "JOIN", "CALCULATE", "COVERAGE",
}


class SemanticCoreError(RuntimeError):
    pass


def _join_registry_summary() -> dict[str, Any]:
    contracts: list[dict[str, Any]] = []
    for targets, raw in _join_controller._JOIN_CONTRACTS.items():
        contract = deepcopy(raw)
        contract["semantic_targets"] = [
            {"type": target_type, "id": target_id}
            for target_type, target_id in sorted(targets)
        ]
        contracts.append(contract)
    contracts.sort(key=lambda item: str(item.get("contract_id") or ""))
    return {
        "controller_version": _join_controller.JOIN_CONTROLLER_VERSION,
        "contract_version": _join_controller.JOIN_CONTRACT_VERSION,
        "states": [
            _join_controller.JOIN_READY,
            _join_controller.JOIN_CLARIFICATION_REQUIRED,
            _join_controller.JOIN_BLOCKED,
        ],
        "contracts": contracts,
        "registered_contract_ids": [str(item["contract_id"]) for item in contracts],
        "arithmetic_permission_is_separate": True,
    }


def _planning_summary() -> dict[str, Any]:
    return {
        "plan_version": "marketplace_execution_plan.v2",
        "runtime_entry": "marketplace_query_plan",
        "source_families": [
            SOURCE_CANONICAL_ARCHIVE,
            SOURCE_LIVE_CABINET_API,
            SOURCE_PUBLIC_MARKETPLACE,
            SOURCE_SYSTEM_INTERNAL,
            SOURCE_HYBRID,
            SOURCE_UNAVAILABLE,
        ],
        "execution_states": [
            EXECUTION_READY, EXECUTION_READY_WITH_GATES,
            EXECUTION_NEEDS_CONTEXT, EXECUTION_BLOCKED,
        ],
        "availability_facts": deepcopy(SOURCE_AVAILABILITY_FACTS),
        "wb_operational_orders_retention_days": WB_OPERATIONAL_ORDERS_RETENTION_DAYS,
        "source_family_before_provider_field": True,
        "silent_substitution_forbidden": True,
    }


def _dispatch_summary() -> dict[str, Any]:
    return {
        "controller_version": CONTROLLER_VERSION,
        "leg_contract_version": LEG_CONTRACT_VERSION,
        "runtime_entry": "marketplace_execution_control",
        "server_owned_executor_arguments": True,
        "compound_question_forwarding_forbidden": True,
        "required_legs_all_or_nothing": True,
    }


def _inline_gaps(intents: dict[str, Any]) -> dict[str, Any]:
    return {
        str(route["id"]): {"status": "REQUIRES_OTHER_SOURCE", "reason": route["reason"]}
        for route in intents.get("routes", [])
        if route.get("target_type") == "inline_not_covered"
    }


def validate_semantic_core(snapshot: dict[str, Any]) -> None:
    if snapshot.get("version") != SEMANTIC_CORE_VERSION or snapshot.get("status") != SEMANTIC_CORE_STATUS:
        raise SemanticCoreError("unexpected Semantic Core identity")
    registry = snapshot["data_semantics"]
    intents = snapshot["understanding"]["intents"]
    metric_dictionary = snapshot["metric_dictionary"]
    knowledge_catalog = snapshot["knowledge_catalog"]
    execution = snapshot["execution"]["archive_execution_registry"]
    calculation = snapshot["calculation_control"]
    join = snapshot["join_control"]

    if (registry.get("policy") or {}).get("fail_closed") is not True:
        raise SemanticCoreError("data semantics must fail closed")
    if (intents.get("policy") or {}).get("fail_closed_on_unknown") is not True:
        raise SemanticCoreError("intent routing must fail closed")
    if (metric_dictionary.get("policy") or {}).get("metric_dictionary_grants_execution") is not False:
        raise SemanticCoreError("metric dictionary must never grant execution permission")
    if (metric_dictionary.get("policy") or {}).get("unknown_provider_mapping_must_not_be_inferred") is not True:
        raise SemanticCoreError("metric dictionary provider mappings must fail closed")
    if (knowledge_catalog.get("policy") or {}).get("verified_requires_official_sources") is not True:
        raise SemanticCoreError("verified human semantics must require official sources")
    try:
        validate_knowledge_against_metric_registry(knowledge_catalog, metric_dictionary)
    except Exception as exc:
        raise SemanticCoreError(f"knowledge catalog cross-validation failed: {exc}") from exc
    if (execution.get("policy") or {}).get("fail_closed") is not True:
        raise SemanticCoreError("archive execution must fail closed")
    if (execution.get("policy") or {}).get("require_full_coverage") is not True:
        raise SemanticCoreError("archive execution must require FULL_COVERAGE")
    if (calculation.get("policy") or {}).get("fail_closed") is not True:
        raise SemanticCoreError("calculation registry must fail closed")
    if (calculation.get("policy") or {}).get("client_authored_formulas_forbidden") is not True:
        raise SemanticCoreError("client-authored formulas must remain forbidden")

    capabilities = registry.get("capabilities") or {}
    metrics = intents.get("business_metrics") or {}
    executors = execution.get("executors") or {}
    unknown_executors = sorted(set(executors) - set(capabilities))
    if unknown_executors:
        raise SemanticCoreError(f"executors reference unknown capabilities: {unknown_executors}")

    for metric_id, metric in (metric_dictionary.get("metrics") or {}).items():
        target = metric.get("semantic_target") or {}
        kind, target_id = target.get("type"), target.get("id")
        if kind == "CAPABILITY" and target_id not in capabilities:
            raise SemanticCoreError(f"metric dictionary {metric_id} references unknown capability {target_id!r}")
        if kind == "BUSINESS_METRIC" and target_id not in metrics:
            raise SemanticCoreError(f"metric dictionary {metric_id} references unknown business metric {target_id!r}")
        if kind not in {"CAPABILITY", "BUSINESS_METRIC"}:
            raise SemanticCoreError(f"metric dictionary {metric_id} has unsupported semantic target {kind!r}")

    for contract in join.get("contracts") or []:
        for target in contract.get("semantic_targets") or []:
            kind, target_id = target.get("type"), target.get("id")
            if kind == "CAPABILITY" and target_id not in capabilities:
                raise SemanticCoreError(f"join references unknown capability {target_id!r}")
            if kind == "BUSINESS_METRIC" and target_id not in metrics:
                raise SemanticCoreError(f"join references unknown business metric {target_id!r}")

    ids = calculation.get("registered_calculation_ids") or []
    if calculation.get("registered_calculation_count") != len(ids):
        raise SemanticCoreError("calculation registry count is inconsistent")
    if calculation.get("arithmetic_enabled") != bool(ids):
        raise SemanticCoreError("calculation arithmetic flag is inconsistent")

    families = snapshot["planning"].get("source_families") or []
    if len(families) != len(set(families)) or SOURCE_UNAVAILABLE not in families:
        raise SemanticCoreError("source-family registry is incomplete or duplicated")
    missing = sorted(_REQUIRED_STAGES - set(snapshot["process_ownership"]))
    if missing:
        raise SemanticCoreError(f"process ownership incomplete: {missing}")


def _summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    registry = snapshot["data_semantics"]
    intents = snapshot["understanding"]["intents"]
    metric_dictionary = snapshot["metric_dictionary"]
    knowledge_catalog = snapshot["knowledge_catalog"]
    execution = snapshot["execution"]["archive_execution_registry"]
    calculation = snapshot["calculation_control"]
    return {
        "version": snapshot["version"],
        "status": snapshot["status"],
        "validated": snapshot["validated"],
        "runtime_entry": "marketplace_semantic_core",
        "canonical_flow": snapshot["canonical_flow"],
        "component_versions": snapshot["component_versions"],
        "counts": {
            "sources": len(registry.get("sources", {})),
            "datasets": len(registry.get("datasets", {})),
            "capabilities": len(registry.get("capabilities", {})),
            "business_metrics": len(intents.get("business_metrics", {})),
            "metric_dictionary_entries": len(metric_dictionary.get("metrics", {})),
            "knowledge_catalog_entries": len(knowledge_catalog.get("metrics", {})),
            "intent_routes": len(intents.get("routes", [])),
            "archive_executors": len(execution.get("executors", {})),
            "join_contracts": len(snapshot["join_control"].get("contracts", [])),
            "calculation_contracts": int(calculation.get("registered_calculation_count", 0)),
            "known_source_gaps": len(snapshot["gaps"]["registry"]) + len(snapshot["gaps"]["inline"]),
        },
        "safety": {
            "fail_closed": True,
            "metric_dictionary_is_semantic_only": True,
            "verified_human_semantics_require_official_sources": True,
            "silent_source_substitution_forbidden": True,
            "full_coverage_required_for_archive_execution": True,
            "cross_currency_arithmetic_requires_explicit_contract": True,
            "client_authored_formulas_forbidden": True,
            "provenance_required_for_calculations": True,
        },
        "available_sections": [
            "understanding", "metric_dictionary", "knowledge_catalog", "data_semantics", "planning", "execution",
            "join_control", "calculation_control", "gaps", "process_ownership",
            "component_versions", "all",
        ],
    }


def build_semantic_core_snapshot() -> dict[str, Any]:
    registry = load_semantic_registry()
    intents = load_semantic_intents()
    metric_dictionary = load_metric_registry()
    knowledge_catalog = load_marketplace_knowledge_catalog()
    archive_execution = load_semantic_execution()
    calculation = calculation_registry_summary()
    join = _join_registry_summary()
    snapshot: dict[str, Any] = {
        "version": SEMANTIC_CORE_VERSION,
        "status": SEMANTIC_CORE_STATUS,
        "validated": False,
        "canonical_flow": [
            "UNDERSTAND", "RESOLVE", "PLAN_SOURCE", "CLARIFY", "DISPATCH",
            "EXECUTE", "JOIN", "CALCULATE_IF_REGISTERED", "ANSWER_WITH_PROVENANCE",
        ],
        "understanding": {
            "parser_owner": "core/business_query_parser.py",
            "resolver_owner": "core/semantic_resolver.py",
            "metric_dictionary_owner": "core/metric_registry.yaml",
            "human_semantics_owner": "core/marketplace_knowledge_catalog.yaml",
            "intents": intents,
        },
        "metric_dictionary": metric_dictionary,
        "knowledge_catalog": knowledge_catalog,
        "data_semantics": registry,
        "planning": _planning_summary(),
        "execution": {
            "dispatch": _dispatch_summary(),
            "archive_execution_registry": archive_execution,
            "domain_executors": {
                "weekly_finance": "core/semantic_archive.py",
                "advertising": "core/semantic_advertising.py",
                "current_stock": "core/semantic_current_stock.py",
                "business_runtime_router": "core/semantic_business_router.py",
            },
        },
        "join_control": join,
        "calculation_control": calculation,
        "gaps": {
            "registry": deepcopy(registry.get("not_covered") or {}),
            "inline": _inline_gaps(intents),
        },
        "process_ownership": deepcopy(PROCESS_OWNERSHIP),
        "component_versions": {
            "semantic_core": SEMANTIC_CORE_VERSION,
            "metric_registry": metric_dictionary.get("version"),
            "knowledge_catalog": knowledge_catalog.get("version"),
            "semantic_registry": registry.get("version"),
            "semantic_registry_extensions": list(registry.get("extension_versions") or []),
            "semantic_intents": intents.get("version"),
            "semantic_execution": archive_execution.get("version"),
            "source_plan": "marketplace_execution_plan.v2",
            "execution_controller": CONTROLLER_VERSION,
            "leg_contract": LEG_CONTRACT_VERSION,
            "join_controller": join.get("controller_version"),
            "join_contract": join.get("contract_version"),
            "calculation_registry": calculation.get("registry_version"),
            "calculation_contract": calculation.get("contract_version"),
        },
    }
    validate_semantic_core(snapshot)
    snapshot["validated"] = True
    snapshot["summary"] = _summary(snapshot)
    return snapshot


def semantic_core_view(section: str = "summary") -> dict[str, Any]:
    snapshot = build_semantic_core_snapshot()
    key = str(section or "summary").strip().lower()
    if key in {"summary", ""}:
        return deepcopy(snapshot["summary"])
    if key == "all":
        return snapshot
    resolved = {
        "registry": "data_semantics",
        "metrics": "metric_dictionary",
        "metric_registry": "metric_dictionary",
        "knowledge": "knowledge_catalog",
        "knowledge_catalog": "knowledge_catalog",
        "intents": "understanding",
        "join": "join_control",
        "calculation": "calculation_control",
        "ownership": "process_ownership",
        "versions": "component_versions",
    }.get(key, key)
    if resolved not in snapshot:
        raise SemanticCoreError(
            f"unknown Semantic Core section {section!r}; use one of {snapshot['summary']['available_sections']}"
        )
    return deepcopy(snapshot[resolved])


def _install_system_map_extension() -> None:
    """Publish brain authority separately from physical executor completeness."""
    current = dict(_system_map.SYSTEM_MAP.get("semantic_core") or {})
    current.update({
        "brain_status": SEMANTIC_CORE_STATUS,
        "brain_version": SEMANTIC_CORE_VERSION,
        "brain_runtime_entry": "marketplace_semantic_core",
        "brain_composer": "core/semantic_core.py",
        "metric_dictionary": "core/metric_registry.yaml",
        "knowledge_catalog": "core/marketplace_knowledge_catalog.yaml",
        "brain_policy": "compose canonical owner registries; human business meaning comes from verified marketplace knowledge, never from provider field names alone",
        "canonical_flow": [
            "UNDERSTAND", "RESOLVE", "PLAN_SOURCE", "CLARIFY", "DISPATCH",
            "EXECUTE", "JOIN", "CALCULATE_IF_REGISTERED", "ANSWER_WITH_PROVENANCE",
        ],
    })
    _system_map.SYSTEM_MAP["semantic_core"] = current
    _system_map.SYSTEM_MAP.setdefault("routing_policy", {})["semantic_core_brain"] = (
        "marketplace_semantic_core is the canonical composed business-logic view; owner registries remain the only writable sources of truth"
    )
    marker = "marketplace_semantic_core is the canonical composed business brain"
    if marker not in _system_map.SYSTEM_INSTRUCTIONS:
        _system_map.SYSTEM_INSTRUCTIONS += (
            "\n" + marker + ". Consult it for business meaning, canonical metric terminology, source routing, execution, join, calculation, coverage and known-gap policy. "
            "Physical source/executor coverage may still be incomplete and must remain fail-closed. "
            "Do not create or rely on an independent parallel business-rule catalog.\n"
        )


_install_system_map_extension()


def register_semantic_core_tool(mcp: FastMCP) -> None:
    @mcp.tool(
        name="marketplace_semantic_core",
        annotations={
            "title": "Canonical Marketplace Semantic Core brain",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_semantic_core(section: str = "summary") -> str:
        """Inspect the validated server-side business brain or one named layer."""
        return json.dumps(semantic_core_view(section), ensure_ascii=False, indent=2, default=str)
