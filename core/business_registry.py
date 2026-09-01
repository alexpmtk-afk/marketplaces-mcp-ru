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
    """Resolve a canonical cabinet or a declared business alias.

    Resolution is case-insensitive for human aliases.  It never fabricates
    credentials and it does not alter the active cabinet selection.
    """
    needle = seller.strip().casefold()
    if not needle:
        return None
    for entry in _CABINETS:
        if entry.service != service:
            continue
        if needle == entry.cabinet.casefold() or any(
            needle == alias.casefold() for alias in entry.aliases
        ):
            return entry
    return None
