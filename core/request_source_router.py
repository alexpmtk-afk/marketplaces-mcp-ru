"""Top-level source-family planner for natural marketplace requests.

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


def _base_plan(
    *,
    question: str,
    marketplace: str,
    time_mode: str,
    source_family: str,
    source_status: str,
    downstream: str | None,
    reason: str,
    required_context: list[str] | None = None,
    forbidden_substitutes: list[str] | None = None,
    legs: list[dict[str, Any]] | None = None,
    semantic_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": source_family != SOURCE_UNAVAILABLE,
        "planner": "marketplace_query_plan.v1",
        "question": question,
        "marketplace": marketplace or None,
        "time_mode": time_mode,
        "source_family": source_family,
        "source_status": source_status,
        "downstream_handler": downstream,
        "reason": reason,
        "required_context": required_context or [],
        "forbidden_substitutes": forbidden_substitutes or [],
        "legs": legs or [],
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
    """Return a deterministic source-family plan without reading provider data."""
    question = str(question or "").strip()
    if not question:
        raise ValueError("question must be a non-empty string")

    today = today or date.today()
    text = normalize_business_text(question)
    market = _infer_marketplace(question, marketplace)
    mode = _time_mode(question, date_from, date_to, today=today)

    if mode in {"INVALID", "FUTURE"}:
        return _base_plan(
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
        return _base_plan(
            question=question,
            marketplace=market,
            time_mode=mode,
            source_family=SOURCE_SYSTEM_INTERNAL,
            source_status="AVAILABLE",
            downstream=handler,
            reason="The question asks what the MCP itself knows, stores, or can route; no marketplace data call is needed.",
        )

    if _is_rules_or_public_reference(text):
        return _base_plan(
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
            })
        if any(x in text for x in ("продаж", "выкуп", "финанс", "прибыл", "маржин")):
            legs.append({
                "purpose": "sales_or_finance",
                "source_family": SOURCE_CANONICAL_ARCHIVE if mode == "HISTORICAL" else SOURCE_LIVE_CABINET_API,
                "status": "FULL_COVERAGE_REQUIRED" if mode == "HISTORICAL" else "CONTRACT_REQUIRED",
            })
        if "заказ" in text:
            legs.append({
                "purpose": "orders",
                "source_family": SOURCE_LIVE_CABINET_API,
                "status": "WITHIN_PROVIDER_RETENTION_ONLY",
            })
        if any(x in text for x in ("цена на сайте", "цена для покупателя", "карточк")):
            legs.append({
                "purpose": "public_card",
                "source_family": SOURCE_PUBLIC_MARKETPLACE,
                "status": "AVAILABLE_IF_MONITORED",
            })
        return _base_plan(
            question=question,
            marketplace=market,
            time_mode=mode,
            source_family=SOURCE_HYBRID,
            source_status="MULTI_SOURCE_PLAN",
            downstream="marketplace_business_query",
            reason="The question combines facts that belong to different data domains; each leg must be validated separately before joining.",
            required_context=[] if seller else ["seller/cabinet where a private leg is required"],
            forbidden_substitutes=["one source presented as complete for all legs", "partial leg silently treated as complete"],
            legs=legs,
        )

    if _is_public_question(text):
        return _base_plan(
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
            return _base_plan(
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
        return _base_plan(
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
        return _base_plan(
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
                return _base_plan(
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
            return _base_plan(
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
                return _base_plan(
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
            return _base_plan(
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
                return _base_plan(
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
            return _base_plan(
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

        return _base_plan(
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
        return _base_plan(
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

    return _base_plan(
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
            "title": "Marketplace top-level request source plan",
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
        """Plan where a marketplace question is allowed to get its data.

        Call this before choosing archive, seller-cabinet/API, public-card/site,
        or system-internal tools. It does not fetch business data itself.
        """
        return _j(plan_marketplace_request(
            question,
            marketplace=marketplace,
            seller=seller,
            date_from=date_from,
            date_to=date_to,
        ))
