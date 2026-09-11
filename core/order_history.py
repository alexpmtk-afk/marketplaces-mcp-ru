"""Selective historical storage for canonical Wildberries ORDERS.

The store deliberately archives only the approved Statistics Orders semantics.
It is not a generic marketplace warehouse. Rows are keyed by cabinet + srid;
newer ``lastChangeDate`` versions replace older state so cancellations remain
consistent with the canonical live metric.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .business_registry import resolve_business_cabinet
from .errors import make_error

WB_HISTORY_BOOTSTRAP_MAX_DAYS = 90
WB_STATS_MAX_ROWS = 80_000


@dataclass(frozen=True, slots=True)
class OrderHistoryCoverage:
    cabinet: str
    target_from: str | None = None
    covered_from: str | None = None
    covered_to: str | None = None
    watermark_last_change_date: str | None = None
    last_successful_sync_at: str | None = None
    status: str = "empty"  # empty | syncing | complete | gap

    def covers(self, start: date, end: date) -> bool:
        if self.status != "complete" or not self.covered_from or not self.covered_to:
            return False
        return self.covered_from <= start.isoformat() and self.covered_to >= end.isoformat()


class OrderHistoryStore(Protocol):
    storage_scope: str

    def coverage(self, cabinet: str) -> OrderHistoryCoverage: ...
    def upsert_rows(self, cabinet: str, rows: list[dict[str, Any]]) -> int: ...
    def read_rows(self, cabinet: str, start: date, end: date) -> list[dict[str, Any]]: ...
    def save_coverage(self, coverage: OrderHistoryCoverage) -> None: ...
    def count(self, cabinet: str) -> int: ...


class MemoryOrderHistoryStore:
    """Deterministic reference store used by tests/local development."""

    storage_scope = "memory"

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, dict[str, Any]]] = {}
        self._coverage: dict[str, OrderHistoryCoverage] = {}

    def coverage(self, cabinet: str) -> OrderHistoryCoverage:
        return self._coverage.get(cabinet, OrderHistoryCoverage(cabinet=cabinet))

    def upsert_rows(self, cabinet: str, rows: list[dict[str, Any]]) -> int:
        normalized = normalize_wb_order_rows(rows)
        bucket = self._rows.setdefault(cabinet, {})
        changed = 0
        for row in normalized:
            srid = str(row["srid"])
            previous = bucket.get(srid)
            if previous is not None and str(previous["lastChangeDate"]) > str(row["lastChangeDate"]):
                continue
            if previous != row:
                changed += 1
            bucket[srid] = dict(row)
        return changed

    def read_rows(self, cabinet: str, start: date, end: date) -> list[dict[str, Any]]:
        out = []
        for row in self._rows.get(cabinet, {}).values():
            row_day = str(row["date"])[:10]
            if start.isoformat() <= row_day <= end.isoformat():
                out.append(dict(row))
        out.sort(key=lambda item: (str(item.get("date", "")), str(item.get("srid", ""))))
        return out

    def save_coverage(self, coverage: OrderHistoryCoverage) -> None:
        self._coverage[coverage.cabinet] = coverage

    def count(self, cabinet: str) -> int:
        return len(self._rows.get(cabinet, {}))


def normalize_wb_order_rows(rows: object) -> list[dict[str, Any]]:
    """Validate one provider page and keep the freshest row for every srid."""
    if not isinstance(rows, list):
        raise ValueError("WB Statistics Orders response is not an array")
    freshest: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise ValueError(f"WB order row #{index} is not an object")
        row = dict(raw)
        srid = str(row.get("srid") or "").strip()
        order_date = str(row.get("date") or "").strip()
        last_change = str(row.get("lastChangeDate") or "").strip()
        if not srid:
            raise ValueError(f"WB order row #{index} has no srid")
        _iso_day(order_date, f"WB order row #{index} date")
        _iso_timestamp(last_change, f"WB order row #{index} lastChangeDate")
        try:
            Decimal(str(row.get("finishedPrice")))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"WB order row #{index} has no valid finishedPrice") from exc
        previous = freshest.get(srid)
        if previous is None or str(previous["lastChangeDate"]) <= last_change:
            freshest[srid] = row
    return list(freshest.values())


def merge_latest_rows(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate overlapping Historical Store/API rows by srid + lastChangeDate."""
    freshest: dict[str, dict[str, Any]] = {}
    for rows in groups:
        for row in normalize_wb_order_rows(rows):
            srid = str(row["srid"])
            previous = freshest.get(srid)
            if previous is None or str(previous["lastChangeDate"]) <= str(row["lastChangeDate"]):
                freshest[srid] = row
    return list(freshest.values())


