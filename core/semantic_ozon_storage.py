"""Approved Ozon CURRENT storage/placement semantic.

Generic storage expense includes only provider accrual types that explicitly
describe storage/placement:
46 Placements,
60 ReturnStorageInTheWarehouse,
78 TemporaryPlacement,
79 TemporaryPlacementsAgent,
102 B2CTemporaryPlacement.

The values are read from their provider-native fee locations in the canonical
CURRENT accrual archive. Logistics, commission, advertising and unrelated fees
are never substituted.
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
VERSION = "ozon_current_storage.v1"
DATASET = "ozon_current_accruals"

STORAGE_TYPES = {
    46: "Placements",
    60: "ReturnStorageInTheWarehouse",
    78: "TemporaryPlacement",
    79: "TemporaryPlacementsAgent",
    102: "B2CTemporaryPlacement",
}


class SemanticOzonStorageError(RuntimeError):
    pass


def _norm(v: Any) -> str:
    return " ".join(
        re.sub(r"[^a-zа-я0-9₽]+", " ", str(v or "").casefold().replace("ё", "е")).split()
    )


def requested_ozon_storage(question: str) -> dict[str, Any] | None:
    t = _norm(question)
    if "хранен" not in t and "размещ" not in t:
        return None
    if any(x in t for x in ("реклам", "продвижен", "размещение рекламы")):
        return None
    return {"metric": "STORAGE_COST", "requested_measure": "RUB"}


def _cabinet(seller: str) -> str:
    entry = resolve_business_cabinet("ozon", str(seller or "").strip())
    value = entry.cabinet if entry else str(seller or "").strip()
    if value not in OZON_ARCHIVE_CABINETS:
        raise SemanticOzonStorageError("Ozon cabinet is required and must be configured")
    return value


def _coverage_rows(raw: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";")
    return [dict(x) for x in reader]


def _raw(row: dict[str, str]) -> dict[str, Any]:
    try:
        value = json.loads(str(row.get("_raw_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise SemanticOzonStorageError(
            "Ozon CURRENT accrual row contains invalid raw JSON"
        ) from exc
    if not isinstance(value, dict):
        raise SemanticOzonStorageError("Ozon CURRENT accrual raw JSON is not an object")
    return value


def _money(v: Any) -> Decimal:
    if isinstance(v, dict):
        for key in ("amount", "value", "accrued", "total_accrued"):
            if v.get(key) not in (None, ""):
                v = v.get(key)
                break
        else:
            raise SemanticOzonStorageError(
                "Ozon storage money object has no approved numeric field"
            )
    try:
        return Decimal(str(v or "0").replace(",", "."))
    except InvalidOperation as exc:
        raise SemanticOzonStorageError("Ozon storage field is not numeric") from exc


def _period(
    question: str,
    date_from: str,
    date_to: str,
    coverage: dict[str, str],
) -> tuple[date, date]:
    cov_from = date.fromisoformat(coverage["date_from"])
    cov_to = date.fromisoformat(coverage["date_to"])
    if bool(date_from) != bool(date_to):
        raise SemanticOzonStorageError("date_from and date_to must be supplied together")
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
            raise SemanticOzonStorageError(
                "Ozon CURRENT storage supports only the open month"
            )
        start = cov_from
        end = cov_to
    if start < cov_from or end > cov_to or start > end:
        raise SemanticOzonStorageError(
            "Requested period is outside canonical Ozon CURRENT coverage"
        )
    return start, end


def _add(
    totals: dict[int, Decimal],
    counts: dict[int, int],
    type_id: Any,
    accrued: Any,
) -> None:
    try:
        tid = int(type_id)
    except (TypeError, ValueError):
        return
    if tid not in STORAGE_TYPES:
        return
    totals[tid] += _money(accrued)
    counts[tid] += 1


async def execute_ozon_current_storage(
    store: Any,
    *,
    question: str,
    seller: str = "",
    date_from: str = "",
    date_to: str = "",
) -> dict[str, Any]:
    if requested_ozon_storage(question) is None:
        raise SemanticOzonStorageError("No Ozon storage semantic recognized")

    cabinet = _cabinet(seller)
    today = datetime.now(MOSCOW).date()
    parts, cov_name = coverage_location(cabinet, today.year, today.month)
    parent = await store.ensure_folder_path(parts)
    _, cov_raw = await store.download_named(parent, cov_name)
    if cov_raw is None:
        raise SemanticOzonStorageError(
            "Canonical Ozon CURRENT coverage registry is missing"
        )
    cov = [
        x for x in _coverage_rows(cov_raw)
        if x.get("dataset") == DATASET and x.get("status") == "COMPLETE"
    ]
    if len(cov) != 1:
        raise SemanticOzonStorageError(
            "Canonical Ozon CURRENT accrual coverage is not exactly one COMPLETE record"
        )
    coverage = cov[0]
    start, end = _period(question, date_from, date_to, coverage)

    data_parts, data_name = canonical_location(
        cabinet, today.year, today.month, DATASET
    )
    if coverage.get("canonical_file") != data_name:
        raise SemanticOzonStorageError(
            "Ozon CURRENT coverage points to an unexpected accrual file"
        )
    data_parent = await store.ensure_folder_path(data_parts)
    _, raw = await store.download_named(data_parent, data_name)
    if raw is None:
        raise SemanticOzonStorageError(
            "Canonical Ozon CURRENT accrual file is missing"
        )

    sha = hashlib.sha256(raw).hexdigest()
    if coverage.get("sha256") and coverage["sha256"].lower() != sha:
        raise SemanticOzonStorageError(
            "Ozon CURRENT accrual hash does not match COMPLETE coverage"
        )
    _, rows = parse_snapshot(raw)
    if len(rows) != int(coverage.get("rows") or "0"):
        raise SemanticOzonStorageError(
            "Ozon CURRENT accrual row count does not match COMPLETE coverage"
        )
    quality = stable_key_quality(rows, tuple(DATASETS[DATASET]["stable_key"]))
    if quality["duplicate_stable_key_rows"] or quality["incomplete_stable_key_rows"]:
        raise SemanticOzonStorageError(
            "Ozon CURRENT accrual stable-key validation failed"
        )

    totals: dict[int, Decimal] = defaultdict(Decimal)
    counts: dict[int, int] = defaultdict(int)
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

        non_item = obj.get("non_item_fee")
        if isinstance(non_item, dict):
            _add(totals, counts, non_item.get("type_id"), non_item.get("accrued"))

        item_fees = obj.get("item_fees")
        if isinstance(item_fees, dict):
            for group in item_fees.get("fees") or []:
                if not isinstance(group, dict):
                    continue
                inner = group.get("fees")
                if isinstance(inner, list):
                    fees = inner
                elif group.get("type_id") is not None:
                    fees = [group]
                else:
                    fees = []
                for fee in fees:
                    if isinstance(fee, dict):
                        _add(totals, counts, fee.get("type_id"), fee.get("accrued"))

        posting = obj.get("posting")
        if isinstance(posting, dict):
            for product in posting.get("products") or []:
                if not isinstance(product, dict):
                    continue
                delivery = product.get("delivery")
                if not isinstance(delivery, dict):
                    continue
                for fee in delivery.get("services") or []:
                    if isinstance(fee, dict):
                        _add(totals, counts, fee.get("type_id"), fee.get("accrued"))

    signed = sum(totals.values(), Decimal("0"))
    breakdown = [
        {
            "type_id": tid,
            "type_name": STORAGE_TYPES[tid],
            "signed_provider_value": float(totals[tid]),
            "expense_rub": float(abs(totals[tid])),
            "occurrences": counts[tid],
        }
        for tid in sorted(STORAGE_TYPES)
    ]

    return {
        "ok": True,
        "complete": True,
        "executor": VERSION,
        "marketplace": "ozon",
        "cabinet": cabinet,
        "metric_id": "STORAGE_COST",
        "unit": "RUB",
        "value": float(abs(signed)),
        "signed_provider_value": float(signed),
        "period": {"date_from": start.isoformat(), "date_to": end.isoformat()},
        "source": "ozon_current_accruals",
        "source_fields": [
            "non_item_fee.{type_id,accrued}",
            "item_fees.fees[].fees[].{type_id,accrued}",
            "posting.products[].delivery.services[].{type_id,accrued}",
        ],
        "approved_type_ids": sorted(STORAGE_TYPES),
        "breakdown": breakdown,
        "coverage": {
            "status": "FULL_COVERAGE",
            "rows": len(rows),
            "sha256": sha,
            "date_from": coverage["date_from"],
            "date_to": coverage["date_to"],
        },
        "rows_scanned": scanned_rows,
        "semantic_note": (
            "Generic Ozon storage includes only explicit provider storage/placement "
            "types 46, 60, 78, 79 and 102. Other services are excluded."
        ),
    }


__all__ = [
    "SemanticOzonStorageError",
    "execute_ozon_current_storage",
    "requested_ozon_storage",
]
