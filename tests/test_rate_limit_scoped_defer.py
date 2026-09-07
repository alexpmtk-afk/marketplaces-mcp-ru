"""Regression coverage for marketplace Retry-After scoping."""
from __future__ import annotations

import asyncio

from core.rate_limit import GlobalRateController, RateRule


def test_defer_prefers_specific_rule_over_global(monkeypatch, tmp_path):
    controller = GlobalRateController(redis_url="", sqlite_path=tmp_path / "rates.sqlite3")
    captured = {}

    def fake_defer(keys, seconds):
        captured["keys"] = list(keys)
        captured["seconds"] = seconds

    monkeypatch.setattr(controller, "_defer_sqlite", fake_defer)
    rules = [
        RateRule("marketplace-rate:v1:wb:cab:global", 0.2),
        RateRule("marketplace-rate:v1:wb:cab:catalog:stocks", 900.0),
    ]
    asyncio.run(controller.defer(rules, 3511.0))

    assert captured["keys"] == ["marketplace-rate:v1:wb:cab:catalog:stocks"]
    assert captured["seconds"] == 3511.0


def test_defer_uses_global_when_no_specific_rule_exists(monkeypatch, tmp_path):
    controller = GlobalRateController(redis_url="", sqlite_path=tmp_path / "rates.sqlite3")
    captured = {}

    def fake_defer(keys, seconds):
        captured["keys"] = list(keys)

    monkeypatch.setattr(controller, "_defer_sqlite", fake_defer)
    rules = [RateRule("marketplace-rate:v1:ozon:cab:global", 0.05)]
    asyncio.run(controller.defer(rules, 5.0))

    assert captured["keys"] == ["marketplace-rate:v1:ozon:cab:global"]
