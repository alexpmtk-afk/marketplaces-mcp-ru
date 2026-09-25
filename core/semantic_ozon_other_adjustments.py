"""Approved Ozon CURRENT other-services and adjustments semantics.

OTHER_SERVICES_COST uses an explicit allowlist of reviewed provider fee types
from non-item and item-fee locations only. Delivery service rows are excluded
because they are already included in LOGISTICS_COST via delivery.total_accrued.

DEDUCTIONS_ADJUSTMENTS uses only reviewed settlement/correction provider types.
If such a type appears inside delivery.services, execution fails closed to avoid
double-counting against logistics.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from .business_registry import resolve_business_cabinet
from .ozon_current_archive import (
    DATASETS,
    OZON_ARCHIVE_CABINETS,
    canonical_location,
    coverage_location,
    parse_snapshot,
    stable_key_quality,
)

MOSCOW = ZoneInfo("Europe/Moscow")
VERSION = "ozon_current_other_adjustments.v1"
DATASET = "ozon_current_accruals"

OTHER_SERVICE_TYPES = {
    1: "Acquiring",
    22: "Installment",
    38: "PackageCost",
    39: "PackingFee",
    52: "PremiumSubscription",
    63: "RfbsDomesticAgentFee",
    71: "SellerReturns",
    76: "StockInsurance",
    84: "ItemPacking",
}

ADJUSTMENT_TYPES = {
    11: "CorrectionCommission",
    57: "RealizationReportCorrection",
    72: "SetOff",
    83: "BrandDeposit",
}


class SemanticOzonOtherAdjustmentsError(RuntimeError):
    pass


def _norm(v: Any) -> str:
    return " ".join(
        re.sub(r"[^a-zа-я0-9₽]+", " ", str(v or "").casefold().replace("ё", "е")).split()
    )


def requested_ozon_other_adjustments(question: str) -> dict[str, Any] | None:
    t = _norm(question)
    if any(
        x in t
        for x in (
            "удержан",
            "корректиров",
            "взаимозач",
            "обеспечительн",
        )
    ):
        return {"metric": "DEDUCTIONS_ADJUSTMENTS", "requested_measure": "RUB"}
    if any(
        x in t
        for x in (
            "прочие услуги",
            "другие услуги",
            "прочие расходы",
            "прочие комиссии и услуги",
        )
    ):
        return {"metric": "OTHER_SERVICES_COST", "requested_measure": "RUB"}
    return None


def _cabinet(seller: str) -> str:
    entry = resolve_business_cabinet("ozon", str(seller or "").strip())
    value = entry.cabinet if entry else str(seller or "").strip()
    if value not in OZON_ARCHIVE_CABINETS:
        raise SemanticOzonOtherAdjustmentsError(
            "Ozon cabinet is required and must be configured"
        )
    return value


def _coverage_rows(raw: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";")
    return [dict(x) for x in reader]


def _raw(row: dict[str, str]) -> dict[str, Any]:
    try:
        value = json.loads(str(row.get("_raw_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise SemanticOzonOtherAdjustmentsError(
            "Ozon CURRENT accrual row contains invalid raw JSON"
        ) from exc
    if not isinstance(value, dict):
        raise SemanticOzonOtherAdjustmentsError(
            "Ozon CURRENT accrual raw JSON is not an object"
        )
    return value


def _money(v: Any) -> Decimal:
    if isinstance(v, dict):
        for key in ("amount", "value", "accrued", "total_accrued"):
            if v.get(key) not in (None, ""):
                v = v.get(key)
                break
        else:
            raise SemanticOzonOtherAdjustmentsError(
                "Ozon finance money object has no approved numeric field"
            )
    try:
        return Decimal(str(v or "0").replace(",", "."))
    except InvalidOperation as exc:
        raise SemanticOzonOtherAdjustmentsError(
            "Ozon finance field is not numeric"
        ) from exc


def _period(
    question: str,
    date_from: str,
    date_to: str,
    coverage: dict[str, str],
) -> tuple[date, date]:
    cov_from = date.fromisoformat(coverage["date_from"])
    cov_to = date.fromisoformat(coverage["date_to"])
    if bool(date_from) != bool(date_to):
        raise SemanticOzonOtherAdjustmentsError(
            "date_from and date_to must be supplied together"
        )
    if date_from:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    else:
        t = _norm(question)
        months = {
            "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "май": 5, "июн": 6,
            "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
        }
        found = next((m for stem, m in months.items() if stem in t), None)
        year_match = re.search(r"\b(20\d{2})\b", t)
        now = datetime.now(MOSCOW).date()
        if found is not None and (
            found != now.month or (year_match and int(year_match.group(1)) != now.year)
        ):
            raise SemanticOzonOtherAdjustmentsError(
                "Ozon CURRENT finance supports only the open month"
            )
        start = cov_from
        end = cov_to
    if start < cov_from or end > cov_to or start > end:
        raise SemanticOzonOtherAdjustmentsError(
            "Requested period is outside canonical Ozon CURRENT coverage"
        )
    return start, end


def _fee_groups(obj: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    non_item: list[dict[str, Any]] = []
    item: list[dict[str, Any]] = []
    delivery: list[dict[str, Any]] = []

    nif = obj.get("non_item_fee")
    if isinstance(nif, dict):
        non_item.append(nif)

    item_fees = obj.get("item_fees")
    if isinstance(item_fees, dict):
        for group in item_fees.get("fees") or []:
            if not isinstance(group, dict):
                continue
            inner = group.get("fees")
            if isinstance(inner, list):
                item.extend(x for x in inner if isinstance(x, dict))
            elif group.get("type_id") is not None:
                item.append(group)

    posting = obj.get("posting")
    if isinstance(posting, dict):
        for product in posting.get("products") or []:
            if not isinstance(product, dict):
                continue
            delivery_obj = product.get("delivery")
            if not isinstance(delivery_obj, dict):
                continue
            delivery.extend(
                x for x in (delivery_obj.get("services") or []) if isinstance(x, dict)
            )

    return non_item, item, delivery


async def execute_ozon_current_other_adjustments(
    store: Any,
    *,
    question: str,
    seller: str = "",
    date_from: str = "",
    date_to: str = "",
) -> dict[str, Any]:
    request = requested_ozon_other_adjustments(question)
    if request is None:
        raise SemanticOzonOtherAdjustmentsError(
            "No Ozon other-services/adjustments semantic recognized"
        )

    metric = request["metric"]
    type_map = (
        OTHER_SERVICE_TYPES
        if metric == "OTHER_SERVICES_COST"
        else ADJUSTMENT_TYPES
    )

    cabinet = _cabinet(seller)
    today = datetime.now(MOSCOW).date()
    parts, cov_name = coverage_location(cabinet, today.year, today.month)
    parent = await store.ensure_folder_path(parts)
    _, cov_raw = await store.download_named(parent, cov_name)
    if cov_raw is None:
        raise SemanticOzonOtherAdjustmentsError(
            "Canonical Ozon CURRENT coverage registry is missing"
        )
    cov = [
        x for x in _coverage_rows(cov_raw)
        if x.get("dataset") == DATASET and x.get("status") == "COMPLETE"
    ]
    if len(cov) != 1:
        raise SemanticOzonOtherAdjustmentsError(
            "Canonical Ozon CURRENT accrual coverage is not exactly one COMPLETE record"
        )
    coverage = cov[0]
    start, end = _period(question, date_from, date_to, coverage)

    data_parts, data_name = canonical_location(
        cabinet, today.year, today.month, DATASET
    )
    if coverage.get("canonical_file") != data_name:
        raise SemanticOzonOtherAdjustmentsError(
            "Ozon CURRENT coverage points to an unexpected accrual file"
        )
    data_parent = await store.ensure_folder_path(data_parts)
    _, raw = await store.download_named(data_parent, data_name)
    if raw is None:
        raise SemanticOzonOtherAdjustmentsError(
            "Canonical Ozon CURRENT accrual file is missing"
        )

    sha = hashlib.sha256(raw).hexdigest()
    if coverage.get("sha256") and coverage["sha256"].lower() != sha:
        raise SemanticOzonOtherAdjustmentsError(
            "Ozon CURRENT accrual hash does not match COMPLETE coverage"
        )
    _, rows = parse_snapshot(raw)
    if len(rows) != int(coverage.get("rows") or "0"):
        raise SemanticOzonOtherAdjustmentsError(
            "Ozon CURRENT accrual row count does not match COMPLETE coverage"
        )
    quality = stable_key_quality(rows, tuple(DATASETS[DATASET]["stable_key"]))
    if quality["duplicate_stable_key_rows"] or quality["incomplete_stable_key_rows"]:
        raise SemanticOzonOtherAdjustmentsError(
            "Ozon CURRENT accrual stable-key validation failed"
        )

    totals: dict[int, Decimal] = defaultdict(Decimal)
    counts: dict[int, int] = defaultdict(int)
    delivery_overlap: dict[int, int] = defaultdict(int)
    scanned_rows = 0

    for row in rows:
        obj = _raw(row)
        try:
            row_date = date.fromisoformat(str(obj.get("date") or ""))
        except ValueError:
            continue
        if not (start <= row_date <= end):
            continue
        scanned_rows += 1
        non_item, item, delivery = _fee_groups(obj)

        for fee in non_item + item:
            try:
                tid = int(fee.get("type_id"))
            except (TypeError, ValueError):
                continue
            if tid not in type_map:
                continue
            totals[tid] += _money(fee.get("accrued"))
            counts[tid] += 1

        for fee in delivery:
            try:
                tid = int(fee.get("type_id"))
            except (TypeError, ValueError):
                continue
            if tid in type_map:
                delivery_overlap[tid] += 1

    if delivery_overlap:
        raise SemanticOzonOtherAdjustmentsError(
            "Approved Ozon fee type overlaps delivery.services and would double-count logistics"
        )

    signed = sum(totals.values(), Decimal("0"))
    breakdown = [
        {
            "type_id": tid,
            "type_name": type_map[tid],
            "signed_provider_value": float(totals[tid]),
            "expense_rub": float(abs(totals[tid])),
            "occurrences": counts[tid],
        }
        for tid in sorted(type_map)
    ]

    if metric == "OTHER_SERVICES_COST":
        note = (
            "Other Ozon services use an explicit reviewed allowlist and only "
            "non_item_fee/item_fees. delivery.services are excluded because they "
            "are already included in LOGISTICS_COST."
        )
    else:
        note = (
            "Ozon deductions/adjustments use only reviewed settlement/correction "
            "types 11, 57, 72 and 83. Delivery overlap fails closed."
        )

    return {
        "ok": True,
        "complete": True,
        "executor": VERSION,
        "marketplace": "ozon",
        "cabinet": cabinet,
        "metric_id": metric,
        "unit": "RUB",
        "value": float(abs(signed)),
        "signed_provider_value": float(signed),
        "period": {"date_from": start.isoformat(), "date_to": end.isoformat()},
        "source": "ozon_current_accruals",
        "source_fields": [
            "non_item_fee.{type_id,accrued}",
            "item_fees.fees[].fees[].{type_id,accrued}",
        ],
        "approved_type_ids": sorted(type_map),
        "breakdown": breakdown,
        "coverage": {
            "status": "FULL_COVERAGE",
            "rows": len(rows),
            "sha256": sha,
            "date_from": coverage["date_from"],
            "date_to": coverage["date_to"],
        },
        "rows_scanned": scanned_rows,
        "semantic_note": note,
    }


__all__ = [
    "ADJUSTMENT_TYPES",
    "OTHER_SERVICE_TYPES",
    "SemanticOzonOtherAdjustmentsError",
    "execute_ozon_current_other_adjustments",
    "requested_ozon_other_adjustments",
]
