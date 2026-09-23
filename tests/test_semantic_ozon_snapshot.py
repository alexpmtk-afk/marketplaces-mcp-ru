import asyncio
from types import SimpleNamespace

from core.metric_registry import load_metric_registry, resolve_metric_terms
from core.semantic_ozon_snapshot import execute_ozon_current_snapshot


class FakeStore:
    def __init__(self, cabinets=None):
        self.cabinets = cabinets or ["ozon_dmitrieva", "ozon_laser_master", "ozon_novokshenov"]

    def list_cabinets(self, service):
        assert service == "ozon"
        return {"active": "ozon_dmitrieva", "cabinets": list(self.cabinets)}

    def resolve_named(self, service, fields, env_map, name):
        assert service == "ozon"
        if name not in self.cabinets:
            return {}, False
        return {"client_id": f"id-{name}", "api_key": f"key-{name}"}, True


class FakeClient:
    def __init__(self, responses, cabinets=None):
        self.responses = list(responses)
        self.calls = []
        self.config = SimpleNamespace(
            name="ozon",
            fields=["client_id", "api_key"],
            env_map={"client_id": "OZON_CLIENT_ID", "api_key": "OZON_API_KEY"},
            store=FakeStore(cabinets),
        )

    async def call_spec(self, spec, *, json_body=None, creds_override=None, **kwargs):
        self.calls.append({"spec": spec, "json_body": json_body, "creds_override": creds_override})
        response = self.responses.pop(0)
        if callable(response):
            return response(spec, json_body)
        return response


class FakeOzon:
    def __init__(self, responses, cabinets=None):
        self.client = FakeClient(responses, cabinets)
        self.catalog = {
            "ozon_product_info_list": "entity-spec",
            "ozon_prices_get": "price-spec",
            "ozon_stocks_info": "stock-spec",
        }


def entity_response(product_id="3276772809", sku="3276433388"):
    return {
        "ok": True,
        "data": {
            "items": [{
                "id": product_id,
                "offer_id": "ТРН.03.006.9005.02.15/3",
                "sources": [{"sku": sku, "source": "sds"}],
            }]
        },
    }


def price_response(product_id="3276772809"):
    return {
        "ok": True,
        "data": {
            "items": [{
                "product_id": product_id,
                "offer_id": "ТРН.03.006.9005.02.15/3",
                "price": {
                    "marketing_seller_price": "981",
                    "price": "1001",
                    "old_price": "1820",
                    "min_price": "996",
                    "currency_code": "RUB",
                },
            }]
        },
    }


def stock_response(product_id="3276772809", sku="3276433388"):
    return {
        "ok": True,
        "data": {
            "items": [{
                "product_id": product_id,
                "offer_id": "ТРН.03.006.9005.02.15/3",
                "stocks": {
                    "fbo": {"present": 0, "reserved": 0, "sku": sku, "warehouse_ids": []},
                    "fbs": {"present": 1, "reserved": 0, "sku": sku, "warehouse_ids": [42]},
                },
            }]
        },
    }


def test_metric_registry_registers_current_selling_price_and_ozon_stock_mapping():
    registry = load_metric_registry()
    assert len(registry["metrics"]) == 30
    price = registry["metrics"]["CURRENT_SELLING_PRICE"]
    stock = registry["metrics"]["CURRENT_STOCK"]
    assert price["provider_mappings"]["ozon"]["source_id"] == "ozon_current_prices"
    assert price["provider_mappings"]["ozon"]["fields"][0] == "price.marketing_seller_price"
    assert stock["provider_mappings"]["ozon"]["source_id"] == "ozon_current_stocks"
    matches = resolve_metric_terms("Какая цена и остаток сейчас?")
    assert {item["metric_id"] for item in matches} >= {"CURRENT_SELLING_PRICE", "CURRENT_STOCK"}


