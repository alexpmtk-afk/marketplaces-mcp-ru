"""MCP tools for the central marketplace archive layer."""
from __future__ import annotations

import json
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .archive_queue import WBFinanceArchiveJobQueue
from .archive_refresh import enqueue_refresh_cycle, normalize_refresh_family, refresh_catalog
from .archive_refresh_verify import verify_registered_archive
from .archive_resumable_diagnostic import WBFinanceResumableDiagnostic
from .archive_resumable_worker import WBFinanceResumableWorker
from .ozon_current_archive import (
    OZON_ARCHIVE_CABINETS,
    OzonCurrentArchiveJobQueue,
)
from .ozon_final_archive import OzonFinalArchiveJobQueue
from .wb_advertising_archive import ARCHIVE_CABINETS as ADS_ARCHIVE_CABINETS
from .wb_advertising_archive_queue import WBAdvertisingArchiveJobQueue
from .wb_advertising_archive_verified_worker import VerifiedWBAdvertisingArchiveWorker
from .wb_finance_archive import ARCHIVE_CABINETS, WBFinanceArchiveManager

_BLOCKED_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|install|load|pragma|call|vacuum|truncate|replace|merge)\b"
    r"|\b(read_csv|read_csv_auto|read_parquet|parquet_scan|sqlite_scan|postgres_scan|glob|read_blob|httpfs)\s*\(",
    re.IGNORECASE,
)


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _not_configured() -> str:
    return _j({
        "ok": False,
        "error": "archive_storage_not_configured",
        "message": (
            "Central archive requires canonical Google Drive storage through the Apps Script bridge "
            "plus an explicitly configured durable backend for queue/staging/candidate/backup state. "
            "After the REMOTE migration, do not assume Yandex Object Storage is active from legacy names. "
            "Check MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL, "
            "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET and "
            "MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID, then audit the actual REMOTE durable-backend "
            "configuration before any mutating archive operation."
        ),
        "retryable": False,
    })


def _available_sellers(marketplace: str) -> list[str]:
    market = str(marketplace or "").strip().lower()
    if market == "wb":
        return sorted(set(ARCHIVE_CABINETS) | set(ADS_ARCHIVE_CABINETS))
    if market == "ozon":
        return sorted(OZON_ARCHIVE_CABINETS)
    return []


