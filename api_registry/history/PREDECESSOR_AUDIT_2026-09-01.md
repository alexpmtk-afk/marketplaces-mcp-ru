# API Registry Audit — 2026-09-01

Статус: research branch only. Production не изменён.

## Подтверждённые снимки источников

### Wildberries

Пинован вторичный снимок `MissiaL/wildberries-api` commit `6f56592666e423bf587da06fe204d287094c3a38`.

Его manifest указывает:

- 13 официальных страниц OpenAPI;
- 286 операций;
- browser-redoc-openapi extraction;
- capture/rate-limit verification time: 2026-08-19T18:12:37.448Z.

Это не заменяет официальный `dev.wildberries.ru`, но даёт воспроизводимый snapshot для машинного diff, пока официальный портал блокирует plain HTTP anti-bot проверкой.

### Ozon

Пинован вторичный снимок `MissiaL/ozon-api` commit `1953152c36955225b459cf55963a2c3a7a234661`.

Снимок заявляет:

- Seller API: 463 операции / 57 разделов;
- Performance API: 48 операций / 6 разделов;
- источник сборки — браузерная выгрузка официального Ozon Swagger.

Официальный браузерный export остаётся обязательным источником перед финальным production-переходом на спорных/критичных методах.

## Уже найденные расхождения production-каталога

### 1. Ozon FBS v3

В production `ozon_mcp/endpoints.yaml` реализованы:

- `ozon_fbs_unfulfilled` → `POST /v3/posting/fbs/unfulfilled/list`;
- `ozon_fbs_list` → `POST /v3/posting/fbs/list`.

Пинованный OpenAPI index помечает оба v3 метода deprecated и одновременно содержит v4 replacements:

- `/v4/posting/fbs/unfulfilled/list`;
- `/v4/posting/fbs/list`.

Snapshot README отдельно указывает shutdown v3 unfulfilled на 2026-08-31. На дату аудита 2026-09-01 это CRITICAL drift candidate.

### 2. Ozon Finance v3

Production содержит:

- `/v3/finance/transaction/list`;
- `/v3/finance/transaction/totals`.

Официальный Ozon Seller API notification channel объявил отключение 2026-09-08 и переход на семейство accrual API:

- `/v1/finance/accrual/postings`;
- `/v1/finance/accrual/types`;
- `/v1/finance/accrual/by-day`.

Это не one-to-one замена. До миграции надо доказать семантику расчёта итогов и обработку неизвестных типов начислений.

### 3. Ozon auto-imported UNVERIFIED operations

В production-каталоге имеется большой блок методов с комментарием `auto-imported from go-client; UNVERIFIED`.

Риск двойной:

1. request/body contract может не совпадать с актуальным Swagger;
2. `safety` местами нельзя выводить из HTTP POST. В Ozon чистые read-операции часто являются POST.

До OpenAPI-сверки эти записи должны считаться implementation inventory, а не подтверждённым API contract.

## Wildberries Orders — оптимизация подтверждена как кандидат

Для `GET /api/v1/supplier/orders` зафиксировано:

- 1 request / 60 sec / seller account / method;
- история до 90 дней;
- обновление приблизительно каждые 30 минут;
- `flag=0` выбирает по `lastChangeDate >= dateFrom`;
- до 80 000 строк до продолжения pagination.

Следствие: пользовательский запрос на несколько близких календарных дней потенциально можно выполнить одним upstream WB request и сгруппировать внутри MCP по фактической дате заказа. Обязательное условие — доказать полноту ответа и обработать 80k boundary.

## Создан механизм нормализации

`api_registry/build_catalog.py` строит детерминированный `api_registry/api_catalog.json` из:

- `wb_mcp/endpoints.yaml`;
- `ozon_mcp/endpoints.yaml`;
- `ozon_mcp/perf_endpoints.yaml`;
- проверенных `api_registry/overrides.yaml`.

Он считает endpoint inventory, SHA-256 исходных каталогов, safety breakdown и поддерживает `--check` для drift detection.

Следующий технический шаг после завершения текущей production-задачи другим Codex: запустить builder на актуальном canonical HEAD и построить автоматический diff production catalog ↔ pinned OpenAPI snapshots.
