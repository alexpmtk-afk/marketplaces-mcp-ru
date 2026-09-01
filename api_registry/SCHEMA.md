# API Registry schema

`api_catalog.json` — производный файл. Его источник — implementation catalogs + verified overrides.

## Обязательные поля записи

- `marketplace`
- `operation_id`
- `http_method`
- `host`
- `path`
- `section`
- `scope`
- `safety`
- `summary`
- `params`
- `pagination`
- `rate_limit`
- `documentation`
- `implementation_source`
- `verification`

## Дополнительные проверенные блоки

### `contract`

Фактические ограничения API:

- диапазон дат;
- retention/freshness;
- request batching;
- response row/object limit;
- pagination continuation;
- rate-limit scope/period/burst;
- специальные режимы (`flag`, cursor, last_id и т.п.).

### `lifecycle`

- `status`: current / deprecated / shutdown_announced / shutdown_date_passed;
- notice/shutdown dates;
- replacement paths;
- semantic one-to-one replacement flag.

### `optimization`

- preferred specialized MCP tool;
- generic fallback policy;
- can-combine-multiple-user-questions;
- minimum expected upstream requests;
- completeness guard;
- cache suitability.

## Уровни доверия

1. `official_contract_verified=true` — подтверждено официальным Swagger/docs/change notice.
2. `mirror_contract_verified=true` — подтверждено пинованным снимком, который заявляет происхождение из official OpenAPI; требуется final official capture для production-critical migrations.
3. `implementation_inventory=true` — метод существует в коде проекта, но это НЕ доказательство актуальности внешнего API.

## Safety

Для Ozon HTTP-метод не определяет безопасность. Большинство read-операций используют POST. `safety` должен определяться по назначению операции и официальной семантике.

Для методов, явно помеченных текущим каталогом как `UNVERIFIED`, production safety не повышается автоматически даже если endpoint отвечает 200.
