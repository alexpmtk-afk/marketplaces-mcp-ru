"""Non-secret registry of known marketplace business entities.

This registry deliberately stays separate from :mod:`core.credentials`:
it can describe an organisation and its canonical cabinet name even when
Lockbox/local storage has no credentials for that cabinet yet.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BusinessCabinet:
    """A canonical cabinet identity and its human business meaning."""

    service: str
    cabinet: str
    business_entity: str
    aliases: tuple[str, ...] = ()


_CABINETS = (
    BusinessCabinet("wb", "wb_dmitrieva", "ИП Дмитриева", ("DTE",)),
    BusinessCabinet("ozon", "ozon_dmitrieva", "ИП Дмитриева", ("DTE",)),
    BusinessCabinet("wb", "wb_novokshenov", "ИП Новокшенов"),
    BusinessCabinet("ozon", "ozon_novokshenov", "ИП Новокшенов"),
    BusinessCabinet("wb", "wb_laser_master", "ООО «Лазер - Мастер»"),
    BusinessCabinet("ozon", "ozon_laser_master", "ООО «Лазер - Мастер»"),
)


def resolve_business_cabinet(service: str, seller: str) -> BusinessCabinet | None:
    """Resolve a canonical cabinet, business name, or declared alias.

    Resolution is case-insensitive. It never fabricates credentials and it does
    not alter the active cabinet selection. Matching the human business name is
    intentional: user-facing tools may receive ``ИП Новокшенов`` while the
    credential store is keyed by ``wb_novokshenov``/``ozon_novokshenov``.
    """
    needle = seller.strip().casefold()
    if not needle:
        return None
    for entry in _CABINETS:
        if entry.service != service:
            continue
        candidates = (entry.cabinet, entry.business_entity, *entry.aliases)
        if any(needle == candidate.casefold() for candidate in candidates):
            return entry
    return None
