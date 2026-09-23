from __future__ import annotations

from pathlib import Path

from core.registry import Catalog
from core.tools import has_proven_quota


ROOT = Path(__file__).parents[1]


def test_wb_content_cards_list_has_executable_proven_quota_contract():
    catalog = Catalog.from_yaml(ROOT / "wb_mcp" / "endpoints.yaml")
    spec = catalog.get("wb_content_cards_list")

    assert spec is not None
    assert spec.rate_limit == "100 req/min"
    assert spec.quota_proven is True
    assert has_proven_quota(spec) is True
