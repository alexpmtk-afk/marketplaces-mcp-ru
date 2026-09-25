import asyncio
import hashlib

import pytest

from core.ozon_current_archive import DATASETS, serialize_coverage_registry, serialize_snapshot
from core.semantic_ozon_other_adjustments import (
    SemanticOzonOtherAdjustmentsError,
    execute_ozon_current_other_adjustments,
    requested_ozon_other_adjustments,
)


class FakeItem:
    def __init__(self, file_id):
        self.id = file_id


class FakeStore:
    def __init__(self, files):
        self.files = dict(files)

    async def ensure_folder_path(self, parts):
        return "/".join(parts)

    async def download_named(self, parent, name):
        raw = self.files.get((parent, name))
        if raw is None:
            return None, None
        return FakeItem("id-" + name), raw


def build_store(include_adjustment_delivery_overlap=False):
    delivery_services = [{"type_id": 45, "accrued": -40}]
    if include_adjustment_delivery_overlap:
        delivery_services.append({"type_id": 57, "accrued": -5})

    rows = [
        {
            "accrual_id": "1",
            "date": "2026-09-10",
            "accrued_category": "ITEM",
            "item_fees": {
                "fees": [
                    {
                        "sku": 1,
                        "quantity": 1,
                        "fees": [
                            {"type_id": 1, "accrued": -100},
                            {"type_id": 38, "accrued": -10},
                            {"type_id": 74, "accrued": -999},
                        ],
                    }
                ]
            },
        },
        {
            "accrual_id": "2",
            "date": "2026-09-11",
            "accrued_category": "NON_ITEM",
            "non_item_fee": {"type_id": 52, "accrued": -50},
        },
        {
            "accrual_id": "3",
            "date": "2026-09-12",
            "accrued_category": "NON_ITEM",
            "non_item_fee": {"type_id": 57, "accrued": -25},
        },
        {
            "accrual_id": "4",
            "date": "2026-09-13",
            "accrued_category": "POSTING",
            "posting": {
                "products": [
                    {
                        "delivery": {
                            "total_accrued": -40,
                            "services": delivery_services,
                        }
                    }
                ]
            },
        },
    ]
    raw = serialize_snapshot(
        rows,
        stable_key=tuple(DATASETS["ozon_current_accruals"]["stable_key"]),
    )
    sha = hashlib.sha256(raw).hexdigest()
    coverage = serialize_coverage_registry([
        {
            "marketplace": "ozon",
            "cabinet": "ozon_laser_master",
            "dataset": "ozon_current_accruals",
            "period": "2026-09",
            "date_from": "2026-09-01",
            "date_to": "2026-09-25",
            "stable_key": "accrual_id",
            "rows": 4,
            "canonical_file": "ozon_laser_master__accruals__2026-09.csv",
            "canonical_file_id": "file-id",
            "bytes": len(raw),
            "sha256": sha,
            "status": "COMPLETE",
            "refreshed_at_utc": "2026-09-25T10:00:00+00:00",
        }
    ])
    return FakeStore({
        (
            "База данных/Ozon/ozon_laser_master/2026/CURRENT/2026-09",
            "current_coverage_registry.csv",
        ): coverage,
        (
            "База данных/Ozon/ozon_laser_master/2026/CURRENT/2026-09",
            "ozon_laser_master__accruals__2026-09.csv",
        ): raw,
    })


def patch_now(monkeypatch):
    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            from datetime import datetime
            return datetime(2026, 9, 25, 12, 0, tzinfo=tz)

    import core.semantic_ozon_other_adjustments as module
    monkeypatch.setattr(module, "datetime", FixedDateTime)


def test_parser_distinguishes_other_services_and_adjustments():
    assert requested_ozon_other_adjustments("Прочие услуги Ozon")["metric"] == "OTHER_SERVICES_COST"
    assert requested_ozon_other_adjustments("Удержания и корректировки Ozon")["metric"] == "DEDUCTIONS_ADJUSTMENTS"
    assert requested_ozon_other_adjustments("Логистика Ozon") is None


def test_other_services_excludes_delivery_and_promotion(monkeypatch):
    patch_now(monkeypatch)
    result = asyncio.run(execute_ozon_current_other_adjustments(
        build_store(),
        question="Прочие услуги Ozon",
        seller="ozon_laser_master",
        date_from="2026-09-01",
        date_to="2026-09-24",
    ))
    assert result["ok"] is True
    assert result["metric_id"] == "OTHER_SERVICES_COST"
    assert result["value"] == 160.0
    assert result["signed_provider_value"] == -160.0
    assert 45 not in result["approved_type_ids"]
    assert 74 not in result["approved_type_ids"]


def test_adjustments_use_only_reviewed_types(monkeypatch):
    patch_now(monkeypatch)
    result = asyncio.run(execute_ozon_current_other_adjustments(
        build_store(),
        question="Удержания и корректировки Ozon",
        seller="ozon_laser_master",
    ))
    assert result["ok"] is True
    assert result["metric_id"] == "DEDUCTIONS_ADJUSTMENTS"
    assert result["value"] == 25.0
    assert result["signed_provider_value"] == -25.0
    assert result["approved_type_ids"] == [11, 57, 72, 83]


def test_adjustment_delivery_overlap_fails_closed(monkeypatch):
    patch_now(monkeypatch)
    with pytest.raises(
        SemanticOzonOtherAdjustmentsError,
        match="double-count logistics",
    ):
        asyncio.run(execute_ozon_current_other_adjustments(
            build_store(include_adjustment_delivery_overlap=True),
            question="Корректировки Ozon",
            seller="ozon_laser_master",
        ))


def test_historical_month_fails_closed(monkeypatch):
    patch_now(monkeypatch)
    with pytest.raises(SemanticOzonOtherAdjustmentsError, match="open month"):
        asyncio.run(execute_ozon_current_other_adjustments(
            build_store(),
            question="Прочие услуги Ozon за август 2026",
            seller="ozon_laser_master",
        ))
