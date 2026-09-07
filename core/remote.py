"""Authenticated production Streamable HTTP entry point for marketplace MCP.

``/mcp`` is protected by the deployment bearer token. ``/healthz`` is public
liveness only. ``/card-monitor/ingest`` accepts browser-card batches from the
trusted Windows User Node and stores them in shared Redis/Valkey.
"""
from __future__ import annotations

import hmac
import os
from typing import Any

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse

from .card_monitor import STORE
from .combined import build
from .credentials import ENV_CABINETS
from .rate_limit import verify_shared_redis
from .rate_limit_repair import repair_legacy_wb_global_cooldowns

PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
BEARER_ENV = "MARKETPLACE_MCP_BEARER_TOKEN"
CARD_MONITOR_INGEST_ENV = "CARD_MONITOR_INGEST_TOKEN"
MAX_INGEST_BYTES = 1_000_000

mcp = build(host=HOST, port=PORT, stateless_http=True)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _bearer_matches(request: Request, expected: str) -> bool:
    authorization = request.headers.get("authorization", "")
    scheme, _, presented = authorization.partition(" ")
    return (
        scheme.lower() == "bearer"
        and bool(presented)
        and bool(expected)
        and hmac.compare_digest(presented, expected)
    )


@mcp.custom_route("/card-monitor/ingest", methods=["POST"])
async def card_monitor_ingest(request: Request) -> JSONResponse:
    token = os.environ.get(CARD_MONITOR_INGEST_ENV, "").strip()
    if not token:
        return JSONResponse({"error": "card_monitor_ingest_disabled"}, status_code=503)
    if not _bearer_matches(request, token):
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        )
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_INGEST_BYTES:
                return JSONResponse({"error": "payload_too_large"}, status_code=413)
        except ValueError:
            pass
    body = await request.body()
    if len(body) > MAX_INGEST_BYTES:
        return JSONResponse({"error": "payload_too_large"}, status_code=413)
    try:
        batch = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(batch, dict):
        return JSONResponse({"error": "payload_must_be_object"}, status_code=400)
    try:
        accepted = await STORE.ingest_batch(batch)
    except ValueError as exc:
        return JSONResponse({"error": "invalid_batch", "detail": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"error": "storage_unavailable", "detail": type(exc).__name__},
            status_code=503,
        )
    return JSONResponse(accepted, status_code=202, headers={"Cache-Control": "no-store"})


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
