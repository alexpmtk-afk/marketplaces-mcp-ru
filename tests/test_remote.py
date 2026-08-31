"""Contract checks for the remote Streamable HTTP entry point."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import combined, remote  # noqa: E402


def _tool_names(mcp) -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


def test_remote_mcp_is_exactly_the_local_combined_contract():
    assert _tool_names(remote.mcp) == _tool_names(combined.build())


def test_healthz_is_static_and_successful():
    response = asyncio.run(remote.healthz(None))
    assert response.status_code == 200
    assert response.body == b'{"status":"ok"}'


def test_remote_defaults_to_serverless_port_and_all_interfaces():
    assert remote.mcp.settings.host == "0.0.0.0"
    assert remote.mcp.settings.port == 8080
    assert remote.mcp.settings.streamable_http_path == "/mcp"
    assert remote.mcp.settings.stateless_http is True
