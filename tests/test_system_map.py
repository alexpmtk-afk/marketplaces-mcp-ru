"""Guardrails for the canonical Marketplaces MCP architecture map."""
from __future__ import annotations

import asyncio
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from core.system_map import (
    ARCHITECTURE_VERSION,
    SYSTEM_INSTRUCTIONS,
    SYSTEM_MAP,
    register_system_map_tool,
)

ROOT = Path(__file__).resolve().parent.parent


def test_canonical_map_fixes_storage_boundaries():
    assert SYSTEM_MAP["status"] == "CANONICAL"
    assert SYSTEM_MAP["runtime"]["cloud"] == "Yandex Cloud only"
    storage = SYSTEM_MAP["storage_policy"]
    assert storage["primary_archive_storage"] == "Google Drive"
    assert storage["google_drive_root"] == "MCP архив базы данных"
    assert "Apps Script" in storage["google_drive_auth"]
    assert "Yandex Lockbox" in storage["google_drive_auth"]
    assert "Google Drive API" in storage["google_drive_large_upload"]
    assert "resumable" in storage["google_drive_large_upload"]
    assert "job state" in storage["yandex_object_storage"]
    assert "backup" in storage["yandex_object_storage"]
    assert "not part of the runtime architecture" in storage["google_cloud"]
    assert "OAuth" in storage["google_cloud"]
    assert SYSTEM_MAP["archive_policy"]["canonical_source_of_truth"] == "Google Drive annual CSV plus reports registry"
    assert SYSTEM_MAP["archive_policy"]["wb_weekly_finance_main"]["report_type"] == 1
    assert SYSTEM_MAP["archive_policy"]["wb_weekly_finance_main"]["logical_week"] == "Monday-Sunday"
    assert SYSTEM_MAP["archive_policy"]["wb_weekly_finance_main"]["row_deduplication"] == "(reportId, rrdId)"


def test_large_annual_files_use_resumable_drive_api_not_apps_script():
    policy = SYSTEM_MAP["archive_policy"]["large_file_upload"]
    assert policy["transport"] == "Google Drive API resumable upload"
    assert policy["apps_script_large_upload"] == "forbidden"
    assert "one bounded chunk" in policy["worker_model"]
    assert "confirmed byte offset" in policy["resume_state"]
    assert "256 KiB" in policy["chunk_rule"]
    assert "COMMIT" in policy["commit_rule"]


def test_wb_advertising_m0_is_read_only_and_separate_from_profit():
    policy = SYSTEM_MAP["advertising_policy"]
    assert policy["current_scope"].startswith("Wildberries only")
    assert policy["phase"] == "WB Advertising M0 read-only"
    assert policy["credential_service"] == "wb_ads"
    assert policy["active_campaign_status"] == 9
    assert "wb_ads_audit_active" in policy["m0_tools"]
    assert "advertising_attribution_operational" == policy["metric_class"]
    assert "not actual business profit" in policy["profitability_boundary"]
    assert policy["archive_domain"] == "База данных/WB/<cabinet>/<year>/advertising"
    assert "not yet implemented" in policy["archive_status"]
    assert policy["write_control_status"].startswith("not accepted in M0")
    assert "WRITE/DESTRUCTIVE" in policy["safety_override"]


def test_server_instructions_contain_hard_architecture_boundaries():
    assert ARCHITECTURE_VERSION in SYSTEM_INSTRUCTIONS
    assert "Yandex Cloud" in SYSTEM_INSTRUCTIONS
    assert "Google Drive" in SYSTEM_INSTRUCTIONS
    assert "Apps Script" in SYSTEM_INSTRUCTIONS
    assert "resumable upload" in SYSTEM_INSTRUCTIONS
    assert "OAuth refresh-token" in SYSTEM_INSTRUCTIONS
    assert "Yandex Object Storage" in SYSTEM_INSTRUCTIONS
    assert "Yandex Lockbox" in SYSTEM_INSTRUCTIONS
    assert "source of truth" in SYSTEM_INSTRUCTIONS
    assert "Google Cloud is not a runtime provider" in SYSTEM_INSTRUCTIONS
    assert "WB Advertising M0" in SYSTEM_INSTRUCTIONS
    assert "wb_ads" in SYSTEM_INSTRUCTIONS
    assert "actual business profit" in SYSTEM_INSTRUCTIONS
    assert "fail closed" in SYSTEM_INSTRUCTIONS


def test_system_map_tool_is_registered_for_canonical_version():
    mcp = FastMCP("architecture-map-test")
    register_system_map_tool(mcp)
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert "marketplace_system_map" in tools
    assert SYSTEM_MAP["architecture_version"] == ARCHITECTURE_VERSION
    assert SYSTEM_MAP["status"] == "CANONICAL"


def test_human_and_agent_docs_reference_canonical_architecture_version():
    architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert ARCHITECTURE_VERSION in architecture
    assert "core/system_map.py" in architecture
    assert "Canonical marketplace archive: **Google Drive**" in architecture
    assert "Google Apps Script" in architecture
    assert "resumable" in architecture.lower()
    assert "Google Drive API" in architecture
    assert "Yandex Object Storage" in architecture
    assert "WB Advertising M0" in architecture
    assert "wb_ads" in architecture
    assert "core/system_map.py" in agents
    assert "Primary shared marketplace archive/storage is **Google Drive**" in agents
    assert "Google Apps Script" in agents
    assert "resumable" in agents.lower()
    assert "Yandex Object Storage" in agents
