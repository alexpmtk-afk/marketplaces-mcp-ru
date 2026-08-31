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

import asyncio
import importlib
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .rate_limit import RateLimitUnavailable, verify_shared_redis

SERVICE_MODULES = ("wb_mcp.server", "ozon_mcp.server", "ozon_perf_mcp.server")


def _rate_status_tool(client: Any):
    async def status() -> str:
        backend = client.rate_controller.backend
        reachable = True
        error = None
        if backend == "redis":
            try:
                await asyncio.to_thread(verify_shared_redis)
            except RateLimitUnavailable as exc:
                reachable = False
                error = str(exc)
        return json.dumps({
            "ok": reachable,
            "backend": backend,
            "shared": backend == "redis",
            "reachable": reachable,
            "error": error,
        }, ensure_ascii=False)
    return status


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
        svc = mod.client.config.name
        combined.tool(
            name=f"{svc}_rate_limit_status",
            annotations={
                "title": f"{svc.upper()} shared rate-limit status",
                "readOnlyHint": True,
                "openWorldHint": False,
            },
        )(_rate_status_tool(mod.client))
    return combined


def main() -> None:
    build().run()


if __name__ == "__main__":
    main()
