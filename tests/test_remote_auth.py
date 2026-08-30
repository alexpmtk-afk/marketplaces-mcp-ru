from __future__ import annotations

import asyncio

import pytest

from core import remote


def _run_middleware(headers: list[tuple[bytes, bytes]], path: str = "/mcp"):
    called = {"app": False}
    messages: list[dict] = []

    async def downstream(scope, receive, send):
        called["app"] = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    middleware = remote.BearerAuthMiddleware(downstream, token="expected-token")
    asyncio.run(middleware({"type": "http", "path": path, "headers": headers}, receive, send))
    return called, messages


def test_mcp_rejects_missing_bearer():
    called, messages = _run_middleware([])
    assert called["app"] is False
    assert messages[0]["status"] == 401


def test_mcp_rejects_wrong_bearer():
    called, messages = _run_middleware([(b"authorization", b"Bearer wrong")])
    assert called["app"] is False
    assert messages[0]["status"] == 401


def test_mcp_accepts_correct_bearer():
    called, messages = _run_middleware([(b"authorization", b"Bearer expected-token")])
    assert called["app"] is True
    assert messages[0]["status"] == 204


def test_health_route_is_not_bearer_guarded():
    called, messages = _run_middleware([], path="/healthz")
    assert called["app"] is True
    assert messages[0]["status"] == 204


def test_remote_refuses_to_start_without_bearer(monkeypatch):
    monkeypatch.delenv(remote.BEARER_ENV, raising=False)
    with pytest.raises(RuntimeError, match=remote.BEARER_ENV):
        remote._remote_app()


def test_remote_refuses_to_start_without_lockbox_cabinets(monkeypatch):
    monkeypatch.setenv(remote.BEARER_ENV, "x")
    monkeypatch.delenv(remote.ENV_CABINETS, raising=False)
    with pytest.raises(RuntimeError, match=remote.ENV_CABINETS):
        remote._remote_app()


def test_remote_preflights_shared_redis(monkeypatch):
    monkeypatch.setenv(remote.BEARER_ENV, "x")
    monkeypatch.setenv(remote.ENV_CABINETS, "{}")
    called = {"redis": False}
    monkeypatch.setattr(remote, "verify_shared_redis", lambda: called.__setitem__("redis", True))
    app = remote._remote_app()
    assert app is not None
    assert called["redis"] is True
