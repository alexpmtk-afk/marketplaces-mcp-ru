"""Guardrails for the canonical Marketplaces MCP architecture map."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from core.system_map import (
    ARCHITECTURE_VERSION,
    SYSTEM_INSTRUCTIONS,
    SYSTEM_MAP,
    register_system_map_tool,
)

ROOT = Path(__file__).resolve().parent.parent


def test_canonical_map_fixes_yandex_runtime_boundaries():
    assert SYSTEM_MAP["status"] == "CANONICAL"
    assert SYSTEM_MAP["runtime"]["cloud"] == "Yandex Cloud only"
    assert SYSTEM_MAP["storage_policy"]["primary_runtime_storage"] == "Yandex Cloud"
    assert SYSTEM_MAP["storage_policy"]["google_cloud"] == "not part of the runtime architecture"
    assert "optional export/mirror only" in SYSTEM_MAP["storage_policy"]["google_drive"]
    assert SYSTEM_MAP["archive_policy"]["wb_weekly_finance_main"]["report_type"] == 1
    assert SYSTEM_MAP["archive_policy"]["wb_weekly_finance_main"]["logical_week"] == "Monday-Sunday"


def test_server_instructions_contain_hard_architecture_boundaries():
    assert ARCHITECTURE_VERSION in SYSTEM_INSTRUCTIONS
    assert "Yandex Cloud" in SYSTEM_INSTRUCTIONS
    assert "Google Cloud is not part" in SYSTEM_INSTRUCTIONS
    assert "optional export/mirror" in SYSTEM_INSTRUCTIONS
    assert "fail closed" in SYSTEM_INSTRUCTIONS


def test_system_map_tool_returns_same_canonical_version():
    mcp = FastMCP("architecture-map-test")
    register_system_map_tool(mcp)
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert "marketplace_system_map" in tools
    result = asyncio.run(tools["marketplace_system_map"].fn())
    body = json.loads(result)
    assert body["architecture_version"] == ARCHITECTURE_VERSION
    assert body["status"] == "CANONICAL"


def test_human_and_agent_docs_reference_canonical_architecture_version():
    architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert ARCHITECTURE_VERSION in architecture
    assert "core/system_map.py" in architecture
    assert "core/system_map.py" in agents
    assert "Google Drive may only be an optional export/mirror" in agents
