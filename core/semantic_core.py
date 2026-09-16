"""Canonical unified Semantic Core brain for Marketplaces MCP.

This module does not duplicate business rules. It composes the already
validated canonical contracts that own meaning, routing, execution, joins and
calculation permissions into one server-side snapshot. The snapshot is the
single place an agent can inspect the complete business-logic state while the
underlying owner files remain the only writable sources of truth.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import request_join_controller as _join_controller
from . import system_map as _system_map
from .calculation_contract_registry import calculation_registry_summary
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

# This manifest owns where logic lives, not the business rules themselves. That
# prevents a future contributor from creating another independent rule catalog.
PROCESS_OWNERSHIP: dict[str, dict[str, Any]] = {
    "UNDERSTAND": {
        "owners": ["core/business_query_parser.py", "core/semantic_intents.yaml"],
        "purpose": "normalize natural-language business meaning without selecting provider fields",
    },
    "RESOLVE": {
        "owners": [
            "core/semantic_registry.yaml",
            "core/semantic_registry_extensions.yaml",
            "core/semantic_resolver.py",
        ],
        "purpose": "resolve business meaning to registered metrics/capabilities/sources and field semantics",
    },
    "PLAN_SOURCE": {
        "owners": ["core/request_source_router.py"],
        "purpose": "select source family before any report, field or endpoint is chosen",
    },
    "CLARIFY": {
        "owners": ["core/request_source_router.py", "core/request_source_system_map.py"],
        "purpose": "fail closed on missing context, ambiguity or unknown meaning before data reads",
    },
    "DISPATCH": {
        "owners": ["core/request_execution_controller.py"],
        "purpose": "build deterministic per-leg executor contracts for every required source leg",
    },
    "EXECUTE_ARCHIVE": {
        "owners": ["core/semantic_execution.yaml", "core/semantic_archive.py"],
        "purpose": "execute approved weekly-finance semantics only after required coverage is proven",
    },
    "EXECUTE_ADVERTISING": {
        "owners": ["core/semantic_advertising.py", "core/semantic_registry_extensions.yaml"],
        "purpose": "execute approved historical WB advertising semantics with roster/fullstats coverage gates",
    },
    "EXECUTE_OPERATIONAL": {
        "owners": ["core/semantic_business_router.py", "core/semantic_current_stock.py", "core/business_router.py"],
        "purpose": "execute approved live/operational business metrics without substituting archive history",
    },
    "JOIN": {
        "owners": ["core/request_join_controller.py"],
        "purpose": "bind required executor results and authorize only registered cross-source synthesis",
    },
    "CALCULATE": {
        "owners": ["core/calculation_contract_registry.py"],
        "purpose": "authorize only registered server-owned derived formulas; validation alone never executes arithmetic",
    },
    "COVERAGE": {
        "owners": ["core/archive_coverage.py", "core/archive_refresh_verify.py"],
        "purpose": "prove source coverage/completeness and canonical-file integrity before answerability",
    },
    "ARCHIVE_LIFECYCLE": {
        "owners": ["core/archive_refresh.py", "core/archive_tools.py", "core/archive_hybrid.py"],
        "purpose": "refresh, verify and expose canonical historical datasets without becoming a business-semantic source",
    },
    "ENTITY_AND_CABINET": {
        "owners": ["core/entities.yaml", "core/entities.py", "core/business_registry.py"],
        "purpose": "resolve marketplace entities and business cabinet aliases without exposing credentials",
    },
    "PROVIDER_API": {
        "owners": ["core/registry.py", "core/tools.py", "wb_mcp/endpoints.yaml", "ozon_mcp/endpoints.yaml"],
        "purpose": "execute provider endpoints only after Semantic Core has selected the correct business/source contract",
    },
}

_REQUIRED_STAGES = {
    "UNDERSTAND",
    "RESOLVE",
    "PLAN_SOURCE",
    "CLARIFY",
    "DISPATCH",
    "EXECUTE_ARCHIVE",
    "EXECUTE_ADVERTISING",
    "EXECUTE_OPERATIONAL",
    "JOIN",
    "CALCULATE",
    "COVERAGE",
}


class SemanticCoreError(RuntimeError):
    """Raised when canonical Semantic Core layers drift or become unsafe."""


def _join_registry_summary() -> dict[str, Any]:
    """Project the actual Join Controller registry without copying its rules."""
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
    source_families = [
        SOURCE_CANONICAL_ARCHIVE,
        SOURCE_LIVE_CABINET_API,
        SOURCE_PUBLIC_MARKETPLACE,
        SOURCE_SYSTEM_INTERNAL,
        SOURCE_HYBRID,
        SOURCE_UNAVAILABLE,
    ]
    return {
        "plan_version": "marketplace_execution_plan.v2",
        "runtime_entry": "marketplace_query_plan",
        "source_families": source_families,
        "execution_states": [
            EXECUTION_READY,
            EXECUTION_READY_WITH_GATES,
            EXECUTION_NEEDS_CONTEXT,
            EXECUTION_BLOCKED,
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
        str(route["id"]): {
            "status": "REQUIRES_OTHER_SOURCE",
            "reason": route["reason"],
        }
        for route in intents.get("routes", [])
        if route.get("target_type") == "inline_not_covered"
    }


def _summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    registry = snapshot["data_semantics"]
    intents = snapshot["understanding"]["intents"]
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
            "intent_routes": len(intents.get("routes", [])),
            "archive_executors": len(execution.get("executors", {})),
            "join_contracts": len(snapshot["join_control"].get("contracts", [])),
            "calculation_contracts": int(calculation.get("registered_calculation_count", 0)),
            "known_source_gaps": len(snapshot["gaps"]["registry"]) + len(snapshot["gaps"]["inline"]),
        },
        "safety": {
            "fail_closed": True,
            "silent_source_substitution_forbidden": True,
            "full_coverage_required_for_archive_execution": True,
            "cross_currency_arithmetic_requires_explicit_contract": True,
            "client_authored_formulas_forbidden": True,
            "provenance_required_for_calculations": True,
        },
        "available_sections": [
            "understanding",
            "data_semantics",
            "planning",
            "execution",
            "join_control",
            "calculation_control",
            "gaps",
            "process_ownership",
            "component_versions",
            "all",
        ],
    }


def validate_semantic_core(snapshot: dict[str, Any]) -> None:
    """Validate cross-layer invariants after each owner registry validates itself."""
    if snapshot.get("version") != SEMANTIC_CORE_VERSION:
        raise SemanticCoreError("unexpected Semantic Core version")
    if snapshot.get("status") != SEMANTIC_CORE_STATUS:
        raise SemanticCoreError("Semantic Core must be CANONICAL_BRAIN")

    registry = snapshot.get("data_semantics") or {}
    intents = (snapshot.get("understanding") or {}).get("intents") or {}
    execution = (snapshot.get("execution") or {}).get("archive_execution_registry") or {}
    calculation = snapshot.get("calculation_control") or {}
    join = snapshot.get("join_control") or {}

    if (registry.get("policy") or {}).get("fail_closed") is not True:
        raise SemanticCoreError("data semantics must fail closed")
    if (intents.get("policy") or {}).get("fail_closed_on_unknown") is not True:
        raise SemanticCoreError("intent routing must fail closed on unknown meaning")
    if (execution.get("policy") or {}).get("fail_closed") is not True:
        raise SemanticCoreError("archive execution must fail closed")
    if (execution.get("policy") or {}).get("require_full_coverage") is not True:
        raise SemanticCoreError("archive execution must require FULL_COVERAGE")
    if (calculation.get("policy") or {}).get("fail_closed") is not True:
        raise SemanticCoreError("calculation registry must fail closed")
    if (calculation.get("policy") or {}).get("client_authored_formulas_forbidden") is not True:
        raise SemanticCoreError("client-authored formulas must remain forbidden")

    capabilities = registry.get("capabilities") or {}
    business_metrics = intents.get("business_metrics") or {}
    archive_executors = execution.get("executors") or {}
    unknown_executor_targets = sorted(set(archive_executors) - set(capabilities))
    if unknown_executor_targets:
        raise SemanticCoreError(
            f"archive executors reference unknown capabilities: {unknown_executor_targets}"
        )

    for contract in join.get("contracts") or []:
        for target in contract.get("semantic_targets") or []:
            target_type = target.get("type")
            target_id = target.get("id")
            if target_type == "CAPABILITY" and target_id not in capabilities:
                raise SemanticCoreError(
                    f"join contract {contract.get('contract_id')} references unknown capability {target_id!r}"
                )
            if target_type == "BUSINESS_METRIC" and target_id not in business_metrics:
                raise SemanticCoreError(
                    f"join contract {contract.get('contract_id')} references unknown business metric {target_id!r}"
                )

    if calculation.get("registered_calculation_count") != len(
        calculation.get("registered_calculation_ids") or []
    ):
        raise SemanticCoreError("calculation registry summary count is inconsistent")
    if calculation.get("arithmetic_enabled") != bool(
        calculation.get("registered_calculation_ids") or []
    ):
        raise SemanticCoreError("calculation arithmetic_enabled does not match registry contents")

    planning = snapshot.get("planning") or {}
    source_families = planning.get("source_families") or []
    if len(source_families) != len(set(source_families)) or SOURCE_UNAVAILABLE not in source_families:
        raise SemanticCoreError("source-family registry is incomplete or duplicated")

    ownership = snapshot.get("process_ownership") or {}
    missing_stages = sorted(_REQUIRED_STAGES - set(ownership))
    if missing_stages:
        raise SemanticCoreError(f"Semantic Core ownership is incomplete: {missing_stages}")


def build_semantic_core_snapshot() -> dict[str, Any]:
    """Build and cross-validate the complete current business-logic brain."""
    registry = load_semantic_registry()
    intents = load_semantic_intents()
    archive_execution = load_semantic_execution()
    calculation = calculation_registry_summary()
    join = _join_registry_summary()

    snapshot: dict[str, Any] = {
        "version": SEMANTIC_CORE_VERSION,
        "status": SEMANTIC_CORE_STATUS,
        "validated": False,
        "canonical_flow": [
            "UNDERSTAND",
            "RESOLVE",
            "PLAN_SOURCE",
            "CLARIFY",
            "DISPATCH",
            "EXECUTE",
            "JOIN",
            "CALCULATE_IF_REGISTERED",
            "ANSWER_WITH_PROVENANCE",
        ],
        "understanding": {
            "parser_owner": "core/business_query_parser.py",
            "resolver_owner": "core/semantic_resolver.py",
            "intents": intents,
        },
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
    """Return a stable section of the canonical brain without a second registry."""
    snapshot = build_semantic_core_snapshot()
    key = str(section or "summary").strip().lower()
    if key in {"summary", ""}:
        return deepcopy(snapshot["summary"])
    if key == "all":
        return snapshot
    aliases = {
        "registry": "data_semantics",
        "intents": "understanding",
        "execution": "execution",
        "planning": "planning",
        "join": "join_control",
        "calculation": "calculation_control",
        "ownership": "process_ownership",
        "versions": "component_versions",
        "gaps": "gaps",
    }
    resolved = aliases.get(key, key)
    if resolved not in snapshot:
        raise SemanticCoreError(
            f"unknown Semantic Core section {section!r}; use one of {snapshot['summary']['available_sections']}"
        )
    return deepcopy(snapshot[resolved])


def _install_system_map_extension() -> None:
    """Mark the composed brain canonical without duplicating its business rules."""
    current = dict(_system_map.SYSTEM_MAP.get("semantic_core") or {})
    current.update({
        "status": SEMANTIC_CORE_STATUS,
        "brain_version": SEMANTIC_CORE_VERSION,
        "brain_runtime_entry": "marketplace_semantic_core",
        "brain_composer": "core/semantic_core.py",
        "brain_policy": (
            "compose and validate canonical owner registries at runtime; do not create a second independent business-rule catalog"
        ),
        "canonical_flow": [
            "UNDERSTAND",
            "RESOLVE",
            "PLAN_SOURCE",
            "CLARIFY",
            "DISPATCH",
            "EXECUTE",
            "JOIN",
            "CALCULATE_IF_REGISTERED",
            "ANSWER_WITH_PROVENANCE",
        ],
    })
    _system_map.SYSTEM_MAP["semantic_core"] = current
    _system_map.SYSTEM_MAP.setdefault("routing_policy", {})["semantic_core_brain"] = (
        "marketplace_semantic_core is the canonical composed business-logic view; underlying owner registries remain the only writable sources of truth"
    )
    marker = "marketplace_semantic_core is the canonical composed business brain"
    if marker not in _system_map.SYSTEM_INSTRUCTIONS:
        _system_map.SYSTEM_INSTRUCTIONS += (
            "\n" + marker + ". Consult it when inspecting business meaning, source routing, execution, join, calculation, coverage or known-gap policy. "
            "Do not create or rely on an independent parallel business-rule catalog.\n"
        )


_install_system_map_extension()


def register_semantic_core_tool(mcp: FastMCP) -> None:
    """Expose the canonical composed business brain as a read-only MCP tool."""

    @mcp.tool(
        name="marketplace_semantic_core",
        annotations={
            "title": "Canonical Marketplace Semantic Core brain",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_semantic_core(section: str = "summary") -> str:
        """Inspect the validated server-side business logic.

        Use ``summary`` for the compact state, a named section such as
        ``planning``, ``execution``, ``registry``, ``intents``, ``join``,
        ``calculation``, ``gaps`` or ``ownership`` for one layer, and ``all``
        only when the entire brain snapshot is required.
        """
        return json.dumps(semantic_core_view(section), ensure_ascii=False, indent=2, default=str)
