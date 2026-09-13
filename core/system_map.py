"""Canonical architecture map exposed by the MCP server itself."""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

ARCHITECTURE_VERSION = "2026-09-13.v3"

SYSTEM_MAP: dict[str, Any] = {
    "architecture_version": ARCHITECTURE_VERSION,
    "status": "CANONICAL",
    "scope": "Marketplaces MCP runtime and marketplace archive",
    "runtime": {
        "cloud": "Yandex Cloud only",
        "entry": "ChatGPT/Codex -> marketplaces-yandex -> Yandex API Gateway -> Yandex Serverless Container",
        "secrets": "Yandex Lockbox",
        "shared_rate_limit_and_locks": "Yandex Managed Redis/Valkey",
        "marketplace_sources": ["Wildberries official API", "Ozon official API"],
    },
    "storage_policy": {
        "primary_archive_storage": "Google Drive",
        "canonical_archive_data": "annual marketplace CSV files plus reports registry",
        "google_drive_root": "MCP архив базы данных",
        "google_drive_auth": "OAuth refresh credential kept in Yandex Lockbox; access token only in runtime memory",
        "yandex_object_storage": "durable archive job state, staging, and byte-for-byte backup of canonical files",
        "archive_write_order": "Google Drive canonical write first; Yandex backup second",
        "read_through_migration": "if a canonical file is absent on Drive but exists in Yandex Object Storage, copy it to Drive before use",
        "google_cloud": "not part of the runtime architecture; only Google Drive API is used as archive storage",
        "client_local_files": "never authoritative for shared server state",
    },
    "archive_policy": {
        "shared_server_state": True,
        "default_update_scope": "all configured marketplace cabinets",
        "idempotent": True,
        "registry_required": True,
        "annual_partitioning": "one logical annual dataset per marketplace/cabinet/dataset/year",
        "annual_csv_pattern": "<cabinet>__<dataset>__<year>.csv",
        "canonical_source_of_truth": "Google Drive annual CSV plus reports registry",
        "wb_weekly_finance_main": {
            "period": "weekly",
            "report_type": 1,
            "meaning": "Основной",
            "logical_week": "Monday-Sunday",
            "month_boundary": "multiple WB reportId fragments may belong to one logical week and must be combined logically",
            "report_type_2": "По выкупам; separate dataset, never mixed into main",
            "row_deduplication": "(reportId, rrdId)",
            "registry_deduplication": "(cabinet, dataset, report_id)",
        },
    },
    "routing_policy": {
        "update_database": "route to the server archive update workflow; compare canonical registry and fetch only missing provider reports",
        "historical_queries": "read canonical Google Drive archive for covered periods before repeatedly querying provider APIs",
        "current_or_uncovered": "use provider APIs or explicit gap/backfill workflow",
        "multi_client": "all clients see the same remote canonical Drive state; no chat-local architecture decisions",
    },
    "change_control": {
        "new_cloud_provider": "FORBIDDEN without explicit architecture change",
        "new_primary_storage": "FORBIDDEN without explicit architecture change",
        "bypass_registry_or_idempotency": "FORBIDDEN",
        "architecture_change_requires": [
            "update SYSTEM_MAP and server instructions",
            "update architecture documentation",
            "update guardrail tests",
            "pass CI/security/deployment acceptance",
        ],
    },
}

SYSTEM_INSTRUCTIONS = f"""CANONICAL MARKETPLACES MCP ARCHITECTURE — {ARCHITECTURE_VERSION}
Treat marketplace_system_map as the source of truth for this MCP.
Runtime infrastructure is Yandex Cloud. Google Cloud is not part of the runtime architecture.
Canonical marketplace archive data is stored on Google Drive under the server-owned archive root: annual CSV files and the report registry are the source of truth.
Yandex Object Storage is required for durable queue/job state, staging, and a secondary byte-for-byte backup of canonical archive files.
Google Drive OAuth refresh credentials must remain in Yandex Lockbox; access tokens exist only in runtime memory.
For database/archive tasks, use shared server state, registry/idempotent update logic, official WB/Ozon APIs, and the canonical Drive archive. Do not invent chat-local storage or bypass Drive with another source of truth.
If a requested implementation conflicts with the canonical map, fail closed and surface the conflict instead of silently changing architecture.
"""


def register_system_map_tool(mcp: FastMCP) -> None:
    @mcp.tool(
        name="marketplace_system_map",
        annotations={
            "title": "Canonical Marketplaces MCP architecture map",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_system_map() -> str:
        """Return the canonical server-side architecture and routing rules.

        Agents should consult this tool before architecture, storage, deployment,
        archive, routing, or cross-client state changes. The returned map is the
        MCP source of truth and overrides chat-local assumptions.
        """
        return json.dumps(SYSTEM_MAP, ensure_ascii=False, indent=2)
