from core.business_registry import resolve_business_cabinet


def test_dte_resolves_to_dmitrieva_wb_without_using_another_cabinet():
    entry = resolve_business_cabinet("wb", "DTE")
    assert entry is not None
    assert entry.business_entity == "ИП Дмитриева"
    assert entry.cabinet == "wb_dmitrieva"


def test_dte_resolves_to_dmitrieva_ozon():
    entry = resolve_business_cabinet("ozon", "dte")
    assert entry is not None
    assert entry.business_entity == "ИП Дмитриева"
    assert entry.cabinet == "ozon_dmitrieva"


def test_human_business_names_resolve_without_exposing_cabinet_ids():
    dmitrieva = resolve_business_cabinet("wb", "Дмитриева")
    novokshenov = resolve_business_cabinet("wb", "ИП Новокшенов")
    laser = resolve_business_cabinet("wb", "Лазер-Мастер")
    assert dmitrieva is not None and dmitrieva.cabinet == "wb_dmitrieva"
    assert novokshenov is not None and novokshenov.cabinet == "wb_novokshenov"
    assert laser is not None and laser.cabinet == "wb_laser_master"


def test_unknown_business_seller_is_not_resolved():
    assert resolve_business_cabinet("wb", "unknown seller") is None
