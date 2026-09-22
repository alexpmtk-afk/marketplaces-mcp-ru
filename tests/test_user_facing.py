from core.user_facing import (
    present_business_result,
    present_execution_control,
    present_query_plan,
)


def test_archive_error_is_plain_russian_and_keeps_technical_message():
    raw = {
        "ok": False,
        "error": "source_not_suitable",
        "error_type": "source_not_suitable",
        "message": (
            "The approved semantic calculation requires the canonical marketplace "
            "archive, but archive storage is not configured."
        ),
        "details": {"required": "canonical Google Drive archive"},
        "retryable": False,
    }

    shown = present_business_result(raw, seller="LaserMaster")

    assert shown["message"] == (
        "Не удалось получить данные: сейчас нет доступа к базе данных "
        "с историческими данными по магазину LaserMaster."
    )
    assert shown["user_message"] == shown["message"]
    assert shown["technical_message"] == raw["message"]
    assert "канонич" not in shown["message"].casefold()
    assert "live" not in shown["message"].casefold()
    assert "fail" not in shown["message"].casefold()


def test_query_plan_reason_is_human_but_technical_reason_is_preserved():
    raw = {
        "ok": True,
        "source_family": "CANONICAL_ARCHIVE",
        "source_status": "FULL_COVERAGE_REQUIRED",
        "reason": (
            "This approved historical business capability belongs to the canonical "
            "archive and requires full coverage."
        ),
    }

    shown = present_query_plan(raw)

    assert shown["reason"] == (
        "Для расчёта будут использованы данные из базы. "
        "Сначала сервер проверит, что весь указанный период загружен полностью."
    )
    assert shown["user_reason"] == shown["reason"]
    assert shown["technical_reason"] == raw["reason"]
    assert "архив" not in shown["reason"].casefold()
    assert "full_coverage" not in shown["reason"].casefold()


def test_historical_source_absent_has_plain_reason():
    shown = present_query_plan({
        "ok": False,
        "source_family": "UNAVAILABLE",
        "source_status": "HISTORICAL_SOURCE_ABSENT",
        "reason": "No approved historical source exists.",
    })
    assert shown["reason"] == "Исторические данные для такого запроса пока не подключены."


def test_execution_control_humanizes_source_gap():
    shown = present_execution_control({
        "ok": False,
        "source_gap": {
            "source_family": "UNAVAILABLE",
            "source_status": "NO_VERIFIED_ARCHIVE_HISTORY",
            "reason": "There is no verified populated historical orders archive.",
        },
    })
    assert shown["user_message"] == (
        "В базе данных пока нет подтверждённых данных за весь указанный период."
    )
    assert shown["source_gap"]["technical_reason"].startswith("There is no verified")


def test_ozon_success_has_short_russian_summary():
    shown = present_business_result({
        "ok": True,
        "current_selling_price": {"amount": "981", "currency": "RUB"},
        "current_stock": {"available_units": 0},
    })
    assert shown["user_message"] == "Цена: 981 ₽ Остаток: 0 шт."


def test_preliminary_orders_note_is_plain_russian():
    shown = present_business_result({
        "ok": True,
        "metric": "ORDERS",
        "orders_count": 7,
        "orders_amount": 8177,
        "business_completeness": "PRELIMINARY_NOT_ALL_ORDERS",
    })
    assert shown["user_message"] == "Заказов: 7 на сумму 8 177 ₽."
    assert shown["user_note"] == (
        "Это оперативные данные Wildberries; часть заказов может появляться с задержкой."
    )


def test_user_message_does_not_add_substitution_boilerplate():
    shown = present_business_result({
        "ok": False,
        "error": "source_not_suitable",
        "error_type": "source_not_suitable",
        "message": "No suitable source.",
        "details": {
            "forbidden_substitutes": [
                "another source",
                "current snapshot",
            ]
        },
    })
    text = shown["user_message"].casefold()
    assert "не подмен" not in text
    assert "обход" not in text
    assert "forbidden" not in text
