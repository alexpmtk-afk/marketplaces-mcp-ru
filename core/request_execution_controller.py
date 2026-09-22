"""Fail-closed execution controller for marketplace_execution_plan.v2.

The top-level source router decides *what* source legs are required. This module
turns an already-safe plan into explicit per-leg executor contracts. It never
executes provider/archive reads itself; its job is to prevent a client from
reusing one compound natural-language question as the input to every lower
executor.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Optional

from .request_source_router import (
    EXECUTION_BLOCKED,
    EXECUTION_NEEDS_CONTEXT,
    EXECUTION_READY,
    EXECUTION_READY_WITH_GATES,
    plan_marketplace_request,
)
from .semantic_resolver import resolve_semantic_question
from .user_facing import present_execution_control

CONTROLLER_VERSION = "marketplace_execution_controller.v1"
LEG_CONTRACT_VERSION = "marketplace_leg_execution.v1"

CONTROL_READY = "READY"
CONTROL_READY_WITH_GATES = "READY_WITH_GATES"
CONTROL_CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
CONTROL_BLOCKED = "BLOCKED"

_CANONICAL_BUSINESS_QUESTION_BY_PURPOSE = {
    "advertising": "рекламные расходы",
    "sales_or_finance": "продажи",
    "orders": "заказы",
    "current_stock": "текущие остатки",
}

_CANONICAL_BUSINESS_QUESTION_BY_TARGET = {
    ("CAPABILITY", "advertising_performance"): "рекламные расходы",
    ("CAPABILITY", "sale_and_return_operations"): "продажи",
    ("BUSINESS_METRIC", "ORDERS"): "заказы",
    ("BUSINESS_METRIC", "CURRENT_STOCK"): "текущие остатки",
}


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _semantic_key(target: dict[str, Any] | None) -> tuple[str, str] | None:
    if not isinstance(target, dict):
        return None
    target_type = str(target.get("type") or "")
    target_id = str(target.get("id") or "")
    if not target_type or not target_id:
        return None
    return target_type, target_id


def _resolution_target(resolution: dict[str, Any]) -> dict[str, str] | None:
    resolution_type = str(resolution.get("resolution_type") or "")
    if resolution_type == "BUSINESS_METRIC":
        target_id = str(resolution.get("metric_id") or "")
    elif resolution_type == "CAPABILITY":
        target_id = str(resolution.get("capability_id") or "")
    else:
        return None
    return {"type": resolution_type, "id": target_id} if target_id else None


def _targeted_business_question(
    *, leg: dict[str, Any], original_question: str, multi_source: bool,
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Return a narrow question + verified target, or a fail-closed blocker."""
    semantic_target = leg.get("semantic_target") if isinstance(leg.get("semantic_target"), dict) else None
    key = _semantic_key(semantic_target)
    targeted = _CANONICAL_BUSINESS_QUESTION_BY_TARGET.get(key) if key else None
    if not targeted:
        targeted = _CANONICAL_BUSINESS_QUESTION_BY_PURPOSE.get(str(leg.get("purpose") or ""))

    if not targeted and not multi_source and semantic_target:
        targeted = original_question

    if not targeted:
        return None, None, {
            "leg_id": leg.get("leg_id"),
            "type": "LEG_SEMANTIC_TARGET_REQUIRED",
            "details": "No deterministic narrow business question is registered for this execution leg.",
        }

    resolution = resolve_semantic_question(targeted)
    resolved_target = _resolution_target(resolution)
    if resolved_target is None:
        return None, None, {
            "leg_id": leg.get("leg_id"),
            "type": "LEG_SEMANTIC_TARGET_NOT_EXECUTABLE",
            "details": {
                "targeted_question": targeted,
                "resolution_type": resolution.get("resolution_type"),
            },
        }

    if semantic_target and _semantic_key(semantic_target) != _semantic_key(resolved_target):
        return None, None, {
            "leg_id": leg.get("leg_id"),
            "type": "LEG_SEMANTIC_TARGET_MISMATCH",
            "details": {
                "planned": semantic_target,
                "resolved": resolved_target,
                "targeted_question": targeted,
            },
        }

    return targeted, resolved_target, None


def _card_marketplace(marketplace: str) -> str:
    value = str(marketplace or "").strip().lower()
    if value in {"wb", "wildberries", "вайлдберриз"}:
        return "wildberries"
    if value in {"ozon", "озон"}:
        return "ozon"
    return value


