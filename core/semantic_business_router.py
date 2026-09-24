"""Semantic business-query entry point with domain-specific executors."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Optional

from . import request_source_system_map as _request_source_system_map  # noqa: F401
from .business_router import execute_business_query as execute_legacy_business_query
from .errors import make_error
from .metric_observation import build_metric_observation
from .metric_registry import resolve_metric_terms
from .request_execution_controller import register_request_execution_controller_tool
from .request_join_controller import register_request_join_controller_tool
from .request_source_router import register_request_source_router_tool
from .semantic_advertising import (
    SemanticAdvertisingExecutionError,
    execute_semantic_advertising_question,
)
from .semantic_current_stock import (
    SemanticCurrentStockExecutionError,
    execute_current_stock_question,
)
from .semantic_ozon_snapshot import (
    SemanticOzonSnapshotError,
    execute_ozon_current_snapshot,
    requested_snapshot_metrics,
)
from .semantic_resolver import resolve_semantic_question
from .user_facing import present_business_result


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _resolution_with_period(
    resolution: dict[str, Any], *, date_from: str, date_to: str,
) -> dict[str, Any]:
    enriched = deepcopy(resolution)
    normalized = dict(enriched.get("normalized_query") or {})
    normalized["period"] = {"date_from": date_from, "date_to": date_to}
    enriched["normalized_query"] = normalized
    return enriched


def _attach_semantic_context(
    result: dict[str, Any], *, question: str, resolution: dict[str, Any],
) -> dict[str, Any]:
    result["semantic_question"] = question
    result["semantic_resolution"] = resolution
    result["normalized_query"] = deepcopy(resolution.get("normalized_query"))
    return result


_WB_PRICE_METRIC_FIELDS = {
    "WB_SELLER_PRICE_BEFORE_DISCOUNT": ("price_amount", "price"),
    "WB_SELLER_PRICE_AFTER_DISCOUNT": ("discounted_price_amount", "discountedPrice"),
    "WB_CLUB_PRICE_AFTER_DISCOUNT": ("club_discounted_price_amount", "clubDiscountedPrice"),
}
_WB_PRICE_METRIC_IDS = {"CURRENT_SELLING_PRICE", *_WB_PRICE_METRIC_FIELDS}


def _normalize_wb_price_result(
    result: dict[str, Any],
    *,
    requested_metric_id: str,
) -> dict[str, Any]:
    """Attach knowledge-backed price observations without changing raw WB tools."""
    out = deepcopy(result)
    if out.get("ok") is not True:
        return out

    observations: list[dict[str, Any]] = []
    source_name = str(out.get("source") or "wb_prices_list")
    for product in out.get("products") or []:
        if not isinstance(product, dict):
            continue
        currency = str(product.get("currency") or "RUB")
        for size in product.get("sizes") or []:
            if not isinstance(size, dict):
                continue
            for metric_id, (value_key, provider_field) in _WB_PRICE_METRIC_FIELDS.items():
                if requested_metric_id != "CURRENT_SELLING_PRICE" and metric_id != requested_metric_id:
                    continue
                value = size.get(value_key)
                if value is None:
                    continue
                observation = build_metric_observation(
                    metric_id=metric_id,
                    value=value,
                    unit=currency,
                    marketplace="wb",
                    source_name=source_name,
                    source_field=provider_field,
                    observed_at=None,
                ).to_dict()
                observation["dimensions"] = {
                    "nm_id": product.get("nm_id"),
                    "vendor_code": product.get("vendor_code"),
                    "size_id": size.get("size_id"),
                    "tech_size": size.get("tech_size"),
                }
                observations.append(observation)

    out["metric_observations"] = observations
    out["metric_ids"] = list(dict.fromkeys(
        str(item["metric_id"]) for item in observations
    ))
    out["knowledge_catalog_version"] = "marketplace_knowledge_catalog.v1"
    out["semantic_price_set"] = requested_metric_id == "CURRENT_SELLING_PRICE"
    return out


def _extract_wb_nm_ids(question: str, nm_ids: Optional[list[int]]) -> list[int]:
    ids = [int(value) for value in (nm_ids or [])]
    if not ids:
        ids = [
            int(value)
            for value in re.findall(r"(?<!\d)\d{6,}(?!\d)", str(question or ""))
        ]
    return list(dict.fromkeys(ids))


def _wb_product_snapshot_parts(
    question: str,
    *,
    nm_ids: Optional[list[int]],
) -> list[str]:
    """Recognize a product-specific generic price/stock request.

    This deliberately does not intercept explicit FBW/FBS-only wording or
    historical/sales questions. It exists so a plain request such as
    "цены и остатки по артикулу 218395039" returns the complete current
    seller-cabinet product panel instead of one arbitrarily selected stock bucket.
    """
    ids = _extract_wb_nm_ids(question, nm_ids)
    if len(ids) != 1:
        return []

    text = str(question or "").casefold().replace("ё", "е")
    if any(term in text for term in ("продаж", "заказ", "возврат", "за период", "вчера", "позавчера")):
        return []

    stock_requested = "остат" in text
    price_requested = bool(re.search(r"\bцен\w*|\bстоимост\w*", text))
    if not stock_requested:
        return []

    explicit_stock_scope = (
        any(term in text for term in (
            "свой склад",
            "склад продавца",
            "fbs",
            "fbw",
        ))
        or re.search(r"\bсклад\w*\s+(?:wb|wildberries)\b", text) is not None
    )
    if explicit_stock_scope:
        return []

    parts = ["stocks"]
    if price_requested:
        parts.insert(0, "prices")
    return parts


def _wb_product_snapshot_resolution(question: str, parts: list[str]) -> dict[str, Any]:
    metric_ids: list[str] = []
    source_ids: list[str] = []
    if "prices" in parts:
        metric_ids.extend([
            "WB_SELLER_PRICE_BEFORE_DISCOUNT",
            "WB_SELLER_PRICE_AFTER_DISCOUNT",
            "WB_CLUB_PRICE_AFTER_DISCOUNT",
        ])
        source_ids.append("wb_current_prices")
    if "stocks" in parts:
        metric_ids.extend(["CURRENT_STOCK", "CURRENT_FBS_STOCK"])
        source_ids.extend(["wb_current_stocks", "wb_fbs_stock"])
    return {
        "resolution_type": "BUSINESS_METRIC_SET",
        "execution_allowed": True,
        "status": "AVAILABLE",
        "route_id": "wb_product_current_snapshot",
        "metric_ids": metric_ids,
        "source_ids": source_ids,
        "normalized_query": {
            "marketplace": "wb",
            "temporal_class": "CURRENT_SNAPSHOT",
            "period": None,
            "metrics": metric_ids,
            "parts": list(parts),
        },
        "guardrail": (
            "Generic product stock means the seller-cabinet stock set: "
            "«Остатки “Склад WB”», «Остатки “Свой склад”» and transit. "
            "Transit is never added to physical stock. Generic product price means "
            "all approved fields from the current Prices and Discounts endpoint."
        ),
        "question": question,
    }


def _wb_price_set_from_result(result: dict[str, Any], *, nm_id: int) -> dict[str, Any]:
    products = [
        item for item in (result.get("products") or [])
        if isinstance(item, dict) and int(item.get("nm_id") or 0) == int(nm_id)
    ]
    if not products:
        return {
            "ok": False,
            "error_type": "not_found",
            "code": "PRODUCT_NOT_FOUND",
            "message": f"WB price source returned no product {nm_id}.",
        }

    product = products[0]
    sizes: list[dict[str, Any]] = []
    for size in product.get("sizes") or []:
        if not isinstance(size, dict):
            continue
        prices = []
        for metric_id, value_key, label in (
            ("WB_SELLER_PRICE_BEFORE_DISCOUNT", "price_amount", "Цена продавца до скидки"),
            ("WB_SELLER_PRICE_AFTER_DISCOUNT", "discounted_price_amount", "Цена со скидкой продавца"),
            ("WB_CLUB_PRICE_AFTER_DISCOUNT", "club_discounted_price_amount", "Цена для WB Клуба"),
        ):
            prices.append({
                "metric_id": metric_id,
                "label_ru": label,
                "amount": size.get(value_key),
                "currency": size.get("currency") or product.get("currency") or "RUB",
                "available": size.get(value_key) is not None,
            })
        sizes.append({
            "size_id": size.get("size_id"),
            "tech_size": size.get("tech_size"),
            "prices": prices,
        })

    return {
        "ok": True,
        "cabinet_surface_ru": "Товары и цены → Цены и скидки",
        "discount_percent": product.get("discount_percent"),
        "discount_percent_label_ru": "Скидка продавца",
        "club_discount_percent": product.get("club_discount_percent"),
        "club_discount_percent_label_ru": "Скидка WB Клуба",
        "sizes": sizes,
        "scope": deepcopy(result.get("price_scope") or {}),
    }


async def _execute_wb_product_current_snapshot(
    modules: dict[str, Any],
    *,
    seller: str,
    question: str,
    nm_ids: Optional[list[int]],
    parts: list[str],
) -> dict[str, Any]:
    wb = modules.get("wb")
    if wb is None:
        return make_error(
            "source_not_suitable",
            "Wildberries runtime module is required for the product current snapshot.",
            operation_id="marketplace_business_query",
            retryable=False,
        )

    ids = _extract_wb_nm_ids(question, nm_ids)
    if len(ids) != 1:
        return make_error(
            "invalid_params",
            "WB product current snapshot requires exactly one WB article.",
            operation_id="marketplace_business_query",
            retryable=False,
            details={"nm_ids": ids},
        )
    nm_id = int(ids[0])

    result: dict[str, Any] = {
        "ok": True,
        "complete": True,
        "result_type": "WB_PRODUCT_CURRENT_SNAPSHOT",
        "marketplace": "wb",
        "seller": seller,
        "nm_id": nm_id,
        "requested_parts": list(parts),
    }

    if "prices" in parts:
        payload = await wb.wb_get_prices(
            limit=1000,
            offset=0,
            filter_nm_id=nm_id,
            cabinet=seller,
        )
        try:
            price_raw = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return make_error(
                "server_error",
                "WB price executor returned a non-JSON result.",
                operation_id="marketplace_business_query",
                retryable=False,
                details={"stage": "prices", "nm_id": nm_id},
            )
        if not isinstance(price_raw, dict) or price_raw.get("ok") is not True:
            return {
                "ok": False,
                "complete": False,
                "error_type": "provider_leg_failed",
                "code": "PROVIDER_LEG_FAILED",
                "stage": "prices",
                "nm_id": nm_id,
                "provider_error": price_raw,
            }
        normalized_price = _normalize_wb_price_result(
            price_raw,
            requested_metric_id="CURRENT_SELLING_PRICE",
        )
        price_set = _wb_price_set_from_result(normalized_price, nm_id=nm_id)
        if price_set.get("ok") is not True:
            return {**price_set, "complete": False, "stage": "prices"}
        result["prices"] = price_set
        result["price_metric_observations"] = normalized_price.get("metric_observations") or []

    if "stocks" in parts:
        try:
            wb_stock = await execute_current_stock_question(
                wb,
                seller=seller,
                date_from="",
                date_to="",
                grouping="WAREHOUSE",
                nm_ids=[nm_id],
            )
        except SemanticCurrentStockExecutionError as exc:
            return make_error(
                "invalid_params",
                str(exc),
                operation_id="marketplace_business_query",
                retryable=False,
                details={"stage": "stock_wb", "nm_id": nm_id},
            )
        if wb_stock.get("ok") is not True:
            return {
                "ok": False,
                "complete": False,
                "error_type": "provider_leg_failed",
                "code": "PROVIDER_LEG_FAILED",
                "stage": "stock_wb",
                "nm_id": nm_id,
                "provider_error": wb_stock,
            }

        fbs_stock = await wb._wb_fbs_stock_result(
            nm_id=nm_id,
            cabinet=seller,
        )
        if not isinstance(fbs_stock, dict) or fbs_stock.get("ok") is not True:
            return {
                "ok": False,
                "complete": False,
                "error_type": "provider_leg_failed",
                "code": "PROVIDER_LEG_FAILED",
                "stage": "stock_own",
                "nm_id": nm_id,
                "provider_error": fbs_stock,
            }

        transit = wb_stock.get("in_transit")
        if not isinstance(transit, dict):
            transit = {
                "label_ru": "Товары в пути",
                "status": "UNAVAILABLE_FROM_CURRENT_SOURCE",
            }

        result["stocks"] = {
            "warehouse_wb": {
                "label_ru": "Остатки «Склад WB»",
                "units": wb_stock.get("stock_units"),
                "by_warehouse": deepcopy(wb_stock.get("by_warehouse") or []),
                "source": wb_stock.get("source"),
            },
            "own_warehouse": {
                "label_ru": "Остатки «Свой склад»",
                "units": fbs_stock.get("available_units"),
                "warehouses": deepcopy(fbs_stock.get("warehouses") or []),
                "source": fbs_stock.get("source"),
            },
            "in_transit": deepcopy(transit),
        }
        result["as_of_date"] = wb_stock.get("as_of_date")

    result["price_scope_note"] = (
        "Минимальная цена для автоакций не входит в одобренный текущий API «Получить товары с ценами». "
        "«Цена для участия в акции» относится к отдельному контексту конкретной акции и не подмешивается "
        "в текущий набор цен товара."
    )
    result["knowledge_catalog_version"] = "marketplace_knowledge_catalog.v1"
    return result


def _marketplace_from_request(marketplace: str, question: str) -> str:
    explicit = str(marketplace or "").strip().lower()
    if explicit in {"ozon", "озон"}:
        return "ozon"
    if explicit in {"wb", "wildberries", "вайлдберриз"}:
        return "wb"
    text = str(question or "").casefold().replace("ё", "е")
    if "ozon" in text or "озон" in text:
        return "ozon"
    if "wildberries" in text or "вайлдберриз" in text or " wb " in f" {text} ":
        return "wb"
    return explicit


def _ozon_snapshot_resolution(question: str, metrics: list[str]) -> dict[str, Any]:
    dictionary = {item["metric_id"]: item for item in resolve_metric_terms(question)}
    canonical = [deepcopy(dictionary[metric]) for metric in metrics if metric in dictionary]
    target_type = "BUSINESS_METRIC" if len(metrics) == 1 else "BUSINESS_METRIC_SET"
    result: dict[str, Any] = {
        "resolution_type": target_type,
        "execution_allowed": True,
        "status": "AVAILABLE_WITH_LIMITATION" if "CURRENT_SELLING_PRICE" in metrics else "AVAILABLE",
        "route_id": "ozon_current_snapshot",
        "source_ids": [
            source for source in (
                "ozon_current_prices" if "CURRENT_SELLING_PRICE" in metrics else "",
                "ozon_current_stocks" if "CURRENT_STOCK" in metrics else "",
            ) if source
        ],
        "canonical_metrics": canonical,
        "normalized_query": {
            "marketplace": "ozon",
            "temporal_class": "CURRENT_SNAPSHOT",
            "period": None,
            "metrics": list(metrics),
        },
        "guardrail": (
            "Only the approved live Ozon current-price/current-stock snapshot may execute. "
            "Historical substitution, active-cabinet fallback, fuzzy product substitution and partial answers are forbidden."
        ),
    }
    if len(metrics) == 1:
        result["metric_id"] = metrics[0]
        result["canonical_metric"] = canonical[0] if canonical else None
    else:
        result["metric_ids"] = list(metrics)
    return result


async def execute_business_query(
    modules: dict[str, Any],
    *,
    marketplace: str = "",
    seller: str = "",
    date_from: str = "",
    date_to: str = "",
    metric: str = "",
    question: str = "",
    nm_ids: Optional[list[int]] = None,
    product_ids: Optional[list[str]] = None,
) -> dict:
    """Resolve natural wording first, then execute only an approved route."""
    natural_question = str(question or "").strip()
    marketplace_key = _marketplace_from_request(marketplace, natural_question)

    # Ozon current snapshot is intentionally resolved before legacy period
    # parsing. CURRENT_SNAPSHOT metrics do not require fake date_from/date_to.
    if natural_question and marketplace_key == "ozon":
        snapshot_metrics = requested_snapshot_metrics(natural_question)
        if snapshot_metrics:
            ozon = modules.get("ozon")
            resolution = _ozon_snapshot_resolution(natural_question, snapshot_metrics)
            if ozon is None:
                return make_error(
                    "source_not_suitable",
                    "Ozon runtime module is required for the approved current snapshot metrics.",
                    operation_id="marketplace_business_query",
                    retryable=False,
                    details={"question": natural_question, "semantic_resolution": resolution},
                )
            try:
                result = await execute_ozon_current_snapshot(
                    ozon,
                    question=natural_question,
                    seller=seller,
                    date_from=date_from,
                    date_to=date_to,
                    product_ids=product_ids,
                )
            except SemanticOzonSnapshotError as exc:
                result = make_error(
                    "source_not_suitable",
                    str(exc),
                    operation_id="marketplace_business_query",
                    retryable=False,
                )
            if isinstance(result, dict):
                return _attach_semantic_context(
                    result, question=natural_question, resolution=resolution,
                )
            return result

        # Do not fall into legacy Ozon routing/date parsing for unsupported
        # wording. The current vertical slice remains deliberately fail-closed.
        return make_error(
            "source_not_suitable",
            "The Ozon question is outside the currently approved server-side business executors.",
            operation_id="marketplace_business_query",
            retryable=False,
            details={
                "question": natural_question,
                "semantic_status": "REQUIRES_OTHER_SOURCE_OR_EXECUTOR",
                "approved_ozon_metrics": ["CURRENT_SELLING_PRICE", "CURRENT_STOCK"],
            },
        )

    if natural_question and marketplace_key in {"wb", "wildberries"}:
        snapshot_parts = _wb_product_snapshot_parts(
            natural_question,
            nm_ids=nm_ids,
        )
        if snapshot_parts:
            resolution = _wb_product_snapshot_resolution(
                natural_question,
                snapshot_parts,
            )
            result = await _execute_wb_product_current_snapshot(
                modules,
                seller=seller,
                question=natural_question,
                nm_ids=nm_ids,
                parts=snapshot_parts,
            )
            if isinstance(result, dict):
                return _attach_semantic_context(
                    result, question=natural_question, resolution=resolution,
                )
            return result

        resolution = _resolution_with_period(
            resolve_semantic_question(natural_question),
            date_from=date_from,
            date_to=date_to,
        )

        if resolution.get("resolution_type") in {"NOT_COVERED", "UNKNOWN", "AMBIGUOUS"}:
            return make_error(
                "source_not_suitable",
                "The WB question is understood but the required approved source or executor is not available.",
                operation_id="marketplace_business_query",
                retryable=False,
                details={
                    "question": natural_question,
                    "semantic_resolution": resolution,
                },
            )

        if resolution.get("resolution_type") == "BUSINESS_METRIC":
            metric_id = str(resolution.get("metric_id") or "")
            if resolution.get("execution_allowed") is not True:
                return make_error(
                    "source_not_suitable",
                    "The business metric was understood, but the requested grouping or filter does not yet have an approved execution contract.",
                    operation_id="marketplace_business_query",
                    retryable=False,
                    details={
                        "question": natural_question,
                        "semantic_resolution": resolution,
                    },
                )

            if metric_id == "ORDERS":
                result = await execute_legacy_business_query(
                    modules,
                    marketplace=marketplace or "wb",
                    seller=seller,
                    date_from=date_from,
                    date_to=date_to,
                    metric="ORDERS",
                    question="",
                    nm_ids=nm_ids,
                )
                if isinstance(result, dict):
                    return _attach_semantic_context(
                        result, question=natural_question, resolution=resolution,
                    )
                return result

            if metric_id == "CURRENT_STOCK":
                wb = modules.get("wb")
                if wb is None:
                    return make_error(
                        "source_not_suitable",
                        "Wildberries runtime module is required for the approved current-stock metric.",
                        operation_id="marketplace_business_query",
                        retryable=False,
                        details={
                            "question": natural_question,
                            "semantic_resolution": resolution,
                        },
                    )
                try:
                    result = await execute_current_stock_question(
                        wb,
                        seller=seller,
                        date_from=date_from,
                        date_to=date_to,
                        grouping=str(
                            (resolution.get("normalized_query") or {}).get("grouping") or "TOTAL"
                        ),
                        nm_ids=nm_ids,
                    )
                except SemanticCurrentStockExecutionError as exc:
                    result = make_error(
                        "invalid_params",
                        str(exc),
                        operation_id="marketplace_business_query",
                        retryable=False,
                    )
                if isinstance(result, dict):
                    return _attach_semantic_context(
                        result, question=natural_question, resolution=resolution,
                    )
                return result
            if metric_id == "CURRENT_FBS_STOCK":
                wb = modules.get("wb")
                if wb is None:
                    return make_error(
                        "source_not_suitable",
                        "Wildberries runtime module is required for seller-warehouse stock.",
                        operation_id="marketplace_business_query",
                        retryable=False,
                    )
                ids = list(nm_ids or [])
                if not ids:
                    ids = [int(value) for value in re.findall(r"(?<!\d)\d{6,}(?!\d)", natural_question)]
                ids = list(dict.fromkeys(ids))
                if len(ids) != 1:
                    return make_error(
                        "invalid_params",
                        "CURRENT_FBS_STOCK requires exactly one WB nmId.",
                        operation_id="marketplace_business_query",
                        retryable=False,
                        details={"nm_ids": ids},
                    )
                result = await wb._wb_fbs_stock_result(
                    nm_id=int(ids[0]),
                    cabinet=seller,
                )
                if isinstance(result, dict):
                    return _attach_semantic_context(
                        result, question=natural_question, resolution=resolution,
                    )
                return result

            if metric_id in _WB_PRICE_METRIC_IDS:
                wb = modules.get("wb")
                if wb is None:
                    return make_error(
                        "source_not_suitable",
                        "Wildberries runtime module is required for the approved current-price metric.",
                        operation_id="marketplace_business_query",
                        retryable=False,
                    )
                ids = list(nm_ids or [])
                if not ids:
                    ids = [int(value) for value in re.findall(r"(?<!\d)\d{6,}(?!\d)", natural_question)]
                ids = list(dict.fromkeys(ids))
                if len(ids) > 1:
                    return make_error(
                        "invalid_params",
                        "WB current price metrics accept at most one nmId per request.",
                        operation_id="marketplace_business_query",
                        retryable=False,
                        details={"nm_ids": ids},
                    )
                payload = await wb.wb_get_prices(
                    limit=1 if not ids else 1000,
                    offset=0,
                    filter_nm_id=(int(ids[0]) if ids else None),
                    cabinet=seller,
                )
                try:
                    result = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    return make_error(
                        "server_error",
                        "WB price executor returned a non-JSON result.",
                        operation_id="marketplace_business_query",
                        retryable=False,
                    )
                if isinstance(result, dict):
                    result = _normalize_wb_price_result(
                        result,
                        requested_metric_id=metric_id,
                    )
                    return _attach_semantic_context(
                        result, question=natural_question, resolution=resolution,
                    )
                return result


            return make_error(
                "source_not_suitable",
                f"Business metric {metric_id!r} is registered but has no approved runtime executor.",
                operation_id="marketplace_business_query",
                retryable=False,
                details={
                    "question": natural_question,
                    "semantic_resolution": resolution,
                },
            )

        if (
            resolution.get("resolution_type") == "CAPABILITY"
            and resolution.get("capability_id") == "advertising_performance"
        ):
            store = modules.get("_archive_store")
            if store is None:
                return make_error(
                    "source_not_suitable",
                    "Canonical marketplace archive storage is required for historical advertising semantics.",
                    operation_id="marketplace_business_query",
                    retryable=False,
                    details={
                        "question": natural_question,
                        "semantic_resolution": resolution,
                        "required": "canonical Google Drive advertising archive",
                    },
                )
            try:
                result = await execute_semantic_advertising_question(
                    store,
                    question=natural_question,
                    seller=seller,
                    date_from=date_from,
                    date_to=date_to,
                    nm_ids=nm_ids,
                )
            except SemanticAdvertisingExecutionError as exc:
                text = str(exc)
                lowered = text.lower()
                if "full_coverage" in lowered or "coverage" in lowered:
                    error_type = "coverage_gap"
                elif "yyyy-mm-dd" in lowered or "date_from" in lowered or "date_to" in lowered:
                    error_type = "invalid_params"
                else:
                    error_type = "source_not_suitable"
                return make_error(
                    error_type,
                    text,
                    operation_id="marketplace_business_query",
                    retryable=False,
                    details={
                        "question": natural_question,
                        "semantic_resolution": resolution,
                        "capability_id": "advertising_performance",
                    },
                )
            return _attach_semantic_context(
                result, question=natural_question, resolution=resolution,
            )

    return await execute_legacy_business_query(
        modules,
        marketplace=marketplace,
        seller=seller,
        date_from=date_from,
        date_to=date_to,
        metric=metric,
        question=question,
        nm_ids=nm_ids,
    )


def register_business_query_tool(combined: Any, modules: dict[str, Any]) -> None:
    """Register planning, execution, join control, and business execution."""

    # The planner is always first. Execution Controller owns exact per-leg calls;
    # Join Controller later validates completed leg results before synthesis/math.
    register_request_source_router_tool(combined)
    register_request_execution_controller_tool(combined)
    register_request_join_controller_tool(combined)

    @combined.tool(
        name="marketplace_business_query",
        annotations={
            "title": "Marketplace business query (semantic server-side routing)",
            "readOnlyHint": True,
            "openWorldHint": True,
        },
    )
    async def marketplace_business_query(
        question: str = "",
        marketplace: str = "",
        seller: str = "",
        date_from: str = "",
        date_to: str = "",
        metric: str = "",
        nm_ids: Optional[list[int]] = None,
        product_ids: Optional[list[str]] = None,
    ) -> str:
        try:
            result = await execute_business_query(
                modules,
                marketplace=marketplace,
                seller=seller,
                date_from=date_from,
                date_to=date_to,
                question=question,
                metric=metric,
                nm_ids=nm_ids,
                product_ids=product_ids,
            )
        except Exception as exc:  # last-resort user-facing boundary; diagnostics stay technical
            technical = str(exc)
            lowered = technical.casefold()
            archive_failure = any(marker in lowered for marker in (
                "archive",
                "google drive",
                "read_range_by_id",
                "historical database",
                "historical store",
            ))
            result = make_error(
                "source_not_suitable" if archive_failure else "server_error",
                technical,
                operation_id="marketplace_business_query",
                retryable=not archive_failure,
                details={
                    "question": question,
                    "required": "historical database" if archive_failure else "marketplace runtime",
                    "exception_type": type(exc).__name__,
                },
            )
        if isinstance(result, dict):
            result = present_business_result(
                result,
                question=question,
                marketplace=marketplace,
                seller=seller,
            )
        return _j(result)
