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


def test_unknown_business_seller_is_not_resolved():
    assert resolve_business_cabinet("wb", "unknown seller") is None
