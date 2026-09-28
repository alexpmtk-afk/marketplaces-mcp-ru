import asyncio
import hashlib

import pytest

from core.ozon_current_archive import DATASETS, serialize_coverage_registry, serialize_snapshot
from core.semantic_ozon_fines_compensations import (
    SemanticOzonFinesCompensationsError,
    execute_ozon_current_fines_compensations,
    requested_ozon_fines_compensations,
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


def build_store(include_delivery_overlap=False):
    delivery_services = [{"type_id": 32, "accrued": -40}]
    if include_delivery_overlap:
        delivery_services.append({"type_id": 94, "accrued": -5})

    rows = [
        {
            "accrual_id": "1",
            "date": "2026-09-10",
            "accrued_category": "NON_ITEM",
            "non_item_fee": {"type_id": 94, "accrued": -100},
        },
        {
            "accrual_id": "2",
            "date": "2026-09-11",
            "accrued_category": "ITEM",
            "item_fees": {
                "fees": [
                    {
                        "sku": 1,
                        "quantity": 1,
                        "fees": [
                            {"type_id": 10, "accrued": 75},
                            {"type_id": 25, "accrued": 25},
                            {"type_id": 1, "accrued": -999},
                        ],
                    }
                ]
            },
        },
        {
            "accrual_id": "3",
            "date": "2026-09-12",
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


def patch_now(monkeypatch):
    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            from datetime import datetime
            return datetime(2026, 9, 25, 12, 0, tzinfo=tz)

    import core.semantic_ozon_fines_compensations as module
    monkeypatch.setattr(module, "datetime", FixedDateTime)


def test_parser_distinguishes_penalties_and_compensations():
    assert requested_ozon_fines_compensations("Штрафы Ozon")["metric"] == "PENALTIES"
    assert requested_ozon_fines_compensations("Компенсации Ozon")["metric"] == "COMPENSATIONS"
    assert requested_ozon_fines_compensations("Страховое возмещение Ozon")["metric"] == "COMPENSATIONS"
    assert requested_ozon_fines_compensations("Логистика Ozon") is None


def test_penalties_use_only_reviewed_fine_types(monkeypatch):
    patch_now(monkeypatch)
    result = asyncio.run(execute_ozon_current_fines_compensations(
        build_store(),
        question="Какие штрафы Ozon?",
        seller="ozon_laser_master",
        date_from="2026-09-01",
        date_to="2026-09-24",
    ))
    assert result["ok"] is True
    assert result["metric_id"] == "PENALTIES"
    assert result["value"] == 100.0
    assert result["signed_provider_value"] == -100.0
    assert result["approved_type_ids"] == [89, 90, 91, 92, 93, 94]


def test_compensations_preserve_provider_net_sign(monkeypatch):
    patch_now(monkeypatch)
    result = asyncio.run(execute_ozon_current_fines_compensations(
        build_store(),
        question="Какие компенсации Ozon?",
        seller="ozon_laser_master",
    ))
    assert result["ok"] is True
    assert result["metric_id"] == "COMPENSATIONS"
    assert result["value"] == 100.0
    assert result["signed_provider_value"] == 100.0
    assert result["approved_type_ids"] == [10, 25, 104]


def test_delivery_overlap_fails_closed(monkeypatch):
    patch_now(monkeypatch)
    with pytest.raises(
        SemanticOzonFinesCompensationsError,
        match="double-count logistics",
    ):
        asyncio.run(execute_ozon_current_fines_compensations(
            build_store(include_delivery_overlap=True),
            question="Штрафы Ozon",
            seller="ozon_laser_master",
        ))


def test_historical_month_fails_closed(monkeypatch):
    patch_now(monkeypatch)
    with pytest.raises(SemanticOzonFinesCompensationsError, match="open month"):
        asyncio.run(execute_ozon_current_fines_compensations(
            build_store(),
            question="Штрафы Ozon за август 2026",
            seller="ozon_laser_master",
        ))
