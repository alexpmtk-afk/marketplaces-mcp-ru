"""Semantic executor for Ozon orders vs postings.

Orders and postings are deliberately separate:
- order = unique order_number across FBO/FBS posting feeds;
- posting = unique posting_number;
- status/substatus belong to postings, not to an order as a whole.

The executor is read-only and fail-closed for monetary order totals until a
separate approved monetary contract exists.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
import calendar
import re
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .business_query_parser import parse_business_query_dimensions
from .metric_observation import build_metric_observation
from .tools import resolve_named_cabinet

OZON_ORDERS_EXECUTOR_VERSION = "ozon_orders_postings.v1"
OZON_ORDER_METRICS = frozenset({"OZON_ORDERS", "OZON_POSTINGS"})
MOSCOW = ZoneInfo("Europe/Moscow")

_STATUS_MARKERS = {
    "awaiting_packaging": (
        "ожидает упаковки", "ожидают упаковки", "ожидающие упаковки",
        "ожидает сборки", "ожидают сборки",
    ),
    "awaiting_deliver": (
        "ожидает отгрузки", "ожидают отгрузки", "ожидающие отгрузки",
        "готов к отгрузке", "готовы к отгрузке",
    ),
    "delivering": (
        "доставляется", "доставляются", "в доставке", "в пути",
    ),
    "delivered": (
        "доставлен", "доставлены", "доставлено",
    ),
    "cancelled": (
        "отменен", "отменены", "отменено", "отмененные", "отмена",
    ),
}

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "май": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
    "ноябр": 11, "декабр": 12,
}


class SemanticOzonOrdersError(RuntimeError):
    """Raised for deterministic semantic/input failures."""


def _norm(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^a-zа-я0-9_%₽]+", " ", text).split())


def requested_ozon_order_request(question: str) -> dict[str, Any] | None:
    text = _norm(question)
    has_orders = bool(re.search(r"\bзаказ", text))
    has_postings = "отправлени" in text
    if not has_orders and not has_postings:
        return None

    metric_id = "OZON_POSTINGS" if has_postings else "OZON_ORDERS"

    has_fbo = any(marker in text for marker in (" fbo", "фбо"))
    has_fbs = any(marker in text for marker in (" fbs", "фбс"))
    fulfillment = "all"
    if has_fbo and not has_fbs:
        fulfillment = "fbo"
    elif has_fbs and not has_fbo:
        fulfillment = "fbs"

    statuses: list[str] = []
    for status, markers in _STATUS_MARKERS.items():
        if any(_norm(marker) in text for marker in markers):
            statuses.append(status)

    dimensions = parse_business_query_dimensions(question)
    requested_measure = dimensions.get("requested_measure") or "UNITS"
    asks_status = bool(statuses) or any(marker in text for marker in ("статус", "стадия", "этап"))

    return {
        "metric_id": metric_id,
        "fulfillment": fulfillment,
        "statuses": statuses,
        "asks_status": asks_status,
        "requested_measure": requested_measure,
        "period_hint": dimensions.get("period_hint"),
    }


def _canonical_cabinet(ozon: Any, seller: str, question: str) -> tuple[str | None, dict[str, Any] | None]:
    info = ozon.client.config.store.list_cabinets("ozon")
    names = [str(name) for name in info.get("cabinets", [])]
    requested = str(seller or "").strip()

    def aliases(name: str) -> set[str]:
        bare = re.sub(r"^ozon[_\-\s]*", "", name, flags=re.IGNORECASE)
        return {_norm(name), _norm(bare)}

    if requested:
        key = _norm(requested)
        matches = [name for name in names if key in aliases(name)]
        if len(matches) != 1:
            return None, {
                "ok": False,
                "error": "cabinet_not_configured" if not matches else "cabinet_ambiguous",
                "code": "CABINET_NOT_CONFIGURED" if not matches else "CABINET_AMBIGUOUS",
                "retryable": False,
                "complete": False,
                "available_cabinets": names,
            }
        return matches[0], None

    q = _norm(question)
    matches = [name for name in names if any(alias and alias in q for alias in aliases(name))]
    if len(matches) == 1:
        return matches[0], None
    return None, {
        "ok": False,
        "error": "cabinet_required" if not matches else "cabinet_ambiguous",
        "code": "NEEDS_CONTEXT" if not matches else "CABINET_AMBIGUOUS",
        "retryable": False,
        "complete": False,
        "required_context": ["seller/cabinet"],
        "available_cabinets": names,
    }


def _local_date_bounds(start: date, end: date) -> tuple[str, str]:
    start_local = datetime.combine(start, time.min, tzinfo=MOSCOW)
    end_local = datetime.combine(end, time.max.replace(microsecond=0), tzinfo=MOSCOW)
    return (
        start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise SemanticOzonOrdersError(f"Invalid ISO date: {value!r}") from exc


def _named_month_period(question: str, today_local: date) -> tuple[date, date] | None:
    text = _norm(question)
    for stem, month in _MONTHS.items():
        if stem in text:
            year_match = re.search(r"\b(20\d{2})\b", text)
            year = int(year_match.group(1)) if year_match else today_local.year
            last = calendar.monthrange(year, month)[1]
            return date(year, month, 1), date(year, month, last)
    return None


def _resolve_period(
    question: str,
    *,
    date_from: str,
    date_to: str,
    period_hint: str | None,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    if bool(str(date_from or "").strip()) != bool(str(date_to or "").strip()):
        return None, {
            "ok": False,
            "error": "period_incomplete",
            "code": "NEEDS_CONTEXT",
            "retryable": False,
            "complete": False,
            "required_context": ["date_from and date_to"],
        }

    if str(date_from or "").strip() and str(date_to or "").strip():
        start = _parse_iso_date(date_from)
        end = _parse_iso_date(date_to)
    else:
        today_local = datetime.now(MOSCOW).date()
        named = _named_month_period(question, today_local)
        if named is not None:
            start, end = named
        elif period_hint == "TODAY":
            start = end = today_local
        elif period_hint == "YESTERDAY":
            start = end = today_local - timedelta(days=1)
        elif period_hint == "WEEK":
            start = today_local - timedelta(days=today_local.weekday())
            end = today_local
        elif period_hint == "MONTH":
            start = today_local.replace(day=1)
            end = today_local
        else:
            return None, {
                "ok": False,
                "error": "period_required",
                "code": "NEEDS_CONTEXT",
                "retryable": False,
                "complete": False,
                "required_context": ["period/date range"],
                "message": "Ozon order/posting questions require an explicit period.",
            }

    if start > end:
        raise SemanticOzonOrdersError("date_from must be <= date_to")

    since, to = _local_date_bounds(start, end)
    return {
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "since_utc": since,
        "to_utc": to,
        "business_timezone": "Europe/Moscow",
    }, None


def _provider_error(stage: str, response: Any) -> dict[str, Any]:
    details = response if isinstance(response, dict) else {"response_type": type(response).__name__}
    return {
        "ok": False,
        "error": "provider_leg_failed",
        "code": "PROVIDER_LEG_FAILED",
        "retryable": bool(details.get("retryable", False)) if isinstance(details, dict) else False,
        "stage": stage,
        "complete": False,
        "provider_error": details,
    }


def _data(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("data")
    return value if isinstance(value, dict) else response


def _postings(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = _data(response)
    candidates: Any = data.get("postings")
    if not isinstance(candidates, list):
        result = data.get("result")
        if isinstance(result, dict):
            candidates = result.get("postings")
        elif isinstance(result, list):
            candidates = result
    return [item for item in (candidates or []) if isinstance(item, dict)]


def _cursor(response: dict[str, Any]) -> str:
    data = _data(response)
    value = data.get("cursor")
    if value not in (None, ""):
        return str(value)
    result = data.get("result")
    if isinstance(result, dict) and result.get("cursor") not in (None, ""):
        return str(result["cursor"])
    return ""


def _body(kind: str, period: dict[str, str], statuses: list[str], cursor: str) -> dict[str, Any]:
    if kind == "fbo":
        return {
            "filter": {
                "since": period["since_utc"],
                "to": period["to_utc"],
                "statuses": list(statuses),
                "posting_numbers": [],
                "order_numbers": [],
            },
            "limit": 100,
            "cursor": cursor,
            "sort_dir": "ASC",
            "translit": False,
            "with": {
                "analytics_data": False,
                "financial_data": False,
                "legal_info": False,
            },
        }
    return {
        "filter": {
            "since": period["since_utc"],
            "to": period["to_utc"],
            "statuses": list(statuses),
            "delivery_method_ids": [],
            "provider_ids": [],
            "warehouse_ids": [],
            "order_numbers": [],
        },
        "limit": 100,
        "cursor": cursor,
        "sort_dir": "ASC",
        "translit": False,
        "with": {},
    }


async def _fetch_kind(
    ozon: Any,
    *,
    kind: str,
    period: dict[str, str],
    statuses: list[str],
    creds: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None, int]:
    operation_id = "ozon_fbo_list" if kind == "fbo" else "ozon_fbs_list"
    spec = ozon.catalog.get(operation_id)
    if spec is None:
        return None, {
            "ok": False,
            "error": "source_contract_missing",
            "code": "SOURCE_CONTRACT_MISSING",
            "stage": kind,
            "complete": False,
        }, 0

    rows: list[dict[str, Any]] = []
    cursor = ""
    pages = 0
    seen_cursors: set[str] = set()

    while pages < 100:
        response = await ozon.client.call_spec(
            spec,
            json_body=_body(kind, period, statuses, cursor),
            creds_override=creds,
        )
        if not isinstance(response, dict) or response.get("ok") is not True:
            return None, _provider_error(kind, response), pages

        batch = _postings(response)
        rows.extend(batch)
        pages += 1

        next_cursor = _cursor(response)
        if not batch or not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        return None, {
            "ok": False,
            "error": "pagination_limit_exceeded",
            "code": "PAGINATION_LIMIT_EXCEEDED",
            "stage": kind,
            "complete": False,
        }, pages

    return rows, None, pages


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_posting: dict[str, dict[str, Any]] = {}
    for row in rows:
        posting = str(row.get("posting_number") or "").strip()
        if posting:
            by_posting[posting] = row
    return list(by_posting.values())


def _summarize(rows_by_kind: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    all_rows: list[tuple[str, dict[str, Any]]] = []
    for kind, rows in rows_by_kind.items():
        all_rows.extend((kind, row) for row in _dedupe(rows))

    posting_numbers = {
        str(row.get("posting_number"))
        for _, row in all_rows if row.get("posting_number") not in (None, "")
    }
    order_numbers = {
        str(row.get("order_number"))
        for _, row in all_rows if row.get("order_number") not in (None, "")
    }

    order_kinds: dict[str, set[str]] = defaultdict(set)
    order_postings: dict[str, set[str]] = defaultdict(set)
    statuses = Counter()
    substatuses = Counter()
    by_fulfillment: dict[str, dict[str, int]] = {}

    for kind, rows in rows_by_kind.items():
        unique_rows = _dedupe(rows)
        kind_orders = {
            str(row.get("order_number"))
            for row in unique_rows if row.get("order_number") not in (None, "")
        }
        by_fulfillment[kind] = {
            "orders": len(kind_orders),
            "postings": len(unique_rows),
        }

    for kind, row in all_rows:
        order = str(row.get("order_number") or "")
        posting = str(row.get("posting_number") or "")
        if order:
            order_kinds[order].add(kind)
            if posting:
                order_postings[order].add(posting)
        status = str(row.get("status") or "").strip()
        substatus = str(row.get("substatus") or "").strip()
        if status:
            statuses[status] += 1
        if substatus:
            substatuses[substatus] += 1

    return {
        "orders": len(order_numbers),
        "postings": len(posting_numbers),
        "by_fulfillment": by_fulfillment,
        "by_status": dict(sorted(statuses.items())),
        "by_substatus": dict(sorted(substatuses.items())),
        "cross_fulfillment_orders": sum(1 for kinds in order_kinds.values() if len(kinds) > 1),
        "multi_posting_orders": sum(1 for postings in order_postings.values() if len(postings) > 1),
    }


async def execute_ozon_orders_question(
    ozon: Any,
    *,
    question: str,
    seller: str = "",
    date_from: str = "",
    date_to: str = "",
) -> dict[str, Any]:
    request = requested_ozon_order_request(question)
    if request is None:
        raise SemanticOzonOrdersError("No Ozon order/posting semantic was recognized.")

    if request["requested_measure"] != "UNITS":
        return {
            "ok": False,
            "error": "ozon_order_amount_not_approved",
            "code": "OZON_ORDER_AMOUNT_NOT_APPROVED",
            "retryable": False,
            "complete": False,
            "requested_metric": request["metric_id"],
            "message": (
                "Monetary Ozon order totals are not approved by this semantic contract. "
                "Use the order/posting count contract only."
            ),
        }

    if request["metric_id"] == "OZON_ORDERS" and request["asks_status"]:
        return {
            "ok": False,
            "error": "order_status_is_posting_scope",
            "code": "ORDER_STATUS_IS_POSTING_SCOPE",
            "retryable": False,
            "complete": False,
            "requested_metric": "OZON_ORDERS",
            "recommended_metric": "OZON_POSTINGS",
            "message": (
                "Ozon status/substatus belongs to posting_number. One order_number can have "
                "several postings with different statuses, so a single order status is ambiguous."
            ),
        }

    period, period_error = _resolve_period(
        question,
        date_from=date_from,
        date_to=date_to,
        period_hint=request.get("period_hint"),
    )
    if period_error:
        return period_error
    assert period is not None

    cabinet, cabinet_error = _canonical_cabinet(ozon, seller, question)
    if cabinet_error:
        return cabinet_error
    assert cabinet is not None

    creds, named_error = resolve_named_cabinet(ozon.client, cabinet)
    if named_error:
        return {**named_error, "complete": False}
    assert creds is not None

    kinds = ["fbo", "fbs"] if request["fulfillment"] == "all" else [request["fulfillment"]]
    rows_by_kind: dict[str, list[dict[str, Any]]] = {}
    pages_by_kind: dict[str, int] = {}

    for kind in kinds:
        rows, error, pages = await _fetch_kind(
            ozon,
            kind=kind,
            period=period,
            statuses=request["statuses"] if request["metric_id"] == "OZON_POSTINGS" else [],
            creds=creds,
        )
        if error:
            return {
                **error,
                "cabinet": cabinet,
                "period": period,
                "fulfillment_scope": request["fulfillment"],
            }
        rows_by_kind[kind] = rows or []
        pages_by_kind[kind] = pages

    summary = _summarize(rows_by_kind)

    metric_id = request["metric_id"]
    value = summary["orders"] if metric_id == "OZON_ORDERS" else summary["postings"]
    source_field = "order_number" if metric_id == "OZON_ORDERS" else "posting_number"

    observation = build_metric_observation(
        metric_id=metric_id,
        value=value,
        unit="UNITS",
        marketplace="ozon",
        source_name="ozon_order_postings",
        source_field=source_field,
    ).to_dict()

    result: dict[str, Any] = {
        "ok": True,
        "complete": True,
        "executor": OZON_ORDERS_EXECUTOR_VERSION,
        "marketplace": "ozon",
        "cabinet": cabinet,
        "metric_id": metric_id,
        "value": value,
        "unit": "UNITS",
        "period": period,
        "fulfillment_scope": request["fulfillment"],
        "status_filter": list(request["statuses"]),
        "orders": summary["orders"],
        "postings": summary["postings"],
        "by_fulfillment": summary["by_fulfillment"],
        "cross_fulfillment_orders": summary["cross_fulfillment_orders"],
        "multi_posting_orders": summary["multi_posting_orders"],
        "metric_observations": [observation],
        "pages_by_fulfillment": pages_by_kind,
        "provenance": {
            "fbo_source": "ozon_fbo_list" if "fbo" in kinds else None,
            "fbs_source": "ozon_fbs_list" if "fbs" in kinds else None,
            "order_key": "order_number",
            "posting_key": "posting_number",
            "period_field_semantics": "provider posting list since/to; in_process_at is UTC",
            "named_cabinet": True,
        },
        "guardrails": [
            "OZON_ORDERS is count(distinct order_number) across the selected FBO/FBS feeds.",
            "FBO and FBS order counts overlap and must not be added without deduplication.",
            "OZON_POSTINGS is count(distinct posting_number).",
            "status/substatus belongs to postings, not to an order as a whole.",
            "Monetary Ozon order totals are outside this approved contract.",
        ],
        "knowledge_catalog_version": "marketplace_knowledge_catalog.v1",
    }

    if metric_id == "OZON_POSTINGS":
        result["by_status"] = summary["by_status"]
        result["by_substatus"] = summary["by_substatus"]

    return result
