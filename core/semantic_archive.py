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
    executors = _require_mapping(execution.get("executors"), "semantic executors")
    allowed_modes = {"sum_by_currency", "grouped_reason_amount"}

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
        if spec.get("mode") not in allowed_modes:
            raise SemanticArchiveExecutionError(
                f"semantic executor {capability_id} has an unsupported mode"
            )
        for key in ("date_field", "amount_field", "currency_field"):
            field = spec.get(key)
            if field not in dataset_fields:
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} references unknown field {field!r}"
                )
        if spec["date_field"] not in capability["fields"]:
            raise SemanticArchiveExecutionError(
                f"semantic executor {capability_id} date field is outside capability semantics"
            )
        if spec["amount_field"] not in capability["fields"]:
            raise SemanticArchiveExecutionError(
                f"semantic executor {capability_id} amount field is outside capability semantics"
            )
        product_filter_field = spec.get("product_filter_field")
        if product_filter_field and product_filter_field not in dataset_fields:
            raise SemanticArchiveExecutionError(
                f"semantic executor {capability_id} has an unknown product filter field"
            )
        if spec["mode"] == "grouped_reason_amount":
            reason_fields = spec.get("reason_fields")
            if (
                not isinstance(reason_fields, list)
                or not reason_fields
                or any(field not in capability["fields"] for field in reason_fields)
            ):
                raise SemanticArchiveExecutionError(
                    f"semantic executor {capability_id} has invalid reason fields"
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
    date_field = str(executor["date_field"])
    amount_field = str(executor["amount_field"])
    currency_field = str(executor["currency_field"])
    amount = _numeric_expr(amount_field)
    date_value = _date_expr(date_field)
    currency = (
        "COALESCE(NULLIF(TRIM(" + _quote_identifier(currency_field) + "), ''), 'UNKNOWN')"
    )
    where = (
        f"{date_value} BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'"
        + _nm_filter(executor.get("product_filter_field"), nm_ids)
    )

    if executor["mode"] == "sum_by_currency":
        return (
            f"SELECT {currency} AS currency, SUM({amount}) AS amount, "
            f"SUM(CASE WHEN {amount} <> 0 THEN 1 ELSE 0 END) AS operation_rows "
            f"FROM {table} WHERE {where} GROUP BY 1 ORDER BY 1"
        )

    if executor["mode"] == "grouped_reason_amount":
        reason_fields = [str(value) for value in executor["reason_fields"]]
        fallback = "'Не указано'"
        for field in reversed(reason_fields):
            fallback = (
                "COALESCE(NULLIF(TRIM("
                + _quote_identifier(field)
                + "), ''), "
                + fallback
                + ")"
            )
        operation = (
            "COALESCE(NULLIF(TRIM("
            + _quote_identifier("sellerOperName")
            + "), ''), 'Не указано')"
        )
        return (
            f"SELECT {currency} AS currency, {fallback} AS reason, "
            f"{operation} AS operation, SUM({amount}) AS amount, COUNT(*) AS operation_rows "
            f"FROM {table} WHERE {where} AND {amount} <> 0 "
            "GROUP BY 1, 2, 3 ORDER BY ABS(SUM("
            + amount
            + ")) DESC, 1, 2, 3"
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
    )
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
        "provenance": {
            "date_field": executor["date_field"],
            "amount_field": executor["amount_field"],
            "currency_field": executor["currency_field"],
            "mode": executor["mode"],
            "semantics": executor.get("semantics"),
            "sum_rule": "as_reported_no_sign_conversion",
            "currency_rule": "never_sum_across_currencies",
            "product_filter": deepcopy(nm_ids) if nm_ids else None,
        },
        "guardrail": resolution.get("guardrail"),
    }
