"""Normalize business-language dimensions before source selection.

This module deliberately does not choose marketplace fields or APIs.  It only
extracts dimensions that are safe to derive from the user's wording.  Source
selection remains the responsibility of Semantic Core / Business Query Router.
"""
from __future__ import annotations

import re
from typing import Any


def normalize_business_text(text: str) -> str:
    value = str(text or "").casefold().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9_%₽]+", " ", value)
    return " ".join(value.split())


def _contains_any(normalized: str, phrases: tuple[str, ...]) -> bool:
    return any(normalize_business_text(phrase) in normalized for phrase in phrases)


def parse_business_query_dimensions(question: str) -> dict[str, Any]:
    """Return source-independent dimensions parsed from natural-language text.

    The parser is intentionally conservative: ambiguous business meaning is not
    guessed here.  A later semantic route supplies the metric contract.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")

    normalized = normalize_business_text(question)

    amount_markers = (
        "на какую сумму", "сумма", "в рублях", "рублей", "руб ", " ₽", "₽",
        "денег", "стоимость",
    )
    units_markers = (
        "сколько штук", "в штуках", "штук", "единиц", "количество",
        "сколько товаров",
    )

    if _contains_any(normalized, amount_markers):
        requested_measure = "AMOUNT_RUB"
    elif _contains_any(normalized, units_markers) or normalized.startswith("сколько "):
        requested_measure = "UNITS"
    else:
        requested_measure = None

    grouping = "TOTAL"
    grouping_markers: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("DAY", ("по дням", "каждый день", "день за днем")),
        ("WEEK", ("по неделям", "каждую неделю")),
        ("MONTH", ("по месяцам", "каждый месяц")),
        ("PRODUCT", ("по товарам", "по товару", "по артикулам", "по артикулу")),
        ("WAREHOUSE", ("по складам", "по складу")),
        ("CABINET", ("по кабинетам", "по кабинету")),
        ("MARKETPLACE", ("по маркетплейсам", "по маркетплейсу")),
    )
    for group_id, markers in grouping_markers:
        if _contains_any(normalized, markers):
            grouping = group_id
            break

    complete_order_flow = _contains_any(
        normalized,
        (
            "полный поток заказов",
            "полная история заказов",
            "все оформленные заказы",
            "все заказы",
            "сколько всего заказов",
            "все заказы без исключения",
            "абсолютно все заказы",
            "включая неоплаченные заказы",
            "включая неоплаченные",
            "всего оформленных заказов",
            "включая заказы с неподтвержденной оплатой",
        ),
    )

    period_hint = None
    if _contains_any(normalized, ("сегодня", "за сегодня")):
        period_hint = "TODAY"
    elif _contains_any(normalized, ("вчера", "за вчера")):
        period_hint = "YESTERDAY"
    elif _contains_any(normalized, ("за неделю", "эта неделя", "текущая неделя")):
        period_hint = "WEEK"
    elif _contains_any(normalized, ("за месяц", "этот месяц", "текущий месяц")):
        period_hint = "MONTH"
    elif re.search(r"\bс\s+\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\s+по\s+\d{1,2}[./-]\d{1,2}", normalized):
        period_hint = "RANGE"

    filter_hints: list[str] = []
    if _contains_any(normalized, ("товар", "артикул", "nmid", "nm id", "sku")):
        filter_hints.append("PRODUCT")
    if _contains_any(normalized, ("склад", "складу", "склада")):
        filter_hints.append("WAREHOUSE")
    if _contains_any(normalized, ("кабинет", "продавец", "ип ", "ооо ")):
        filter_hints.append("CABINET")

    return {
        "normalized_text": normalized,
        "requested_measure": requested_measure,
        "grouping": grouping,
        "period_hint": period_hint,
        "filter_hints": filter_hints,
        "complete_order_flow": complete_order_flow,
    }
