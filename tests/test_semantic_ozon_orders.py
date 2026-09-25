import asyncio
from types import SimpleNamespace

from core.semantic_ozon_orders import (
    execute_ozon_orders_question,
    requested_ozon_order_request,
)


class FakeStore:
    def __init__(self):
        self.cabinets = ["ozon_laser_master"]

    def list_cabinets(self, service):
        assert service == "ozon"
        return {"active": "ozon_laser_master", "cabinets": list(self.cabinets)}

    def resolve_named(self, service, fields, env_map, name):
        assert service == "ozon"
        if name not in self.cabinets:
            return {}, False
        return {"client_id": f"id-{name}", "api_key": f"key-{name}"}, True


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.config = SimpleNamespace(
            name="ozon",
            fields=["client_id", "api_key"],
            env_map={"client_id": "OZON_CLIENT_ID", "api_key": "OZON_API_KEY"},
            store=FakeStore(),
        )

    async def call_spec(self, spec, *, json_body=None, creds_override=None, **kwargs):
        self.calls.append({
            "spec": spec,
            "json_body": json_body,
            "creds_override": creds_override,
        })
        return self.responses.pop(0)


class FakeOzon:
    def __init__(self, responses):
        self.client = FakeClient(responses)
        self.catalog = {
            "ozon_fbo_list": "fbo-spec",
            "ozon_fbs_list": "fbs-spec",
        }


def posting_response(rows, cursor=""):
    return {
        "ok": True,
        "data": {
            "postings": rows,
            "cursor": cursor,
        },
    }


def row(posting, order, status="delivered", substatus=""):
    return {
        "posting_number": posting,
        "order_number": order,
        "order_id": int(order.replace("O", "") or 0),
        "status": status,
        "substatus": substatus,
        "in_process_at": "2026-09-10T10:00:00Z",
        "products": [{"price": "100", "quantity": 1, "sku": 1}],
    }


def test_request_parser_separates_orders_postings_and_fulfillment():
    orders = requested_ozon_order_request("Сколько заказов FBO Ozon за сентябрь?")
    postings = requested_ozon_order_request("Сколько отправлений FBS Ozon доставлено за сентябрь?")

    assert orders["metric_id"] == "OZON_ORDERS"
    assert orders["fulfillment"] == "fbo"
    assert orders["statuses"] == []

    assert postings["metric_id"] == "OZON_POSTINGS"
    assert postings["fulfillment"] == "fbs"
    assert postings["statuses"] == ["delivered"]


def test_plain_ozon_cancellations_route_to_cancelled_postings():
    request = requested_ozon_order_request("Сколько отмен Ozon за сентябрь?")
    assert request["metric_id"] == "OZON_CANCELLED_POSTINGS"
    assert request["statuses"] == ["cancelled"]
    assert request["fulfillment"] == "all"


def test_cancelled_postings_are_counted_at_posting_scope():
    ozon = FakeOzon([
        posting_response([
            row("P-FBO-1", "O1", "cancelled"),
            row("P-FBO-2", "O1", "cancelled"),
        ]),
        posting_response([
            row("P-FBS-1", "O2", "cancelled"),
        ]),
    ])

    result = asyncio.run(execute_ozon_orders_question(
        ozon,
        question="Сколько отмен Ozon за сентябрь?",
        seller="ozon_laser_master",
    ))

    assert result["ok"] is True
    assert result["metric_id"] == "OZON_CANCELLED_POSTINGS"
    assert result["value"] == 3
    assert result["postings"] == 3
    assert result["orders"] == 2
    assert result["status_filter"] == ["cancelled"]
    assert result["by_status"] == {"cancelled": 3}
    assert all(
        call["json_body"]["filter"]["statuses"] == ["cancelled"]
        for call in ozon.client.calls
    )
    assert result["metric_observations"][0]["source_field"] == "posting_number"


def test_orders_dedupe_order_number_across_fbo_and_fbs():
    ozon = FakeOzon([
        posting_response([
            row("P-FBO-1", "O1"),
            row("P-FBO-2", "O2"),
        ]),
        posting_response([
            row("P-FBS-1", "O1"),
            row("P-FBS-2", "O3"),
        ]),
    ])

    result = asyncio.run(execute_ozon_orders_question(
        ozon,
        question="Сколько заказов Ozon за сентябрь?",
        seller="ozon_laser_master",
    ))

    assert result["ok"] is True
    assert result["metric_id"] == "OZON_ORDERS"
    assert result["value"] == 3
    assert result["orders"] == 3
    assert result["postings"] == 4
    assert result["cross_fulfillment_orders"] == 1
    assert result["multi_posting_orders"] == 1
    assert result["by_fulfillment"] == {
        "fbo": {"orders": 2, "postings": 2},
        "fbs": {"orders": 2, "postings": 2},
    }
    assert result["metric_observations"][0]["source_field"] == "order_number"
    assert result["metric_observations"][0]["semantic_status"] == "provisional"