def _build_leg_contract(
    leg: dict[str, Any], *, original_question: str, marketplace: str, seller: str,
    date_from: str, date_to: str, nm_ids: Optional[list[int]],
    target_ids: Optional[list[str]], multi_source: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    executor = str(leg.get("executor") or "")
    leg_id = str(leg.get("leg_id") or "")
    purpose = str(leg.get("purpose") or "")

    if not executor:
        return None, {
            "leg_id": leg_id,
            "type": "EXECUTOR_UNAVAILABLE",
            "details": leg.get("source_status"),
        }

    semantic_target = leg.get("semantic_target") if isinstance(leg.get("semantic_target"), dict) else None
    targeted_question: str | None = None

    if executor == "marketplace_business_query":
        if purpose == "ozon_current_snapshot":
            # The original single-source wording carries the named cabinet and
            # product identifier required by the Ozon executor. The source
            # planner has already admitted only the approved snapshot metrics,
            # and the executor revalidates them before any provider read.
            if multi_source:
                return None, {
                    "leg_id": leg_id,
                    "type": "OZON_SNAPSHOT_MUST_BE_SINGLE_SOURCE",
                    "details": "The approved Ozon current snapshot contract must execute as one fail-closed single-source leg.",
                }
            targeted_question = original_question
            verified_target = semantic_target
        else:
            targeted_question, verified_target, blocker = _targeted_business_question(
                leg=leg,
                original_question=original_question,
                multi_source=multi_source,
            )
            if blocker:
                return None, blocker
        semantic_target = verified_target
        args: dict[str, Any] = {
            "marketplace": marketplace,
            "seller": seller,
            "date_from": date_from,
            "date_to": date_to,
            "question": targeted_question,
        }
        if nm_ids:
            args["nm_ids"] = list(nm_ids)

    elif executor == "card_monitor_get_latest":
        if not target_ids and not any(marker in original_question.casefold() for marker in ("все", "всех", "список")):
            return None, {
                "leg_id": leg_id,
                "type": "PRODUCT_TARGET_REQUIRED",
                "details": "A public-card leg needs target_ids unless the user explicitly asks for all monitored cards.",
            }
        args = {
            "marketplace": _card_marketplace(marketplace),
            "only_pass": True,
        }
        if target_ids:
            args["target_ids"] = list(target_ids)

    elif executor == "card_monitor_get_history":
        if not target_ids or len(target_ids) != 1:
            return None, {
                "leg_id": leg_id,
                "type": "SINGLE_PRODUCT_TARGET_REQUIRED",
                "details": "Historical public-card lookup requires exactly one monitored target_id.",
            }
        args = {"target_id": str(target_ids[0]), "limit": 100, "only_pass": True}

    elif executor in {"marketplace_data_catalog", "marketplace_system_map"}:
        args = {}

    else:
        return None, {
            "leg_id": leg_id,
            "type": "EXECUTOR_CONTRACT_NOT_REGISTERED",
            "details": executor,
        }

    gate = leg.get("coverage_gate")
    return {
        "contract_version": LEG_CONTRACT_VERSION,
        "contract_id": f"exec-{leg_id}",
        "leg_id": leg_id,
        "purpose": purpose,
        "required": bool(leg.get("required", True)),
        "executor": executor,
        "executor_arguments": args,
        "targeted_question": targeted_question,
        "original_compound_question_allowed": False if multi_source else targeted_question == original_question,
        "semantic_target": semantic_target,
        "source": {
            "family": leg.get("source_family"),
            "status": leg.get("source_status"),
        },
        "coverage_gate": gate,
        "gate_policy": "EXECUTOR_MUST_PROVE_BEFORE_RESULT_IS_ANSWERABLE" if gate else "NO_EXTRA_COVERAGE_GATE",
        "forbidden_substitutes": list(leg.get("forbidden_substitutes") or []),
        "depends_on": list(leg.get("depends_on") or []),
        "provenance_required": {
            "leg_id": leg_id,
            "source_family": leg.get("source_family"),
            "source_status": leg.get("source_status"),
            "executor": executor,
            "coverage_gate": gate,
        },
    }, None


def control_marketplace_execution(
    question: str, *, marketplace: str = "", seller: str = "",
    date_from: str = "", date_to: str = "",
    nm_ids: Optional[list[int]] = None, target_ids: Optional[list[str]] = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Build deterministic dispatch contracts without executing any data source."""
    plan = plan_marketplace_request(
        question,
        marketplace=marketplace,
        seller=seller,
        date_from=date_from,
        date_to=date_to,
        today=today,
    )
    execution = dict(plan.get("execution_plan") or {})
    semantic_resolution = plan.get("semantic_resolution") or {}
    resolution_type = str(semantic_resolution.get("resolution_type") or "")

    clarification_required = (
        execution.get("status") == EXECUTION_NEEDS_CONTEXT
        or resolution_type in {"AMBIGUOUS", "UNKNOWN"}
        or plan.get("source_status") == "UNRESOLVED_SOURCE_CLASS"
    )
    if clarification_required:
        return {
            "ok": False,
            "controller": CONTROLLER_VERSION,
            "state": CONTROL_CLARIFICATION_REQUIRED,
            "can_dispatch": False,
            "dispatch_contracts": [],
            "blockers": list(execution.get("blockers") or []),
            "required_context": list(plan.get("required_context") or []),
            "semantic_resolution": semantic_resolution or None,
            "replan_required_after_clarification": True,
            "query_plan": plan,
        }

    if execution.get("status") == EXECUTION_BLOCKED or execution.get("can_start_execution") is not True:
        return {
            "ok": False,
            "controller": CONTROLLER_VERSION,
            "state": CONTROL_BLOCKED,
            "can_dispatch": False,
            "dispatch_contracts": [],
            "blockers": list(execution.get("blockers") or []),
            "source_gap": {
                "source_family": plan.get("source_family"),
                "source_status": plan.get("source_status"),
                "reason": plan.get("reason"),
            },
            "query_plan": plan,
        }

    legs = list(execution.get("legs") or [])
    multi_source = execution.get("mode") == "MULTI_SOURCE"
    prepared: list[dict[str, Any]] = []
    contract_blockers: list[dict[str, Any]] = []
    for leg in legs:
        contract, blocker = _build_leg_contract(
            leg,
            original_question=question,
            marketplace=str(leg.get("marketplace") or marketplace or ""),
            seller=seller,
            date_from=date_from,
            date_to=date_to,
            nm_ids=nm_ids,
            target_ids=target_ids,
            multi_source=multi_source,
        )
        if blocker:
            contract_blockers.append(blocker)
        elif contract:
            prepared.append(contract)

    # Required legs are all-or-nothing. Even already-prepared contracts are not
    # exposed as dispatchable when another required leg lacks a deterministic
    # contract. This prevents silent partial truth and side effects before a gap
    # is resolved.
    if contract_blockers:
        return {
            "ok": False,
            "controller": CONTROLLER_VERSION,
            "state": CONTROL_BLOCKED,
            "can_dispatch": False,
            "dispatch_contracts": [],
            "prepared_non_dispatchable_contracts": prepared,
            "blockers": contract_blockers,
            "query_plan": plan,
        }

    state = CONTROL_READY_WITH_GATES if execution.get("status") == EXECUTION_READY_WITH_GATES else CONTROL_READY
    contract_ids = [item["contract_id"] for item in prepared]
    join = dict(execution.get("join") or {})
    return {
        "ok": True,
        "controller": CONTROLLER_VERSION,
        "state": state,
        "can_dispatch": True,
        "dispatch_policy": {
            "use_only_returned_executor_and_arguments": True,
            "do_not_rewrite_executor_arguments": True,
            "do_not_pass_original_compound_question_to_each_leg": True,
            "parallel_allowed": bool(execution.get("independent_legs_can_run_in_parallel")),
        },
        "dispatch_contracts": prepared,
        "blockers": [],
        "join_control": {
            "strategy": join.get("strategy"),
            "required_contract_ids": contract_ids,
            "requires_all_required_legs": bool(join.get("requires_all_required_legs", True)),
            "allow_partial_answer": bool(join.get("allow_partial_answer", False)),
            "missing_required_leg_behavior": join.get("missing_required_leg_behavior") or "FAIL_CLOSED",
            "arithmetic_allowed": bool(join.get("arithmetic_allowed_without_explicit_semantic_contract", False)),
            "explicit_calculation_contract_required_for_arithmetic": not bool(
                join.get("arithmetic_allowed_without_explicit_semantic_contract", False)
            ),
            "provenance_required": bool(join.get("provenance_required", True)),
        },
        "query_plan": plan,
    }


def register_request_execution_controller_tool(combined: Any) -> None:
    """Expose the canonical post-clarification execution-contract step."""

    @combined.tool(
        name="marketplace_execution_control",
        annotations={
            "title": "Marketplace Query Execution Controller V1",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_execution_control(
        question: str, marketplace: str = "", seller: str = "",
        date_from: str = "", date_to: str = "",
        nm_ids: Optional[list[int]] = None,
        target_ids: Optional[list[str]] = None,
    ) -> str:
        """Return exact per-leg executor contracts; never read provider data."""
        return _j(present_execution_control(control_marketplace_execution(
            question,
            marketplace=marketplace,
            seller=seller,
            date_from=date_from,
            date_to=date_to,
            nm_ids=nm_ids,
            target_ids=target_ids,
        )))
