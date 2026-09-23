"""Read-only post-refresh verification for canonical marketplace archives."""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .archive_coverage import parse_registry as parse_coverage_registry
from .archive_refresh import REFRESH_CONTRACTS
from .wb_advertising_archive import canonical_location, coverage_registry_location, parse_csv
from .wb_finance_archive import DATASET as FINANCE_DATASET, parse_csv_bytes
from .ozon_current_archive import (
    DATASETS as OZON_CURRENT_DATASETS,
    canonical_location as ozon_current_location,
    coverage_location as ozon_coverage_location,
    parse_snapshot as parse_ozon_snapshot,
)
from .ozon_final_archive import (
    DATASET as OZON_FINAL_DATASET,
    annual_location as ozon_final_annual_location,
    closed_months as ozon_final_closed_months,
    coverage_location as ozon_final_coverage_location,
    month_period as ozon_final_month_period,
    monthly_location as ozon_final_monthly_location,
    parse_final_coverage as parse_ozon_final_coverage,
)


_DATE_FIELDS: dict[str, tuple[str, ...]] = {
    "wb_weekly_finance_main": ("dateTo", "rrDate", "saleDt"),
    "ads_campaign_roster_snapshots": ("observed_at",),
    "ads_campaign_daily": ("date",),
    "ads_product_daily": ("date",),
    "ads_search_cluster_daily": ("date",),
    "ads_campaign_snapshots": ("observed_at",),
    "ads_expenses": ("upd_time", "request_date_to"),
    "ads_payments": ("date", "request_date_to"),
}