def test_postings_keep_status_at_posting_scope():
    ozon = FakeOzon([
        posting_response([
            row("P-FBO-1", "O1", "delivered"),
            row("P-FBO-2", "O2", "delivered"),
        ]),
        posting_response([
            row("P-FBS-1", "O1", "delivered"),
        ]),
    ])

    result = asyncio.run(execute_ozon_orders_question(
        ozon,
        question="Сколько отправлений Ozon доставлено за сентябрь?",
        seller="ozon_laser_master",
    ))

    assert result["ok"] is True
    assert result["metric_id"] == "OZON_POSTINGS"
    assert result["value"] == 3
    assert result["orders"] == 2
    assert result["by_status"] == {"delivered": 3}
    assert result["status_filter"] == ["delivered"]
    assert all(
        call["json_body"]["filter"]["statuses"] == ["delivered"]
        for call in ozon.client.calls
    )


def test_order_status_question_fails_closed_before_provider_call():
    ozon = FakeOzon([])

    result = asyncio.run(execute_ozon_orders_question(
        ozon,
        question="Сколько заказов Ozon доставлено за сентябрь?",
        seller="ozon_laser_master",
    ))

    assert result["ok"] is False
    assert result["code"] == "ORDER_STATUS_IS_POSTING_SCOPE"
    assert result["recommended_metric"] == "OZON_POSTINGS"
    assert ozon.client.calls == []


def test_order_amount_question_fails_closed():
    ozon = FakeOzon([])

    result = asyncio.run(execute_ozon_orders_question(
        ozon,
        question="На какую сумму были заказы Ozon за сентябрь?",
        seller="ozon_laser_master",
    ))

    assert result["ok"] is False
    assert result["code"] == "OZON_ORDER_AMOUNT_NOT_APPROVED"
    assert ozon.client.calls == []


def test_fbo_only_question_never_calls_fbs():
    ozon = FakeOzon([
        posting_response([
            row("P-FBO-1", "O1"),
            row("P-FBO-2", "O1"),
        ]),
    ])

    result = asyncio.run(execute_ozon_orders_question(
        ozon,
        question="Сколько отправлений FBO Ozon за сентябрь?",
        seller="ozon_laser_master",
    ))

    assert result["ok"] is True
    assert result["fulfillment_scope"] == "fbo"
    assert result["postings"] == 2
    assert result["orders"] == 1
    assert len(ozon.client.calls) == 1
    assert ozon.client.calls[0]["spec"] == "fbo-spec"


def test_period_is_required_when_question_has_no_date_context():
    ozon = FakeOzon([])

    result = asyncio.run(execute_ozon_orders_question(
        ozon,
        question="Сколько заказов Ozon?",
        seller="ozon_laser_master",
    ))

    assert result["ok"] is False
    assert result["code"] == "NEEDS_CONTEXT"
    assert "period/date range" in result["required_context"]
    assert ozon.client.calls == []


def test_named_month_uses_moscow_business_dates_and_utc_api_bounds():
    ozon = FakeOzon([
        posting_response([]),
        posting_response([]),
    ])

    result = asyncio.run(execute_ozon_orders_question(
        ozon,
        question="Сколько заказов Ozon за сентябрь 2026?",
        seller="ozon_laser_master",
    ))

    assert result["ok"] is True
    assert result["period"]["date_from"] == "2026-09-01"
    assert result["period"]["date_to"] == "2026-09-30"
    assert result["period"]["since_utc"] == "2026-08-31T21:00:00Z"
    assert result["period"]["to_utc"] == "2026-09-30T20:59:59Z"


def test_duplicate_posting_rows_are_deduplicated():
    duplicate = row("P1", "O1")
    ozon = FakeOzon([
        posting_response([duplicate, duplicate]),
        posting_response([]),
    ])

    result = asyncio.run(execute_ozon_orders_question(
        ozon,
        question="Сколько отправлений Ozon за сентябрь 2026?",
        seller="ozon_laser_master",
    ))

    assert result["ok"] is True
    assert result["postings"] == 1
    assert result["orders"] == 1
