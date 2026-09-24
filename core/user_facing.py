"""User-facing Russian presentation for high-level marketplace MCP tools.

Internal routing/status fields remain machine-readable and unchanged in dedicated
technical fields. Normal ChatGPT/Codex answers should prefer user_message and
user_reason instead of exposing architecture vocabulary.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any


_PLAN_REASON_BY_STATUS = {
    "INVALID_PERIOD": "Указанный период задан некорректно или находится в будущем.",
    "NOT_CONNECTED_ON_DEMAND": "Сейчас MCP не подключён к источнику, где можно получить эти данные.",
    "HISTORICAL_SOURCE_ABSENT": "Исторические данные для такого запроса пока не подключены.",
    "NO_VERIFIED_ARCHIVE_HISTORY": "В базе данных пока нет подтверждённых данных за весь указанный период.",
    "OZON_HISTORICAL_SOURCE_NOT_APPROVED": "Исторические данные Ozon для такого запроса пока не подключены.",
    "MARKETPLACE_REQUIRED": "Уточните маркетплейс: Wildberries или Ozon.",
    "UNRESOLVED_SOURCE_CLASS": "Не удалось однозначно определить, откуда брать данные. Уточните запрос.",
    "CURRENT_SOURCE_NOT_APPROVED": "Источник текущих данных для этого показателя пока не подключён.",
    "CURRENT_PERFORMANCE_EXECUTOR_NOT_APPROVED": "Текущие данные по этому показателю пока не подключены к автоматической обработке.",
    "SOURCE_CLASS_KNOWN_EXECUTOR_NOT_APPROVED": "Источник данных известен, но этот тип запроса пока не подключён к автоматической обработке.",
    "SOURCE_CLASS_KNOWN_SEMANTIC_EXECUTOR_NOT_YET_WIRED": "Источник данных известен, но этот тип запроса пока не подключён к автоматической обработке.",
    "AVAILABLE_IF_MONITORED": "Данные можно получить, если по этой карточке уже есть сохранённые наблюдения.",
    "FULL_COVERAGE_REQUIRED": "Для расчёта будут использованы данные из базы. Сначала сервер проверит, что весь указанный период загружен полностью.",
    "AVAILABLE": "Данные доступны.",
    "AVAILABLE_WITH_LIMITATION": "Данные доступны, но у источника есть ограничения, которые сервер учтёт при ответе.",
    "MULTI_SOURCE_PLAN": "Для ответа нужны данные из нескольких источников. Сервер проверит каждый из них отдельно.",
}


def _seller_suffix(seller: str) -> str:
    value = str(seller or "").strip()
    return f" по магазину {value}" if value else ""


def _plain_error_message(result: dict[str, Any], *, seller: str = "") -> str:
    code = str(result.get("code") or "").upper()
    error_type = str(result.get("error_type") or result.get("error") or "").lower()
    technical = str(result.get("message") or "")
    details = result.get("details") if isinstance(result.get("details"), dict) else {}
    required = str(details.get("required") or "").lower()
    lowered = technical.lower()

    archive_markers = ("archive", "google drive", "historical store", "historical database")
    if (
        any(marker in lowered for marker in archive_markers)
        or "archive" in required
        or code in {"ARCHIVE_STORAGE_NOT_CONFIGURED", "DURABLE_BACKEND_NOT_CONFIGURED"}
    ):
        return (
            "Не удалось получить данные: сейчас нет доступа к базе данных "
            f"с историческими данными{_seller_suffix(seller)}."
        )
    if error_type == "coverage_gap" or code in {"COVERAGE_GAP", "FULL_COVERAGE_REQUIRED"}:
        return "В базе данных пока нет полного набора данных за весь указанный период."
    if code in {"HISTORICAL_SOURCE_ABSENT", "NO_VERIFIED_ARCHIVE_HISTORY"}:
        return "Исторические данные для такого запроса пока недоступны."
    if code in {"CABINET_NOT_CONFIGURED", "SELLER_KNOWN_BUT_NOT_CONFIGURED"} or error_type == "seller_known_but_not_configured":
        return f"Для этого магазина пока не подключён доступ к кабинету{_seller_suffix(seller)}."
    if code in {"CABINET_REQUIRED", "NEEDS_CONTEXT"}:
        return "Не хватает данных для выполнения запроса. Уточните магазин или товар."
    if code == "CABINET_AMBIGUOUS":
        return "Не удалось однозначно определить магазин. Уточните его название."
    if code in {"PRODUCT_NOT_FOUND", "NOT_FOUND"} or error_type == "not_found":
        return "Товар или данные по нему не найдены."
    if code in {"PRODUCT_AMBIGUOUS", "PRODUCT_IDENTIFIER_AMBIGUOUS"}:
        return "Не удалось однозначно определить товар. Уточните артикул."
    if code == "PRODUCT_IDENTIFIER_REQUIRED":
        return "Укажите артикул или другой идентификатор товара."
    if error_type == "auth":
        return "Не удалось получить данные: нет доступа к выбранному кабинету."
    if error_type == "forbidden":
        return "У подключённой учётной записи недостаточно прав для этого запроса."
    if error_type == "rate_limit":
        return "Маркетплейс временно ограничил частоту запросов. Попробуйте немного позже."
    if error_type == "timeout":
        return "Маркетплейс не ответил вовремя. Попробуйте повторить запрос."
    if error_type == "network":
        return "Не удалось связаться с маркетплейсом. Попробуйте повторить запрос позже."
    if error_type == "server_error":
        return "Маркетплейс временно вернул ошибку. Попробуйте повторить запрос позже."
    if error_type == "execution_pending":
        return "Запрос ещё обрабатывается. Итоговые данные пока не готовы."
    if error_type == "provider_data_conflict":
        return "Источник вернул данные в неожиданном формате, поэтому итог не рассчитан."
    if error_type == "invalid_params":
        if "date" in lowered or "yyyy-mm-dd" in lowered:
            return "Проверьте даты в запросе."
        return "Не удалось выполнить запрос: нужно уточнить его параметры."
    if error_type == "source_not_suitable":
        return "Для этого запроса пока нет подключённого источника данных."
    if error_type == "safety_gate":
        return "Этот запрос нельзя выполнить в текущем безопасном режиме."
    return "Не удалось получить данные из-за технической ошибки."


def _format_number(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    if number == number.to_integral():
        return f"{int(number):,}".replace(",", " ")
    return f"{number.normalize():f}".replace(".", ",")


def _currency_label(value: Any) -> str:
    code = str(value or "").upper()
    return "₽" if code == "RUB" else code


def _format_currency_rows(
    rows: Any,
    *,
    value_key: str,
    label: str,
) -> str | None:
    if not isinstance(rows, list) or not rows:
        return None
    parts: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or row.get(value_key) is None:
            continue
        currency = _currency_label(row.get("currency"))
        amount = _format_number(row.get(value_key))
        parts.append(f"{amount} {currency}".rstrip())
    if not parts:
        return None
    return f"{label}: " + "; ".join(parts) + "."


def _metric_observation_message(result: dict[str, Any]) -> tuple[str | None, str | None]:
    observations = result.get("metric_observations")
    if not isinstance(observations, list) or not observations:
        return None, None

    size_keys = {
        (
            (item.get("dimensions") or {}).get("nm_id"),
            (item.get("dimensions") or {}).get("size_id"),
        )
        for item in observations
        if isinstance(item, dict)
    }
    show_size = len(size_keys) > 1
    parts: list[str] = []
    has_unverified = False
    for item in observations:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("metric_id") or "Показатель")
        value = _format_number(item.get("value"))
        unit = _currency_label(item.get("unit")) if item.get("unit") else ""
        context = ""
        if show_size:
            dimensions = item.get("dimensions") or {}
            size = dimensions.get("tech_size") or dimensions.get("size_id")
            if size not in (None, ""):
                context = f" (размер {size})"
        parts.append(f"{label}{context}: {value} {unit}".rstrip())
        if item.get("semantic_status") != "verified":
            has_unverified = True

    if not parts:
        return None, None
    note = (
        "Для части полей официальное бизнес-определение ещё не подтверждено."
        if has_unverified else None
    )
    return "; ".join(parts) + ".", note


def _wb_product_current_snapshot_message(
    result: dict[str, Any],
) -> tuple[str | None, str | None]:
    if result.get("result_type") != "WB_PRODUCT_CURRENT_SNAPSHOT":
        return None, None

    parts: list[str] = []
    prices = result.get("prices")
    if isinstance(prices, dict):
        sizes = prices.get("sizes")
        if isinstance(sizes, list) and sizes:
            price_parts: list[str] = []
            multi_size = len(sizes) > 1
            for size in sizes:
                if not isinstance(size, dict):
                    continue
                prefix = ""
                if multi_size:
                    shown_size = size.get("tech_size") or size.get("size_id")
                    prefix = f"размер {shown_size}: " if shown_size not in (None, "") else ""
                for item in size.get("prices") or []:
                    if not isinstance(item, dict):
                        continue
                    label = str(item.get("label_ru") or "Цена")
                    if item.get("available") is not True:
                        price_parts.append(f"{prefix}{label}: нет значения")
                        continue
                    currency = _currency_label(item.get("currency"))
                    price_parts.append(
                        f"{prefix}{label}: {_format_number(item.get('amount'))} {currency}".rstrip()
                    )
            if prices.get("discount_percent") is not None:
                price_parts.append(
                    f"Скидка продавца: {_format_number(prices.get('discount_percent'))}%"
                )
            if prices.get("club_discount_percent") is not None:
                price_parts.append(
                    f"Скидка WB Клуба: {_format_number(prices.get('club_discount_percent'))}%"
                )
            if price_parts:
                parts.append("Цены — " + "; ".join(price_parts) + ".")

    stocks = result.get("stocks")
    if isinstance(stocks, dict):
        stock_parts: list[str] = []
        warehouse_wb = stocks.get("warehouse_wb")
        if isinstance(warehouse_wb, dict) and warehouse_wb.get("units") is not None:
            stock_parts.append(
                f"{warehouse_wb.get('label_ru') or 'Остатки «Склад WB»'}: "
                f"{_format_number(warehouse_wb.get('units'))} шт."
            )
        own = stocks.get("own_warehouse")
        if isinstance(own, dict) and own.get("units") is not None:
            own_text = (
                f"{own.get('label_ru') or 'Остатки «Свой склад»'}: "
                f"{_format_number(own.get('units'))} шт."
            )
            warehouses = own.get("warehouses")
            named = []
            if isinstance(warehouses, list):
                for row in warehouses:
                    if not isinstance(row, dict):
                        continue
                    if row.get("warehouse_name") and row.get("available_units") is not None:
                        named.append(
                            f"{row.get('warehouse_name')} — "
                            f"{_format_number(row.get('available_units'))} шт."
                        )
            if named:
                own_text += " (" + "; ".join(named) + ")"
            stock_parts.append(own_text)

        transit = stocks.get("in_transit")
        if isinstance(transit, dict):
            if transit.get("units") is not None:
                transit_text = (
                    f"{transit.get('label_ru') or 'Товары в пути'}: "
                    f"{_format_number(transit.get('units'))} шт."
                )
                to_client = transit.get("to_client")
                from_client = transit.get("from_client")
                breakdown = []
                if isinstance(to_client, dict) and to_client.get("units") is not None:
                    breakdown.append(
                        f"{to_client.get('label_ru') or 'Едут к покупателю'} — "
                        f"{_format_number(to_client.get('units'))}"
                    )
                if isinstance(from_client, dict) and from_client.get("units") is not None:
                    breakdown.append(
                        f"{from_client.get('label_ru') or 'Возвращаются на склад'} — "
                        f"{_format_number(from_client.get('units'))}"
                    )
                if breakdown:
                    transit_text += " (" + "; ".join(breakdown) + ")"
                stock_parts.append(transit_text)
            elif transit.get("status"):
                stock_parts.append("Товары в пути: источник не вернул отдельные данные.")

        if stock_parts:
            parts.append("Остатки — " + " ".join(stock_parts))

    if not parts:
        return None, None

    note = str(result.get("price_scope_note") or "").strip() or None
    return " ".join(parts), note


def _success_message(result: dict[str, Any]) -> tuple[str | None, str | None]:
    snapshot_message, snapshot_note = _wb_product_current_snapshot_message(result)
    if snapshot_message:
        return snapshot_message, snapshot_note

    observation_message, observation_note = _metric_observation_message(result)
    if observation_message:
        return observation_message, observation_note

    price = result.get("current_selling_price")
    stock = result.get("current_stock")
    if isinstance(price, dict) or isinstance(stock, dict):
        parts: list[str] = []
        if isinstance(price, dict) and price.get("amount") not in (None, ""):
            currency = _currency_label(price.get("currency"))
            parts.append(f"Цена: {_format_number(price.get('amount'))} {currency}".rstrip())
        if isinstance(stock, dict) and stock.get("available_units") is not None:
            parts.append(f"Остаток: {_format_number(stock.get('available_units'))} шт.")
        if parts:
            return " ".join(parts), None

    if result.get("orders_count") is not None:
        message = f"Заказов: {_format_number(result.get('orders_count'))}"
        if result.get("orders_amount") is not None:
            message += f" на сумму {_format_number(result.get('orders_amount'))} ₽."
        else:
            message += "."
        note = None
        if result.get("business_completeness") == "PRELIMINARY_NOT_ALL_ORDERS":
            note = "Это оперативные данные Wildberries; часть заказов может появляться с задержкой."
        return message, note

    if result.get("metric") == "CURRENT_STOCK" and result.get("stock_units") is not None:
        grouping = str(result.get("grouping") or "TOTAL").upper()
        label = str(result.get("cabinet_label_ru") or "Остатки «Склад WB»")
        message = f"{label}: {_format_number(result.get('stock_units'))} шт."
        if grouping == "PRODUCT" and isinstance(result.get("by_product"), list):
            message += f" Товаров в разбивке: {len(result['by_product'])}."
        elif grouping == "WAREHOUSE" and isinstance(result.get("by_warehouse"), list):
            message += f" Складов в разбивке: {len(result['by_warehouse'])}."
        return message, None

    capability = str(result.get("capability_id") or "")
    calculation = result.get("calculation") if isinstance(result.get("calculation"), dict) else {}

    if capability in {"penalties", "storage_charge", "paid_acceptance"}:
        labels = {
            "penalties": "Штрафы",
            "storage_charge": "Хранение",
            "paid_acceptance": "Платная приёмка",
        }
        message = _format_currency_rows(
            calculation.get("totals_by_currency"),
            value_key="amount",
            label=labels[capability],
        )
        if message:
            return message, None

    if capability == "sale_and_return_operations":
        rows = calculation.get("sales_and_returns_by_currency")
        if isinstance(rows, list) and rows:
            parts: list[str] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                currency = _currency_label(row.get("currency"))
                amount = _format_number(row.get("net_sales_amount"))
                units = _format_number(row.get("net_sales_units"))
                parts.append(f"{amount} {currency}, {units} шт.".rstrip())
            if parts:
                return "Продажи с учётом возвратов: " + "; ".join(parts), None

    if capability == "logistics":
        rows = calculation.get("components_by_currency")
        if isinstance(rows, list) and rows:
            parts: list[str] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                currency = _currency_label(row.get("currency"))
                delivery = _format_number(row.get("delivery_service_amount"))
                rebill = _format_number(row.get("rebilled_transport_warehouse_cost"))
                parts.append(
                    f"доставка {delivery} {currency}, перевыставленные расходы {rebill} {currency}".rstrip()
                )
            if parts:
                return "Логистика: " + "; ".join(parts) + ".", None

    if capability == "deductions_and_adjustments":
        rows = calculation.get("components_by_currency")
        if isinstance(rows, list) and rows:
            parts: list[str] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                currency = _currency_label(row.get("currency"))
                deduction = _format_number(row.get("deduction_amount"))
                adjustment = _format_number(row.get("wb_reward_adjustment_amount"))
                parts.append(
                    f"удержания {deduction} {currency}, корректировка вознаграждения WB {adjustment} {currency}".rstrip()
                )
            if parts:
                return "За период: " + "; ".join(parts) + ".", None

    if capability == "commission_and_wb_reward":
        rows = calculation.get("components_by_currency")
        if isinstance(rows, list) and rows:
            parts: list[str] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                currency = _currency_label(row.get("currency"))
                amount = _format_number(row.get("net_wb_reward_including_vat"))
                parts.append(f"{amount} {currency}".rstrip())
            if parts:
                return "Вознаграждение WB с учётом возвратов и НДС: " + "; ".join(parts) + ".", None

    if capability == "acquiring_and_payment_processing":
        rows = calculation.get("components_by_currency")
        if isinstance(rows, list) and rows:
            parts: list[str] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                currency = _currency_label(row.get("currency"))
                amount = _format_number(row.get("net_weekly_payment_processing_fee"))
                parts.append(f"{amount} {currency}".rstrip())
            if parts:
                return (
                    "Эквайринг и приём платежей: " + "; ".join(parts) + ".",
                    "Это предварительные данные недельного отчёта Wildberries.",
                )

    if capability in {"observed_fulfillment_method", "warehouse_tariff_context"}:
        values = calculation.get("distinct_values")
        if isinstance(values, list) and values:
            shown = ", ".join(str(value) for value in values)
            if capability == "observed_fulfillment_method":
                return (
                    f"В исторических данных за период встречались способы отгрузки: {shown}.",
                    "Это история наблюдений, а не подтверждение текущей настройки.",
                )
            return (
                f"В исторических данных за период встречались коэффициенты склада: {shown}.",
                "Это исторические значения, а не текущий тариф.",
            )

    if result.get("metric") == "ADVERTISING_PERFORMANCE" and isinstance(result.get("metrics"), dict):
        metrics = result["metrics"]
        parts: list[str] = []
        if metrics.get("spend") is not None:
            parts.append(f"расходы {_format_number(metrics.get('spend'))} ₽")
        if metrics.get("drr_order_pct") is not None:
            parts.append(f"ДРР {_format_number(metrics.get('drr_order_pct'))}%")
        if metrics.get("roas") is not None:
            parts.append(f"ROAS {_format_number(metrics.get('roas'))}")
        if metrics.get("clicks") is not None:
            parts.append(f"клики {_format_number(metrics.get('clicks'))}")
        if metrics.get("views") is not None:
            parts.append(f"показы {_format_number(metrics.get('views'))}")
        if parts:
            return (
                "Реклама: " + ", ".join(parts) + ".",
                "Показатели относятся к рекламной атрибуции Wildberries, а не к общей прибыли магазина.",
            )

    if calculation:
        return "Данные получены и рассчитаны за указанный период.", None

    if result.get("ok") is True:
        return "Данные получены.", None

    return None, None


def present_business_result(
    result: dict[str, Any],
    *,
    question: str = "",
    marketplace: str = "",
    seller: str = "",
) -> dict[str, Any]:
    """Attach plain Russian wording while preserving technical diagnostics."""
    del question, marketplace
    out = deepcopy(result)
    if out.get("ok") is False:
        technical = out.get("message")
        if technical not in (None, ""):
            out.setdefault("technical_message", technical)
        user_message = _plain_error_message(out, seller=seller)
        out["message"] = user_message
        out["user_message"] = user_message
        return out

    message, note = _success_message(out)
    if message:
        out["user_message"] = message
    if note:
        out["user_note"] = note
    return out


def present_query_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Make planner reason safe for ordinary user-facing narration."""
    out = deepcopy(plan)
    technical_reason = out.get("reason")
    status = str(out.get("source_status") or "")
    user_reason = _PLAN_REASON_BY_STATUS.get(status)
    if user_reason is None:
        if out.get("source_family") == "CANONICAL_ARCHIVE":
            user_reason = "Для ответа нужны исторические данные из базы данных."
        elif out.get("source_family") == "LIVE_CABINET_API":
            user_reason = "Для ответа будут использованы текущие данные из кабинета маркетплейса."
        elif out.get("source_family") == "UNAVAILABLE":
            user_reason = "Сейчас для этого запроса нет доступного источника данных."
        else:
            user_reason = "Источник данных определён сервером."

    if technical_reason not in (None, ""):
        out["technical_reason"] = technical_reason
    out["reason"] = user_reason
    out["user_reason"] = user_reason
    return out


def present_execution_control(result: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(result)
    source_gap = out.get("source_gap")
    if isinstance(source_gap, dict):
        gap = deepcopy(source_gap)
        shown = present_query_plan({
            "source_family": gap.get("source_family"),
            "source_status": gap.get("source_status"),
            "reason": gap.get("reason"),
        })
        if gap.get("reason") not in (None, ""):
            gap["technical_reason"] = gap.get("reason")
        gap["reason"] = shown["user_reason"]
        gap["user_reason"] = shown["user_reason"]
        out["source_gap"] = gap
        out["user_message"] = shown["user_reason"]
    elif out.get("ok") is False:
        out = present_business_result(out)
    return out


def present_join_control(result: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(result)
    if out.get("ok") is False:
        out = present_business_result(out)
    return out
