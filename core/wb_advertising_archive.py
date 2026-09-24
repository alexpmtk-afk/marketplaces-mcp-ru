"""Archive primitives for Wildberries Advertising Archive V1.

This module contains provider-response normalization, deterministic annual CSV
merge rules and canonical archive paths. It performs no network I/O. Durable
provider calls and Drive commits belong to the advertising archive worker.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from .wb_advertising_normalize import campaign_product_ids

CSV_DELIMITER = ";"
ARCHIVE_CABINETS = ("wb_dmitrieva", "wb_novokshenov", "wb_laser_master")
FULLSTATS_ELIGIBLE_STATUSES = frozenset({7, 9, 11})

DATASET_PATHS: dict[str, tuple[str, ...]] = {
    "ads_campaign_roster_snapshots": ("advertising", "state", "campaign_roster"),
    "ads_campaign_daily": ("advertising", "stats", "campaign_daily"),
    "ads_product_daily": ("advertising", "stats", "product_daily"),
    "ads_search_cluster_daily": ("advertising", "stats", "search_cluster_daily"),
    "ads_expenses": ("advertising", "finance", "expenses"),
    "ads_payments": ("advertising", "finance", "payments"),
    "ads_campaign_snapshots": ("advertising", "state", "campaign_snapshots"),
    "ads_product_identity_snapshots": ("advertising", "state", "product_identity"),
}

DATASET_KEYS: dict[str, tuple[str, ...]] = {
    "ads_campaign_roster_snapshots": ("observed_at", "campaign_id"),
    "ads_campaign_daily": ("date", "campaign_id"),
    "ads_product_daily": ("date", "campaign_id", "app_type", "nm_id"),
    "ads_search_cluster_daily": ("date", "campaign_id", "nm_id", "norm_query"),
    "ads_expenses": ("event_fingerprint",),
    "ads_payments": ("event_key",),
    "ads_campaign_snapshots": ("observed_at", "campaign_id"),
    "ads_product_identity_snapshots": ("observed_at", "nm_id"),
}


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _money(value: Any) -> str:
    if value is None or value == "":
        return "0"
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"invalid money value: {value!r}")


def _campaign_id(row: Mapping[str, Any]) -> int:
    for key in ("advertId", "advert_id", "id"):
        value = _int(row.get(key))
        if value > 0:
            return value
    return 0


def _fingerprint(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_location(cabinet: str, year: int, dataset: str) -> tuple[list[str], str]:
    cabinet = str(cabinet)
    if cabinet not in ARCHIVE_CABINETS:
        raise ValueError(f"unsupported WB archive cabinet: {cabinet}")
    if dataset not in DATASET_PATHS:
        raise ValueError(f"unsupported WB advertising dataset: {dataset}")
    year = int(year)
    if year < 2020 or year > 2100:
        raise ValueError("year is outside supported archive range")
    folder = ["База данных", "WB", cabinet, str(year), *DATASET_PATHS[dataset]]
    filename = f"{cabinet}__{dataset}__{year}.csv"
    return folder, filename


def coverage_registry_location() -> tuple[list[str], str]:
    return ["app", "registry"], "dataset_coverage_registry.csv"


def normalize_campaign_roster(payload: Any, *, observed_at: str) -> list[dict[str, Any]]:
    """Normalize /adv/v1/promotion/count grouped campaign list."""
    observed_at = str(observed_at).strip()
    if not observed_at:
        raise ValueError("observed_at is required")
    root = payload if isinstance(payload, dict) else {}
    output: list[dict[str, Any]] = []
    for group in root.get("adverts") or []:
        if not isinstance(group, dict):
            continue
        status = _int(group.get("status"))
        campaign_type = _int(group.get("type"))
        for row in group.get("advert_list") or []:
            if not isinstance(row, dict):
                continue
            campaign_id = _campaign_id(row)
            if campaign_id <= 0:
                continue
            output.append({
                "observed_at": observed_at,
                "campaign_id": campaign_id,
                "campaign_type": campaign_type,
                "status": status,
                "change_time": _string(row.get("changeTime")) or None,
                "fullstats_eligible": status in FULLSTATS_ELIGIBLE_STATUSES,
            })
    output.sort(key=lambda row: (int(row["campaign_id"]), int(row["status"]), int(row["campaign_type"])))
    return output


def eligible_fullstats_campaign_ids(rows: Iterable[Mapping[str, Any]]) -> list[int]:
    return sorted({
        int(row.get("campaign_id") or 0)
        for row in rows
        if int(row.get("campaign_id") or 0) > 0
        and int(row.get("status") or 0) in FULLSTATS_ELIGIBLE_STATUSES
    })


def normalize_expenses(
    payload: Any,
    *,
    request_date_from: str,
    request_date_to: str,
) -> list[dict[str, Any]]:
    """Normalize /adv/v1/upd without assuming updNum is unique."""
    rows = payload if isinstance(payload, list) else []
    output: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        identity = {
            "upd_num": _int(raw.get("updNum")),
            "upd_time": _string(raw.get("updTime")) or None,
            "upd_sum": _money(raw.get("updSum")),
            "advert_id": _int(raw.get("advertId")),
            "campaign_name": _string(raw.get("campName")) or None,
            "advert_type": _int(raw.get("advertType")),
            "payment_type": _string(raw.get("paymentType")) or None,
            "advert_status": _int(raw.get("advertStatus")),
        }
        output.append({
            "event_fingerprint": _fingerprint(identity),
            **identity,
            "request_date_from": str(request_date_from)[:10],
            "request_date_to": str(request_date_to)[:10],
        })
    return output


def normalize_payments(
    payload: Any,
    *,
    request_date_from: str,
    request_date_to: str,
) -> list[dict[str, Any]]:
    """Normalize /adv/v1/payments, using provider id when present."""
    rows = payload if isinstance(payload, list) else []
    output: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        identity = {
            "payment_id": _int(raw.get("id")),
            "date": _string(raw.get("date")) or None,
            "sum": _money(raw.get("sum")),
            "type": _int(raw.get("type")),
            "status_id": _int(raw.get("statusId")),
            "card_status": _string(raw.get("cardStatus")) or None,
        }
        event_key = (
            f"id:{identity['payment_id']}"
            if identity["payment_id"] > 0
            else "fp:" + _fingerprint(identity)
        )
        output.append({
            "event_key": event_key,
            **identity,
            "request_date_from": str(request_date_from)[:10],
            "request_date_to": str(request_date_to)[:10],
        })
    return output


def normalize_campaign_info_snapshot(payload: Any, *, observed_at: str) -> list[dict[str, Any]]:
    """Persist current /api/advert/v2/adverts observations without inventing event time."""
    observed_at = str(observed_at).strip()
    if not observed_at:
        raise ValueError("observed_at is required")
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("adverts") or payload.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("adverts") or []
    else:
        rows = []
    current_products = campaign_product_ids(payload)
    output: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        campaign_id = _campaign_id(raw)
        if campaign_id <= 0:
            continue
        settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
        timestamps = raw.get("timestamps") if isinstance(raw.get("timestamps"), dict) else {}
        output.append({
            "observed_at": observed_at,
            "campaign_id": campaign_id,
            "status": _int(raw.get("status")),
            "payment_type": _string(
                raw.get("payment_type") or raw.get("paymentType")
                or settings.get("payment_type") or settings.get("paymentType")
            ) or None,
            "bid_type": _string(raw.get("bid_type") or raw.get("bidType")) or None,
            "currency": _string(raw.get("currency")) or None,
            "campaign_nm_ids": sorted(current_products.get(campaign_id, set())),
            "name": _string(raw.get("name") or settings.get("name")) or None,
            "placements": settings.get("placements") if isinstance(settings.get("placements"), dict) else None,
            "created_at": _string(timestamps.get("created")) or None,
            "started_at": _string(timestamps.get("started")) or None,
            "updated_at": _string(timestamps.get("updated")) or None,
            "deleted_at": _string(timestamps.get("deleted")) or None,
            "nm_settings_json": raw.get("nm_settings") if isinstance(raw.get("nm_settings"), list) else [],
            "type": _int(raw.get("type")),
            "raw_json": json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        })
    output.sort(key=lambda row: int(row["campaign_id"]))
    return output


def parse_csv(data: bytes | None) -> tuple[list[str], list[dict[str, str]]]:
    if not data:
        return [], []
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter=CSV_DELIMITER)
    return list(reader.fieldnames or []), [dict(row) for row in reader]


def encode_csv(fieldnames: Iterable[str], rows: Iterable[Mapping[str, Any]]) -> bytes:
    fields = list(fieldnames)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=fields,
        delimiter=CSV_DELIMITER,
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _string(row.get(field)) for field in fields})
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def merge_annual_dataset(
    dataset: str,
    existing: bytes | None,
    new_rows: Iterable[Mapping[str, Any]],
) -> tuple[bytes, dict[str, Any]]:
    """Upsert one advertising dataset by its canonical stable key.

    A repeated provider fetch may contain corrected statistics for an already
    known grain. Incoming rows therefore replace the same stable key while the
    operation remains idempotent: replaying identical input changes no row
    count and cannot create duplicates.
    """
    if dataset not in DATASET_KEYS:
        raise ValueError(f"unsupported WB advertising dataset: {dataset}")
    key_fields = DATASET_KEYS[dataset]
    old_fields, old_rows = parse_csv(existing)
    incoming = [dict(row) for row in new_rows]
    fields = list(old_fields)
    seen = set(fields)
    for row in incoming:
        for field in row:
            if field not in seen:
                fields.append(field)
                seen.add(field)
    if not fields and incoming:
        fields = list(incoming[0])
    if not fields:
        return existing or b"", {"added_rows": 0, "total_rows": 0, "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}

    by_key: dict[tuple[str, ...], dict[str, Any]] = {}

    def stable_key(row: Mapping[str, Any]) -> tuple[str, ...]:
        key = tuple(_string(row.get(field)) for field in key_fields)
        if not all(key):
            raise ValueError(f"{dataset} row has incomplete stable key {key_fields}: {key}")
        return key

    for row in old_rows:
        by_key[stable_key(row)] = dict(row)
    before = len(by_key)
    for row in incoming:
        by_key[stable_key(row)] = row

    rows = [by_key[key] for key in sorted(by_key)]
    payload = encode_csv(fields, rows)
    return payload, {
        "added_rows": len(rows) - before,
        "total_rows": len(rows),
        "columns": len(fields),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