def canonical_order_totals(rows: list[dict[str, Any]], start: date, end: date) -> dict[str, Any]:
    count = 0
    cancelled = 0
    amount = Decimal("0")
    matched = 0
    for row in normalize_wb_order_rows(rows):
        row_day = _iso_day(str(row["date"]), "WB order date")
        if row_day < start or row_day > end:
            continue
        matched += 1
        if bool(row.get("isCancel", False)):
            cancelled += 1
            continue
        count += 1
        amount += Decimal(str(row["finishedPrice"]))
    return {
        "orders_count": count,
        "orders_amount": float(amount),
        "currency": "RUB",
        "cancelled_orders_excluded": cancelled,
        "provider_rows_in_period": matched,
    }


def resolve_history_cabinet(wb: Any, seller: str) -> tuple[str, dict[str, str] | None, dict | None]:
    business = resolve_business_cabinet("wb", seller)
    credential_name = business.cabinet if business else seller.strip()
    creds, resolved = wb.client.config.store.resolve_named(
        "wb", wb.client.config.fields, wb.client.config.env_map, credential_name
    )
    if not resolved or any(not creds.get(field) for field in wb.client.config.fields):
        if business:
            return "", None, make_error(
                "seller_known_but_not_configured",
                f"Кабинет {business.business_entity} известен, но доступ Wildberries не настроен.",
                operation_id="wb_orders_history_sync",
                retryable=False,
                details={"cabinet": business.cabinet, "upstream_request_sent": False},
            )
        return "", None, make_error(
            "invalid_params",
            f"WB cabinet {seller!r} was not found or is incomplete.",
            operation_id="wb_orders_history_sync",
            retryable=False,
        )
    return resolved, creds, None


