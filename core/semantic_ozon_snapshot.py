"""Canonical Ozon current-snapshot executor for server-side business semantics.

The module owns the approved current Ozon price concepts plus CURRENT_STOCK.
It never switches the shared active cabinet, never substitutes historical data,
and never returns a partial price/stock answer as complete.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from .metric_observation import build_metric_observation
from .metric_registry import resolve_metric_terms
from .tools import resolve_named_cabinet

OZON_SNAPSHOT_EXECUTOR_VERSION = "ozon_current_snapshot.v2"
OZON_PRICE_METRICS = frozenset({
    "CURRENT_SELLING_PRICE",
    "OZON_BASE_PRICE",
    "OZON_OLD_PRICE",
    "OZON_MIN_PRICE",
})
SUPPORTED_METRICS = frozenset({*OZON_PRICE_METRICS, "CURRENT_STOCK"})

_OZON_PRICE_FIELD_CONTRACT = {
    "CURRENT_SELLING_PRICE": {
        "field": "marketing_seller_price",
        "result_key": "current_selling_price",
        "meaning": "current Ozon selling price with seller promotions applied",
        "limitation": "not guaranteed to equal a personalized final checkout price",
    },
    "OZON_BASE_PRICE": {
        "field": "price",
        "result_key": "ozon_base_price",
        "meaning": "seller price before Ozon promotions",
        "limitation": "not the current selling price when marketing_seller_price differs",
    },
    "OZON_OLD_PRICE": {
        "field": "old_price",
        "result_key": "ozon_old_price",
        "meaning": "strikethrough/reference price before discounts",
        "limitation": "not the current selling price",
    },
    "OZON_MIN_PRICE": {
        "field": "min_price",
        "result_key": "ozon_min_price",
        "meaning": "minimum allowed seller price threshold",
        "limitation": "not the current selling price",
    },
}


class SemanticOzonSnapshotError(RuntimeError):
    """Raised for deterministic semantic/input failures before provider truth exists."""


def _norm(value: Any) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", str(value or "").casefold().replace("ё", "е"))


def _provider_error(stage: str, response: Any) -> dict[str, Any]:
    details = response if isinstance(response, dict) else {"response_type": type(response).__name__}
    return {
        "ok": False,
        "error": "provider_leg_failed",
        "code": "PROVIDER_LEG_FAILED",
        "retryable": bool(details.get("retryable", False)) if isinstance(details, dict) else False,
        "stage": stage,
        "complete": False,
        "message": f"Ozon provider leg {stage!r} did not complete successfully.",
        "provider_error": details,
    }


def _data(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("data")
    return value if isinstance(value, dict) else {}


def _items(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = _data(response)
    candidates: Any = data.get("items")
    if not isinstance(candidates, list):
        result = data.get("result")
        if isinstance(result, dict):
            candidates = result.get("items")
        elif isinstance(result, list):
            candidates = result
    return [item for item in (candidates or []) if isinstance(item, dict)]


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
                "seller": requested,
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


def _extract_identifier(question: str, product_ids: Optional[list[str]]) -> tuple[str | None, dict[str, Any] | None]:
    explicit = [str(value).strip() for value in (product_ids or []) if str(value).strip()]
    if explicit:
        unique = list(dict.fromkeys(explicit))
        if len(unique) != 1:
            return None, {
                "ok": False,
                "error": "product_identifier_ambiguous",
                "code": "PRODUCT_IDENTIFIER_AMBIGUOUS",
                "retryable": False,
                "complete": False,
                "identifiers": unique,
            }
        return unique[0], None

    text = str(question or "")
    labelled = re.findall(
        r"(?:артикул(?:у|а|ом)?|sku|product[\s_-]*id|offer[\s_-]*id)\s*[:№#=-]?\s*([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9._/\-]*)",
        text,
        flags=re.IGNORECASE,
    )
    if labelled:
        unique = list(dict.fromkeys(labelled))
        if len(unique) == 1:
            return unique[0], None
        return None, {
            "ok": False,
            "error": "product_identifier_ambiguous",
            "code": "PRODUCT_IDENTIFIER_AMBIGUOUS",
            "retryable": False,
            "complete": False,
            "identifiers": unique,
        }

    numeric = list(dict.fromkeys(re.findall(r"(?<!\d)\d{6,}(?!\d)", text)))
    if len(numeric) == 1:
        return numeric[0], None
    return None, {
        "ok": False,
        "error": "product_identifier_required" if not numeric else "product_identifier_ambiguous",
        "code": "NEEDS_CONTEXT" if not numeric else "PRODUCT_IDENTIFIER_AMBIGUOUS",
        "retryable": False,
        "complete": False,
        "required_context": ["product identifier"],
        "identifiers": numeric,
    }


def _identifier_lookup_field(question: str, identifier: str) -> str:
    """Choose exactly one Ozon identifier namespace for /v3/product/info/list.

    Ozon rejects requests that populate offer_id, product_id and sku together.
    Explicit technical labels win.  In normal Russian business wording,
    numeric "артикул" is treated as an Ozon SKU; a non-numeric seller article
    is treated as offer_id.  Unlabelled values follow the same deterministic
    numeric/non-numeric rule instead of fuzzy cross-namespace guessing.
    """
    text = str(question or "").casefold().replace("ё", "е")
    if re.search(r"\bproduct[\s_-]*id\b", text):
        return "product_id"
    if re.search(r"\boffer[\s_-]*id\b", text):
        return "offer_id"
    if re.search(r"\bsku\b", text):
        return "sku"
    if re.search(r"артикул(?:у|а|ом)?", text):
        return "sku" if str(identifier).isdigit() else "offer_id"
    return "sku" if str(identifier).isdigit() else "offer_id"


def requested_snapshot_metrics(question: str) -> list[str]:
    """Recognize only the registered Ozon snapshot metrics, without granting execution."""
    matched = [item["metric_id"] for item in resolve_metric_terms(question)]
    metrics = [metric for metric in matched if metric in SUPPORTED_METRICS]
    lowered = str(question or "").casefold().replace("ё", "е")
    if "CURRENT_STOCK" not in metrics and "остат" in lowered:
        metrics.append("CURRENT_STOCK")
    has_specific_price = any(
        metric in OZON_PRICE_METRICS and metric != "CURRENT_SELLING_PRICE"
        for metric in metrics
    )
    if (
        "CURRENT_SELLING_PRICE" not in metrics
        and not has_specific_price
        and any(marker in lowered for marker in ("цена", "сколько стоит"))
    ):
        metrics.append("CURRENT_SELLING_PRICE")
    return list(dict.fromkeys(metrics))


def _historical_request(question: str, date_from: str, date_to: str) -> bool:
    if str(date_from or "").strip() or str(date_to or "").strip():
        return True
    text = str(question or "").casefold().replace("ё", "е")
    return any(marker in text for marker in (
        "вчера", "прошл", "на 1 ", "на 2 ", "на 3 ", "на 4 ", "на 5 ",
        "в август", "в июл", "в июн", "в мае", "в апрел", "в март",
        "в феврал", "в январ", "в сентябр", "в октябр", "в ноябр", "в декабр",
    ))


def _product_identity(item: dict[str, Any], input_identifier: str) -> dict[str, Any]:
    product_id = item.get("product_id", item.get("id"))
    offer_id = item.get("offer_id")
    skus: list[str] = []
    direct_sku = item.get("sku")
    if direct_sku not in (None, ""):
        skus.append(str(direct_sku))
    for source in item.get("sources") or []:
        if isinstance(source, dict) and source.get("sku") not in (None, ""):
            skus.append(str(source["sku"]))
    skus = list(dict.fromkeys(skus))
    if str(input_identifier) in skus:
        sku = str(input_identifier)
    else:
        sku = skus[0] if len(skus) == 1 else None
    return {
        "marketplace": "OZON",
        "input_identifier": str(input_identifier),
        "product_id": str(product_id) if product_id not in (None, "") else None,
        "offer_id": str(offer_id) if offer_id not in (None, "") else None,
        "sku": sku,
        "known_skus": skus,
    }


def _same_product(item: dict[str, Any], entity: dict[str, Any]) -> bool:
    pid = item.get("product_id", item.get("id"))
    return pid not in (None, "") and str(pid) == str(entity.get("product_id"))


def _decimal_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError):
        return None


def _price_result(item: dict[str, Any], metric_id: str = "CURRENT_SELLING_PRICE") -> dict[str, Any] | None:
    contract = _OZON_PRICE_FIELD_CONTRACT.get(metric_id)
    if contract is None:
        return None
    price = item.get("price") if isinstance(item.get("price"), dict) else item
    field = str(contract["field"])
    amount = _decimal_text(price.get(field))
    if amount is None:
        return None
    currency = str(price.get("currency_code") or item.get("currency_code") or "RUB")
    return {
        "metric_id": metric_id,
        "amount": amount,
        "currency": currency,
        "provider_field": field,
        "provider_path": f"price.{field}",
        "provider_values": {
            "marketing_seller_price": price.get("marketing_seller_price"),
            "price": price.get("price"),
            "old_price": price.get("old_price"),
            "min_price": price.get("min_price"),
        },
        "meaning": str(contract["meaning"]),
        "limitation": str(contract["limitation"]),
    }


def _stock_rows(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw = item.get("stocks")
    rows: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for bucket_type, value in raw.items():
            if isinstance(value, dict):
                rows.append({"type": str(value.get("type") or bucket_type), **value})
    elif isinstance(raw, list):
        rows = [value for value in raw if isinstance(value, dict)]
    return rows


def _stock_result(item: dict[str, Any]) -> dict[str, Any]:
    breakdown = []
    present_total = 0
    reserved_total = 0
    seen_skus: set[str] = set()
    for row in _stock_rows(item):
        present = int(row.get("present") or 0)
        reserved = int(row.get("reserved") or 0)
        present_total += present
        reserved_total += reserved
        sku = row.get("sku")
        if sku not in (None, ""):
            seen_skus.add(str(sku))
        breakdown.append({
            "type": str(row.get("type") or "unknown").lower(),
            "present": present,
            "reserved": reserved,
            "sku": str(sku) if sku not in (None, "") else None,
            "warehouse_ids": list(row.get("warehouse_ids") or []),
        })
    return {
        "metric_id": "CURRENT_STOCK",
        "available_units": present_total,
        "reserved_units": reserved_total,
        "breakdown": breakdown,
        "observed_skus": sorted(seen_skus),
        "meaning": "sum of provider present across returned Ozon stock buckets",
        "limitation": "seller-cabinet operational stock, not public-card availability",
    }


def _pick_single(items: Iterable[dict[str, Any]], *, stage: str, entity: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    candidates = list(items)
    if entity is not None:
        candidates = [item for item in candidates if _same_product(item, entity)]
    if len(candidates) == 1:
        return candidates[0], None
    return None, {
        "ok": False,
        "error": "product_not_found" if not candidates else "product_ambiguous",
        "code": "PRODUCT_NOT_FOUND" if not candidates else "PRODUCT_AMBIGUOUS",
        "retryable": False,
        "stage": stage,
        "complete": False,
        "match_count": len(candidates),
    }


async def execute_ozon_current_snapshot(
    ozon: Any,
    *,
    question: str,
    seller: str = "",
    date_from: str = "",
    date_to: str = "",
    product_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Execute the approved Ozon current price/stock vertical slice."""
    metrics = requested_snapshot_metrics(question)
    if not metrics:
        raise SemanticOzonSnapshotError("No approved Ozon current-snapshot metric was recognized.")
    if any(metric not in SUPPORTED_METRICS for metric in metrics):
        raise SemanticOzonSnapshotError("The request contains an unapproved Ozon snapshot metric.")
    if _historical_request(question, date_from, date_to):
        return {
            "ok": False,
            "error": "historical_source_absent",
            "code": "HISTORICAL_SOURCE_ABSENT",
            "retryable": False,
            "complete": False,
            "temporal_class": "HISTORICAL_PERIOD",
            "requested_metrics": metrics,
            "forbidden_substitutes": ["current Ozon price snapshot", "current Ozon stock snapshot"],
        }

    cabinet, cabinet_error = _canonical_cabinet(ozon, seller, question)
    if cabinet_error:
        return cabinet_error
    assert cabinet is not None
    creds, named_error = resolve_named_cabinet(ozon.client, cabinet)
    if named_error:
        return {**named_error, "complete": False}
    assert creds is not None

    identifier, identifier_error = _extract_identifier(question, product_ids)
    if identifier_error:
        return identifier_error
    assert identifier is not None

    product_spec = ozon.catalog.get("ozon_product_info_list")
    if product_spec is None:
        return {"ok": False, "error": "source_contract_missing", "code": "SOURCE_CONTRACT_MISSING", "stage": "entity", "complete": False}
    identifier_field = _identifier_lookup_field(question, identifier)
    entity_response = await ozon.client.call_spec(
        product_spec,
        json_body={identifier_field: [identifier]},
        creds_override=creds,
    )
    if not isinstance(entity_response, dict) or entity_response.get("ok") is not True:
        return _provider_error("entity", entity_response)
    entity_item, entity_error = _pick_single(_items(entity_response), stage="entity")
    if entity_error:
        return entity_error
    assert entity_item is not None
    entity = _product_identity(entity_item, identifier)
    if not entity.get("product_id"):
        return {"ok": False, "error": "product_identity_incomplete", "code": "PRODUCT_IDENTITY_INCOMPLETE", "stage": "entity", "complete": False}

    leg_states: dict[str, str] = {"entity": "PASS"}
    result: dict[str, Any] = {
        "ok": True,
        "executor": OZON_SNAPSHOT_EXECUTOR_VERSION,
        "marketplace": "ozon",
        "cabinet": cabinet,
        "temporal_class": "CURRENT_SNAPSHOT",
        "requested_metrics": metrics,
        "entity": entity,
    }

    requested_price_metrics = [metric for metric in metrics if metric in OZON_PRICE_METRICS]
    if requested_price_metrics:
        price_spec = ozon.catalog.get("ozon_prices_get")
        if price_spec is None:
            return {"ok": False, "error": "source_contract_missing", "code": "SOURCE_CONTRACT_MISSING", "stage": "price", "complete": False}
        price_response = await ozon.client.call_spec(
            price_spec,
            json_body={"filter": {"product_id": [entity["product_id"]], "visibility": "ALL"}, "limit": 100},
            creds_override=creds,
        )
        if not isinstance(price_response, dict) or price_response.get("ok") is not True:
            return {**_provider_error("price", price_response), "entity": entity, "cabinet": cabinet, "leg_states": {**leg_states, "price": "FAIL"}}
        price_item, price_error = _pick_single(_items(price_response), stage="price", entity=entity)
        if price_error:
            return {**price_error, "entity": entity, "cabinet": cabinet, "leg_states": {**leg_states, "price": "FAIL"}}

        price_metrics: dict[str, Any] = {}
        for metric_id in requested_price_metrics:
            parsed_price = _price_result(price_item or {}, metric_id)
            if parsed_price is None:
                return {
                    "ok": False,
                    "error": "price_semantics_missing",
                    "code": "PRICE_SEMANTICS_MISSING",
                    "stage": "price",
                    "complete": False,
                    "entity": entity,
                    "cabinet": cabinet,
                    "metric_id": metric_id,
                }
            price_metrics[metric_id] = parsed_price
            result[_OZON_PRICE_FIELD_CONTRACT[metric_id]["result_key"]] = parsed_price
            price_observation = build_metric_observation(
                metric_id=metric_id,
                value=parsed_price["amount"],
                unit=parsed_price["currency"],
                marketplace="ozon",
                source_name="ozon_current_prices",
                source_field=parsed_price["provider_path"],
            ).to_dict()
            price_observation["dimensions"] = {
                "product_id": entity.get("product_id"),
                "offer_id": entity.get("offer_id"),
                "sku": entity.get("sku"),
            }
            result.setdefault("metric_observations", []).append(price_observation)

        result["price_metrics"] = price_metrics
        leg_states["price"] = "PASS"

    if "CURRENT_STOCK" in metrics:
        stock_spec = ozon.catalog.get("ozon_stocks_info")
        if stock_spec is None:
            return {"ok": False, "error": "source_contract_missing", "code": "SOURCE_CONTRACT_MISSING", "stage": "stock", "complete": False}
        stock_response = await ozon.client.call_spec(
            stock_spec,
            json_body={"filter": {"product_id": [entity["product_id"]], "visibility": "ALL"}, "limit": 100},
            creds_override=creds,
        )
        if not isinstance(stock_response, dict) or stock_response.get("ok") is not True:
            return {**_provider_error("stock", stock_response), "entity": entity, "cabinet": cabinet, "leg_states": {**leg_states, "stock": "FAIL"}}
        stock_item, stock_error = _pick_single(_items(stock_response), stage="stock", entity=entity)
        if stock_error:
            return {**stock_error, "entity": entity, "cabinet": cabinet, "leg_states": {**leg_states, "stock": "FAIL"}}
        parsed_stock = _stock_result(stock_item or {})
        expected_skus = set(entity.get("known_skus") or [])
        observed_skus = set(parsed_stock.get("observed_skus") or [])
        if expected_skus and observed_skus and expected_skus.isdisjoint(observed_skus):
            return {
                "ok": False,
                "error": "product_join_mismatch",
                "code": "PRODUCT_JOIN_MISMATCH",
                "stage": "join",
                "complete": False,
                "entity": entity,
                "cabinet": cabinet,
                "details": {"entity_skus": sorted(expected_skus), "stock_skus": sorted(observed_skus)},
            }
        result["current_stock"] = parsed_stock
        stock_observation = build_metric_observation(
            metric_id="CURRENT_STOCK",
            value=parsed_stock["available_units"],
            unit="UNITS",
            marketplace="ozon",
            source_name="ozon_current_stocks",
            source_field="stocks.present",
        ).to_dict()
        stock_observation["dimensions"] = {
            "product_id": entity.get("product_id"),
            "offer_id": entity.get("offer_id"),
            "sku": entity.get("sku"),
        }
        result.setdefault("metric_observations", []).append(stock_observation)
        leg_states["stock"] = "PASS"

    result["leg_states"] = leg_states
    result["complete"] = all(
        leg_states.get(stage) == "PASS"
        for stage in (
            ["entity"]
            + (["price"] if requested_price_metrics else [])
            + (["stock"] if "CURRENT_STOCK" in metrics else [])
        )
    )
    result["provenance"] = {
        "entity_source": "ozon_product_info_list",
        "price_source": "ozon_prices_get" if requested_price_metrics else None,
        "stock_source": "ozon_stocks_info" if "CURRENT_STOCK" in metrics else None,
        "named_cabinet": True,
        "join_key": "product_id",
        "entity_lookup_field": identifier_field,
    }
    result["limitations"] = [
        "CURRENT_SELLING_PRICE uses marketing_seller_price and is not a guaranteed personalized buyer checkout price."
    ] if "CURRENT_SELLING_PRICE" in metrics else []
    result["knowledge_catalog_version"] = "marketplace_knowledge_catalog.v1"
    return result
