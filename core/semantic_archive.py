"""Coverage-gated execution for approved Semantic Core archive calculations."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
import re
from typing import Any

import yaml

from .archive_tools import _query_year
from .business_registry import resolve_business_cabinet
from .semantic_registry import get_dataset, load_semantic_registry
from .semantic_resolver import resolve_semantic_question
from .wb_finance_archive import ARCHIVE_CABINETS, DATASET, parse_csv_bytes


EXECUTION_PATH = Path(__file__).with_name("semantic_execution.yaml")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SemanticArchiveExecutionError(RuntimeError):
    """Raised when an archive execution contract is invalid or unsafe."""


def _parse_day(value: str | date, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise SemanticArchiveExecutionError(
            f"{field} must be an ISO calendar date (YYYY-MM-DD)"
        ) from exc


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticArchiveExecutionError(f"{name} must be a mapping")
    return value


def load_semantic_execution(path: str | Path | None = None) -> dict[str, Any]:
    execution_path = Path(path) if path is not None else EXECUTION_PATH
    with execution_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    execution = _require_mapping(raw, "semantic execution registry")
    validate_semantic_execution(execution)
    return execution


def validate_semantic_execution(
    execution: dict[str, Any],
    registry: dict[str, Any] | None = None,
) -> None:
    registry_data = registry if registry is not None else load_semantic_registry()
    policy = _require_mapping(execution.get("policy"), "semantic execution policy")
    if policy.get("fail_closed") is not True:
        raise SemanticArchiveExecutionError("semantic archive execution must fail closed")
    if policy.get("require_full_coverage") is not True:
        raise SemanticArchiveExecutionError("semantic archive execution must require FULL_COVERAGE")
    if policy.get("cross_currency_sum_forbidden") is not True:
        raise SemanticArchiveExecutionError("cross-currency aggregation must remain forbidden")

    dataset = get_dataset(DATASET, registry_data)
    dataset_fields = set(dataset["fields"])
    field_catalog = dataset["field_catalog"]
    executors = _require_mapping(execution.get("executors"), "semantic executors")
    allowed_modes = {
        "sum_by_currency",
        "grouped_reason_amount",
        "sale_return_summary",
        "component_breakdown",
        "sale_return_component_summary",
        "distinct_observations",
    }

    for capability_id, value in executors.items():
        spec = _require_mapping(value, f"semantic executor {capability_id}")
        capability = registry_data.get("capabilities", {}).get(capability_id)
        if not isinstance(capability, dict):
            raise SemanticArchiveExecutionError(
                f"semantic executor {capability_id} references an unknown capability"
            )
        if spec.get("status") != "APPROVED_WITH_FULL_COVERAGE":
            raise SemanticArchiveExecutionError(
                f"semantic executor {capability_id} is not explicitly approved"
            )
        if spec.get("dataset_id") != DATASET or spec.get("source_id") != DATASET:
            raise SemanticArchiveExecutionError(
                f"semantic executor {capability_id} must use {DATASET}"
            )
        mode = spec.get("mode")
        if mode not in allowed_modes:
            raise SemanticArchiveExecutionError(
                f"semantic executor {capability_id} has an unsupported mode"
            )

        required_fields = ["date_field"]
        if mode != "distinct_observations":
            required_fields.extend(["amount_field", "currency_field"])
        for key in required_fields:
            field = spec.get(key)
            if field not in dataset_fields:
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} references unknown field {field!r}"
                )

        if spec["date_field"] not in capability["fields"]:
            date_meta = field_catalog.get(spec["date_field"]) or {}
            if (
                mode
                not in {
                    "component_breakdown",
                    "sale_return_component_summary",
                    "distinct_observations",
                }
                or date_meta.get("role") != "date"
            ):
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} date field is outside capability semantics"
                )
        if mode != "distinct_observations" and spec["amount_field"] not in capability["fields"]:
            raise SemanticArchiveExecutionError(
                f"semantic executor {capability_id} amount field is outside capability semantics"
            )

        product_filter_field = spec.get("product_filter_field")
        if product_filter_field and product_filter_field not in dataset_fields:
            raise SemanticArchiveExecutionError(
                f"semantic executor {capability_id} has an unknown product filter field"
            )

        if mode == "grouped_reason_amount":
            reason_fields = spec.get("reason_fields")
            if (
                not isinstance(reason_fields, list)
                or not reason_fields
                or any(field not in capability["fields"] for field in reason_fields)
            ):
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} has invalid reason fields"
                )

        if mode == "sale_return_summary":
            for key in ("operation_field", "unit_field"):
                field = spec.get(key)
                if field not in dataset_fields or field not in capability["fields"]:
                    raise SemanticArchiveExecutionError(
                        f"semantic executor {capability_id} has invalid {key} {field!r}"
                    )
            for key in ("sale_value", "return_value"):
                value = spec.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise SemanticArchiveExecutionError(
                        f"semantic executor {capability_id} must define {key}"
                    )

        if mode == "component_breakdown":
            if "sellerOperName" not in capability["fields"]:
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} requires sellerOperName semantics"
                )
            reason_fields = spec.get("reason_fields") or []
            if not isinstance(reason_fields, list) or any(
                field not in capability["fields"] for field in reason_fields
            ):
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} has invalid reason fields"
                )
            components = _require_mapping(
                spec.get("components"), f"semantic executor {capability_id} components"
            )
            if not components:
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} must define components"
                )
            for alias, field in components.items():
                if not _IDENTIFIER.fullmatch(str(alias)):
                    raise SemanticArchiveExecutionError(
                        f"semantic executor {capability_id} has unsafe component alias {alias!r}"
                    )
                if field not in dataset_fields or field not in capability["fields"]:
                    raise SemanticArchiveExecutionError(
                        f"semantic executor {capability_id} has invalid component field {field!r}"
                    )
            count_components = spec.get("count_components") or {}
            if not isinstance(count_components, dict):
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} count_components must be a mapping"
                )
            for alias, field in count_components.items():
                if not _IDENTIFIER.fullmatch(str(alias)):
                    raise SemanticArchiveExecutionError(
                        f"semantic executor {capability_id} has unsafe count alias {alias!r}"
                    )
                if field not in dataset_fields or field not in capability["fields"]:
                    raise SemanticArchiveExecutionError(
                        f"semantic executor {capability_id} has invalid count field {field!r}"
                    )

        if mode == "sale_return_component_summary":
            operation_field = spec.get("operation_field")
            operation_meta = field_catalog.get(operation_field) or {}
            if operation_field not in dataset_fields or operation_meta.get("role") != "operation":
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} has invalid operation field {operation_field!r}"
                )
            for key in ("sale_value", "return_value"):
                operation_value = spec.get(key)
                if not isinstance(operation_value, str) or not operation_value.strip():
                    raise SemanticArchiveExecutionError(
                        f"semantic executor {capability_id} must define {key}"
                    )
            components = _require_mapping(
                spec.get("components"), f"semantic executor {capability_id} components"
            )
            if not components:
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} must define components"
                )
            for alias, field in components.items():
                if not _IDENTIFIER.fullmatch(str(alias)):
                    raise SemanticArchiveExecutionError(
                        f"semantic executor {capability_id} has unsafe component alias {alias!r}"
                    )
                if field not in dataset_fields or field not in capability["fields"]:
                    raise SemanticArchiveExecutionError(
                        f"semantic executor {capability_id} has invalid component field {field!r}"
                    )
            breakdown_fields = spec.get("breakdown_fields") or []
            if not isinstance(breakdown_fields, list) or any(
                field not in dataset_fields or field not in capability["fields"]
                for field in breakdown_fields
            ):
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} has invalid breakdown fields"
                )
            combined = spec.get("combined_components") or {}
            if not isinstance(combined, dict):
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} combined_components must be a mapping"
                )
            for alias, members in combined.items():
                if not _IDENTIFIER.fullmatch(str(alias)):
                    raise SemanticArchiveExecutionError(
                        f"semantic executor {capability_id} has unsafe combined alias {alias!r}"
                    )
                if (
                    not isinstance(members, list)
                    or not members
                    or any(member not in components for member in members)
                ):
                    raise SemanticArchiveExecutionError(
                        f"semantic executor {capability_id} has invalid combined component {alias!r}"
                    )

        if mode == "distinct_observations":
            primary_dimension = spec.get("primary_dimension")
            if primary_dimension not in dataset_fields or primary_dimension not in capability["fields"]:
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} has invalid primary dimension {primary_dimension!r}"
                )
            primary_meta = field_catalog.get(primary_dimension) or {}
            if primary_meta.get("role") not in {"dimension", "identifier", "flag"}:
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} primary dimension has unsafe role"
                )
            context_dimensions = spec.get("context_dimensions") or []
            if not isinstance(context_dimensions, list) or any(
                field not in dataset_fields or field not in capability["fields"]
                for field in context_dimensions
            ):
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} has invalid context dimensions"
                )
            for field in context_dimensions:
                meta = field_catalog.get(field) or {}
                if meta.get("role") not in {"dimension", "identifier", "flag"}:
                    raise SemanticArchiveExecutionError(
                        f"semantic executor {capability_id} context dimension {field!r} has unsafe role"
                    )
            data_class = spec.get("data_class")
            if not isinstance(data_class, str) or not data_class.strip():
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} must define historical data_class"
                )


def _resolve_archive_cabinet(seller: str) -> str:
    value = str(seller).strip()
    business = resolve_business_cabinet("wb", value)
    cabinet = business.cabinet if business else value
    if cabinet not in ARCHIVE_CABINETS:
        raise SemanticArchiveExecutionError(
            f"Seller {seller!r} is not a supported canonical WB archive cabinet"
        )
    return cabinet


def evaluate_registry_coverage(
    registry_data: bytes | None,
    *,
    cabinet: str,
    date_from: str | date,
    date_to: str | date,
) -> dict[str, Any]:
    """Return exact coverage for one cabinet from COMPLETE registry fragments.

    Coverage is based on provider report ``date_from``/``date_to`` intervals.
    Split fragments are merged, so month/year boundary fragments can jointly
    provide one continuous covered interval.
    """
    start = _parse_day(date_from, "date_from")
    end = _parse_day(date_to, "date_to")
    if start > end:
        raise SemanticArchiveExecutionError("date_from must be <= date_to")

    _, rows = parse_csv_bytes(registry_data or b"")
    intervals: list[tuple[date, date, int | None]] = []
    matching_reports = 0
    for row in rows:
        if (
            row.get("marketplace") != "wb"
            or row.get("cabinet") != cabinet
            or row.get("dataset") != DATASET
            or row.get("status") != "COMPLETE"
        ):
            continue
        try:
            row_start = date.fromisoformat(str(row.get("date_from") or "")[:10])
            row_end = date.fromisoformat(str(row.get("date_to") or "")[:10])
        except ValueError:
            continue
        if row_start > row_end or row_end < start or row_start > end:
            continue
        try:
            archive_year = int(row.get("year") or 0) or None
        except (TypeError, ValueError):
            archive_year = None
        intervals.append((max(start, row_start), min(end, row_end), archive_year))
        matching_reports += 1

    if not intervals:
        return {
            "status": "NO_COVERAGE",
            "cabinet": cabinet,
            "dataset": DATASET,
            "requested_from": start.isoformat(),
            "requested_to": end.isoformat(),
            "covered_intervals": [],
            "gaps": [[start.isoformat(), end.isoformat()]],
            "matching_complete_reports": 0,
            "archive_years": [],
        }

    intervals.sort(key=lambda item: (item[0], item[1]))
    merged: list[list[date]] = []
    archive_years: set[int] = set()
    for interval_start, interval_end, archive_year in intervals:
        if archive_year is not None:
            archive_years.add(archive_year)
        if not merged or interval_start > merged[-1][1] + timedelta(days=1):
            merged.append([interval_start, interval_end])
        elif interval_end > merged[-1][1]:
            merged[-1][1] = interval_end

    gaps: list[list[str]] = []
    cursor = start
    for interval_start, interval_end in merged:
        if interval_start > cursor:
            gaps.append([
                cursor.isoformat(),
                min(end, interval_start - timedelta(days=1)).isoformat(),
            ])
        cursor = max(cursor, interval_end + timedelta(days=1))
        if cursor > end:
            break
    if cursor <= end:
        gaps.append([cursor.isoformat(), end.isoformat()])

    status = "FULL_COVERAGE" if not gaps else "PARTIAL_COVERAGE"
    return {
        "status": status,
        "cabinet": cabinet,
        "dataset": DATASET,
        "requested_from": start.isoformat(),
        "requested_to": end.isoformat(),
        "covered_intervals": [
            [interval_start.isoformat(), interval_end.isoformat()]
            for interval_start, interval_end in merged
        ],
        "gaps": gaps,
        "matching_complete_reports": matching_reports,
        "archive_years": sorted(archive_years),
    }


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise SemanticArchiveExecutionError(f"Unsafe SQL identifier {value!r}")
    return f'"{value}"'


def _quote_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _numeric_expr(field: str) -> str:
    identifier = _quote_identifier(field)
    return f"COALESCE(TRY_CAST(NULLIF(TRIM({identifier}), '') AS DOUBLE), 0.0)"


def _date_expr(field: str) -> str:
    identifier = _quote_identifier(field)
    return f"TRY_CAST(SUBSTR({identifier}, 1, 10) AS DATE)"


def _nm_filter(field: str | None, nm_ids: list[int] | None) -> str:
    if not nm_ids:
        return ""
    if not field:
        raise SemanticArchiveExecutionError("This semantic executor does not support product filtering")
    values = sorted({int(value) for value in nm_ids})
    if any(value <= 0 for value in values):
        raise SemanticArchiveExecutionError("nm_ids must contain positive integers")
    sql_values = ", ".join(str(value) for value in values)
    return (
        " AND TRY_CAST(NULLIF(TRIM("
        + _quote_identifier(field)
        + f"), '') AS BIGINT) IN ({sql_values})"
    )


def _reason_expr(reason_fields: list[str]) -> str:
    fallback = "'Не указано'"
    for field in reversed(reason_fields):
        fallback = (
            "COALESCE(NULLIF(TRIM("
            + _quote_identifier(field)
            + "), ''), "
            + fallback
            + ")"
        )
    return fallback


def _dimension_expr(field: str) -> str:
    return (
        "COALESCE(NULLIF(TRIM("
        + _quote_identifier(field)
        + "), ''), 'Не указано')"
    )


def build_semantic_archive_sql(
    *,
    cabinet: str,
    executor: dict[str, Any],
    date_from: str | date,
    date_to: str | date,
    nm_ids: list[int] | None = None,
) -> str:
    start = _parse_day(date_from, "date_from")
    end = _parse_day(date_to, "date_to")
    if start > end:
        raise SemanticArchiveExecutionError("date_from must be <= date_to")
    if cabinet not in ARCHIVE_CABINETS:
        raise SemanticArchiveExecutionError("Unsafe or unsupported archive cabinet")

    table = _quote_identifier(cabinet)
    mode = str(executor["mode"])
    date_field = str(executor["date_field"])
    date_value = _date_expr(date_field)
    where = (
        f"{date_value} BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'"
        + _nm_filter(executor.get("product_filter_field"), nm_ids)
    )

    if mode == "distinct_observations":
        primary_field = str(executor["primary_dimension"])
        primary_raw = "NULLIF(TRIM(" + _quote_identifier(primary_field) + "), '')"
        context_fields = [str(field) for field in (executor.get("context_dimensions") or [])]
        select_parts = [f"{primary_raw} AS observation"]
        for field in context_fields:
            select_parts.append(
                f"{_dimension_expr(field)} AS {_quote_identifier(field)}"
            )
        select_parts.extend(
            [
                "COUNT(*) AS observation_rows",
                f"MIN({date_value}) AS first_observed_date",
                f"MAX({date_value}) AS last_observed_date",
            ]
        )
        group_count = 1 + len(context_fields)
        group_sql = ", ".join(str(index) for index in range(1, group_count + 1))
        return (
            "SELECT "
            + ", ".join(select_parts)
            + f" FROM {table} WHERE {where} AND {primary_raw} IS NOT NULL "
            + f"GROUP BY {group_sql} ORDER BY observation_rows DESC, {group_sql}"
        )

    amount_field = str(executor["amount_field"])
    currency_field = str(executor["currency_field"])
    amount = _numeric_expr(amount_field)
    currency = (
        "COALESCE(NULLIF(TRIM(" + _quote_identifier(currency_field) + "), ''), 'UNKNOWN')"
    )

    if mode == "sum_by_currency":
        return (
            f"SELECT {currency} AS currency, SUM({amount}) AS amount, "
            f"SUM(CASE WHEN {amount} <> 0 THEN 1 ELSE 0 END) AS operation_rows "
            f"FROM {table} WHERE {where} GROUP BY 1 ORDER BY 1"
        )

    if mode == "grouped_reason_amount":
        reason_fields = [str(value) for value in executor["reason_fields"]]
        reason = _reason_expr(reason_fields)
        operation = (
            "COALESCE(NULLIF(TRIM("
            + _quote_identifier("sellerOperName")
            + "), ''), 'Не указано')"
        )
        return (
            f"SELECT {currency} AS currency, {reason} AS reason, "
            f"{operation} AS operation, SUM({amount}) AS amount, COUNT(*) AS operation_rows "
            f"FROM {table} WHERE {where} AND {amount} <> 0 "
            "GROUP BY 1, 2, 3 ORDER BY ABS(SUM("
            + amount
            + ")) DESC, 1, 2, 3"
        )

    if mode == "sale_return_summary":
        operation = "TRIM(" + _quote_identifier(str(executor["operation_field"])) + ")"
        units = _numeric_expr(str(executor["unit_field"]))
        sale_value = _quote_literal(str(executor["sale_value"]))
        return_value = _quote_literal(str(executor["return_value"]))
        return (
            f"SELECT {currency} AS currency, "
            f"SUM(CASE WHEN {operation} = {sale_value} THEN {amount} ELSE 0 END) AS sale_amount, "
            f"SUM(CASE WHEN {operation} = {return_value} THEN {amount} ELSE 0 END) AS return_amount, "
            f"SUM(CASE WHEN {operation} = {sale_value} THEN {units} ELSE 0 END) AS sale_units, "
            f"SUM(CASE WHEN {operation} = {return_value} THEN {units} ELSE 0 END) AS return_units, "
            f"SUM(CASE WHEN {operation} = {sale_value} THEN 1 ELSE 0 END) AS sale_rows, "
            f"SUM(CASE WHEN {operation} = {return_value} THEN 1 ELSE 0 END) AS return_rows "
            f"FROM {table} WHERE {where} AND {operation} IN ({sale_value}, {return_value}) "
            "GROUP BY 1 ORDER BY 1"
        )

    if mode == "component_breakdown":
        reason_fields = [str(value) for value in (executor.get("reason_fields") or [])]
        reason = _reason_expr(reason_fields)
        operation = (
            "COALESCE(NULLIF(TRIM("
            + _quote_identifier("sellerOperName")
            + "), ''), 'Не указано')"
        )
        component_selects: list[str] = []
        nonzero_conditions: list[str] = []
        for alias, field in executor["components"].items():
            value = _numeric_expr(str(field))
            component_selects.append(
                f"SUM({value}) AS {_quote_identifier(str(alias))}"
            )
            nonzero_conditions.append(f"{value} <> 0")
        for alias, field in (executor.get("count_components") or {}).items():
            value = _numeric_expr(str(field))
            component_selects.append(
                f"SUM({value}) AS {_quote_identifier(str(alias))}"
            )
            nonzero_conditions.append(f"{value} <> 0")
        component_selects.append("COUNT(*) AS operation_rows")
        select_sql = ", ".join(component_selects)
        nonzero_sql = " OR ".join(nonzero_conditions)
        return (
            f"SELECT {currency} AS currency, {reason} AS reason, {operation} AS operation, "
            f"{select_sql} FROM {table} WHERE {where} AND ({nonzero_sql}) "
            "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        )

    if mode == "sale_return_component_summary":
        operation = "TRIM(" + _quote_identifier(str(executor["operation_field"])) + ")"
        sale_value = _quote_literal(str(executor["sale_value"]))
        return_value = _quote_literal(str(executor["return_value"]))
        breakdown_fields = [str(field) for field in (executor.get("breakdown_fields") or [])]
        dimensions = [_dimension_expr(field) for field in breakdown_fields]
        select_parts = [f"{currency} AS currency"]
        for field, expr in zip(breakdown_fields, dimensions):
            select_parts.append(f"{expr} AS {_quote_identifier(field)}")
        nonzero_conditions: list[str] = []
        for alias, field in executor["components"].items():
            value = _numeric_expr(str(field))
            alias_name = str(alias)
            select_parts.append(
                f"SUM(CASE WHEN {operation} = {sale_value} THEN {value} ELSE 0 END) "
                f"AS {_quote_identifier('sale_' + alias_name)}"
            )
            select_parts.append(
                f"SUM(CASE WHEN {operation} = {return_value} THEN {value} ELSE 0 END) "
                f"AS {_quote_identifier('return_' + alias_name)}"
            )
            nonzero_conditions.append(f"{value} <> 0")
        select_parts.append(
            f"SUM(CASE WHEN {operation} = {sale_value} THEN 1 ELSE 0 END) AS sale_rows"
        )
        select_parts.append(
            f"SUM(CASE WHEN {operation} = {return_value} THEN 1 ELSE 0 END) AS return_rows"
        )
        group_count = 1 + len(breakdown_fields)
        group_sql = ", ".join(str(index) for index in range(1, group_count + 1))
        order_sql = group_sql
        nonzero_sql = " OR ".join(nonzero_conditions)
        return (
            "SELECT " + ", ".join(select_parts)
            + f" FROM {table} WHERE {where} "
            + f"AND {operation} IN ({sale_value}, {return_value}) "
            + f"AND ({nonzero_sql}) GROUP BY {group_sql} ORDER BY {order_sql}"
        )

    raise SemanticArchiveExecutionError(
        f"Unsupported semantic execution mode {executor.get('mode')!r}"
    )


async def _load_registry_bytes(store: Any) -> bytes | None:
    folder = await store.ensure_folder_path(["app", "registry"])
    _, data = await store.download_named(folder, "reports_registry.csv")
    return data


async def _missing_annual_files(
    store: Any,
    *,
    cabinet: str,
    archive_years: list[int],
) -> list[int]:
    missing: list[int] = []
    for archive_year in archive_years:
        folder = await store.ensure_folder_path(
            ["База данных", "WB", cabinet, str(archive_year), "finance", "weekly", "main"]
        )
        name = f"{cabinet}__weekly_main__{archive_year}.csv"
        item, data = await store.download_named(folder, name)
        if item is None or data is None:
            missing.append(archive_year)
    return missing


def _rows_as_dicts(result: dict[str, Any]) -> list[dict[str, Any]]:
    columns = result.get("columns") or []
    rows = result.get("rows") or []
    return [dict(zip(columns, row)) for row in rows]


def _aggregate_execution_results(
    *,
    mode: str,
    year_results: list[dict[str, Any]],
    executor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if mode == "sum_by_currency":
        totals: dict[str, float] = defaultdict(float)
        operation_rows: dict[str, int] = defaultdict(int)
        for result in year_results:
            for row in _rows_as_dicts(result):
                currency = str(row.get("currency") or "UNKNOWN")
                totals[currency] += float(row.get("amount") or 0.0)
                operation_rows[currency] += int(row.get("operation_rows") or 0)
        return {
            "totals_by_currency": [
                {
                    "currency": currency,
                    "amount": round(totals[currency], 6),
                    "operation_rows": operation_rows[currency],
                }
                for currency in sorted(totals)
            ],
            "cross_currency_total": None,
        }

    if mode == "grouped_reason_amount":
        grouped: dict[tuple[str, str, str], list[float | int]] = {}
        for result in year_results:
            for row in _rows_as_dicts(result):
                key = (
                    str(row.get("currency") or "UNKNOWN"),
                    str(row.get("reason") or "Не указано"),
                    str(row.get("operation") or "Не указано"),
                )
                bucket = grouped.setdefault(key, [0.0, 0])
                bucket[0] = float(bucket[0]) + float(row.get("amount") or 0.0)
                bucket[1] = int(bucket[1]) + int(row.get("operation_rows") or 0)
        totals: dict[str, float] = defaultdict(float)
        breakdown: list[dict[str, Any]] = []
        for (currency, reason, operation), (amount, rows) in grouped.items():
            totals[currency] += float(amount)
            breakdown.append({
                "currency": currency,
                "reason": reason,
                "operation": operation,
                "amount": round(float(amount), 6),
                "operation_rows": int(rows),
            })
        breakdown.sort(key=lambda item: (-abs(float(item["amount"])), item["currency"], item["reason"]))
        return {
            "totals_by_currency": [
                {"currency": currency, "amount": round(amount, 6)}
                for currency, amount in sorted(totals.items())
            ],
            "breakdown": breakdown,
            "cross_currency_total": None,
        }

    if mode == "sale_return_summary":
        totals: dict[str, dict[str, float | int]] = {}
        for result in year_results:
            for row in _rows_as_dicts(result):
                currency = str(row.get("currency") or "UNKNOWN")
                bucket = totals.setdefault(
                    currency,
                    {
                        "sale_amount": 0.0,
                        "return_amount": 0.0,
                        "sale_units": 0.0,
                        "return_units": 0.0,
                        "sale_rows": 0,
                        "return_rows": 0,
                    },
                )
                for key in ("sale_amount", "return_amount", "sale_units", "return_units"):
                    bucket[key] = float(bucket[key]) + float(row.get(key) or 0.0)
                for key in ("sale_rows", "return_rows"):
                    bucket[key] = int(bucket[key]) + int(row.get(key) or 0)

        by_currency: list[dict[str, Any]] = []
        for currency in sorted(totals):
            bucket = totals[currency]
            sale_amount = float(bucket["sale_amount"])
            return_amount = float(bucket["return_amount"])
            sale_units = float(bucket["sale_units"])
            return_units = float(bucket["return_units"])
            by_currency.append(
                {
                    "currency": currency,
                    "sale_amount": round(sale_amount, 6),
                    "return_amount": round(return_amount, 6),
                    "net_sales_amount": round(sale_amount - return_amount, 6),
                    "sale_units": round(sale_units, 6),
                    "return_units": round(return_units, 6),
                    "net_sales_units": round(sale_units - return_units, 6),
                    "sale_rows": int(bucket["sale_rows"]),
                    "return_rows": int(bucket["return_rows"]),
                }
            )
        return {
            "sales_and_returns_by_currency": by_currency,
            "cross_currency_total": None,
        }

    if mode == "component_breakdown":
        if not isinstance(executor, dict):
            raise SemanticArchiveExecutionError(
                "component_breakdown aggregation requires executor metadata"
            )
        component_keys = [str(key) for key in executor["components"]]
        count_keys = [str(key) for key in (executor.get("count_components") or {})]
        all_keys = component_keys + count_keys
        grouped: dict[tuple[str, str, str], dict[str, float | int]] = {}
        totals: dict[str, dict[str, float | int]] = {}
        for result in year_results:
            for row in _rows_as_dicts(result):
                currency = str(row.get("currency") or "UNKNOWN")
                reason = str(row.get("reason") or "Не указано")
                operation = str(row.get("operation") or "Не указано")
                total_bucket = totals.setdefault(
                    currency,
                    {**{key: 0.0 for key in all_keys}, "operation_rows": 0},
                )
                group_bucket = grouped.setdefault(
                    (currency, reason, operation),
                    {**{key: 0.0 for key in all_keys}, "operation_rows": 0},
                )
                for key in all_keys:
                    value = float(row.get(key) or 0.0)
                    total_bucket[key] = float(total_bucket[key]) + value
                    group_bucket[key] = float(group_bucket[key]) + value
                rows = int(row.get("operation_rows") or 0)
                total_bucket["operation_rows"] = int(total_bucket["operation_rows"]) + rows
                group_bucket["operation_rows"] = int(group_bucket["operation_rows"]) + rows

        by_currency: list[dict[str, Any]] = []
        for currency in sorted(totals):
            bucket = totals[currency]
            item: dict[str, Any] = {"currency": currency}
            for key in all_keys:
                item[key] = round(float(bucket[key]), 6)
            item["operation_rows"] = int(bucket["operation_rows"])
            by_currency.append(item)

        breakdown: list[dict[str, Any]] = []
        for (currency, reason, operation), bucket in grouped.items():
            item = {
                "currency": currency,
                "reason": reason,
                "operation": operation,
            }
            for key in all_keys:
                item[key] = round(float(bucket[key]), 6)
            item["operation_rows"] = int(bucket["operation_rows"])
            breakdown.append(item)
        breakdown.sort(key=lambda item: (item["currency"], item["operation"], item["reason"]))
        return {
            "components_by_currency": by_currency,
            "breakdown": breakdown,
            "cross_currency_total": None,
            "cross_component_total": None,
        }

    if mode == "sale_return_component_summary":
        if not isinstance(executor, dict):
            raise SemanticArchiveExecutionError(
                "sale_return_component_summary aggregation requires executor metadata"
            )
        component_keys = [str(key) for key in executor["components"]]
        breakdown_fields = [str(field) for field in (executor.get("breakdown_fields") or [])]
        combined_components = executor.get("combined_components") or {}
        totals: dict[str, dict[str, float | int]] = {}
        grouped: dict[tuple[str, ...], dict[str, float | int]] = {}

        def _empty_bucket() -> dict[str, float | int]:
            values: dict[str, float | int] = {"sale_rows": 0, "return_rows": 0}
            for key in component_keys:
                values["sale_" + key] = 0.0
                values["return_" + key] = 0.0
            return values

        for result in year_results:
            for row in _rows_as_dicts(result):
                currency = str(row.get("currency") or "UNKNOWN")
                total_bucket = totals.setdefault(currency, _empty_bucket())
                group_key = tuple(
                    [currency]
                    + [str(row.get(field) or "Не указано") for field in breakdown_fields]
                )
                group_bucket = grouped.setdefault(group_key, _empty_bucket())
                for key in component_keys:
                    for prefix in ("sale_", "return_"):
                        field_name = prefix + key
                        value = float(row.get(field_name) or 0.0)
                        total_bucket[field_name] = float(total_bucket[field_name]) + value
                        group_bucket[field_name] = float(group_bucket[field_name]) + value
                for row_key in ("sale_rows", "return_rows"):
                    value = int(row.get(row_key) or 0)
                    total_bucket[row_key] = int(total_bucket[row_key]) + value
                    group_bucket[row_key] = int(group_bucket[row_key]) + value

        def _render_bucket(base: dict[str, Any], bucket: dict[str, float | int]) -> dict[str, Any]:
            item = dict(base)
            for key in component_keys:
                sale_amount = float(bucket["sale_" + key])
                return_amount = float(bucket["return_" + key])
                item["sale_" + key] = round(sale_amount, 6)
                item["return_" + key] = round(return_amount, 6)
                item["net_" + key] = round(sale_amount - return_amount, 6)
            for alias, members in combined_components.items():
                sale_total = sum(float(bucket["sale_" + member]) for member in members)
                return_total = sum(float(bucket["return_" + member]) for member in members)
                item["sale_" + str(alias)] = round(sale_total, 6)
                item["return_" + str(alias)] = round(return_total, 6)
                item["net_" + str(alias)] = round(sale_total - return_total, 6)
            item["sale_rows"] = int(bucket["sale_rows"])
            item["return_rows"] = int(bucket["return_rows"])
            return item

        by_currency = [
            _render_bucket({"currency": currency}, totals[currency])
            for currency in sorted(totals)
        ]
        breakdown: list[dict[str, Any]] = []
        if breakdown_fields:
            for group_key, bucket in grouped.items():
                base: dict[str, Any] = {"currency": group_key[0]}
                for index, field in enumerate(breakdown_fields, start=1):
                    base[field] = group_key[index]
                breakdown.append(_render_bucket(base, bucket))
            breakdown.sort(
                key=lambda item: tuple(
                    [str(item["currency"])]
                    + [str(item.get(field) or "") for field in breakdown_fields]
                )
            )
        return {
            "components_by_currency": by_currency,
            "breakdown": breakdown,
            "cross_currency_total": None,
        }

    if mode == "distinct_observations":
        if not isinstance(executor, dict):
            raise SemanticArchiveExecutionError(
                "distinct_observations aggregation requires executor metadata"
            )
        context_fields = [str(field) for field in (executor.get("context_dimensions") or [])]
        grouped: dict[tuple[str, ...], dict[str, Any]] = {}
        distinct_values: set[str] = set()
        for result in year_results:
            for row in _rows_as_dicts(result):
                observation = str(row.get("observation") or "").strip()
                if not observation:
                    continue
                distinct_values.add(observation)
                context_values = [
                    str(row.get(field) or "Не указано") for field in context_fields
                ]
                key = tuple([observation] + context_values)
                first_seen = str(row.get("first_observed_date") or "")
                last_seen = str(row.get("last_observed_date") or "")
                bucket = grouped.setdefault(
                    key,
                    {
                        "observation_rows": 0,
                        "first_observed_date": first_seen,
                        "last_observed_date": last_seen,
                    },
                )
                bucket["observation_rows"] = int(bucket["observation_rows"]) + int(
                    row.get("observation_rows") or 0
                )
                if first_seen and (
                    not bucket["first_observed_date"]
                    or first_seen < str(bucket["first_observed_date"])
                ):
                    bucket["first_observed_date"] = first_seen
                if last_seen and (
                    not bucket["last_observed_date"]
                    or last_seen > str(bucket["last_observed_date"])
                ):
                    bucket["last_observed_date"] = last_seen

        observations: list[dict[str, Any]] = []
        for key, bucket in grouped.items():
            item: dict[str, Any] = {"value": key[0]}
            for index, field in enumerate(context_fields, start=1):
                item[field] = key[index]
            item["observation_rows"] = int(bucket["observation_rows"])
            item["first_observed_date"] = str(bucket["first_observed_date"])
            item["last_observed_date"] = str(bucket["last_observed_date"])
            observations.append(item)
        observations.sort(
            key=lambda item: (
                -int(item["observation_rows"]),
                str(item["value"]),
                *[str(item.get(field) or "") for field in context_fields],
            )
        )
        return {
            "distinct_values": sorted(distinct_values),
            "observations": observations,
            "historical_only": True,
            "current_configuration_confirmed": False,
        }

    raise SemanticArchiveExecutionError(f"Unsupported aggregation mode {mode!r}")


async def execute_semantic_archive_question(
    store: Any,
    *,
    question: str,
    seller: str,
    date_from: str | date,
    date_to: str | date,
    nm_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Resolve, coverage-check and execute one approved weekly-report calculation.

    Only capabilities explicitly registered in ``semantic_execution.yaml`` can
    reach archive SQL. Every other recognized capability remains knowledge-only.
    """
    start = _parse_day(date_from, "date_from")
    end = _parse_day(date_to, "date_to")
    if start > end:
        raise SemanticArchiveExecutionError("date_from must be <= date_to")

    resolution = resolve_semantic_question(question)
    if resolution.get("resolution_type") != "CAPABILITY":
        return {
            "ok": False,
            "error": "semantic_source_not_executable",
            "message": resolution.get("reason") or resolution.get("guardrail") or "The question is not executable from the current weekly archive.",
            "resolution": resolution,
            "execution_allowed": False,
        }

    capability_id = str(resolution.get("capability_id") or "")
    execution_registry = load_semantic_execution()
    executor = execution_registry["executors"].get(capability_id)
    if not isinstance(executor, dict):
        return {
            "ok": False,
            "error": "semantic_execution_not_approved",
            "message": (
                f"Capability {capability_id!r} is understood by the Semantic Core, "
                "but no archive calculation formula is approved for it yet."
            ),
            "resolution": resolution,
            "execution_allowed": False,
        }

    cabinet = _resolve_archive_cabinet(seller)
    registry_data = await _load_registry_bytes(store)
    coverage = evaluate_registry_coverage(
        registry_data,
        cabinet=cabinet,
        date_from=start,
        date_to=end,
    )
    if coverage["status"] != "FULL_COVERAGE":
        return {
            "ok": False,
            "error": "coverage_gap",
            "message": "The canonical weekly archive does not fully cover the requested period.",
            "coverage": coverage,
            "resolution": resolution,
            "execution_allowed": False,
        }

    archive_years = list(coverage.get("archive_years") or [])
    if not archive_years:
        return {
            "ok": False,
            "error": "coverage_metadata_conflict",
            "message": "FULL_COVERAGE was reported but no annual archive partition year was recorded.",
            "coverage": coverage,
            "resolution": resolution,
            "execution_allowed": False,
        }

    missing_years = await _missing_annual_files(
        store,
        cabinet=cabinet,
        archive_years=archive_years,
    )
    if missing_years:
        return {
            "ok": False,
            "error": "archive_file_missing",
            "message": "Registry coverage exists, but one or more canonical annual files are missing.",
            "missing_archive_years": missing_years,
            "coverage": coverage,
            "resolution": resolution,
            "execution_allowed": False,
        }

    year_results: list[dict[str, Any]] = []
    for archive_year in archive_years:
        sql = build_semantic_archive_sql(
            cabinet=cabinet,
            executor=executor,
            date_from=start,
            date_to=end,
            nm_ids=nm_ids,
        )
        result = await _query_year(store, int(archive_year), sql)
        if not result.get("ok"):
            return {
                "ok": False,
                "error": "archive_query_failed",
                "message": "The approved archive query could not be completed.",
                "archive_result": result,
                "coverage": coverage,
                "resolution": resolution,
                "execution_allowed": False,
            }
        year_results.append(result)

    calculated = _aggregate_execution_results(
        mode=str(executor["mode"]),
        year_results=year_results,
        executor=executor,
    )
    if executor["mode"] == "sale_return_summary":
        sum_rule = "sale_minus_return_by_doc_type"
    elif executor["mode"] == "component_breakdown":
        sum_rule = "separate_components_as_reported_no_cross_component_netting"
    elif executor["mode"] == "sale_return_component_summary":
        sum_rule = "sale_minus_return_by_doc_type_for_registered_components"
    elif executor["mode"] == "distinct_observations":
        sum_rule = "historical_distinct_observations_no_current_state_inference"
    else:
        sum_rule = "as_reported_no_sign_conversion"

    provenance: dict[str, Any] = {
        "date_field": executor["date_field"],
        "mode": executor["mode"],
        "semantics": executor.get("semantics"),
        "sum_rule": sum_rule,
        "product_filter": deepcopy(nm_ids) if nm_ids else None,
    }
    if executor.get("amount_field"):
        provenance["amount_field"] = executor["amount_field"]
    if executor.get("currency_field"):
        provenance["currency_field"] = executor["currency_field"]
        provenance["currency_rule"] = "never_sum_across_currencies"
    if executor["mode"] == "component_breakdown":
        provenance["component_fields"] = deepcopy(executor["components"])
        provenance["count_component_fields"] = deepcopy(
            executor.get("count_components") or {}
        )
    if executor["mode"] == "sale_return_component_summary":
        provenance["component_fields"] = deepcopy(executor["components"])
        provenance["combined_components"] = deepcopy(
            executor.get("combined_components") or {}
        )
        provenance["breakdown_fields"] = deepcopy(
            executor.get("breakdown_fields") or []
        )
        if executor.get("data_class"):
            provenance["data_class"] = str(executor["data_class"])
    if executor["mode"] == "distinct_observations":
        provenance["primary_dimension"] = str(executor["primary_dimension"])
        provenance["context_dimensions"] = deepcopy(
            executor.get("context_dimensions") or []
        )
        provenance["data_class"] = str(executor["data_class"])
        provenance["current_state_inference_forbidden"] = True

    return {
        "ok": True,
        "marketplace": "wb",
        "seller": cabinet,
        "dataset": DATASET,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "capability_id": capability_id,
        "semantic_status": resolution.get("status"),
        "execution_status": executor["status"],
        "execution_allowed": True,
        "coverage": coverage,
        "calculation": calculated,
        "provenance": provenance,
        "guardrail": resolution.get("guardrail"),
    }
