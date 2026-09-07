"""Pagination walker correctness — all offline with a fake client.

Covers review findings:
  - offset paging must not stop early when the server caps page size below the
    requested limit (silent data loss);
  - a non-numeric total/page_count must not crash the walk with ValueError.
"""
from __future__ import annotations

import asyncio

from core.paginate import fetch_all
from core.registry import EndpointSpec


class _FakeClient:
    """Serves canned pages. Records the offsets/pages requested."""
    def __init__(self, responder):
        self._responder = responder

    async def call_spec(self, spec, *, path_values=None, query=None, json_body=None):
        loc = query or {} if spec.method.upper() == "GET" else (json_body or {})
        return {"ok": True, "status": 200, "data": self._responder(loc)}


def _offset_spec():
    return EndpointSpec(operation_id="op", method="GET",
                        host="h", path="/x", pagination="offset",
                        items_path="items")


def test_offset_does_not_stop_when_server_caps_page_size():
    """Walker asks for limit=1000; server hands back at most 100 per page.
    All 250 rows must be collected, not just the first 100."""
    TOTAL = 250
    SERVER_CAP = 100
    rows = list(range(TOTAL))

    def responder(loc):
        offset = loc.get("offset", 0)
        limit = min(loc.get("limit", 0), SERVER_CAP)
        return {"items": rows[offset:offset + limit]}

    out = asyncio.run(fetch_all(_FakeClient(responder), _offset_spec(), limit=1000))
    assert out["ok"] is True
    assert out["total_fetched"] == TOTAL, out["total_fetched"]
    assert out["truncated"] is False


def test_offset_stops_on_empty_page():
    rows = list(range(30))

    def responder(loc):
        offset = loc.get("offset", 0)
        limit = loc.get("limit", 1000)
        return {"items": rows[offset:offset + limit]}

    out = asyncio.run(fetch_all(_FakeClient(responder), _offset_spec(), limit=1000))
    assert out["total_fetched"] == 30


def test_cursor_nonnumeric_total_does_not_crash():
    spec = EndpointSpec(operation_id="op", method="POST", host="h", path="/x",
                        pagination="cursor", items_path="items")
    pages = [
        {"items": [1, 2], "cursor": "c1", "total": "lots"},   # bad total
        {"items": [3], "cursor": "", "total": "lots"},
    ]
    seq = iter(pages)

    def responder(loc):
        return next(seq)

    out = asyncio.run(fetch_all(_FakeClient(responder), spec, limit=100))
    assert out["ok"] is True
    assert out["total_fetched"] == 3


def test_page_style_nonnumeric_page_count_does_not_crash():
    spec = EndpointSpec(operation_id="op", method="POST", host="h", path="/x",
                        pagination="page", items_path="items")
    pages = [
        {"items": [1, 2], "page_count": "many"},
        {"items": [], "page_count": "many"},
    ]
    seq = iter(pages)

    def responder(loc):
        return next(seq)

    out = asyncio.run(fetch_all(_FakeClient(responder), spec, limit=100))
    assert out["ok"] is True
    assert out["total_fetched"] == 2



def test_cursor_honours_has_next_false_without_extra_request():
    spec = EndpointSpec(operation_id="op", method="POST", host="h", path="/x",
                        pagination="cursor", items_path="postings")
    calls = []

    def responder(loc):
        calls.append(dict(loc))
        return {"postings": [1, 2], "cursor": "would-be-next", "has_next": False}

    out = asyncio.run(fetch_all(_FakeClient(responder), spec, limit=100))
    assert out["total_fetched"] == 2
    assert len(calls) == 1


def test_last_id_no_limit_never_injects_limit():
    spec = EndpointSpec(operation_id="op", method="POST", host="h", path="/x",
                        pagination="last_id_no_limit", items_path="accruals")
    calls = []

    def responder(loc):
        calls.append(dict(loc))
        if loc.get("last_id") == "next":
            return {"accruals": [2], "last_id": ""}
        return {"accruals": [1], "last_id": "next"}

    out = asyncio.run(fetch_all(
        _FakeClient(responder), spec,
        base_body={"date": "2026-09-07", "last_id": ""}, limit=100,
    ))
    assert out["total_fetched"] == 2
    assert len(calls) == 2
    assert all("limit" not in call for call in calls)
    assert calls[1]["last_id"] == "next"
