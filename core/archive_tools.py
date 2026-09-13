"""MCP tools for the central marketplace archive layer."""
from __future__ import annotations

import json
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .archive_yandex import YandexObjectStorageArchiveStore
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
            "Central Yandex Object Storage archive is not configured on the remote MCP. "
            "The server requires MARKETPLACE_MCP_ARCHIVE_BUCKET."
        ),
        "retryable": False,
    })


async def _query_year(
    store: YandexObjectStorageArchiveStore,
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
    store: YandexObjectStorageArchiveStore | None,
) -> None:
    wb = modules["wb"]

    @mcp.tool(
        name="marketplace_archive_update",
        annotations={
            "title": "Update central marketplace database archive",
            "readOnlyHint": False,
            "openWorldHint": True,
        },
    )
    async def marketplace_archive_update(
        year: int = date.today().year,
        seller: str = "all",
        max_reports_per_cabinet: int = 4,
    ) -> str:
        """Update the central WB annual CSV database in Yandex Object Storage.

        IMPORTANT ROUTING: use this tool whenever the user says things like
        ``обнови данные по базе данных``, ``обнови базу маркетплейсов``,
        ``обнови архив WB`` or asks to synchronize the historical database.

        Default behaviour updates ALL three Wildberries cabinets and ONLY the
        canonical weekly ``reportType=1`` (Основной) dataset. It discovers all
        weekly report fragments for the selected year, skips report IDs already
        marked COMPLETE, downloads each missing fragment with rrdId pagination,
        and idempotently rebuilds one annual CSV per cabinet. Month-boundary
        fragments remain separate report IDs inside the annual file but map to
        one Monday-Sunday logical week in the registry.

        If the response has ``continue_required=true`` the client/agent SHOULD
        call this same tool again automatically with the same arguments until
        ``complete=true``. This bounded continuation prevents long historical
        backfills from exceeding one serverless request timeout.
        """
        if store is None:
            return _not_configured()
        if seller.strip().lower() == "all":
            cabinets = ARCHIVE_CABINETS
        else:
            normalized = seller.strip()
            if normalized not in ARCHIVE_CABINETS:
                from .business_registry import resolve_business_cabinet

                entry = resolve_business_cabinet("wb", normalized)
                if entry is None:
                    raise ValueError(f"Unknown WB seller/cabinet: {seller}")
                normalized = entry.cabinet
            cabinets = (normalized,)
        manager = WBFinanceArchiveManager(wb, store)
        return _j(await manager.update(
            year=int(year),
            cabinets=cabinets,
            max_reports_per_cabinet=max_reports_per_cabinet,
        ))

    @mcp.tool(
        name="marketplace_archive_status",
        annotations={
            "title": "Marketplace archive status",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_archive_status(year: int = date.today().year) -> str:
        """Show central WB archive coverage and annual-file state for a year."""
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
        """Run a safe read-only SQL query over annual WB CSV files in Yandex.

        The server exposes views named ``wb_dmitrieva``, ``wb_novokshenov``,
        ``wb_laser_master`` and union view ``wb_all``. Use this for historical
        questions that should be answered from the archive instead of repeatedly
        calling WB APIs. Only SELECT/WITH is accepted and external file readers
        or mutating SQL are blocked. At most 1000 result rows are returned.
        """
        if store is None:
            return _not_configured()
        return _j(await _query_year(store, int(year), sql))