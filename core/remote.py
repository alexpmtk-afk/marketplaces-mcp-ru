"""Authenticated production Streamable HTTP entry point for marketplace MCP.

``/mcp`` is protected by a deployment-supplied bearer token. ``/healthz`` is
public liveness only and never contacts marketplace APIs. Remote startup is
fail-closed: Lockbox cabinet JSON and the shared Redis/Valkey backend must both
be configured and reachable before the MCP application is served.
"""
from __future__ import annotations

import hmac
import os
from typing import Any

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse

from .combined import build
from .credentials import ENV_CABINETS
from .rate_limit import verify_shared_redis
from .rate_limit_repair import repair_legacy_wb_global_cooldowns

PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
BEARER_ENV = "MARKETPLACE_MCP_BEARER_TOKEN"

mcp = build(host=HOST, port=PORT, stateless_http=True)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


class BearerAuthMiddleware:
    def __init__(self, app: Any, *, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "")
        scheme, _, presented = authorization.partition(" ")
        valid = scheme.lower() == "bearer" and bool(presented) and hmac.compare_digest(presented, self.token)
        if valid:
            await self.app(scope, receive, send)
            return
        body = b'{"error":"unauthorized"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"cache-control", b"no-store"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def _remote_app() -> Any:
    token = os.environ.get(BEARER_ENV, "").strip()
    if not token:
        raise RuntimeError(f"{BEARER_ENV} is required for the remote MCP; refusing to start unauthenticated")
    if not os.environ.get(ENV_CABINETS, "").strip():
        raise RuntimeError(f"{ENV_CABINETS} is required for the remote MCP; refusing to start without Lockbox cabinets")
    verify_shared_redis()
    repair_legacy_wb_global_cooldowns()
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app


def main() -> None:
    uvicorn.run(_remote_app(), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