def test_raw_question_resolves_named_cabinet_product_price_and_stock_without_dates():
    ozon = FakeOzon([entity_response(), price_response(), stock_response()])
    result = asyncio.run(execute_ozon_current_snapshot(
        ozon,
        question="Какая цена и остаток на Ozon LaserMaster по артикулу 3276433388?",
    ))

    assert result["ok"] is True
    assert result["complete"] is True
    assert result["cabinet"] == "ozon_laser_master"
    assert result["temporal_class"] == "CURRENT_SNAPSHOT"
    assert result["entity"] == {
        "marketplace": "OZON",
        "input_identifier": "3276433388",
        "product_id": "3276772809",
        "offer_id": "ТРН.03.006.9005.02.15/3",
        "sku": "3276433388",
        "known_skus": ["3276433388"],
    }
    assert result["current_selling_price"]["amount"] == "981"
    assert result["current_selling_price"]["provider_field"] == "marketing_seller_price"
    assert result["current_stock"]["available_units"] == 1
    assert result["current_stock"]["reserved_units"] == 0
    assert result["provenance"]["join_key"] == "product_id"
    assert all(call["creds_override"]["client_id"] == "id-ozon_laser_master" for call in ozon.client.calls)
    assert ozon.client.calls[0]["json_body"] == {"sku": ["3276433388"]}
    assert result["provenance"]["entity_lookup_field"] == "sku"
    observations = {item["metric_id"]: item for item in result["metric_observations"]}
    assert observations["CURRENT_SELLING_PRICE"]["source_field"] == "price.marketing_seller_price"
    assert observations["CURRENT_SELLING_PRICE"]["semantic_status"] == "provisional"
    assert observations["CURRENT_STOCK"]["source_field"] == "stocks.present"
    assert observations["CURRENT_STOCK"]["semantic_status"] == "provisional"
    assert result["knowledge_catalog_version"] == "marketplace_knowledge_catalog.v1"


def test_explicit_product_id_uses_only_product_id_namespace():
    ozon = FakeOzon([entity_response(), price_response()])
    result = asyncio.run(execute_ozon_current_snapshot(
        ozon,
        question="Какая цена Ozon LaserMaster product_id 3276772809?",
    ))
    assert result["ok"] is True
    assert ozon.client.calls[0]["json_body"] == {"product_id": ["3276772809"]}
    assert result["provenance"]["entity_lookup_field"] == "product_id"


def test_non_numeric_article_uses_only_offer_id_namespace():
    ozon = FakeOzon([entity_response(), price_response()])
    result = asyncio.run(execute_ozon_current_snapshot(
        ozon,
        question="Какая цена Ozon LaserMaster по артикулу ТРН.03.006.9005.02.15/3?",
    ))
    assert result["ok"] is True
    assert ozon.client.calls[0]["json_body"] == {"offer_id": ["ТРН.03.006.9005.02.15/3"]}
    assert result["provenance"]["entity_lookup_field"] == "offer_id"


def test_unknown_cabinet_fails_before_provider_and_never_uses_active_fallback():
    ozon = FakeOzon([entity_response()])
    result = asyncio.run(execute_ozon_current_snapshot(
        ozon,
        question="Цена Ozon UnknownShop артикул 3276433388",
        seller="UnknownShop",
    ))
    assert result["ok"] is False
    assert result["code"] == "CABINET_NOT_CONFIGURED"
    assert ozon.client.calls == []


def test_historical_stock_fails_before_provider_instead_of_using_current_snapshot():
    ozon = FakeOzon([entity_response()])
    result = asyncio.run(execute_ozon_current_snapshot(
        ozon,
        question="Какой остаток Ozon LaserMaster по артикулу 3276433388 на 1 августа?",
    ))
    assert result["ok"] is False
    assert result["code"] == "HISTORICAL_SOURCE_ABSENT"
    assert "current Ozon stock snapshot" in result["forbidden_substitutes"]
    assert ozon.client.calls == []


def test_price_success_stock_failure_is_not_reported_as_complete_success():
    ozon = FakeOzon([
        entity_response(),
        price_response(),
        {"ok": False, "error": "upstream", "retryable": True},
    ])
    result = asyncio.run(execute_ozon_current_snapshot(
        ozon,
        question="Цена и остаток Ozon LaserMaster артикул 3276433388",
    ))
    assert result["ok"] is False
    assert result["complete"] is False
    assert result["stage"] == "stock"
    assert result["leg_states"]["price"] == "PASS"
    assert result["leg_states"]["stock"] == "FAIL"


def test_product_identity_mismatch_blocks_join():
    ozon = FakeOzon([
        entity_response(),
        price_response(),
        stock_response(sku="9999999999"),
    ])
    result = asyncio.run(execute_ozon_current_snapshot(
        ozon,
        question="Цена и остаток Ozon LaserMaster артикул 3276433388",
    ))
    assert result["ok"] is False
    assert result["code"] == "PRODUCT_JOIN_MISMATCH"
    assert result["stage"] == "join"
