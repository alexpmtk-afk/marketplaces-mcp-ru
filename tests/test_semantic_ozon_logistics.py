import asyncio
import hashlib
import json

import pytest

from core.ozon_current_archive import DATASETS, serialize_coverage_registry, serialize_snapshot
from core.semantic_ozon_logistics import (
    SemanticOzonLogisticsError,
    execute_ozon_current_logistics,
    requested_ozon_logistics,
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
            "accrued_category": "POSTING",
            "posting": {
                "products": [
                    {
                        "commission": {"sale_commission": -30},
                        "delivery": {
                            "total_accrued": -100,
                            "services": [
                                {"type_id": 32, "accrued": -80},
                                {"type_id": 29, "accrued": -20},
                            ],
                        },
                    }
                ]
            },
        },
        {
            "accrual_id": "2",
            "date": "2026-09-11",
            "accrued_category": "POSTING",
            "posting": {
                "products": [
                    {
                        "delivery": {
                            "total_accrued": -50,
                            "services": [{"type_id": 59, "accrued": -50}],
                        }
                    }
                ]
            },
        },
        {
            "accrual_id": "3",
            "date": "2026-09-12",
            "accrued_category": "NON_ITEM",
            "non_item_fee": {"type_id": 46, "accrued": -999},
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
            "rows": 3,
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


def test_parser_recognizes_logistics_not_delivery_status():
    assert requested_ozon_logistics("Какие расходы на логистику Ozon?") is not None
    assert requested_ozon_logistics("Какая стоимость доставки Ozon?") is not None
    assert requested_ozon_logistics("Сколько заказов доставлено сегодня?") is None


def test_logistics_uses_only_delivery_total_accrued(monkeypatch):
    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            from datetime import datetime
            return datetime(2026, 9, 25, 12, 0, tzinfo=tz)

    import core.semantic_ozon_logistics as module
    monkeypatch.setattr(module, "datetime", FixedDateTime)

    result = asyncio.run(execute_ozon_current_logistics(
        build_store(),
        question="Какие расходы на логистику Ozon за сентябрь 2026?",
        seller="ozon_laser_master",
        date_from="2026-09-01",
        date_to="2026-09-24",
    ))
    assert result["ok"] is True
    assert result["complete"] is True
    assert result["metric_id"] == "LOGISTICS_COST"
    assert result["unit"] == "RUB"
    assert result["value"] == 150.0
    assert result["signed_provider_value"] == -150.0
    assert result["source_field"] == "posting.products[].delivery.total_accrued"
    assert result["delivery_product_lines"] == 2


def test_storage_and_commission_are_not_added_to_logistics(monkeypatch):
    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            from datetime import datetime
            return datetime(2026, 9, 25, 12, 0, tzinfo=tz)

    import core.semantic_ozon_logistics as module
    monkeypatch.setattr(module, "datetime", FixedDateTime)

    result = asyncio.run(execute_ozon_current_logistics(
        build_store(),
        question="Стоимость логистики Ozon",
        seller="ozon_laser_master",
    ))
    assert result["value"] == 150.0


def test_historical_month_is_fail_closed(monkeypatch):
    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            from datetime import datetime
            return datetime(2026, 9, 25, 12, 0, tzinfo=tz)

    import core.semantic_ozon_logistics as module
    monkeypatch.setattr(module, "datetime", FixedDateTime)

    with pytest.raises(SemanticOzonLogisticsError, match="open month"):
        asyncio.run(execute_ozon_current_logistics(
            build_store(),
            question="Расходы на логистику Ozon за август 2026",
            seller="ozon_laser_master",
        ))