async def sync_wb_orders_history(
    wb: Any,
    store: OrderHistoryStore,
    *,
    seller: str,
    bootstrap_days: int = WB_HISTORY_BOOTSTRAP_MAX_DAYS,
) -> dict[str, Any]:
    """Persist one canonical Statistics Orders page and advance verified coverage.

    One invocation sends at most one WB request. If WB returns the provider page
    ceiling, rows are stored but coverage is not declared complete; the next
    invocation continues from the stored lastChangeDate after the provider quota
    allows another request.
    """
    cabinet, creds, error = resolve_history_cabinet(wb, seller)
    if error:
        return error
    assert creds is not None

    coverage = store.coverage(cabinet)
    today = date.today()
    verified_to = today - timedelta(days=1)
    bootstrap_days = max(1, min(int(bootstrap_days), WB_HISTORY_BOOTSTRAP_MAX_DAYS))

    if coverage.status == "complete" and coverage.last_successful_sync_at:
        last_sync_day = _iso_day(coverage.last_successful_sync_at, "last_successful_sync_at")
        if (today - last_sync_day).days >= WB_HISTORY_BOOTSTRAP_MAX_DAYS:
            gap = OrderHistoryCoverage(
                cabinet=cabinet,
                target_from=coverage.target_from or coverage.covered_from,
                covered_from=coverage.covered_from,
                covered_to=coverage.covered_to,
                watermark_last_change_date=coverage.watermark_last_change_date,
                last_successful_sync_at=coverage.last_successful_sync_at,
                status="gap",
            )
            store.save_coverage(gap)
            return make_error(
                "coverage_gap",
                "WB ORDERS historical sync was inactive longer than the safe Statistics retention window. Continuity can no longer be proven automatically.",
                operation_id="wb_orders_history_sync",
                retryable=False,
                details={"cabinet": cabinet, "last_successful_sync_at": coverage.last_successful_sync_at},
            )

    if coverage.watermark_last_change_date:
        request_from = coverage.watermark_last_change_date
        target_from = coverage.target_from or coverage.covered_from
    else:
        # Provider retention is a rolling N-day window, not N guaranteed whole
        # calendar days. Start at today-(N-1) so every advertised historical day
        # is fully inside the retention window; the partial boundary day is not
        # claimed as complete coverage.
        bootstrap_start = today - timedelta(days=bootstrap_days - 1)
        request_from = bootstrap_start.isoformat() + "T00:00:00"
        target_from = bootstrap_start.isoformat()

    spec = wb.catalog.get("wb_stats_orders")
    if spec is None:
        return make_error(
            "source_not_suitable",
            "Canonical wb_stats_orders is absent from the runtime catalog.",
            operation_id="wb_orders_history_sync",
            retryable=False,
        )

    response = await wb.client.call_spec(
        spec,
        query={"dateFrom": request_from, "flag": 0},
        creds_override=creds,
        retry_on_429=False,
    )
    if not response.get("ok"):
        return response

    raw_rows = response.get("data")
    try:
        normalized = normalize_wb_order_rows(raw_rows)
    except ValueError as exc:
        return make_error(
            "provider_data_conflict",
            str(exc),
            operation_id="wb_orders_history_sync",
            retryable=False,
        )
    assert isinstance(raw_rows, list)  # validated by normalize_wb_order_rows
    raw_row_count = len(raw_rows)

    changed = store.upsert_rows(cabinet, normalized)
    watermark = coverage.watermark_last_change_date
    if normalized:
        watermark = max(str(row["lastChangeDate"]) for row in normalized)
    now = datetime.now(timezone.utc).isoformat()

    # The provider ceiling applies to the raw response page. Deduplication by
    # srid can reduce the row count, so checking len(normalized) could falsely
    # mark an 80k page complete and lose its required continuation.
    if raw_row_count >= WB_STATS_MAX_ROWS:
        store.save_coverage(OrderHistoryCoverage(
            cabinet=cabinet,
            target_from=target_from,
            covered_from=coverage.covered_from,
            covered_to=coverage.covered_to,
            watermark_last_change_date=watermark,
            last_successful_sync_at=coverage.last_successful_sync_at,
            status="syncing",
        ))
        return make_error(
            "execution_pending",
            "WB ORDERS history page reached the provider ceiling. Stored rows are durable, but coverage is not marked complete until continuation finishes.",
            operation_id="wb_orders_history_sync",
            retryable=True,
            retry_after_seconds=60,
            details={
                "cabinet": cabinet,
                "rows_received": raw_row_count,
                "rows_normalized": len(normalized),
                "rows_stored": changed,
                "resume_last_change_date": watermark,
                "complete": False,
            },
        )

    old_to = coverage.covered_to or ""
    new_to = max(old_to, verified_to.isoformat())
    completed = OrderHistoryCoverage(
        cabinet=cabinet,
        target_from=target_from,
        covered_from=coverage.covered_from or target_from,
        covered_to=new_to,
        watermark_last_change_date=watermark,
        last_successful_sync_at=now,
        status="complete",
    )
    store.save_coverage(completed)
    return {
        "ok": True,
        "metric": "ORDERS",
        "marketplace": "WB",
        "cabinet": cabinet,
        "source": "wb_stats_orders",
        "storage": store.storage_scope,
        "rows_received": raw_row_count,
        "rows_normalized": len(normalized),
        "rows_changed": changed,
        "stored_rows": store.count(cabinet),
        "covered_from": completed.covered_from,
        "covered_to": completed.covered_to,
        "watermark_last_change_date": completed.watermark_last_change_date,
        "complete": True,
    }


def coverage_payload(store: OrderHistoryStore, cabinet: str) -> dict[str, Any]:
    coverage = store.coverage(cabinet)
    return {
        "ok": True,
        "cabinet": cabinet,
        "storage": store.storage_scope,
        "status": coverage.status,
        "target_from": coverage.target_from,
        "covered_from": coverage.covered_from,
        "covered_to": coverage.covered_to,
        "watermark_last_change_date": coverage.watermark_last_change_date,
        "last_successful_sync_at": coverage.last_successful_sync_at,
        "stored_rows": store.count(cabinet),
    }


def _iso_day(value: str, field: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must contain an ISO date") from exc


def _iso_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    return parsed


def canonical_json(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
