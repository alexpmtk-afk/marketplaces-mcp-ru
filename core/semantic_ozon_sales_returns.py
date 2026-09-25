"""Closed-month Ozon sales/returns semantic executor.

Safe scope:
- canonical FINAL realization archive only;
- one complete closed calendar month;
- unit counts only:
  sales_units = sum(delivery_commission.quantity)
  return_units = sum(return_commission.quantity)

Money remains fail-closed until the provider meaning of amount/total fields is
approved separately.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .business_query_parser import parse_business_query_dimensions
from .business_registry import resolve_business_cabinet
from .metric_observation import build_metric_observation
from .ozon_current_archive import (
    OZON_ARCHIVE_CABINETS,
    parse_snapshot,
    stable_key_quality,
)
from .ozon_final_archive import (
    STABLE_KEY,
    coverage_location,
    monthly_location,
    parse_final_coverage,
)

MOSCOW = ZoneInfo("Europe/Moscow")
OZON_FINAL_SALES_RETURNS_VERSION = "ozon_final_sales_returns.v1"

_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "май": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}


class SemanticOzonFinalSalesReturnsError(RuntimeError):
    """Deterministic semantic/archive contract failure."""


def _norm(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^a-zа-я0-9_%₽]+", " ", text).split())


def requested_ozon_sales_returns(question: str) -> dict[str, Any] | None:
    text = _norm(question)
    metrics: list[str] = []
    sales_markers = (
        "сколько продаж",
        "количество продаж",
        "сумма продаж",
        "продажи ",
        " продаж ",
        "продано",
        "реализация",
        "реализовано",
        "реализованные единицы",
    )
    has_sales = any(marker in f" {text} " for marker in sales_markers)
    price_context = any(marker in text for marker in ("цена продажи", "цена продаж", "продажная цена"))
    if has_sales and not (
        price_context
        and not any(marker in text for marker in ("сколько продаж", "количество продаж", "сумма продаж", "продано", "реализац"))
    ):
        metrics.append("SALES")
    if "возврат" in text:
        metrics.append("RETURNS")
    if not metrics:
        return None

    dimensions = parse_business_query_dimensions(question)
    requested_measure = dimensions.get("requested_measure")
    if requested_measure is None:
        requested_measure = "UNITS"

    return {
        "metrics": list(dict.fromkeys(metrics)),
        "requested_measure": requested_measure,
        "period_hint": dimensions.get("period_hint"),
    }


def _resolve_cabinet(seller: str, question: str) -> tuple[str | None, dict[str, Any] | None]:
    requested = str(seller or "").strip()
    if requested:
        entry = resolve_business_cabinet("ozon", requested)
        cabinet = entry.cabinet if entry else requested
        if cabinet not in OZON_ARCHIVE_CABINETS:
            return None, {
                "ok": False,
                "error": "cabinet_not_configured",
                "code": "CABINET_NOT_CONFIGURED",
                "complete": False,
                "retryable": False,
            }
        return cabinet, None

    text = _norm(question)
    matches: list[str] = []
    for cabinet in OZON_ARCHIVE_CABINETS:
        entry = resolve_business_cabinet("ozon", cabinet)
        if entry is None:
            continue
        aliases = (entry.cabinet, entry.business_entity, *entry.aliases)
        if any(_norm(alias) and _norm(alias) in text for alias in aliases):
            matches.append(cabinet)
    matches = list(dict.fromkeys(matches))
    if len(matches) == 1:
        return matches[0], None
    return None, {
        "ok": False,
        "error": "cabinet_required" if not matches else "cabinet_ambiguous",
        "code": "NEEDS_CONTEXT" if not matches else "CABINET_AMBIGUOUS",
        "complete": False,
        "retryable": False,
        "required_context": ["seller/cabinet"],
    }


def _parse_iso(value: str, name: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise SemanticOzonFinalSalesReturnsError(
            f"{name} must be YYYY-MM-DD"
        ) from exc


def _named_month(question: str) -> tuple[int, int] | None:
    text = _norm(question)
    for stem, month in _MONTHS.items():
        if stem in text:
            year_match = re.search(r"\b(20\d{2})\b", text)
            year = int(year_match.group(1)) if year_match else datetime.now(MOSCOW).year
            return year, month
    return None


def _resolve_closed_full_month(
    question: str,
    *,
    date_from: str,
    date_to: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if bool(str(date_from or "").strip()) != bool(str(date_to or "").strip()):
        return None, {
            "ok": False,
            "error": "period_incomplete",
            "code": "NEEDS_CONTEXT",
            "complete": False,
            "retryable": False,
            "required_context": ["date_from and date_to"],
        }

    if str(date_from or "").strip():
        start = _parse_iso(date_from, "date_from")
        end = _parse_iso(date_to, "date_to")
        if start > end:
            raise SemanticOzonFinalSalesReturnsError("date_from must be <= date_to")
        if start.year != end.year or start.month != end.month:
            return None, {
                "ok": False,
                "error": "ozon_final_requires_one_full_month",
                "code": "OZON_FINAL_REQUIRES_FULL_MONTH",
                "complete": False,
                "retryable": False,
                "message": "Ozon FINAL sales/returns V1 supports one complete closed calendar month per query.",
            }
        year, month = start.year, start.month
        expected_end = date(year, month, calendar.monthrange(year, month)[1])
        if start != date(year, month, 1) or end != expected_end:
            return None, {
                "ok": False,
                "error": "ozon_final_requires_full_month",
                "code": "OZON_FINAL_REQUIRES_FULL_MONTH",
                "complete": False,
                "retryable": False,
                "message": "The canonical Ozon FINAL realization report is monthly; partial-month attribution is not approved.",
            }
    else:
        named = _named_month(question)
        if named is None:
            hint = parse_business_query_dimensions(question).get("period_hint")
            if hint in {"TODAY", "YESTERDAY", "WEEK", "MONTH"}:
                return None, {
                    "ok": False,
                    "error": "ozon_current_sales_returns_not_approved",
                    "code": "OZON_CURRENT_SALES_RETURNS_NOT_APPROVED",
                    "complete": False,
                    "retryable": False,
                    "message": (
                        "Current/open-month Ozon sales/returns require a separately approved CURRENT accrual semantic mapping."
                    ),
                }
            return None, {
                "ok": False,
                "error": "period_required",
                "code": "NEEDS_CONTEXT",
                "complete": False,
                "retryable": False,
                "required_context": ["closed calendar month"],
            }
        year, month = named
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])

    today = datetime.now(MOSCOW).date()
    if (year, month) >= (today.year, today.month):
        return None, {
            "ok": False,
            "error": "ozon_current_sales_returns_not_approved",
            "code": "OZON_CURRENT_SALES_RETURNS_NOT_APPROVED",
            "complete": False,
            "retryable": False,
            "message": (
                "Open-month Ozon sales/returns require a separately approved CURRENT accrual semantic mapping; "
                "FINAL realization is not substituted."
            ),
        }

    return {
        "year": year,
        "month": month,
        "period": f"{year:04d}-{month:02d}",
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
    }, None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SemanticOzonFinalSalesReturnsError(
            "Canonical Ozon FINAL row contains invalid JSON component"
        ) from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise SemanticOzonFinalSalesReturnsError(
            "Canonical Ozon FINAL commission component must be an object"
        )
    return parsed


def _quantity(component: dict[str, Any], field: str) -> int:
    value = component.get("quantity")
    if value in (None, ""):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SemanticOzonFinalSalesReturnsError(
            f"{field}.quantity is not an integer"
        ) from exc
    if number < 0:
        raise SemanticOzonFinalSalesReturnsError(
            f"{field}.quantity must not be negative"
        )
    return number


async def _load_verified_month(
    store: Any,
    *,
    cabinet: str,
    year: int,
    month: int,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    coverage_parts, coverage_name = coverage_location(cabinet, year)
    coverage_parent = await store.ensure_folder_path(coverage_parts)
    _, coverage_raw = await store.download_named(coverage_parent, coverage_name)
    if coverage_raw is None:
        raise SemanticOzonFinalSalesReturnsError(
            "Canonical Ozon FINAL coverage registry is missing"
        )

    period = f"{year:04d}-{month:02d}"
    coverage_rows = [
        row for row in parse_final_coverage(coverage_raw)
        if row.get("marketplace") == "ozon"
        and row.get("cabinet") == cabinet
        and row.get("dataset") == "ozon_final_realization"
        and row.get("period") == period
        and row.get("status") == "COMPLETE"
    ]
    if len(coverage_rows) != 1:
        raise SemanticOzonFinalSalesReturnsError(
            "Canonical Ozon FINAL does not prove one COMPLETE coverage record for the requested month"
        )
    coverage = coverage_rows[0]

    monthly_parts, monthly_name = monthly_location(cabinet, year, month)
    if coverage.get("monthly_file") != monthly_name:
        raise SemanticOzonFinalSalesReturnsError(
            "Ozon FINAL coverage points to an unexpected monthly file"
        )
    parent = await store.ensure_folder_path(monthly_parts)
    _, raw = await store.download_named(parent, monthly_name)
    if raw is None:
        raise SemanticOzonFinalSalesReturnsError(
            "Canonical Ozon FINAL monthly realization file is missing"
        )

    expected_sha = str(coverage.get("sha256") or "").strip().lower()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if expected_sha and actual_sha != expected_sha:
        raise SemanticOzonFinalSalesReturnsError(
            "Ozon FINAL monthly file hash does not match COMPLETE coverage"
        )

    _, rows = parse_snapshot(raw)
    expected_rows = int(str(coverage.get("rows") or "0"))
    if len(rows) != expected_rows:
        raise SemanticOzonFinalSalesReturnsError(
            "Ozon FINAL monthly row count does not match COMPLETE coverage"
        )
    quality = stable_key_quality(rows, STABLE_KEY)
    if quality["duplicate_stable_key_rows"] or quality["incomplete_stable_key_rows"]:
        raise SemanticOzonFinalSalesReturnsError(
            "Ozon FINAL stable-key validation failed"
        )
    if any(str(row.get("report_month") or "") != period for row in rows):
        raise SemanticOzonFinalSalesReturnsError(
            "Ozon FINAL file contains rows outside the requested report month"
        )

    return rows, {
        "status": "FULL_COVERAGE",
        "dataset": "ozon_final_realization",
        "period": period,
        "monthly_file": monthly_name,
        "rows": len(rows),
        "sha256": actual_sha,
        "stable_key": "+".join(STABLE_KEY),
    }


async def execute_ozon_final_sales_returns_question(
    store: Any,
    *,
    question: str,
    seller: str = "",
    date_from: str = "",
    date_to: str = "",
) -> dict[str, Any]:
    request = requested_ozon_sales_returns(question)
    if request is None:
        raise SemanticOzonFinalSalesReturnsError(
            "No Ozon sales/returns semantic was recognized"
        )

    if request["requested_measure"] != "UNITS":
        return {
            "ok": False,
            "error": "ozon_sales_returns_amount_not_approved",
            "code": "OZON_SALES_RETURNS_AMOUNT_NOT_APPROVED",
            "complete": False,
            "retryable": False,
            "message": (
                "Ozon FINAL contains multiple monetary fields (amount/total/standard_fee). "
                "No monetary sales/returns field is approved yet, so no amount is guessed."
            ),
        }

    period, period_error = _resolve_closed_full_month(
        question,
        date_from=date_from,
        date_to=date_to,
    )
    if period_error:
        return period_error
    assert period is not None

    cabinet, cabinet_error = _resolve_cabinet(seller, question)
    if cabinet_error:
        return cabinet_error
    assert cabinet is not None

    rows, coverage = await _load_verified_month(
        store,
        cabinet=cabinet,
        year=int(period["year"]),
        month=int(period["month"]),
    )

    sales_units = 0
    return_units = 0
    delivery_rows = 0
    return_rows = 0
    for row in rows:
        delivery = _json_object(row.get("delivery_commission"))
        returned = _json_object(row.get("return_commission"))
        dq = _quantity(delivery, "delivery_commission")
        rq = _quantity(returned, "return_commission")
        sales_units += dq
        return_units += rq
        if dq:
            delivery_rows += 1
        if rq:
            return_rows += 1

    metrics = {
        "SALES": {
            "metric_id": "SALES",
            "label_ru": "Реализованные единицы Ozon",
            "value": sales_units,
            "unit": "UNITS",
            "provider_field": "delivery_commission.quantity",
        },
        "RETURNS": {
            "metric_id": "RETURNS",
            "label_ru": "Возвращённые единицы Ozon",
            "value": return_units,
            "unit": "UNITS",
            "provider_field": "return_commission.quantity",
        },
    }
    observations = []
    for metric_id in request["metrics"]:
        item = metrics[metric_id]
        observation = build_metric_observation(
            metric_id=metric_id,
            value=item["value"],
            unit="UNITS",
            marketplace="ozon",
            source_name="ozon_final_realization",
            source_field=item["provider_field"],
        ).to_dict()
        observations.append(observation)

    result: dict[str, Any] = {
        "ok": True,
        "complete": True,
        "executor": OZON_FINAL_SALES_RETURNS_VERSION,
        "marketplace": "ozon",
        "cabinet": cabinet,
        "requested_metrics": list(request["metrics"]),
        "period": {
            "date_from": period["date_from"],
            "date_to": period["date_to"],
            "report_month": period["period"],
        },
        "data_class": "FINAL_CLOSED_MONTH_REALIZATION",
        "source": "ozon_final_realization",
        "source_validation": "canonical_google_drive_final_with_complete_coverage_hash_and_stable_key",
        "coverage": coverage,
        "sales_units": sales_units,
        "return_units": return_units,
        "net_units": sales_units - return_units,
        "delivery_rows": delivery_rows,
        "return_rows": return_rows,
        "metric_observations": observations,
        "metrics": {metric_id: metrics[metric_id] for metric_id in request["metrics"]},
        "guardrails": [
            "SALES units use delivery_commission.quantity from the official closed-month realization report.",
            "RETURNS units use return_commission.quantity from the same report.",
            "net_units = sales_units - return_units is explicit arithmetic, not a provider field.",
            "Money is fail-closed until the meaning of amount/total/standard_fee is approved.",
            "Partial-month attribution is not approved because FINAL realization is monthly.",
            "Open-month CURRENT accruals are not substituted for FINAL semantics.",
        ],
        "knowledge_catalog_version": "marketplace_knowledge_catalog.v1",
    }
    if len(request["metrics"]) == 1:
        metric_id = request["metrics"][0]
        result["metric_id"] = metric_id
        result["value"] = metrics[metric_id]["value"]
        result["unit"] = "UNITS"
    return result


__all__ = [
    "OZON_FINAL_SALES_RETURNS_VERSION",
    "SemanticOzonFinalSalesReturnsError",
    "execute_ozon_final_sales_returns_question",
    "requested_ozon_sales_returns",
]
