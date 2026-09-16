"""Top-level source-family and execution planner for natural marketplace requests.

This layer sits above metric-specific Semantic Core. It decides *where* a
request is allowed to look before a lower-level executor chooses fields or API
methods. It is deliberately read-only and fail-closed: missing archive history
is never replaced with today's live snapshot or a merely similar report.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from .business_query_parser import normalize_business_text
from .semantic_resolver import resolve_semantic_question

SOURCE_CANONICAL_ARCHIVE = "CANONICAL_ARCHIVE"
SOURCE_LIVE_CABINET_API = "LIVE_CABINET_API"
SOURCE_PUBLIC_MARKETPLACE = "PUBLIC_MARKETPLACE_SOURCE"
SOURCE_SYSTEM_INTERNAL = "SYSTEM_INTERNAL"
SOURCE_HYBRID = "HYBRID"
SOURCE_UNAVAILABLE = "UNAVAILABLE"

EXECUTION_READY = "READY"
EXECUTION_READY_WITH_GATES = "READY_WITH_GATES"
EXECUTION_NEEDS_CONTEXT = "NEEDS_CONTEXT"
EXECUTION_BLOCKED = "BLOCKED"

WB_OPERATIONAL_ORDERS_RETENTION_DAYS = 90

# Availability facts describe real populated/approved sources, not code presence.
SOURCE_AVAILABILITY_FACTS: dict[str, Any] = {
    "wb_orders_historical_archive": False,
    "wb_stock_historical_archive": False,
    "wb_weekly_finance_archive": True,
    "wb_advertising_archive_v1": True,
    "wb_current_orders_api": True,
    "wb_current_stock_api": True,
    "public_card_monitor": True,
    "general_public_web_fetcher": False,
}

_GATE_BY_STATUS = {
    "FULL_COVERAGE_REQUIRED": "FULL_COVERAGE",
    "AVAILABLE_WITH_LIMITATION": "LIVE_RETENTION_WINDOW",
    "WITHIN_PROVIDER_RETENTION_ONLY": "LIVE_RETENTION_WINDOW",
    "AVAILABLE_IF_MONITORED": "MONITORING_COVERAGE",
}


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _day(value: str | None) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _infer_marketplace(question: str, marketplace: str) -> str:
    explicit = str(marketplace or "").strip().lower()
    if explicit in {"wb", "wildberries", "вайлдберриз"}:
        return "wb"
    if explicit in {"ozon", "озон"}:
        return "ozon"
    text = normalize_business_text(question)
    if "wildberries" in text or "вайлдберриз" in text or " wb " in f" {text} ":
        return "wb"
    if "ozon" in text or "озон" in text:
        return "ozon"
    return ""


def _time_mode(question: str, date_from: str, date_to: str, *, today: date) -> str:
    start = _day(date_from)
    end = _day(date_to)
    if start and end:
        if start > end:
            return "INVALID"
        if start == today and end == today:
            return "CURRENT"
        if end < today:
            return "HISTORICAL"
        if start < today <= end:
            return "MIXED"
        if start > today:
            return "FUTURE"

    text = normalize_business_text(question)
    current_markers = (
        "сейчас", "сегодня", "текущ", "актуальн", "на данный момент",
    )
    historical_markers = (
        "вчера", "прошл", "истори", "за период", "за месяц", "за неделю",
        "в август", "в июл", "в июн", "в мае", "в апрел", "в март",
        "в феврал", "в январ", "в сентябр", "в октябр", "в ноябр", "в декабр",
    )
    if any(marker in text for marker in current_markers):
        return "CURRENT"
    if any(marker in text for marker in historical_markers):
        return "HISTORICAL"
    return "UNSPECIFIED"


def _is_system_question(text: str) -> bool:
    markers = (
        "какие данные есть", "какие данные доступны", "где хранятся данные",
        "какие источники", "какой источник", "покрытие базы", "покрытие данных",
        "статус mcp", "архитектур", "системная карта", "что есть в базе",
        "есть ли история", "есть ли данные",
    )
    return any(marker in text for marker in markers)


def _is_public_question(text: str) -> bool:
    markers = (
        "на сайте", "карточк", "цена для покупателя", "публичная цена",
        "рейтинг товара", "отзыв", "позиция в поиске", "выдач",
        "страница товара", "витрин",
    )
    return any(marker in text for marker in markers)


def _is_rules_or_public_reference(text: str) -> bool:
    markers = (
        "правила wildberries", "правила ozon", "правила вайлдберриз",
        "официальная справка", "документация маркетплейса", "новые правила",
        "условия площадки",
    )
    return any(marker in text for marker in markers)


def _is_hybrid_question(text: str) -> bool:
    ad = any(x in text for x in ("реклам", "ддр", "drr", "roas", "расходы на продвиж"))
    business = any(x in text for x in ("продаж", "заказ", "выкуп", "маржин", "прибыл", "финанс"))
    public = any(x in text for x in ("цена на сайте", "цена для покупателя", "карточк"))
    private = any(x in text for x in ("остат", "заказ", "продаж", "реклам", "финанс"))
    return (ad and business) or (public and private)


def _semantic_target(resolution: dict[str, Any] | None) -> dict[str, Any] | None:
    if not resolution:
        return None
    resolution_type = str(resolution.get("resolution_type") or "")
    if resolution_type == "BUSINESS_METRIC":
        return {
            "type": "BUSINESS_METRIC",
            "id": resolution.get("metric_id"),
        }
    if resolution_type == "CAPABILITY":
        return {
            "type": "CAPABILITY",
            "id": resolution.get("capability_id"),
        }
    if resolution_type:
        return {
            "type": resolution_type,
            "id": resolution.get("route_id"),
        }
    return None


def _single_leg_purpose(
    source_family: str,
    semantic_resolution: dict[str, Any] | None,
) -> str:
    target = _semantic_target(semantic_resolution)
    if target and target.get("id"):
        return str(target["id"]).lower()
    if source_family == SOURCE_SYSTEM_INTERNAL:
        return "system_internal"
    if source_family == SOURCE_PUBLIC_MARKETPLACE:
        return "public_marketplace"
    return "request"


def _leg_executor(
    *,
    purpose: str,
    source_family: str,
    source_status: str,
    time_mode: str,
    explicit_downstream: str | None = None,
) -> str | None:
    if explicit_downstream:
        return explicit_downstream
    if source_family == SOURCE_UNAVAILABLE:
        return None
    if source_status in {
        "NOT_CONNECTED_ON_DEMAND",
        "CONTRACT_REQUIRED",
        "SOURCE_CLASS_KNOWN_SEMANTIC_EXECUTOR_NOT_YET_WIRED",
        "OZON_HISTORICAL_SOURCE_NOT_APPROVED",
        "CURRENT_PERFORMANCE_EXECUTOR_NOT_APPROVED",
        "CURRENT_SOURCE_NOT_APPROVED",
        "SOURCE_CLASS_KNOWN_EXECUTOR_NOT_APPROVED",
        "UNRESOLVED_SOURCE_CLASS",
        "INVALID_PERIOD",
    }:
        return None
    if purpose in {"advertising", "sales_or_finance", "orders", "current_stock"}:
        return "marketplace_business_query"
    if purpose == "public_card":
        return "card_monitor_get_history" if time_mode == "HISTORICAL" else "card_monitor_get_latest"
    return None


def _leg_execution_status(
    *,
    source_family: str,
    source_status: str,
    executor: str | None,
    required_context: list[str],
) -> str:
    if required_context:
        return EXECUTION_NEEDS_CONTEXT
    if source_family == SOURCE_UNAVAILABLE:
        return EXECUTION_BLOCKED
    if executor is None:
        return EXECUTION_BLOCKED
    if source_status in _GATE_BY_STATUS:
        return EXECUTION_READY_WITH_GATES
    return EXECUTION_READY


def _normalize_execution_leg(
    *,
    index: int,
    purpose: str,
    source_family: str,
    source_status: str,
    time_mode: str,
    marketplace: str,
    required_context: list[str],
    forbidden_substitutes: list[str],
    semantic_resolution: dict[str, Any] | None,
    explicit_downstream: str | None = None,
) -> dict[str, Any]:
    private_source = source_family in {SOURCE_CANONICAL_ARCHIVE, SOURCE_LIVE_CABINET_API}
    leg_context = list(required_context) if private_source else []
    executor = _leg_executor(
        purpose=purpose,
        source_family=source_family,
        source_status=source_status,
        time_mode=time_mode,
        explicit_downstream=explicit_downstream,
    )
    status = _leg_execution_status(
        source_family=source_family,
        source_status=source_status,
        executor=executor,
        required_context=leg_context,
    )
    return {
        "leg_id": f"leg-{index}-{purpose}",
        "purpose": purpose,
        "required": True,
        "marketplace": marketplace or None,
        "time_mode": time_mode,
        "source_family": source_family,
        "source_status": source_status,
        "executor": executor,
        "execution_status": status,
        "coverage_gate": _GATE_BY_STATUS.get(source_status),
        "required_context": leg_context,
        "forbidden_substitutes": list(forbidden_substitutes),
        "semantic_target": _semantic_target(semantic_resolution),
        "depends_on": [],
    }


def _join_strategy(question: str, *, multi_source: bool) -> str:
    if not multi_source:
        return "NONE"
    text = normalize_business_text(question)
    if "сравн" in text or "сопостав" in text or "разниц" in text:
        return "SIDE_BY_SIDE_COMPARISON"
    return "MULTI_SOURCE_SYNTHESIS"


def _build_execution_plan(
    *,
    question: str,
    marketplace: str,
    time_mode: str,
    source_family: str,
    source_status: str,
    downstream: str | None,
    required_context: list[str],
    forbidden_substitutes: list[str],
    legacy_legs: list[dict[str, Any]],
    semantic_resolution: dict[str, Any] | None,
) -> dict[str, Any]:
    execution_legs: list[dict[str, Any]] = []
    if legacy_legs:
        for index, leg in enumerate(legacy_legs, start=1):
            execution_legs.append(_normalize_execution_leg(
                index=index,
                purpose=str(leg.get("purpose") or f"source_{index}"),
                source_family=str(leg.get("source_family") or SOURCE_UNAVAILABLE),
                source_status=str(leg.get("source_status") or leg.get("status") or "UNKNOWN"),
                time_mode=time_mode,
                marketplace=marketplace,
                required_context=list(leg.get("required_context") or required_context),
                forbidden_substitutes=list(leg.get("forbidden_substitutes") or forbidden_substitutes),
                semantic_resolution=leg.get("semantic_resolution") or semantic_resolution,
                explicit_downstream=leg.get("downstream_handler"),
            ))
    else:
        execution_legs.append(_normalize_execution_leg(
            index=1,
            purpose=_single_leg_purpose(source_family, semantic_resolution),
            source_family=source_family,
            source_status=source_status,
            time_mode=time_mode,
            marketplace=marketplace,
            required_context=required_context,
            forbidden_substitutes=forbidden_substitutes,
            semantic_resolution=semantic_resolution,
            explicit_downstream=downstream,
        ))

    statuses = {leg["execution_status"] for leg in execution_legs}
    if EXECUTION_NEEDS_CONTEXT in statuses:
        overall_status = EXECUTION_NEEDS_CONTEXT
    elif EXECUTION_BLOCKED in statuses:
        overall_status = EXECUTION_BLOCKED
    elif EXECUTION_READY_WITH_GATES in statuses:
        overall_status = EXECUTION_READY_WITH_GATES
    else:
        overall_status = EXECUTION_READY

    blockers: list[dict[str, Any]] = []
    for leg in execution_legs:
        if leg["execution_status"] == EXECUTION_NEEDS_CONTEXT:
            blockers.append({
                "leg_id": leg["leg_id"],
                "type": "MISSING_CONTEXT",
                "details": leg["required_context"],
            })
        elif leg["execution_status"] == EXECUTION_BLOCKED:
            blockers.append({
                "leg_id": leg["leg_id"],
                "type": "SOURCE_OR_EXECUTOR_UNAVAILABLE",
                "details": leg["source_status"],
            })

    multi_source = len(execution_legs) > 1 or source_family == SOURCE_HYBRID
    return {
        "version": "marketplace_execution_plan.v2",
        "mode": "MULTI_SOURCE" if multi_source else "SINGLE_SOURCE",
        "status": overall_status,
        "can_start_execution": overall_status in {EXECUTION_READY, EXECUTION_READY_WITH_GATES},
        "can_answer_without_more_validation": overall_status == EXECUTION_READY,
        "execution_order": [leg["leg_id"] for leg in execution_legs],
        "independent_legs_can_run_in_parallel": multi_source,
        "legs": execution_legs,
        "blockers": blockers,
        "join": {
            "strategy": _join_strategy(question, multi_source=multi_source),
            "requires_all_required_legs": True,
            "allow_partial_answer": False,
            "missing_required_leg_behavior": "FAIL_CLOSED",
            "arithmetic_allowed_without_explicit_semantic_contract": False,
            "provenance_required": True,
        },
    }


def _base_plan(
    *,
    question: str,
    marketplace: str,
    time_mode: str,
    source_family: str,
    source_status: str,
    downstream: str | None,
    reason: str,
    seller: str = "",
    date_from: str = "",
    date_to: str = "",
    required_context: list[str] | None = None,
    forbidden_substitutes: list[str] | None = None,
    legs: list[dict[str, Any]] | None = None,
    semantic_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    required = required_context or []
    forbidden = forbidden_substitutes or []
    legacy_legs = legs or []
    execution_plan = _build_execution_plan(
        question=question,
        marketplace=marketplace,
        time_mode=time_mode,
        source_family=source_family,
        source_status=source_status,
        downstream=downstream,
        required_context=required,
        forbidden_substitutes=forbidden,
        legacy_legs=legacy_legs,
        semantic_resolution=semantic_resolution,
    )
    return {
        "ok": source_family != SOURCE_UNAVAILABLE,
        "planner": "marketplace_query_plan.v2",
        "question": question,
        "marketplace": marketplace or None,
        "time_mode": time_mode,
        "request_scope": {
            "seller_provided": bool(str(seller or "").strip()),
            "date_from": str(date_from or "") or None,
            "date_to": str(date_to or "") or None,
        },
        "source_family": source_family,
        "source_status": source_status,
        "downstream_handler": downstream,
        "reason": reason,
        "required_context": required,
        "forbidden_substitutes": forbidden,
        "legs": legacy_legs,
        "execution_plan": execution_plan,
        "semantic_resolution": semantic_resolution,
        "availability_facts": {
            "orders_historical_archive": SOURCE_AVAILABILITY_FACTS["wb_orders_historical_archive"],
            "stock_historical_archive": SOURCE_AVAILABILITY_FACTS["wb_stock_historical_archive"],
        },
    }


def plan_marketplace_request(
    question: str,
    *,
    marketplace: str = "",
    seller: str = "",
    date_from: str = "",
    date_to: str = "",
    today: date | None = None,
) -> dict[str, Any]:
    """Return a deterministic source-family and execution plan without provider reads."""
    question = str(question or "").strip()
    if not question:
        raise ValueError("question must be a non-empty string")

    today = today or date.today()
    text = normalize_business_text(question)
    market = _infer_marketplace(question, marketplace)
    mode = _time_mode(question, date_from, date_to, today=today)

    def make_plan(**kwargs: Any) -> dict[str, Any]:
        return _base_plan(
            seller=seller,
            date_from=date_from,
            date_to=date_to,
            **kwargs,
        )

    if mode in {"INVALID", "FUTURE"}:
        return make_plan(
            question=question,
            marketplace=market,
            time_mode=mode,
            source_family=SOURCE_UNAVAILABLE,
            source_status="INVALID_PERIOD",
            downstream=None,
            reason="Requested dates are invalid or lie entirely in the future.",
        )

    if _is_system_question(text):
        handler = "marketplace_data_catalog" if any(
            marker in text for marker in ("данные", "источник", "покрытие", "баз")
        ) else "marketplace_system_map"
        return make_plan(
            question=question,
            marketplace=market,
            time_mode=mode,
            source_family=SOURCE_SYSTEM_INTERNAL,
            source_status="AVAILABLE",
            downstream=handler,
            reason="The question asks what the MCP itself knows, stores, or can route; no marketplace data call is needed.",
        )

    if _is_rules_or_public_reference(text):
        return make_plan(
            question=question,
            marketplace=market,
            time_mode=mode,
            source_family=SOURCE_PUBLIC_MARKETPLACE,
            source_status="NOT_CONNECTED_ON_DEMAND",
            downstream=None,
            reason="This requires an official public marketplace page/document source. The MCP has no general on-demand public-web fetcher yet.",
            forbidden_substitutes=["seller cabinet API", "canonical business archive"],
        )

    if _is_hybrid_question(text):
        legs: list[dict[str, Any]] = []
        if any(x in text for x in ("реклам", "ддр", "drr", "roas", "расходы на продвиж")):
            legs.append({
                "purpose": "advertising",
                "source_family": SOURCE_CANONICAL_ARCHIVE if mode == "HISTORICAL" else SOURCE_LIVE_CABINET_API,
                "status": "FULL_COVERAGE_REQUIRED" if mode == "HISTORICAL" else "CONTRACT_REQUIRED",
                "downstream_handler": "marketplace_business_query" if mode == "HISTORICAL" else None,
            })
        if any(x in text for x in ("продаж", "выкуп", "финанс", "прибыл", "маржин")):
            legs.append({
                "purpose": "sales_or_finance",
                "source_family": SOURCE_CANONICAL_ARCHIVE if mode == "HISTORICAL" else SOURCE_LIVE_CABINET_API,
                "status": "FULL_COVERAGE_REQUIRED" if mode == "HISTORICAL" else "CONTRACT_REQUIRED",
                "downstream_handler": "marketplace_business_query" if mode == "HISTORICAL" else None,
            })
        if "заказ" in text:
            start = _day(date_from)
            oldest_live = today - timedelta(days=WB_OPERATIONAL_ORDERS_RETENTION_DAYS - 1)
            if start and start < oldest_live:
                legs.append({
                    "purpose": "orders",
                    "source_family": SOURCE_UNAVAILABLE,
                    "status": "NO_VERIFIED_ARCHIVE_HISTORY",
                    "forbidden_substitutes": ["weekly finance order fields", "unverified historical store"],
                })
            else:
                legs.append({
                    "purpose": "orders",
                    "source_family": SOURCE_LIVE_CABINET_API,
                    "status": "AVAILABLE_WITH_LIMITATION" if mode == "HISTORICAL" else "AVAILABLE",
                    "downstream_handler": "marketplace_business_query",
                })
        if "остат" in text:
            if mode == "HISTORICAL":
                legs.append({
                    "purpose": "current_stock",
                    "source_family": SOURCE_UNAVAILABLE,
                    "status": "HISTORICAL_SOURCE_ABSENT",
                    "forbidden_substitutes": ["today's stock snapshot", "weekly finance report"],
                })
            else:
                legs.append({
                    "purpose": "current_stock",
                    "source_family": SOURCE_LIVE_CABINET_API,
                    "status": "AVAILABLE",
                    "downstream_handler": "marketplace_business_query",
                })
        if any(x in text for x in ("цена на сайте", "цена для покупателя", "карточк")):
            legs.append({
                "purpose": "public_card",
                "source_family": SOURCE_PUBLIC_MARKETPLACE,
                "status": "AVAILABLE_IF_MONITORED",
                "downstream_handler": "card_monitor_get_history" if mode == "HISTORICAL" else "card_monitor_get_latest",
                "forbidden_substitutes": ["seller cabinet price treated as buyer-visible site price"],
            })
        hybrid_context: list[str] = []
        if not market:
            hybrid_context.append("marketplace")
        if not seller and any(leg.get("source_family") in {SOURCE_CANONICAL_ARCHIVE, SOURCE_LIVE_CABINET_API} for leg in legs):
            hybrid_context.append("seller/cabinet where a private leg is required")
        return make_plan(
            question=question,
            marketplace=market,
            time_mode=mode,
            source_family=SOURCE_HYBRID,
            source_status="MULTI_SOURCE_PLAN",
            downstream="marketplace_business_query",
            reason="The question combines facts that belong to different data domains; each leg must be validated separately before joining.",
            required_context=hybrid_context,
            forbidden_substitutes=["one source presented as complete for all legs", "partial leg silently treated as complete"],
            legs=legs,
        )

    if _is_public_question(text):
        return make_plan(
            question=question,
            marketplace=market,
            time_mode=mode,
            source_family=SOURCE_PUBLIC_MARKETPLACE,
            source_status="AVAILABLE_IF_MONITORED",
            downstream="card_monitor_get_latest" if mode != "HISTORICAL" else "card_monitor_get_history",
            reason="The question concerns a public marketplace card/price observation, not private seller-cabinet accounting.",
            forbidden_substitutes=["seller cabinet price treated as buyer-visible site price"],
        )

    private_business_markers = (
        "заказ", "остат", "продаж", "возврат", "выкуп", "реклам",
        "комис", "логист", "штраф", "хранен", "начислен", "финанс",
    )
    looks_private_business = any(marker in text for marker in private_business_markers)

    if market == "ozon" and looks_private_business:
        if mode in {"CURRENT", "UNSPECIFIED"}:
            return make_plan(
                question=question,
                marketplace="ozon",
                time_mode=mode,
                source_family=SOURCE_LIVE_CABINET_API,
                source_status="SOURCE_CLASS_KNOWN_SEMANTIC_EXECUTOR_NOT_YET_WIRED",
                downstream=None,
                reason="This is private Ozon seller data. The correct source class is the named Ozon cabinet/API, but the upper semantic executor is not yet wired for this metric.",
                required_context=[] if seller else ["seller/cabinet"],
                forbidden_substitutes=["public Ozon site", "WB archive", "another seller cabinet"],
            )
        return make_plan(
            question=question,
            marketplace="ozon",
            time_mode=mode,
            source_family=SOURCE_UNAVAILABLE,
            source_status="OZON_HISTORICAL_SOURCE_NOT_APPROVED",
            downstream=None,
            reason="The request is historical private Ozon data, but no approved canonical historical source contract is registered in the upper router yet.",
            required_context=[] if seller else ["seller/cabinet"],
            forbidden_substitutes=["current cabinet snapshot", "public Ozon site", "WB archive"],
        )

    if not market and looks_private_business:
        return make_plan(
            question=question,
            marketplace="",
            time_mode=mode,
            source_family=SOURCE_UNAVAILABLE,
            source_status="MARKETPLACE_REQUIRED",
            downstream=None,
            reason="Private business data needs a marketplace identity before the server can choose the correct cabinet/archive family.",
            required_context=["marketplace"] + ([] if seller else ["seller/cabinet"]),
            forbidden_substitutes=["guessing WB or Ozon from unrelated context"],
        )

    resolution: dict[str, Any] | None = None
    if market in {"", "wb"}:
        resolution = resolve_semantic_question(question)

    if resolution and resolution.get("resolution_type") == "BUSINESS_METRIC":
        metric_id = str(resolution.get("metric_id") or "")
        if metric_id == "CURRENT_STOCK":
            if mode == "HISTORICAL":
                return make_plan(
                    question=question,
                    marketplace=market or "wb",
                    time_mode=mode,
                    source_family=SOURCE_UNAVAILABLE,
                    source_status="HISTORICAL_SOURCE_ABSENT",
                    downstream=None,
                    reason="Current stock exists only as a live snapshot; no approved historical stock database is populated.",
                    forbidden_substitutes=["today's stock snapshot", "weekly finance report"],
                    semantic_resolution=resolution,
                )
            return make_plan(
                question=question,
                marketplace=market or "wb",
                time_mode=mode,
                source_family=SOURCE_LIVE_CABINET_API,
                source_status="AVAILABLE",
                downstream="marketplace_business_query",
                reason="Current seller stock is private operational data and must come from the named WB cabinet/API.",
                required_context=[] if seller else ["seller/cabinet"],
                forbidden_substitutes=["historical archive", "public product-card availability"],
                semantic_resolution=resolution,
            )

        if metric_id == "ORDERS":
            start = _day(date_from)
            end = _day(date_to)
            oldest_live = today - timedelta(days=WB_OPERATIONAL_ORDERS_RETENTION_DAYS - 1)
            if start and start < oldest_live:
                return make_plan(
                    question=question,
                    marketplace=market or "wb",
                    time_mode=mode,
                    source_family=SOURCE_UNAVAILABLE,
                    source_status="NO_VERIFIED_ARCHIVE_HISTORY",
                    downstream=None,
                    reason="There is no verified populated historical orders archive, and the requested period is older than the operational WB API window.",
                    forbidden_substitutes=["weekly finance orderDt/orderUid", "analytics funnel", "unverified YDB history scaffolding"],
                    semantic_resolution=resolution,
                )
            status = "AVAILABLE_WITH_LIMITATION" if mode == "HISTORICAL" else "AVAILABLE"
            return make_plan(
                question=question,
                marketplace=market or "wb",
                time_mode=mode,
                source_family=SOURCE_LIVE_CABINET_API,
                source_status=status,
                downstream="marketplace_business_query",
                reason="Orders are not backed by a verified historical archive. Recent periods may be read only from the operational WB cabinet API within its retention window.",
                required_context=([] if seller else ["seller/cabinet"]) + ([] if (start and end) else ["exact period"]),
                forbidden_substitutes=["weekly finance order fields", "unverified historical store"],
                semantic_resolution=resolution,
            )

    if resolution and resolution.get("resolution_type") == "CAPABILITY":
        capability_id = str(resolution.get("capability_id") or "")
        if capability_id == "advertising_performance":
            if mode == "CURRENT":
                return make_plan(
                    question=question,
                    marketplace=market or "wb",
                    time_mode=mode,
                    source_family=SOURCE_UNAVAILABLE,
                    source_status="CURRENT_PERFORMANCE_EXECUTOR_NOT_APPROVED",
                    downstream=None,
                    reason="Historical advertising analytics are archived, but current-day advertising performance has no approved Semantic executor.",
                    forbidden_substitutes=["closed-period advertising archive treated as current"],
                    semantic_resolution=resolution,
                )
            return make_plan(
                question=question,
                marketplace=market or "wb",
                time_mode=mode,
                source_family=SOURCE_CANONICAL_ARCHIVE,
                source_status="FULL_COVERAGE_REQUIRED",
                downstream="marketplace_business_query",
                reason="Approved historical advertising analytics must come from the canonical advertising archive after complete coverage is proven.",
                required_context=([] if seller else ["seller/cabinet"]) + ([] if (_day(date_from) and _day(date_to)) else ["exact period"]),
                forbidden_substitutes=["live campaign state", "weekly finance report"],
                semantic_resolution=resolution,
            )

        return make_plan(
            question=question,
            marketplace=market or "wb",
            time_mode=mode,
            source_family=SOURCE_CANONICAL_ARCHIVE if mode != "CURRENT" else SOURCE_UNAVAILABLE,
            source_status="FULL_COVERAGE_REQUIRED" if mode != "CURRENT" else "CURRENT_SOURCE_NOT_APPROVED",
            downstream="marketplace_business_query" if mode != "CURRENT" else None,
            reason=(
                "This approved historical business capability belongs to the canonical archive and requires full coverage."
                if mode != "CURRENT"
                else "This capability is historical in the current contract; a suitable live source has not been approved."
            ),
            required_context=([] if seller else ["seller/cabinet"]) + ([] if (_day(date_from) and _day(date_to)) else ["exact period"]),
            forbidden_substitutes=["current data silently used for a historical calculation", "semantically similar report"],
            semantic_resolution=resolution,
        )

    if any(x in text for x in ("тариф", "коэффициент склада", "комисси")) and mode == "CURRENT":
        return make_plan(
            question=question,
            marketplace=market,
            time_mode=mode,
            source_family=SOURCE_LIVE_CABINET_API,
            source_status="SOURCE_CLASS_KNOWN_EXECUTOR_NOT_APPROVED",
            downstream=None,
            reason="The request is current seller/platform state, so archive data is the wrong class of source even though a concrete live executor is not yet approved.",
            required_context=[] if seller else ["seller/cabinet"],
            forbidden_substitutes=["historical weekly-report coefficients"],
            semantic_resolution=resolution,
        )

    return make_plan(
        question=question,
        marketplace=market,
        time_mode=mode,
        source_family=SOURCE_UNAVAILABLE,
        source_status="UNRESOLVED_SOURCE_CLASS",
        downstream=None,
        reason="The upper layer cannot yet choose a source family safely from this wording.",
        forbidden_substitutes=["guessing a similar metric or source"],
        semantic_resolution=resolution,
    )


def register_request_source_router_tool(combined: Any) -> None:
    """Expose the canonical first planning step for ChatGPT/Codex requests."""

    @combined.tool(
        name="marketplace_query_plan",
        annotations={
            "title": "Marketplace top-level request execution plan",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_query_plan(
        question: str,
        marketplace: str = "",
        seller: str = "",
        date_from: str = "",
        date_to: str = "",
    ) -> str:
        """Plan where a marketplace question may get data and how it may execute.

        Call this before choosing archive, seller-cabinet/API, public-card/site,
        or system-internal tools. It returns source legs, execution gates,
        blockers and join policy, but does not fetch business data itself.
        """
        return _j(plan_marketplace_request(
            question,
            marketplace=marketplace,
            seller=seller,
            date_from=date_from,
            date_to=date_to,
        ))
