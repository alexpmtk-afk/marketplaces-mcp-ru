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
            "plus Yandex Object Storage for durable queue/staging. Check "
            "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL, "
            "MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET, "
            "MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID and MARKETPLACE_MCP_ARCHIVE_BUCKET."
        ),
        "retryable": False,
    })


async def _query_year(
    store: Any,
    year: int,
    sql: str,
) -> dict[str, Any]:
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


def register_archive_tools(
    mcp: FastMCP,
    modules: dict[str, Any],
    store: Any | None,
) -> None:
    wb = modules["wb"]

    @mcp.tool(
        name="marketplace_archive_update",
        annotations={
            "title": "Queue central marketplace database archive update",
            "readOnlyHint": False,
            "openWorldHint": True,
        },
    )
    async def marketplace_archive_update(
        year: int = date.today().year,
        seller: str = "all",
        max_reports_per_cabinet: int = 4,
    ) -> str:
        """Queue a durable WB archive job instead of holding one long MCP call.

        Job state/staging is durable in Yandex Object Storage. Final annual CSV
        files and the report registry are canonical on Google Drive. A worker
        step performs at most one WB API request; quota waits are rescheduled.
        ``max_reports_per_cabinet`` is retained only for client compatibility.
        """
        del max_reports_per_cabinet
        if store is None:
            return _not_configured()
        queue = WBFinanceArchiveJobQueue(wb, store)
        sellers = ARCHIVE_CABINETS if seller.strip().lower() == "all" else (seller,)
        jobs = [await queue.enqueue(year=int(year), seller=item) for item in sellers]
        return _j({
            "ok": True,
            "marketplace": "wb",
            "dataset": "wb_weekly_finance_main",
            "year": int(year),
            "queued": True,
            "jobs": jobs,
            "instruction": "Process queued jobs with marketplace_archive_worker_step; no long quota wait occurs inside MCP.",
        })

    @mcp.tool(
        name="marketplace_archive_worker_step",
        annotations={
            "title": "Process one durable archive queue step",
            "readOnlyHint": False,
            "openWorldHint": True,
        },
    )
    async def marketplace_archive_worker_step(job_id: str = "") -> str:
        """Run at most one real marketplace API request for one queued job."""
        if store is None:
            return _not_configured()
        queue = WBFinanceArchiveJobQueue(wb, store)
        return _j(await queue.worker_step(job_id))

    @mcp.tool(
        name="marketplace_archive_job_status",
        annotations={
            "title": "Durable archive job status",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_archive_job_status(job_id: str) -> str:
        """Show persisted progress for an archive queue job."""
        if store is None:
            return _not_configured()
        queue = WBFinanceArchiveJobQueue(wb, store)
        return _j(await queue.status(job_id))

    @mcp.tool(
        name="marketplace_archive_status",
        annotations={
            "title": "Marketplace archive status",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_archive_status(year: int = date.today().year) -> str:
        """Show central WB archive coverage and canonical annual-file state."""
        if store is None:
            return _not_configured()
        manager = WBFinanceArchiveManager(wb, store)
        return _j(await manager.status(int(year)))

    @mcp.tool(
        name="marketplace_archive_query",
        annotations={
            "title": "Query WB annual archive",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_archive_query(year: int, sql: str) -> str:
        """Run safe read-only SQL over canonical annual WB CSV files on Drive.

        The server exposes views named ``wb_dmitrieva``, ``wb_novokshenov``,
        ``wb_laser_master`` and union view ``wb_all``. Use this for historical
        questions covered by the archive rather than repeatedly calling WB APIs.
        Only SELECT/WITH is accepted; mutating/external-reader SQL is blocked.
        At most 1000 result rows are returned.
        """
        if store is None:
            return _not_configured()
        return _j(await _query_year(store, int(year), sql))
