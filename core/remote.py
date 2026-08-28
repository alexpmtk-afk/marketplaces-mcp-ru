"""Remote Streamable HTTP entry point for the combined marketplace MCP.

This module changes only transport and process binding. Tool registration and
business logic remain in the existing service modules and core.combined.
"""
from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from .combined import build

PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("MCP_HOST", "0.0.0.0")

mcp = build(host=HOST, port=PORT)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    """Container liveness probe; never calls marketplace APIs."""
    return JSONResponse({"status": "ok"})


def main() -> None:
    """Run the existing MCP contract through Streamable HTTP at /mcp."""
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
