"""Canonical architecture map exposed by the MCP server itself."""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

ARCHITECTURE_VERSION = "2026-09-13.v1"

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
        "primary_runtime_storage": "Yandex Cloud",
        "google_drive": "optional export/mirror only; never a required runtime dependency or source of truth",
        "google_cloud": "not part of the runtime architecture",
        "client_local_files": "never authoritative for shared server state",
    },
    "archive_policy": {
        "shared_server_state": True,
        "default_update_scope": "all configured marketplace cabinets",
        "idempotent": True,
        "registry_required": True,
        "annual_partitioning": "one logical annual dataset per marketplace/cabinet/dataset/year",
        "wb_weekly_finance_main": {
            "period": "weekly",
            "report_type": 1,
            "meaning": "Основной",
            "logical_week": "Monday-Sunday",
            "month_boundary": "multiple WB reportId fragments may belong to one logical week and must be combined logically",
            "report_type_2": "По выкупам; separate dataset, never mixed into main",
        },
    },
    "routing_policy": {
        "update_database": "route to the server archive update workflow; compare registry and fetch only missing provider reports",
        "historical_queries": "use shared archive for covered periods before repeatedly querying provider APIs",
        "current_or_uncovered": "use provider APIs or explicit gap/backfill workflow",
        "multi_client": "all clients must see the same remote state; no chat-local architecture decisions",
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
Primary shared archive/storage must live in Yandex Cloud. Google Drive may only be an optional export/mirror and must never become a required runtime dependency or source of truth.
For database/archive tasks, use server-owned shared state, registry/idempotent update logic, and official WB/Ozon APIs. Do not invent chat-local storage, a new cloud provider, or a new architecture path.
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
