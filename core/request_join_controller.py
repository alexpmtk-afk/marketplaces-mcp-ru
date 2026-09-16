"""Fail-closed join/calculation controller for executed marketplace source legs.

V1 deliberately separates two permissions:
1. a registered descriptive join may place independently validated leg results
   side by side while preserving their provenance;
2. arithmetic is forbidden unless a separate calculation contract is explicitly
   registered. There are no cross-source arithmetic contracts in V1.

The controller rebuilds Query Execution Controller V1 from the original request,
so callers cannot invent required legs or weaken their gates. It then binds
actual executor results to the exact contract IDs returned by that controller.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date
from typing import Any, Optional

from .business_registry import resolve_business_cabinet
from .request_execution_controller import control_marketplace_execution

JOIN_CONTROLLER_VERSION = "marketplace_join_controller.v1"
JOIN_CONTRACT_VERSION = "marketplace_join_contract.v1"
CALCULATION_CONTRACT_VERSION = "marketplace_calculation_contract.v1"

JOIN_READY = "READY"
JOIN_BLOCKED = "BLOCKED"
JOIN_CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"

_JOIN_CONTRACTS: dict[frozenset[tuple[str, str]], dict[str, Any]] = {
    frozenset({
        ("CAPABILITY", "sale_and_return_operations"),
        ("CAPABILITY", "advertising_performance"),
    }): {
        "contract_id": "wb_sales_vs_advertising_side_by_side.v1",
        "marketplace": "wb",
        "strategy": "SIDE_BY_SIDE_COMPARISON",
        "comparison_mode": "SIDE_BY_SIDE_ONLY",
        "required_scope_alignment": ["marketplace", "seller", "date_from", "date_to"],
        "currency_policy": (
            "preserve each leg's currency/provenance independently; currency alignment is not inferred"
        ),
        "arithmetic_allowed": False,
    },
}

# Existing advertising DRR/ROAS remain internal to wb_ads_m0.v1. V1 has no
# approved cross-source arithmetic formula.
_CALCULATION_CONTRACTS: dict[str, dict[str, Any]] = {}

_ARITHMETIC_MARKERS = re.compile(
    r"(?:\b(?:доля|процент|процента|процентов|отношение|разница|вычти|вычесть|"
    r"делить|разделить|во\s+сколько|окупаемость|маржинальность|рентабельность)\b|"
    r"\b(?:drr|roas)\b|ддр|%|рассчитай|посчитай)",
    re.IGNORECASE,
)


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _normalize_marketplace(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"wb", "wildberries", "вайлдберриз"}:
        return "wb"
    if text in {"ozon", "озон"}:
        return "ozon"
    return text


def _canonical_seller(marketplace: str, seller: str) -> str:
    resolved = resolve_business_cabinet(marketplace, str(seller or "").strip())
    return resolved.cabinet if resolved else str(seller or "").strip()


def _semantic_key(target: Any) -> tuple[str, str] | None:
    if not isinstance(target, dict):
        return None
    target_type = str(target.get("type") or "")
    target_id = str(target.get("id") or "")
    if not target_type or not target_id:
        return None
    return target_type, target_id


def _result_semantic_key(result: dict[str, Any]) -> tuple[str, str] | None:
    resolution = result.get("semantic_resolution")
    if isinstance(resolution, dict):
        kind = str(resolution.get("resolution_type") or "")
        if kind == "CAPABILITY" and resolution.get("capability_id"):
            return kind, str(resolution["capability_id"])
        if kind == "BUSINESS_METRIC" and resolution.get("metric_id"):
            return kind, str(resolution["metric_id"])
    if result.get("capability_id"):
        return "CAPABILITY", str(result["capability_id"])
    metric = str(result.get("metric") or "")
    if metric:
        return "BUSINESS_METRIC", metric
    return None


def _coverage_status(result: dict[str, Any]) -> str:
    coverage = result.get("coverage")
    if isinstance(coverage, dict):
        return str(coverage.get("status") or "")
    return str(coverage or "")


def _result_scope(result: dict[str, Any]) -> dict[str, Any]:
    normalized = result.get("normalized_query")
    normalized = normalized if isinstance(normalized, dict) else {}
    period = normalized.get("period")
    period = period if isinstance(period, dict) else {}
    return {
        "marketplace": _normalize_marketplace(result.get("marketplace")),
        "seller": str(result.get("cabinet") or result.get("seller") or "").strip(),
        "date_from": str(result.get("date_from") or period.get("date_from") or ""),
        "date_to": str(result.get("date_to") or period.get("date_to") or ""),
    }


def _validate_leg_result(
    contract: dict[str, Any],
    result: Any,
    *,
    marketplace: str,
    seller: str,
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    contract_id = str(contract.get("contract_id") or "")
    if not isinstance(result, dict):
        return [{
            "contract_id": contract_id,
            "type": "LEG_RESULT_INVALID",
            "details": "Executor result must be a mapping.",
        }]
    if result.get("ok") is not True:
        blockers.append({
            "contract_id": contract_id,
            "type": "LEG_RESULT_NOT_SUCCESSFUL",
            "details": result.get("error") or result.get("message") or "ok != true",
        })

    expected_target = _semantic_key(contract.get("semantic_target"))
    actual_target = _result_semantic_key(result)
    if expected_target and actual_target != expected_target:
        blockers.append({
            "contract_id": contract_id,
            "type": "SEMANTIC_TARGET_MISMATCH",
            "details": {"expected": expected_target, "actual": actual_target},
        })

    gate = str(contract.get("coverage_gate") or "")
    if gate == "FULL_COVERAGE" and _coverage_status(result) != "FULL_COVERAGE":
        blockers.append({
            "contract_id": contract_id,
            "type": "COVERAGE_GATE_NOT_PROVEN",
            "details": {"required": "FULL_COVERAGE", "actual": _coverage_status(result) or None},
        })

    scope = _result_scope(result)
    expected_marketplace = _normalize_marketplace(marketplace)
    if scope["marketplace"] and scope["marketplace"] != expected_marketplace:
        blockers.append({
            "contract_id": contract_id,
            "type": "MARKETPLACE_SCOPE_MISMATCH",
            "details": {"expected": expected_marketplace, "actual": scope["marketplace"]},
        })

    canonical_seller = _canonical_seller(expected_marketplace, seller)
    if scope["seller"] and scope["seller"] not in {str(seller).strip(), canonical_seller}:
        blockers.append({
            "contract_id": contract_id,
            "type": "SELLER_SCOPE_MISMATCH",
            "details": {"expected": canonical_seller, "actual": scope["seller"]},
        })

    for field, expected in (("date_from", date_from), ("date_to", date_to)):
        actual = scope[field]
        if actual and expected and actual != expected:
            blockers.append({
                "contract_id": contract_id,
                "type": "PERIOD_SCOPE_MISMATCH",
                "details": {"field": field, "expected": expected, "actual": actual},
            })
    return blockers


def _block(
    *,
    execution_control: dict[str, Any],
    blockers: list[dict[str, Any]],
    state: str = JOIN_BLOCKED,
) -> dict[str, Any]:
    return {
        "ok": False,
        "controller": JOIN_CONTROLLER_VERSION,
        "state": state,
        "can_join": False,
        "arithmetic_allowed": False,
        "join_contract": None,
        "calculation_contract": None,
        "blockers": blockers,
        "execution_control": execution_control,
    }


def control_marketplace_join(
    question: str,
    *,
    marketplace: str = "",
    seller: str = "",
    date_from: str = "",
    date_to: str = "",
    leg_results: Optional[list[dict[str, Any]]] = None,
    calculation_id: str = "",
    nm_ids: Optional[list[int]] = None,
    target_ids: Optional[list[str]] = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Validate executed legs and authorize only a registered result join.

    ``leg_results`` entries must be ``{"contract_id": ..., "result": {...}}``.
    The exact execution contracts are rebuilt from the original request first.
    """
    execution = control_marketplace_execution(
        question,
        marketplace=marketplace,
        seller=seller,
        date_from=date_from,
        date_to=date_to,
        nm_ids=nm_ids,
        target_ids=target_ids,
        today=today,
    )
    if execution.get("state") == "CLARIFICATION_REQUIRED":
        return _block(
            execution_control=execution,
            state=JOIN_CLARIFICATION_REQUIRED,
            blockers=[{
                "type": "EXECUTION_CLARIFICATION_REQUIRED",
                "details": execution.get("required_context") or execution.get("blockers") or [],
            }],
        )
    if execution.get("can_dispatch") is not True:
        return _block(
            execution_control=execution,
            blockers=[{
                "type": "EXECUTION_NOT_DISPATCHABLE",
                "details": execution.get("blockers") or execution.get("source_gap") or [],
            }],
        )

    contracts = list(execution.get("dispatch_contracts") or [])
    if len(contracts) < 2:
        return _block(
            execution_control=execution,
            blockers=[{
                "type": "JOIN_NOT_REQUIRED",
                "details": "The request has fewer than two dispatch contracts.",
            }],
        )

    target_set = frozenset(
        key for key in (_semantic_key(item.get("semantic_target")) for item in contracts) if key
    )
    registered = _JOIN_CONTRACTS.get(target_set)
    if registered is None:
        return _block(
            execution_control=execution,
            blockers=[{
                "type": "JOIN_CONTRACT_NOT_REGISTERED",
                "details": sorted([list(item) for item in target_set]),
            }],
        )
    if _normalize_marketplace(marketplace) != registered["marketplace"]:
        return _block(
            execution_control=execution,
            blockers=[{
                "type": "JOIN_MARKETPLACE_NOT_APPROVED",
                "details": _normalize_marketplace(marketplace),
            }],
        )

    requested_calculation = str(calculation_id or "").strip()
    arithmetic_requested = bool(_ARITHMETIC_MARKERS.search(str(question or "")))
    if requested_calculation:
        calculation = _CALCULATION_CONTRACTS.get(requested_calculation)
        if calculation is None:
            return _block(
                execution_control=execution,
                blockers=[{
                    "type": "CALCULATION_CONTRACT_NOT_REGISTERED",
                    "details": requested_calculation,
                }],
            )
    elif arithmetic_requested:
        return _block(
            execution_control=execution,
            blockers=[{
                "type": "CALCULATION_CONTRACT_REQUIRED",
                "details": (
                    "The wording requests derived arithmetic, but V1 has no approved cross-source calculation formula."
                ),
            }],
        )

    strategy = str((execution.get("join_control") or {}).get("strategy") or "")
    if strategy != registered["strategy"]:
        return _block(
            execution_control=execution,
            blockers=[{
                "type": "JOIN_STRATEGY_NOT_APPROVED",
                "details": {"required": registered["strategy"], "actual": strategy},
            }],
        )

    supplied = leg_results or []
    indexed: dict[str, Any] = {}
    result_blockers: list[dict[str, Any]] = []
    for item in supplied:
        if not isinstance(item, dict):
            result_blockers.append({"type": "LEG_RESULT_ENVELOPE_INVALID", "details": item})
            continue
        contract_id = str(item.get("contract_id") or "")
        if not contract_id:
            result_blockers.append({"type": "LEG_RESULT_CONTRACT_ID_REQUIRED", "details": item})
            continue
        if contract_id in indexed:
            result_blockers.append({"type": "DUPLICATE_LEG_RESULT", "details": contract_id})
            continue
        indexed[contract_id] = item.get("result")

    expected_ids = {str(item.get("contract_id") or "") for item in contracts}
    supplied_ids = set(indexed)
    missing = sorted(expected_ids - supplied_ids)
    unexpected = sorted(supplied_ids - expected_ids)
    if missing:
        result_blockers.append({"type": "REQUIRED_LEG_RESULT_MISSING", "details": missing})
    if unexpected:
        result_blockers.append({"type": "UNEXPECTED_LEG_RESULT", "details": unexpected})

    for contract in contracts:
        contract_id = str(contract.get("contract_id") or "")
        if contract_id not in indexed:
            continue
        result_blockers.extend(_validate_leg_result(
            contract,
            indexed[contract_id],
            marketplace=marketplace,
            seller=seller,
            date_from=date_from,
            date_to=date_to,
        ))

    if result_blockers:
        return _block(execution_control=execution, blockers=result_blockers)

    joined_legs = []
    for contract in contracts:
        contract_id = str(contract["contract_id"])
        joined_legs.append({
            "contract_id": contract_id,
            "semantic_target": deepcopy(contract.get("semantic_target")),
            "source": deepcopy(contract.get("source")),
            "coverage_gate": contract.get("coverage_gate"),
            "result": deepcopy(indexed[contract_id]),
        })

    return {
        "ok": True,
        "controller": JOIN_CONTROLLER_VERSION,
        "state": JOIN_READY,
        "can_join": True,
        "join_contract": {
            "contract_version": JOIN_CONTRACT_VERSION,
            **deepcopy(registered),
            "required_contract_ids": [item["contract_id"] for item in contracts],
            "provenance_required": True,
        },
        "joined_legs": joined_legs,
        "arithmetic_allowed": False,
        "calculation_contract": None,
        "calculation_contract_version": CALCULATION_CONTRACT_VERSION,
        "registered_cross_source_calculations": [],
        "guardrail": (
            "This contract authorizes descriptive side-by-side comparison only. "
            "Do not derive percentages, ratios, differences, totals, DRR, ROAS or profitability from these legs."
        ),
        "execution_control": execution,
    }


def register_request_join_controller_tool(combined: Any) -> None:
    """Expose the canonical post-execution join/calculation authorization step."""

    @combined.tool(
        name="marketplace_join_control",
        annotations={
            "title": "Marketplace Join / Calculation Contracts V1",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_join_control(
        question: str,
        marketplace: str = "",
        seller: str = "",
        date_from: str = "",
        date_to: str = "",
        leg_results: Optional[list[dict[str, Any]]] = None,
        calculation_id: str = "",
        nm_ids: Optional[list[int]] = None,
        target_ids: Optional[list[str]] = None,
    ) -> str:
        """Authorize a registered join; never invent cross-source arithmetic."""
        return _j(control_marketplace_join(
            question,
            marketplace=marketplace,
            seller=seller,
            date_from=date_from,
            date_to=date_to,
            leg_results=leg_results,
            calculation_id=calculation_id,
            nm_ids=nm_ids,
            target_ids=target_ids,
        ))
