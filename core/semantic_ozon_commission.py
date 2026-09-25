"""Approved Ozon CURRENT commission semantic.

Uses only the canonical CURRENT accrual archive with COMPLETE coverage.
Commission means the provider's posting product field commission.sale_commission.
No other fee, logistics, storage or payout field is silently substituted.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
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
VERSION = "ozon_current_commission.v1"
DATASET = "ozon_current_accruals"

class SemanticOzonCommissionError(RuntimeError):
    pass

def _norm(v: Any) -> str:
    return " ".join(re.sub(r"[^a-zа-я0-9₽]+"," ",str(v or "").casefold().replace("ё","е")).split())

def requested_ozon_commission(question: str) -> dict[str, Any] | None:
    t=_norm(question)
    if "комисс" not in t and "вознаграждение за продаж" not in t:
        return None
    if any(x in t for x in ("реклам", "трафарет", "продвижен")):
        return None
    return {"metric":"COMMISSION","requested_measure":"RUB"}

def _cabinet(seller: str) -> str:
    entry=resolve_business_cabinet("ozon",str(seller or "").strip())
    value=entry.cabinet if entry else str(seller or "").strip()
    if value not in OZON_ARCHIVE_CABINETS:
        raise SemanticOzonCommissionError("Ozon cabinet is required and must be configured")
    return value

def _coverage_rows(raw: bytes) -> list[dict[str,str]]:
    reader=csv.DictReader(io.StringIO(raw.decode("utf-8-sig")),delimiter=";")
    return [dict(x) for x in reader]

def _raw(row: dict[str,str]) -> dict[str,Any]:
    try:
        value=json.loads(str(row.get("_raw_json") or "{}"))
    except json.JSONDecodeError as exc:
        raise SemanticOzonCommissionError("Ozon CURRENT accrual row contains invalid raw JSON") from exc
    if not isinstance(value,dict):
        raise SemanticOzonCommissionError("Ozon CURRENT accrual raw JSON is not an object")
    return value

def _money(v: Any) -> Decimal:
    if isinstance(v, dict):
        for key in ("amount", "value", "accrued"):
            if v.get(key) not in (None, ""):
                v = v.get(key)
                break
        else:
            raise SemanticOzonCommissionError("Ozon commission money object has no approved numeric field")
    try:
        return Decimal(str(v or "0").replace(",","."))
    except InvalidOperation as exc:
        raise SemanticOzonCommissionError("Ozon commission field is not numeric") from exc

def _period(question: str,date_from: str,date_to: str,coverage: dict[str,str]) -> tuple[date,date]:
    cov_from=date.fromisoformat(coverage["date_from"])
    cov_to=date.fromisoformat(coverage["date_to"])
    if bool(date_from)!=bool(date_to):
        raise SemanticOzonCommissionError("date_from and date_to must be supplied together")
    if date_from:
        start=date.fromisoformat(date_from); end=date.fromisoformat(date_to)
    else:
        t=_norm(question)
        months={"январ":1,"феврал":2,"март":3,"апрел":4,"май":5,"июн":6,"июл":7,"август":8,"сентябр":9,"октябр":10,"ноябр":11,"декабр":12}
        found=next((m for stem,m in months.items() if stem in t),None)
        year_match=re.search(r"\b(20\d{2})\b",t)
        now=datetime.now(MOSCOW).date()
        if found is not None and (found != now.month or (year_match and int(year_match.group(1)) != now.year)):
            raise SemanticOzonCommissionError("Ozon CURRENT commission supports only the open month")
        start=cov_from; end=cov_to
    if start<cov_from or end>cov_to or start>end:
        raise SemanticOzonCommissionError("Requested period is outside canonical Ozon CURRENT coverage")
    return start,end

async def execute_ozon_current_commission(
    store: Any, *, question: str, seller: str="", date_from: str="", date_to: str=""
) -> dict[str,Any]:
    if requested_ozon_commission(question) is None:
        raise SemanticOzonCommissionError("No Ozon commission semantic recognized")
    cabinet=_cabinet(seller)
    today=datetime.now(MOSCOW).date()
    parts,cov_name=coverage_location(cabinet,today.year,today.month)
    parent=await store.ensure_folder_path(parts)
    _,cov_raw=await store.download_named(parent,cov_name)
    if cov_raw is None:
        raise SemanticOzonCommissionError("Canonical Ozon CURRENT coverage registry is missing")
    cov=[x for x in _coverage_rows(cov_raw) if x.get("dataset")==DATASET and x.get("status")=="COMPLETE"]
    if len(cov)!=1:
        raise SemanticOzonCommissionError("Canonical Ozon CURRENT accrual coverage is not exactly one COMPLETE record")
    coverage=cov[0]
    start,end=_period(question,date_from,date_to,coverage)

    data_parts,data_name=canonical_location(cabinet,today.year,today.month,DATASET)
    if coverage.get("canonical_file")!=data_name:
        raise SemanticOzonCommissionError("Ozon CURRENT coverage points to an unexpected accrual file")
    data_parent=await store.ensure_folder_path(data_parts)
    _,raw=await store.download_named(data_parent,data_name)
    if raw is None:
        raise SemanticOzonCommissionError("Canonical Ozon CURRENT accrual file is missing")
    sha=hashlib.sha256(raw).hexdigest()
    if coverage.get("sha256") and coverage["sha256"].lower()!=sha:
        raise SemanticOzonCommissionError("Ozon CURRENT accrual hash does not match COMPLETE coverage")
    _,rows=parse_snapshot(raw)
    if len(rows)!=int(coverage.get("rows") or "0"):
        raise SemanticOzonCommissionError("Ozon CURRENT accrual row count does not match COMPLETE coverage")
    q=stable_key_quality(rows,tuple(DATASETS[DATASET]["stable_key"]))
    if q["duplicate_stable_key_rows"] or q["incomplete_stable_key_rows"]:
        raise SemanticOzonCommissionError("Ozon CURRENT accrual stable-key validation failed")

    signed=Decimal("0"); product_lines=0; posting_rows=0
    for row in rows:
        obj=_raw(row)
        try:
            row_date=date.fromisoformat(str(obj.get("date") or ""))
        except ValueError:
            continue
        if not (start<=row_date<=end) or str(obj.get("accrued_category") or "")!="POSTING":
            continue
        posting_rows+=1
        posting=obj.get("posting")
        if not isinstance(posting,dict):
            continue
        for product in posting.get("products") or []:
            if not isinstance(product,dict):
                continue
            commission=product.get("commission")
            if not isinstance(commission,dict) or commission.get("sale_commission") in (None,""):
                continue
            signed += _money(commission.get("sale_commission"))
            product_lines+=1

    expense=abs(signed)
    return {
        "ok":True,"complete":True,"executor":VERSION,"marketplace":"ozon","cabinet":cabinet,
        "metric_id":"COMMISSION","unit":"RUB","value":float(expense),
        "signed_provider_value":float(signed),
        "period":{"date_from":start.isoformat(),"date_to":end.isoformat()},
        "source":"ozon_current_accruals",
        "source_field":"posting.products[].commission.sale_commission",
        "coverage":{"status":"FULL_COVERAGE","rows":len(rows),"sha256":sha,"date_from":coverage["date_from"],"date_to":coverage["date_to"]},
        "posting_rows_scanned":posting_rows,"commission_product_lines":product_lines,
        "semantic_note":"Commission expense is the absolute value of provider sale_commission; signed_provider_value is retained for audit.",
    }