def _split_seller_scope(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    if raw.lower() == "all":
        return ["all"]
    return [item.strip() for item in re.split(r"[,;]", raw) if item.strip()]


def _database_update_scope(
    marketplace: str = "",
    seller: str = "",
    dataset_family: str = "",
) -> dict[str, Any]:
    """Validate a database-update request without starting any provider work.

    Mutating refreshes never infer marketplace, cabinet or dataset family.
    The caller must provide all three explicitly; all is accepted only when
    it was explicitly supplied for seller and/or dataset family.
    """
    market = str(marketplace or "").strip().lower()
    seller_value = str(seller or "").strip()
    family_value = str(dataset_family or "").strip()

    missing: list[str] = []
    questions: list[str] = []
    if not market:
        missing.append("marketplace")
        questions.append("Какой маркетплейс обновлять: WB или Ozon?")
    if not seller_value:
        missing.append("seller")
        questions.append("Какой магазин/кабинет обновлять? Укажите конкретный кабинет или явно all.")
    if not family_value:
        missing.append("dataset_family")
        questions.append(
            "Какую базу обновлять? Для WB: finance или advertising; "
            "для Ozon: current или final; all — только если нужны все базы выбранного маркетплейса."
        )
    if missing:
        return {
            "ok": False,
            "error": "clarification_required",
            "missing_fields": missing,
            "questions": questions,
            "no_jobs_queued": True,
        }

    if market not in {"wb", "ozon"}:
        return {
            "ok": False,
            "error": "unsupported_marketplace",
            "marketplace": market,
            "supported_marketplaces": ["wb", "ozon"],
            "no_jobs_queued": True,
        }

    try:
        families = normalize_refresh_family(family_value, marketplace=market)
    except ValueError as exc:
        return {
            "ok": False,
            "error": "archive_refresh_not_registered",
            "marketplace": market,
            "message": str(exc),
            "registered": refresh_catalog(),
            "no_jobs_queued": True,
        }

    seller_tokens = _split_seller_scope(seller_value)
    if not seller_tokens:
        return {
            "ok": False,
            "error": "clarification_required",
            "missing_fields": ["seller"],
            "questions": ["Какой магазин/кабинет обновлять?"],
            "no_jobs_queued": True,
        }

    selected_sellers = (
        _available_sellers(market)
        if seller_tokens == ["all"]
        else seller_tokens
    )
    return {
        "ok": True,
        "marketplace": market,
        "seller_scope": seller_value,
        "selected_sellers": selected_sellers,
        "dataset_families": list(families),
        "explicit_all_sellers": seller_tokens == ["all"],
        "explicit_all_families": family_value.strip().lower() == "all",
        "no_jobs_queued": True,
    }


async def _query_year(store: Any, year: int, sql: str) -> dict[str, Any]:
    statement = str(sql).strip()
    if statement.endswith(";"):
        statement = statement[:-1].strip()
    lowered = statement.lstrip().lower()
    if not (lowered.startswith("select ") or lowered.startswith("with ")):
        raise ValueError("archive SQL must be a single SELECT/WITH query")
    if ";" in statement or _BLOCKED_SQL.search(statement):
        raise ValueError("archive SQL contains a forbidden statement or external-reader function")

    import duckdb

    available: list[str] = []
    with tempfile.TemporaryDirectory(prefix="marketplace-archive-") as tmp:
        conn = duckdb.connect(database=":memory:")
        try:
            for cabinet in ARCHIVE_CABINETS:
                folder = await store.ensure_folder_path(
                    ["База данных", "WB", cabinet, str(year), "finance", "weekly", "main"]
                )
                name = f"{cabinet}__weekly_main__{year}.csv"
                item, data = await store.download_named(folder, name)
                if item is None or data is None:
                    continue
                path = Path(tmp) / name
                path.write_bytes(data)
                escaped = str(path).replace("'", "''")
                conn.execute(
                    f"CREATE VIEW {cabinet} AS "
                    f"SELECT *, '{cabinet}' AS archive_cabinet "
                    f"FROM read_csv_auto('{escaped}', delim=';', header=true, "
                    "all_varchar=true, union_by_name=true, ignore_errors=false)"
                )
                available.append(cabinet)
            if not available:
                return {
                    "ok": False,
                    "error": "archive_year_not_found",
                    "year": year,
                    "message": "No WB annual archive files are available for this year.",
                }
            union_sql = " UNION ALL BY NAME ".join(f"SELECT * FROM {name}" for name in available)
            conn.execute(f"CREATE VIEW wb_all AS {union_sql}")
            cursor = conn.execute(statement)
            columns = [str(item[0]) for item in cursor.description or []]
            rows = cursor.fetchmany(1001)
            truncated = len(rows) > 1000
            rows = rows[:1000]
            return {
                "ok": True,
                "year": year,
                "views": [*available, "wb_all"],
                "columns": columns,
                "rows": [list(row) for row in rows],
                "row_count": len(rows),
                "truncated": truncated,
            }
        finally:
            conn.close()


def register_archive_tools(mcp: FastMCP, modules: dict[str, Any], store: Any | None) -> None:
    wb = modules["wb"]
    ozon = modules["ozon"]

    @mcp.tool(
        name="marketplace_database_refresh_catalog",
        annotations={"title": "Marketplace database refresh contracts", "readOnlyHint": True, "openWorldHint": False},
    )
    async def marketplace_database_refresh_catalog() -> str:
        """Describe every archive dataset family supported by the common refresh mechanism.

        Each registered family declares its provider coverage model, freshness
        evidence, stable deduplication keys and completion invariants. Future
        archive datasets must register here before the generic database-update
        workflow may claim to update them.
        """
        return _j({
            "ok": True,
            "contracts": refresh_catalog(),
            "rule": (
                "A database refresh always re-runs dataset-specific discovery/coverage reconciliation. "
                "COMPLETE means the previous refresh cycle completed; it never means the annual database is permanently final."
            ),
        })

    @mcp.tool(
        name="marketplace_database_update_plan",
        annotations={"title": "Plan marketplace database update scope", "readOnlyHint": True, "openWorldHint": False},
    )
    async def marketplace_database_update_plan(
        marketplace: str = "",
        seller: str = "",
        dataset_family: str = "",
        year: int = date.today().year,
    ) -> str:
        """Validate exactly what a future database update would touch.

        This tool never queues jobs. Missing marketplace, seller or dataset
        family produces structured clarification questions instead of silently
        defaulting to WB/all/all.
        """
        scope = _database_update_scope(marketplace, seller, dataset_family)
        if not scope.get("ok"):
            return _j(scope)
        public_family_names = {
            str(item).removeprefix("ozon_")
            for item in scope["dataset_families"]
        }
        contracts = {
            item["dataset_family"]: item
            for item in refresh_catalog()
            if item["marketplace"] == scope["marketplace"]
            and item["dataset_family"] in public_family_names
        }
        scope["year"] = int(year)
        scope["contracts"] = contracts
        scope["update_rule"] = (
            "Only the explicitly selected marketplace, seller scope and dataset families may be queued. "
            "Advertising refresh re-discovers the campaign roster every cycle and reconciles closed history through yesterday Europe/Moscow."
        )
        return _j(scope)

    @mcp.tool(
        name="marketplace_database_verify",
        annotations={"title": "Verify canonical marketplace database integrity", "readOnlyHint": True, "openWorldHint": False},
    )
    async def marketplace_database_verify(
        marketplace: str = "wb",
        year: int = date.today().year,
        seller: str = "all",
        dataset_family: str = "all",
    ) -> str:
        """Verify canonical files after a refresh cycle."""
        if store is None:
            return _not_configured()
        marketplace = str(marketplace).strip().lower()
        try:
            families = normalize_refresh_family(
                dataset_family,
                marketplace=marketplace,
            )
        except ValueError as exc:
            return _j({
                "ok": False,
                "error": "archive_refresh_not_registered",
                "marketplace": marketplace,
                "message": str(exc),
                "registered": refresh_catalog(),
            })

        finance_cabinets: tuple[str, ...] = ()
        advertising_cabinets: tuple[str, ...] = ()
        ozon_current_cabinets: tuple[str, ...] = ()
        ozon_final_cabinets: tuple[str, ...] = ()

        if marketplace == "wb":
            if "finance" in families:
                finance_queue = WBFinanceArchiveJobQueue(wb, store)
                finance_cabinets = (
                    ARCHIVE_CABINETS
                    if seller.strip().lower() == "all"
                    else (finance_queue.normalize_cabinet(seller),)
                )
            if "advertising" in families:
                advertising_queue = WBAdvertisingArchiveJobQueue(wb, store)
                advertising_cabinets = (
                    ADS_ARCHIVE_CABINETS
                    if seller.strip().lower() == "all"
                    else (advertising_queue.normalize_cabinet(seller),)
                )
        elif marketplace == "ozon":
            normalizer = OzonCurrentArchiveJobQueue(ozon, store)
            selected_cabinets = (
                OZON_ARCHIVE_CABINETS
                if seller.strip().lower() == "all"
                else (normalizer.normalize_cabinet(seller),)
            )
            if "ozon_current" in families:
                ozon_current_cabinets = selected_cabinets
            if "ozon_final" in families:
                ozon_final_cabinets = selected_cabinets

        return _j(await verify_registered_archive(
            store,
            marketplace=marketplace,
            year=int(year),
            finance_cabinets=finance_cabinets,
            advertising_cabinets=advertising_cabinets,
            ozon_current_cabinets=ozon_current_cabinets,
            ozon_final_cabinets=ozon_final_cabinets,
            families=families,
        ))

    @mcp.tool(
        name="marketplace_database_update",
        annotations={"title": "Update canonical marketplace databases", "readOnlyHint": False, "openWorldHint": True},
    )
    async def marketplace_database_update(
        marketplace: str = "",
        year: int = date.today().year,
        seller: str = "",
        dataset_family: str = "",
    ) -> str:
        """Preferred top-level tool for requests such as "update our database".

        WB performs provider reconciliation against its annual coverage.
        Ozon CURRENT refetches the complete open month so later status,
        cancellation and financial corrections replace stale observations.
        """
        scope = _database_update_scope(marketplace, seller, dataset_family)
        if not scope.get("ok"):
            return _j(scope)
        if store is None:
            return _not_configured()

        marketplace = str(scope["marketplace"])
        families = tuple(str(item) for item in scope["dataset_families"])
        selected_sellers = [str(item) for item in scope["selected_sellers"]]

        jobs: list[dict[str, Any]] = []
        worker_tools: set[str] = set()

        if marketplace == "wb":
            for family in families:
                if family == "finance":
                    queue = WBFinanceArchiveJobQueue(wb, store)
                    sellers = selected_sellers
                    jobs.extend([
                        await enqueue_refresh_cycle(
                            queue,
                            family="finance",
                            year=int(year),
                            seller=item,
                        )
                        for item in sellers
                    ])
                    worker_tools.add("marketplace_archive_worker_step")
                elif family == "advertising":
                    queue = WBAdvertisingArchiveJobQueue(wb, store)
                    sellers = selected_sellers
                    jobs.extend([
                        await enqueue_refresh_cycle(
                            queue,
                            family="advertising",
                            year=int(year),
                            seller=item,
                        )
                        for item in sellers
                    ])
                    worker_tools.add("marketplace_advertising_archive_worker_step")

        elif marketplace == "ozon":
            sellers = selected_sellers
            for family in families:
                if family == "ozon_final":
                    final_queue = OzonFinalArchiveJobQueue(ozon, store)
                    jobs.extend([
                        await final_queue.enqueue(year=int(year), seller=item)
                        for item in sellers
                    ])
                    worker_tools.add("marketplace_ozon_final_archive_worker_step")
                elif family == "ozon_current":
                    current_queue = OzonCurrentArchiveJobQueue(ozon, store)
                    jobs.extend([
                        await current_queue.enqueue(year=int(year), seller=item)
                        for item in sellers
                    ])
                    worker_tools.add("marketplace_ozon_current_archive_worker_step")

        scheduled = [job for job in jobs if job.get("scheduled")]
        return _j({
            "ok": True,
            "marketplace": marketplace,
            "year": int(year),
            "dataset_families": list(families),
            "selected_sellers": selected_sellers,
            "scope_was_explicit": True,
            "queued": bool(scheduled),
            "queued_jobs": len(scheduled),
            "jobs": jobs,
            "worker_tools": sorted(worker_tools),
            "verification_tool": "marketplace_database_verify",
            "completion_policy": (
                "Do not report the database as updated merely because jobs were queued. "
                "Every requested job must reach COMPLETE after stable-key validation, "
                "verified canonical publication and coverage commit; then call "
                "marketplace_database_verify."
            ),
        })

    @mcp.tool(
        name="marketplace_ozon_final_archive_worker_step",
        annotations={"title": "Process one Ozon FINAL archive step", "readOnlyHint": False, "openWorldHint": True},
    )
    async def marketplace_ozon_final_archive_worker_step(job_id: str = "") -> str:
        if store is None:
            return _not_configured()
        queue = OzonFinalArchiveJobQueue(ozon, store)
        return _j(await queue.worker_step(job_id))

    @mcp.tool(
        name="marketplace_ozon_final_archive_job_status",
        annotations={"title": "Ozon FINAL archive job status", "readOnlyHint": True, "openWorldHint": False},
    )
    async def marketplace_ozon_final_archive_job_status(job_id: str) -> str:
        if store is None:
            return _not_configured()
        queue = OzonFinalArchiveJobQueue(ozon, store)
        return _j(await queue.status(job_id))

    @mcp.tool(
        name="marketplace_ozon_current_archive_worker_step",
        annotations={"title": "Process one Ozon CURRENT archive step", "readOnlyHint": False, "openWorldHint": True},
    )
    async def marketplace_ozon_current_archive_worker_step(job_id: str = "") -> str:
        if store is None:
            return _not_configured()
        queue = OzonCurrentArchiveJobQueue(ozon, store)
        return _j(await queue.worker_step(job_id))

    @mcp.tool(
        name="marketplace_ozon_current_archive_job_status",
        annotations={"title": "Ozon CURRENT archive job status", "readOnlyHint": True, "openWorldHint": False},
    )
    async def marketplace_ozon_current_archive_job_status(job_id: str) -> str:
        if store is None:
            return _not_configured()
        queue = OzonCurrentArchiveJobQueue(ozon, store)
        return _j(await queue.status(job_id))

    @mcp.tool(
        name="marketplace_archive_update",
        annotations={"title": "Queue WB weekly-finance archive refresh", "readOnlyHint": False, "openWorldHint": True},
    )
    async def marketplace_archive_update(
        year: int = date.today().year,
        seller: str = "",
        max_reports_per_cabinet: int = 4,
    ) -> str:
        """Compatibility wrapper for the WB weekly-finance dataset family.

        COMPLETE annual jobs are reopened into a new DISCOVER cycle so new WB
        reportId values can be found. Canonical reports_registry.csv remains the
        authority for what is already ingested, preventing duplicate downloads.
        """
        del max_reports_per_cabinet
        scope = _database_update_scope("wb", seller, "finance")
        if not scope.get("ok"):
            return _j(scope)
        if store is None:
            return _not_configured()
        queue = WBFinanceArchiveJobQueue(wb, store)
        sellers = [str(item) for item in scope["selected_sellers"]]
        jobs = [
            await enqueue_refresh_cycle(queue, family="finance", year=int(year), seller=item)
            for item in sellers
        ]
        scheduled = [job for job in jobs if job.get("scheduled")]
        return _j({
            "ok": True,
            "marketplace": "wb",
            "dataset": "wb_weekly_finance_main",
            "year": int(year),
            "queued": bool(scheduled),
            "queued_jobs": len(scheduled),
            "jobs": jobs,
            "verification_tool": "marketplace_database_verify",
            "instruction": (
                "Process queued jobs with marketplace_archive_worker_step. A previously COMPLETE annual job is "
                "reopened at DISCOVER; reports_registry.csv filters old reportId values so only missing provider reports are ingested. "
                "After COMPLETE, call marketplace_database_verify for canonical row/date/dedup checks."
            ),
        })

    @mcp.tool(
        name="marketplace_archive_worker_step",
        annotations={"title": "Process one durable archive queue step", "readOnlyHint": False, "openWorldHint": True},
    )
    async def marketplace_archive_worker_step(job_id: str = "") -> str:
        if store is None:
            return _not_configured()
        queue = WBFinanceArchiveJobQueue(wb, store)
        worker = WBFinanceResumableWorker(queue, store)
        return _j(await worker.worker_step(job_id))

    @mcp.tool(
        name="marketplace_archive_resumable_diagnostic_step",
        annotations={
            "title": "Verify existing archive candidate through temporary Drive resumable copy",
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": True,
        },
    )
    async def marketplace_archive_resumable_diagnostic_step(job_id: str) -> str:
        if store is None:
            return _not_configured()
        queue = WBFinanceArchiveJobQueue(wb, store)
        diagnostic = WBFinanceResumableDiagnostic(queue, store)
        return _j(await diagnostic.step(job_id))

    @mcp.tool(
        name="marketplace_archive_job_status",
        annotations={"title": "Durable archive job status", "readOnlyHint": True, "openWorldHint": False},
    )
    async def marketplace_archive_job_status(job_id: str) -> str:
        if store is None:
            return _not_configured()
        queue = WBFinanceArchiveJobQueue(wb, store)
        return _j(await queue.status(job_id))

    @mcp.tool(
        name="marketplace_archive_status",
        annotations={"title": "Marketplace archive status", "readOnlyHint": True, "openWorldHint": False},
    )
    async def marketplace_archive_status(year: int = date.today().year) -> str:
        if store is None:
            return _not_configured()
        manager = WBFinanceArchiveManager(wb, store)
        return _j(await manager.status(int(year)))

    @mcp.tool(
        name="marketplace_archive_query",
        annotations={"title": "Query WB annual archive", "readOnlyHint": True, "openWorldHint": False},
    )
    async def marketplace_archive_query(year: int, sql: str) -> str:
        if store is None:
            return _not_configured()
        return _j(await _query_year(store, int(year), sql))

    @mcp.tool(
        name="marketplace_advertising_archive_update",
        annotations={"title": "Queue WB advertising archive refresh", "readOnlyHint": False, "openWorldHint": True},
    )
    async def marketplace_advertising_archive_update(
        year: int = date.today().year,
        seller: str = "",
    ) -> str:
        """Compatibility wrapper for the WB advertising archive family.

        A COMPLETE job is reopened for a new provider reconciliation cycle.
        Canonical datasets remain idempotent because each dataset upserts by its
        registered stable key and coverage is committed only after verified
        canonical publication.
        """
        scope = _database_update_scope("wb", seller, "advertising")
        if not scope.get("ok"):
            return _j(scope)
        if store is None:
            return _not_configured()
        queue = WBAdvertisingArchiveJobQueue(wb, store)
        sellers = [str(item) for item in scope["selected_sellers"]]
        jobs = [
            await enqueue_refresh_cycle(queue, family="advertising", year=int(year), seller=item)
            for item in sellers
        ]
        scheduled = [job for job in jobs if job.get("scheduled")]
        return _j({
            "ok": True,
            "marketplace": "wb",
            "dataset_family": "advertising",
            "year": int(year),
            "queued": bool(scheduled),
            "queued_jobs": len(scheduled),
            "jobs": jobs,
            "verification_tool": "marketplace_database_verify",
            "instruction": (
                "Process queued jobs with marketplace_advertising_archive_worker_step. "
                "The same worker performs provider reconciliation, stable-key annual upsert, "
                "verified Drive publication and coverage commit. After COMPLETE, call "
                "marketplace_database_verify for canonical row/date/dedup checks."
            ),
        })

    @mcp.tool(
        name="marketplace_advertising_archive_worker_step",
        annotations={"title": "Process one WB advertising archive step", "readOnlyHint": False, "openWorldHint": True},
    )
    async def marketplace_advertising_archive_worker_step(job_id: str = "") -> str:
        if store is None:
            return _not_configured()
        queue = WBAdvertisingArchiveJobQueue(wb, store)
        worker = VerifiedWBAdvertisingArchiveWorker(queue, store)
        return _j(await worker.worker_step(job_id))

    @mcp.tool(
        name="marketplace_advertising_archive_job_status",
        annotations={"title": "WB advertising archive status", "readOnlyHint": True, "openWorldHint": False},
    )
    async def marketplace_advertising_archive_job_status(job_id: str) -> str:
        if store is None:
            return _not_configured()
        queue = WBAdvertisingArchiveJobQueue(wb, store)
        return _j(await queue.status(job_id))