def _date_prefix(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    candidate = text[:10]
    if len(candidate) == 10 and candidate[4:5] == "-" and candidate[7:8] == "-":
        return candidate
    return None


def _max_date(rows: Iterable[Mapping[str, Any]], fields: Iterable[str]) -> str | None:
    values: list[str] = []
    for row in rows:
        for field in fields:
            value = _date_prefix(row.get(field))
            if value:
                values.append(value)
    return max(values) if values else None


def _stable_key_quality(
    rows: Iterable[Mapping[str, Any]],
    fields: tuple[str, ...],
) -> dict[str, int]:
    seen: set[tuple[str, ...]] = set()
    duplicate_rows = 0
    incomplete_rows = 0
    total_rows = 0
    for row in rows:
        total_rows += 1
        key = tuple(str(row.get(field) or "").strip() for field in fields)
        if not all(key):
            incomplete_rows += 1
            continue
        if key in seen:
            duplicate_rows += 1
        else:
            seen.add(key)
    return {
        "rows": total_rows,
        "unique_stable_keys": len(seen),
        "duplicate_stable_key_rows": duplicate_rows,
        "incomplete_stable_key_rows": incomplete_rows,
    }


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def verify_finance_cabinet(store: Any, *, cabinet: str, year: int) -> dict[str, Any]:
    folder = await store.ensure_folder_path(
        ["База данных", "WB", cabinet, str(year), "finance", "weekly", "main"]
    )
    filename = f"{cabinet}__weekly_main__{year}.csv"
    item, raw = await store.download_named(folder, filename)
    _, rows = parse_csv_bytes(raw or b"")

    registry_folder = await store.ensure_folder_path(["app", "registry"])
    _, registry_raw = await store.download_named(registry_folder, "reports_registry.csv")
    _, registry_rows = parse_csv_bytes(registry_raw or b"")
    records = [
        row for row in registry_rows
        if row.get("cabinet") == cabinet
        and row.get("dataset") == FINANCE_DATASET
        and row.get("status") == "COMPLETE"
        and _int(row.get("year")) == int(year)
    ]

    stable = REFRESH_CONTRACTS["finance"].stable_keys[FINANCE_DATASET]
    quality = _stable_key_quality(rows, stable)
    canonical_report_ids = {
        _int(row.get("reportId")) for row in rows if _int(row.get("reportId")) > 0
    }
    registry_report_ids = {
        _int(row.get("report_id")) for row in records if _int(row.get("report_id")) > 0
    }
    missing_nonempty = sorted({
        _int(row.get("report_id"))
        for row in records
        if _int(row.get("report_id")) > 0
        and _int(row.get("rows")) > 0
        and _int(row.get("report_id")) not in canonical_report_ids
    })
    max_registry_date = _max_date(records, ("date_to", "logical_week_to"))
    max_canonical_date = _max_date(rows, _DATE_FIELDS[FINANCE_DATASET])

    initialized = bool(item is not None and records)
    ok = (
        initialized
        and quality["duplicate_stable_key_rows"] == 0
        and quality["incomplete_stable_key_rows"] == 0
        and not missing_nonempty
    )
    return {
        "ok": ok,
        "initialized": initialized,
        "marketplace": "wb",
        "dataset": FINANCE_DATASET,
        "cabinet": cabinet,
        "year": int(year),
        "canonical_file": filename,
        "canonical_file_present": item is not None,
        "bytes": int(getattr(item, "size", 0) or 0) if item is not None else 0,
        "max_canonical_date": max_canonical_date,
        "max_registry_coverage_date": max_registry_date,
        "registry_complete_report_ids": len(registry_report_ids),
        "canonical_report_ids": len(canonical_report_ids),
        "missing_registry_report_ids_with_rows": missing_nonempty,
        **quality,
    }


async def verify_advertising_cabinet(store: Any, *, cabinet: str, year: int) -> dict[str, Any]:
    registry_parts, registry_name = coverage_registry_location()
    registry_parent = await store.ensure_folder_path(registry_parts)
    _, registry_raw = await store.download_named(registry_parent, registry_name)
    coverage_rows = parse_coverage_registry(registry_raw)

    datasets: dict[str, Any] = {}
    all_ok = True
    contract = REFRESH_CONTRACTS["advertising"]
    for dataset in contract.datasets:
        parts, filename = canonical_location(cabinet, year, dataset)
        parent = await store.ensure_folder_path(parts)
        item, raw = await store.download_named(parent, filename)
        _, rows = parse_csv(raw)
        stable = contract.stable_keys[dataset]
        quality = _stable_key_quality(rows, stable)
        records = [
            row for row in coverage_rows
            if row.get("marketplace") == "wb"
            and row.get("cabinet") == cabinet
            and row.get("dataset") == dataset
            and row.get("status") == "COMPLETE"
            and row.get("quality_status") in {"PASS", "PASS_WITH_FLAGS"}
        ]
        max_coverage = _max_date(records, ("date_to",))
        max_canonical = _max_date(rows, _DATE_FIELDS.get(dataset, ()))
        dataset_ok = (
            (item is not None or not records)
            and quality["duplicate_stable_key_rows"] == 0
            and quality["incomplete_stable_key_rows"] == 0
        )
        all_ok = all_ok and dataset_ok
        datasets[dataset] = {
            "ok": dataset_ok,
            "canonical_file": filename,
            "canonical_file_present": item is not None,
            "bytes": int(getattr(item, "size", 0) or 0) if item is not None else 0,
            "max_canonical_date": max_canonical,
            "max_registry_coverage_date": max_coverage,
            "coverage_records": len(records),
            **quality,
        }

    roster = datasets.get("ads_campaign_roster_snapshots") or {}
    initialized = bool(
        roster.get("canonical_file_present")
        and int(roster.get("coverage_records", 0) or 0) > 0
    )
    all_ok = all_ok and initialized
    return {
        "ok": all_ok,
        "initialized": initialized,
        "marketplace": "wb",
        "dataset_family": "advertising",
        "cabinet": cabinet,
        "year": int(year),
        "datasets": datasets,
    }


def _parse_semicolon_registry(raw: bytes | None) -> list[dict[str, str]]:
    if not raw:
        return []
    reader = csv.DictReader(
        io.StringIO(raw.decode("utf-8-sig")),
        delimiter=";",
    )
    return [dict(row) for row in reader]


async def verify_ozon_current_cabinet(
    store: Any,
    *,
    cabinet: str,
    year: int,
) -> dict[str, Any]:
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    period = today.strftime("%Y-%m")
    if int(year) != today.year:
        return {
            "ok": False,
            "initialized": False,
            "marketplace": "ozon",
            "dataset_family": "ozon_current",
            "cabinet": cabinet,
            "year": int(year),
            "period": period,
            "error": "ozon_current_year_must_be_open_year",
        }

    coverage_parts, coverage_name = ozon_coverage_location(
        cabinet,
        today.year,
        today.month,
    )
    coverage_parent = await store.ensure_folder_path(coverage_parts)
    coverage_item, coverage_raw = await store.download_named(
        coverage_parent,
        coverage_name,
    )
    coverage_rows = _parse_semicolon_registry(coverage_raw)

    contract = REFRESH_CONTRACTS["ozon_current"]
    datasets: dict[str, Any] = {}
    all_ok = coverage_item is not None
    for dataset in contract.datasets:
        parts, filename = ozon_current_location(
            cabinet,
            today.year,
            today.month,
            dataset,
        )
        parent = await store.ensure_folder_path(parts)
        item, raw = await store.download_named(parent, filename)
        _fields, rows = parse_ozon_snapshot(raw)
        stable = contract.stable_keys[dataset]
        quality = _stable_key_quality(rows, stable)
        records = [
            row for row in coverage_rows
            if row.get("marketplace") == "ozon"
            and row.get("cabinet") == cabinet
            and row.get("dataset") == dataset
            and row.get("period") == period
            and row.get("status") == "COMPLETE"
        ]
        coverage = records[-1] if records else None
        actual_sha = hashlib.sha256(raw or b"").hexdigest() if raw is not None else None
        coverage_rows_count = _int((coverage or {}).get("rows"))
        file_ok = bool(
            item is not None
            and coverage is not None
            and coverage.get("date_to") == today.isoformat()
            and coverage_rows_count == len(rows)
            and str(coverage.get("sha256") or "").lower() == str(actual_sha or "").lower()
            and quality["duplicate_stable_key_rows"] == 0
            and quality["incomplete_stable_key_rows"] == 0
        )
        all_ok = all_ok and file_ok
        datasets[dataset] = {
            "ok": file_ok,
            "canonical_file": filename,
            "canonical_file_present": item is not None,
            "coverage_present": coverage is not None,
            "coverage_date_to": (coverage or {}).get("date_to"),
            "coverage_rows": coverage_rows_count,
            "bytes": len(raw or b""),
            "sha256": actual_sha,
            **quality,
        }

    initialized = bool(
        coverage_item is not None
        and all(datasets.get(name, {}).get("canonical_file_present") for name in contract.datasets)
    )
    return {
        "ok": bool(all_ok and initialized),
        "initialized": initialized,
        "marketplace": "ozon",
        "dataset_family": "ozon_current",
        "cabinet": cabinet,
        "year": today.year,
        "period": period,
        "coverage_registry": coverage_name,
        "coverage_registry_present": coverage_item is not None,
        "datasets": datasets,
    }


async def verify_ozon_final_cabinet(
    store: Any,
    *,
    cabinet: str,
    year: int,
) -> dict[str, Any]:
    expected_months = ozon_final_closed_months(int(year))
    expected_periods = [
        ozon_final_month_period(int(year), month)
        for month in expected_months
    ]

    coverage_parts, coverage_name = ozon_final_coverage_location(cabinet, int(year))
    coverage_parent = await store.ensure_folder_path(coverage_parts)
    coverage_item, coverage_raw = await store.download_named(
        coverage_parent,
        coverage_name,
    )
    coverage_rows = parse_ozon_final_coverage(coverage_raw)
    records = [
        row for row in coverage_rows
        if row.get("marketplace") == "ozon"
        and row.get("cabinet") == cabinet
        and row.get("dataset") == OZON_FINAL_DATASET
        and row.get("status") == "COMPLETE"
    ]
    by_period = {str(row.get("period") or ""): row for row in records}

    contract = REFRESH_CONTRACTS["ozon_final"]
    stable = contract.stable_keys[OZON_FINAL_DATASET]
    months: dict[str, Any] = {}
    total_month_rows = 0
    all_ok = coverage_item is not None

    for month in expected_months:
        period = ozon_final_month_period(int(year), month)
        parts, filename = ozon_final_monthly_location(cabinet, int(year), month)
        parent = await store.ensure_folder_path(parts)
        item, raw = await store.download_named(parent, filename)
        _fields, rows = parse_ozon_snapshot(raw)
        quality = _stable_key_quality(rows, stable)
        coverage = by_period.get(period)
        actual_sha = hashlib.sha256(raw or b"").hexdigest() if raw is not None else None
        coverage_rows_count = _int((coverage or {}).get("rows"))
        month_ok = bool(
            item is not None
            and coverage is not None
            and coverage_rows_count == len(rows)
            and str((coverage or {}).get("sha256") or "").lower()
            == str(actual_sha or "").lower()
            and quality["duplicate_stable_key_rows"] == 0
            and quality["incomplete_stable_key_rows"] == 0
        )
        all_ok = all_ok and month_ok
        total_month_rows += len(rows)
        months[period] = {
            "ok": month_ok,
            "canonical_file": filename,
            "canonical_file_present": item is not None,
            "coverage_present": coverage is not None,
            "coverage_rows": coverage_rows_count,
            "bytes": len(raw or b""),
            "sha256": actual_sha,
            **quality,
        }

    annual_parts, annual_name = ozon_final_annual_location(cabinet, int(year))
    annual_parent = await store.ensure_folder_path(annual_parts)
    annual_item, annual_raw = await store.download_named(annual_parent, annual_name)
    _annual_fields, annual_rows = parse_ozon_snapshot(annual_raw)
    annual_quality = _stable_key_quality(annual_rows, stable)
    annual_periods = sorted({
        str(row.get("report_month") or "")
        for row in annual_rows
        if str(row.get("report_month") or "")
    })
    annual_ok = bool(
        annual_item is not None
        and len(annual_rows) == total_month_rows
        and annual_periods == expected_periods
        and annual_quality["duplicate_stable_key_rows"] == 0
        and annual_quality["incomplete_stable_key_rows"] == 0
    )
    all_ok = all_ok and annual_ok
    initialized = bool(
        coverage_item is not None
        and annual_item is not None
        and expected_periods
        and all(period in by_period for period in expected_periods)
    )
    return {
        "ok": bool(all_ok and initialized),
        "initialized": initialized,
        "marketplace": "ozon",
        "dataset_family": "ozon_final",
        "cabinet": cabinet,
        "year": int(year),
        "expected_closed_months": expected_periods,
        "coverage_registry": coverage_name,
        "coverage_registry_present": coverage_item is not None,
        "coverage_records": len(records),
        "months": months,
        "annual": {
            "ok": annual_ok,
            "canonical_file": annual_name,
            "canonical_file_present": annual_item is not None,
            "bytes": len(annual_raw or b""),
            "expected_rows_from_months": total_month_rows,
            "periods": annual_periods,
            **annual_quality,
        },
    }


async def verify_registered_archive(
    store: Any,
    *,
    marketplace: str,
    year: int,
    finance_cabinets: Iterable[str] = (),
    advertising_cabinets: Iterable[str] = (),
    ozon_current_cabinets: Iterable[str] = (),
    ozon_final_cabinets: Iterable[str] = (),
    families: Iterable[str] = ("finance", "advertising"),
) -> dict[str, Any]:
    marketplace = str(marketplace).lower()
    selected = tuple(families)
    checks: list[dict[str, Any]] = []

    if marketplace == "wb":
        if "finance" in selected:
            for cabinet in finance_cabinets:
                checks.append(
                    await verify_finance_cabinet(
                        store,
                        cabinet=cabinet,
                        year=int(year),
                    )
                )
        if "advertising" in selected:
            for cabinet in advertising_cabinets:
                checks.append(
                    await verify_advertising_cabinet(
                        store,
                        cabinet=cabinet,
                        year=int(year),
                    )
                )
    elif marketplace == "ozon":
        if "ozon_final" in selected:
            for cabinet in ozon_final_cabinets:
                checks.append(
                    await verify_ozon_final_cabinet(
                        store,
                        cabinet=cabinet,
                        year=int(year),
                    )
                )
        if "ozon_current" in selected:
            for cabinet in ozon_current_cabinets:
                checks.append(
                    await verify_ozon_current_cabinet(
                        store,
                        cabinet=cabinet,
                        year=int(year),
                    )
                )
    else:
        return {
            "ok": False,
            "error": "archive_refresh_not_registered",
            "marketplace": marketplace,
        }

    return {
        "ok": bool(checks) and all(bool(item.get("ok")) for item in checks),
        "marketplace": marketplace,
        "year": int(year),
        "dataset_families": list(selected),
        "checks": checks,
        "interpretation": (
            "Canonical files, stable-key uniqueness and committed coverage are verified. "
            "For Ozon FINAL, every closed month must be present in monthly_source and the annual union; "
            "for Ozon CURRENT, coverage must reach the current Moscow calendar day; "
            "for WB, provider freshness remains established by the completed refresh cycle."
        ),
    }
