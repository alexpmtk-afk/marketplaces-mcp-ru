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


def test_business_entity_name_resolves_to_service_specific_cabinet():
    wb = resolve_business_cabinet("wb", "ИП Новокшенов")
    ozon = resolve_business_cabinet("ozon", "ип новокшенов")

    assert wb is not None and wb.cabinet == "wb_novokshenov"
    assert ozon is not None and ozon.cabinet == "ozon_novokshenov"


def test_lasermaster_latin_alias_resolves_to_service_specific_cabinets():
    wb = resolve_business_cabinet("wb", "LaserMaster")
    ozon = resolve_business_cabinet("ozon", "laser master")

    assert wb is not None and wb.cabinet == "wb_laser_master"
    assert ozon is not None and ozon.cabinet == "ozon_laser_master"


def test_unknown_business_seller_is_not_resolved():
    assert resolve_business_cabinet("wb", "unknown seller") is None
