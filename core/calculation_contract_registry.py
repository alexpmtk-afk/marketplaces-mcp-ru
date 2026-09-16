"""Validated registry for future cross-source marketplace calculations.

The registry owns *permission to define arithmetic*, not business data reads.
V1 intentionally ships with zero approved cross-source calculations.  This
module exists so future formulas cannot be enabled by adding an ad-hoc division
inside a controller or by letting an LLM invent numerator/denominator semantics.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

REGISTRY_VERSION = "marketplace_calculation_registry.v1"
CONTRACT_VERSION = "marketplace_calculation_contract.v1"

APPROVED_STATUS = "APPROVED"
_ALLOWED_MARKETPLACES = {"wb", "ozon"}
_ALLOWED_OPERATIONS = {"RATIO", "RATIO_PERCENT", "DIFFERENCE", "SUM"}
_ALLOWED_TARGET_TYPES = {"CAPABILITY", "BUSINESS_METRIC"}
_ALLOWED_SOURCE_FAMILIES = {
    "CANONICAL_ARCHIVE",
    "LIVE_CABINET_API",
    "PUBLIC_MARKETPLACE_SOURCE",
    "SYSTEM_INTERNAL",
}
_REQUIRED_SCOPE_ALIGNMENT = {"marketplace", "seller", "date_from", "date_to"}
_ALLOWED_CURRENCY_POLICIES = {"SAME_CURRENCY_REQUIRED", "NO_CURRENCY"}
_ALLOWED_ZERO_DENOMINATOR = {"BLOCK", "RETURN_NULL"}

CALCULATION_REGISTRY: dict[str, Any] = {
    "version": REGISTRY_VERSION,
    "contract_version": CONTRACT_VERSION,
    "policy": {
        "fail_closed": True,
        "client_authored_formulas_forbidden": True,
        "require_registered_inputs": True,
        "require_scope_alignment": sorted(_REQUIRED_SCOPE_ALIGNMENT),
        "currency_alignment": "EXPLICIT_ONLY",
        "implicit_currency_conversion_forbidden": True,
        "coverage_requirement_must_be_explicit_per_input": True,
        "zero_denominator_behavior_must_be_explicit_for_ratios": True,
        "provenance_required": True,
    },
    # No formula is approved merely because the framework can validate it.
    "contracts": {},
}


class CalculationContractRegistryError(ValueError):
    """Raised when a calculation registry or contract is unsafe/invalid."""


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CalculationContractRegistryError(f"{name} must be a mapping")
    return value


def _nonempty_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CalculationContractRegistryError(f"{name} must be non-empty")
    return text


def _validate_input(spec: Any, *, contract_id: str, aliases: set[str]) -> None:
    item = _mapping(spec, f"{contract_id} input")
    alias = _nonempty_text(item.get("alias"), f"{contract_id} input.alias")
    if alias in aliases:
        raise CalculationContractRegistryError(
            f"{contract_id} has duplicate input alias {alias!r}"
        )
    aliases.add(alias)

    target = _mapping(item.get("semantic_target"), f"{contract_id}.{alias}.semantic_target")
    target_type = _nonempty_text(target.get("type"), f"{contract_id}.{alias}.target.type")
    target_id = _nonempty_text(target.get("id"), f"{contract_id}.{alias}.target.id")
    if target_type not in _ALLOWED_TARGET_TYPES:
        raise CalculationContractRegistryError(
            f"{contract_id}.{alias} uses unsupported semantic target type {target_type!r}"
        )
    if not target_id:
        raise CalculationContractRegistryError(f"{contract_id}.{alias} target id is required")

    _nonempty_text(item.get("value_path"), f"{contract_id}.{alias}.value_path")

    families = item.get("allowed_source_families")
    if not isinstance(families, list) or not families:
        raise CalculationContractRegistryError(
            f"{contract_id}.{alias}.allowed_source_families must be a non-empty list"
        )
    unknown_families = {str(value) for value in families} - _ALLOWED_SOURCE_FAMILIES
    if unknown_families:
        raise CalculationContractRegistryError(
            f"{contract_id}.{alias} has unsupported source families {sorted(unknown_families)}"
        )

    coverage = _nonempty_text(
        item.get("required_coverage"), f"{contract_id}.{alias}.required_coverage"
    )
    if coverage == "NONE":
        raise CalculationContractRegistryError(
            f"{contract_id}.{alias} must state a real coverage requirement, not NONE"
        )

    data_class = _nonempty_text(item.get("data_class"), f"{contract_id}.{alias}.data_class")
    if data_class == "ANY":
        raise CalculationContractRegistryError(
            f"{contract_id}.{alias} data_class may not be unconstrained"
        )


def _validate_formula(contract_id: str, spec: dict[str, Any], aliases: set[str]) -> None:
    operation = _nonempty_text(spec.get("operation"), f"{contract_id}.operation")
    if operation not in _ALLOWED_OPERATIONS:
        raise CalculationContractRegistryError(
            f"{contract_id} uses unsupported operation {operation!r}"
        )

    formula = _mapping(spec.get("formula"), f"{contract_id}.formula")
    formula_operation = _nonempty_text(
        formula.get("operation"), f"{contract_id}.formula.operation"
    )
    if formula_operation != operation:
        raise CalculationContractRegistryError(
            f"{contract_id} formula operation must match contract operation"
        )

    referenced: list[str]
    if operation in {"RATIO", "RATIO_PERCENT"}:
        numerator = _nonempty_text(
            formula.get("numerator_input"), f"{contract_id}.formula.numerator_input"
        )
        denominator = _nonempty_text(
            formula.get("denominator_input"), f"{contract_id}.formula.denominator_input"
        )
        referenced = [numerator, denominator]
        zero_behavior = _nonempty_text(
            spec.get("zero_denominator_behavior"),
            f"{contract_id}.zero_denominator_behavior",
        )
        if zero_behavior not in _ALLOWED_ZERO_DENOMINATOR:
            raise CalculationContractRegistryError(
                f"{contract_id} has unsupported zero denominator behavior {zero_behavior!r}"
            )
    elif operation == "DIFFERENCE":
        referenced = [
            _nonempty_text(formula.get("minuend_input"), f"{contract_id}.formula.minuend_input"),
            _nonempty_text(formula.get("subtrahend_input"), f"{contract_id}.formula.subtrahend_input"),
        ]
        if spec.get("zero_denominator_behavior") not in (None, ""):
            raise CalculationContractRegistryError(
                f"{contract_id} zero denominator behavior is only valid for ratios"
            )
    else:
        members = formula.get("member_inputs")
        if not isinstance(members, list) or len(members) < 2:
            raise CalculationContractRegistryError(
                f"{contract_id}.formula.member_inputs must contain at least two aliases"
            )
        referenced = [_nonempty_text(value, f"{contract_id}.formula.member_inputs") for value in members]
        if spec.get("zero_denominator_behavior") not in (None, ""):
            raise CalculationContractRegistryError(
                f"{contract_id} zero denominator behavior is only valid for ratios"
            )

    unknown = set(referenced) - aliases
    if unknown:
        raise CalculationContractRegistryError(
            f"{contract_id} formula references unregistered inputs {sorted(unknown)}"
        )


def validate_calculation_registry(registry: dict[str, Any] | None = None) -> None:
    """Validate a full registry and fail closed on underspecified arithmetic."""
    data = registry if registry is not None else CALCULATION_REGISTRY
    root = _mapping(data, "calculation registry")
    if root.get("version") != REGISTRY_VERSION:
        raise CalculationContractRegistryError("unexpected calculation registry version")
    if root.get("contract_version") != CONTRACT_VERSION:
        raise CalculationContractRegistryError("unexpected calculation contract version")

    policy = _mapping(root.get("policy"), "calculation registry policy")
    required_true = {
        "fail_closed",
        "client_authored_formulas_forbidden",
        "require_registered_inputs",
        "implicit_currency_conversion_forbidden",
        "coverage_requirement_must_be_explicit_per_input",
        "zero_denominator_behavior_must_be_explicit_for_ratios",
        "provenance_required",
    }
    missing_true = sorted(key for key in required_true if policy.get(key) is not True)
    if missing_true:
        raise CalculationContractRegistryError(
            f"calculation registry safety policy is not enabled: {missing_true}"
        )
    if policy.get("currency_alignment") != "EXPLICIT_ONLY":
        raise CalculationContractRegistryError("currency alignment must remain EXPLICIT_ONLY")
    if set(policy.get("require_scope_alignment") or []) != _REQUIRED_SCOPE_ALIGNMENT:
        raise CalculationContractRegistryError(
            "scope alignment must require marketplace, seller, date_from and date_to"
        )

    contracts = _mapping(root.get("contracts"), "calculation contracts")
    for contract_id, raw in contracts.items():
        spec = _mapping(raw, f"calculation contract {contract_id}")
        if spec.get("contract_id") != contract_id:
            raise CalculationContractRegistryError(
                f"{contract_id} contract_id must match its registry key"
            )
        if spec.get("contract_version") != CONTRACT_VERSION:
            raise CalculationContractRegistryError(
                f"{contract_id} has an unsupported contract version"
            )
        if spec.get("status") != APPROVED_STATUS:
            raise CalculationContractRegistryError(
                f"{contract_id} must be explicitly {APPROVED_STATUS} before registration"
            )
        _nonempty_text(spec.get("business_meaning"), f"{contract_id}.business_meaning")
        marketplace = _nonempty_text(spec.get("marketplace"), f"{contract_id}.marketplace")
        if marketplace not in _ALLOWED_MARKETPLACES:
            raise CalculationContractRegistryError(
                f"{contract_id} marketplace {marketplace!r} is not approved"
            )

        scope = spec.get("required_scope_alignment")
        if set(scope or []) != _REQUIRED_SCOPE_ALIGNMENT:
            raise CalculationContractRegistryError(
                f"{contract_id} must align marketplace, seller, date_from and date_to"
            )

        currency_policy = _nonempty_text(
            spec.get("currency_policy"), f"{contract_id}.currency_policy"
        )
        if currency_policy not in _ALLOWED_CURRENCY_POLICIES:
            raise CalculationContractRegistryError(
                f"{contract_id} currency policy {currency_policy!r} is unsafe or unsupported"
            )

        output = _mapping(spec.get("output"), f"{contract_id}.output")
        _nonempty_text(output.get("metric_id"), f"{contract_id}.output.metric_id")
        _nonempty_text(output.get("unit"), f"{contract_id}.output.unit")
        _nonempty_text(output.get("data_class"), f"{contract_id}.output.data_class")

        inputs = spec.get("inputs")
        if not isinstance(inputs, list) or len(inputs) < 2:
            raise CalculationContractRegistryError(
                f"{contract_id}.inputs must contain at least two registered inputs"
            )
        aliases: set[str] = set()
        for item in inputs:
            _validate_input(item, contract_id=contract_id, aliases=aliases)
        _validate_formula(contract_id, spec, aliases)

        if spec.get("provenance_required") is not True:
            raise CalculationContractRegistryError(
                f"{contract_id} must require provenance"
            )


def load_calculation_registry() -> dict[str, Any]:
    validate_calculation_registry(CALCULATION_REGISTRY)
    return deepcopy(CALCULATION_REGISTRY)


def registered_calculation_ids() -> list[str]:
    registry = load_calculation_registry()
    return sorted(str(key) for key in registry["contracts"])


def get_calculation_contract(calculation_id: str) -> dict[str, Any] | None:
    key = str(calculation_id or "").strip()
    if not key:
        return None
    registry = load_calculation_registry()
    value = registry["contracts"].get(key)
    return deepcopy(value) if isinstance(value, dict) else None


def calculation_registry_summary() -> dict[str, Any]:
    registry = load_calculation_registry()
    return {
        "registry_version": registry["version"],
        "contract_version": registry["contract_version"],
        "registered_calculation_ids": sorted(registry["contracts"]),
        "registered_calculation_count": len(registry["contracts"]),
        "arithmetic_enabled": bool(registry["contracts"]),
        "policy": deepcopy(registry["policy"]),
    }
