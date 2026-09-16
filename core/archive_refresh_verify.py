"""Read-only post-refresh verification for canonical marketplace archives."""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .archive_coverage import parse_registry as parse_coverage_registry
from .archive_refresh import REFRESH_CONTRACTS
from .wb_advertising_archive import canonical_location, coverage_registry_location, parse_csv
from .wb_finance_archive import DATASET as FINANCE_DATASET, parse_csv_bytes


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


async def verify_registered_archive(
    store: Any,
    *,
    marketplace: str,
    year: int,
    finance_cabinets: Iterable[str] = (),
    advertising_cabinets: Iterable[str] = (),
    families: Iterable[str] = ("finance", "advertising"),
) -> dict[str, Any]:
    marketplace = str(marketplace).lower()
    if marketplace != "wb":
        return {
            "ok": False,
            "error": "archive_refresh_not_registered",
            "marketplace": marketplace,
        }

    selected = tuple(families)
    checks: list[dict[str, Any]] = []
    if "finance" in selected:
        for cabinet in finance_cabinets:
            checks.append(await verify_finance_cabinet(store, cabinet=cabinet, year=int(year)))
    if "advertising" in selected:
        for cabinet in advertising_cabinets:
            checks.append(await verify_advertising_cabinet(store, cabinet=cabinet, year=int(year)))

    return {
        "ok": bool(checks) and all(bool(item.get("ok")) for item in checks),
        "marketplace": marketplace,
        "year": int(year),
        "dataset_families": list(selected),
        "checks": checks,
        "interpretation": (
            "This verifies canonical-file structure, stable-key uniqueness and committed registry/coverage evidence. "
            "Provider freshness is proven by a completed refresh DISCOVER/reconciliation cycle; dates here are an additional high-watermark signal."
        ),
    }
