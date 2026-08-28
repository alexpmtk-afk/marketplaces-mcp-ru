"""Combined MCP server: Wildberries + Ozon + Ozon-Perf on one FastMCP.

One process, one stdio channel, every tool. This backs both the `.mcpb`
Claude Desktop bundle (`serve.py all`) and the `marketplaces-mcp-ru` console
script (`uvx marketplaces-mcp-ru`).

Tool names are already namespaced per service (``wb_*`` / ``ozon_*`` /
``ozon_perf_*``), so merging the three servers' tool sets can never collide.
Each service module builds its FastMCP as an import side effect; we copy the
already-registered tools onto a single parent via FastMCP's tool manager. That
internal surface is stable within the pinned ``mcp>=1.2,<2`` range.
"""
from __future__ import annotations

import importlib
from typing import Any

from mcp.server.fastmcp import FastMCP

SERVICE_MODULES = ("wb_mcp.server", "ozon_mcp.server", "ozon_perf_mcp.server")


def build(**fastmcp_kwargs: Any) -> FastMCP:
    """Return one FastMCP carrying every service's tools.

    Keyword arguments are passed only to the combined FastMCP constructor. This
    keeps the local stdio server unchanged while allowing the remote entry point
    to supply its HTTP host and port.
    """
    combined = FastMCP("marketplaces-mcp-ru", **fastmcp_kwargs)
    for mod_name in SERVICE_MODULES:
        mod = importlib.import_module(mod_name)
        combined._tool_manager._tools.update(mod.mcp._tool_manager._tools)
    return combined


def main() -> None:
    build().run()


if __name__ == "__main__":
    main()
