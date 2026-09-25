import asyncio
import hashlib

import pytest

from core.ozon_current_archive import DATASETS, serialize_coverage_registry, serialize_snapshot
from core.semantic_ozon_storage import (
    SemanticOzonStorageError,
    execute_ozon_current_storage,
    requested_ozon_storage,
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


def build_store():
    rows = [
        {
            "accrual_id": "1",
            "date": "2026-09-10",
            "accrued_category": "NON_ITEM",
            "non_item_fee": {"type_id": 46, "accrued": -100},
        },
        {
            "accrual_id": "2",
            "date": "2026-09-11",
            "accrued_category": "ITEM",
            "item_fees": {
                "fees": [
                    {
                        "sku": 123,
                        "quantity": 1,
                        "fees": [
                            {"type_id": 79, "accrued": -20},
                            {"type_id": 1, "accrued": -99},
                        ],
                    }
                ]
            },
        },
        {
            "accrual_id": "3",
            "date": "2026-09-12",
            "accrued_category": "ITEM",
            "item_fees": {
                "fees": [
                    {
                        "sku": 123,
                        "quantity": 1,
                        "fees": [
                            {"type_id": 60, "accrued": -30},
                            {"type_id": 78, "accrued": -10},
                            {"type_id": 102, "accrued": -5},
                        ],
                    }
                ]
            },
        },
        {
            "accrual_id": "4",
            "date": "2026-09-13",
            "accrued_category": "POSTING",
            "posting": {
                "products": [
                    {
                        "delivery": {
                            "total_accrued": -777,
                            "services": [{"type_id": 32, "accrued": -777}],
                        },
                        "commission": {"sale_commission": -333},
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

    import core.semantic_ozon_storage as module
    monkeypatch.setattr(module, "datetime", FixedDateTime)


def test_parser_recognizes_storage_but_not_ad_placement():
    assert requested_ozon_storage("Сколько стоит хранение Ozon?") is not None
    assert requested_ozon_storage("Стоимость размещения товаров на складах Ozon") is not None
    assert requested_ozon_storage("Сколько стоит размещение рекламы Ozon?") is None


def test_storage_sums_only_approved_storage_types(monkeypatch):
    patch_now(monkeypatch)
    result = asyncio.run(execute_ozon_current_storage(
        build_store(),
        question="Сколько стоит хранение Ozon?",
        seller="ozon_laser_master",
        date_from="2026-09-01",
        date_to="2026-09-24",
    ))
    assert result["ok"] is True
    assert result["complete"] is True
    assert result["metric_id"] == "STORAGE_COST"
    assert result["unit"] == "RUB"
    assert result["value"] == 165.0
    assert result["signed_provider_value"] == -165.0
    assert result["approved_type_ids"] == [46, 60, 78, 79, 102]
    breakdown = {x["type_id"]: x for x in result["breakdown"]}
    assert breakdown[46]["expense_rub"] == 100.0
    assert breakdown[79]["expense_rub"] == 20.0
    assert breakdown[60]["expense_rub"] == 30.0
    assert breakdown[78]["expense_rub"] == 10.0
    assert breakdown[102]["expense_rub"] == 5.0


def test_commission_logistics_and_acquiring_are_not_storage(monkeypatch):
    patch_now(monkeypatch)
    result = asyncio.run(execute_ozon_current_storage(
        build_store(),
        question="Расходы на хранение Ozon",
        seller="ozon_laser_master",
    ))
    assert result["value"] == 165.0


def test_historical_month_is_fail_closed(monkeypatch):
    patch_now(monkeypatch)
    with pytest.raises(SemanticOzonStorageError, match="open month"):
        asyncio.run(execute_ozon_current_storage(
            build_store(),
            question="Хранение Ozon за август 2026",
            seller="ozon_laser_master",
        ))
