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
    assert "job state" in storage["yandex_object_storage"]
    assert "backup" in storage["yandex_object_storage"]
    assert "not part of the runtime architecture" in storage["google_cloud"]
    assert "OAuth" in storage["google_cloud"]
    assert SYSTEM_MAP["archive_policy"]["canonical_source_of_truth"] == "Google Drive annual CSV plus reports registry"
    assert SYSTEM_MAP["archive_policy"]["wb_weekly_finance_main"]["report_type"] == 1
    assert SYSTEM_MAP["archive_policy"]["wb_weekly_finance_main"]["logical_week"] == "Monday-Sunday"
    assert SYSTEM_MAP["archive_policy"]["wb_weekly_finance_main"]["row_deduplication"] == "(reportId, rrdId)"


def test_server_instructions_contain_hard_architecture_boundaries():
    assert ARCHITECTURE_VERSION in SYSTEM_INSTRUCTIONS
    assert "Yandex Cloud" in SYSTEM_INSTRUCTIONS
    assert "Google Drive" in SYSTEM_INSTRUCTIONS
    assert "Apps Script" in SYSTEM_INSTRUCTIONS
    assert "Yandex Object Storage" in SYSTEM_INSTRUCTIONS
    assert "Yandex Lockbox" in SYSTEM_INSTRUCTIONS
    assert "source of truth" in SYSTEM_INSTRUCTIONS
    assert "Google Cloud is not part" in SYSTEM_INSTRUCTIONS
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
    assert "Yandex Object Storage" in architecture
    assert "core/system_map.py" in agents
    assert "Primary shared marketplace archive/storage is **Google Drive**" in agents
    assert "Google Apps Script" in agents
    assert "Yandex Object Storage" in agents
