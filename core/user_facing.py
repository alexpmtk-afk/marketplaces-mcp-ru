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


def _success_message(result: dict[str, Any]) -> tuple[str | None, str | None]:
    price = result.get("current_selling_price")
    stock = result.get("current_stock")
    if isinstance(price, dict) or isinstance(stock, dict):
        parts: list[str] = []
        if isinstance(price, dict) and price.get("amount") not in (None, ""):
            currency = "₽" if str(price.get("currency") or "").upper() == "RUB" else str(price.get("currency") or "")
            parts.append(f"Цена: {_format_number(price.get('amount'))} {currency}".rstrip())
        if isinstance(stock, dict) and stock.get("available_units") is not None:
            parts.append(f"Остаток: {_format_number(stock.get('available_units'))} шт.")
        if parts:
            return " ".join(parts), None

    if result.get("metric") == "ORDERS" and result.get("orders_count") is not None:
        message = f"Заказов: {_format_number(result.get('orders_count'))}"
        if result.get("orders_amount") is not None:
            message += f" на сумму {_format_number(result.get('orders_amount'))} ₽."
        else:
            message += "."
        note = None
        if result.get("business_completeness") == "PRELIMINARY_NOT_ALL_ORDERS":
            note = "Это оперативные данные Wildberries; часть заказов может появляться с задержкой."
        return message, note

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
