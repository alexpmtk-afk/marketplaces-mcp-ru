# Marketplace API Registry

Статус: research / не влияет на production deployment.

Цель каталога — хранить единую проверяемую карту API Wildberries и Ozon, чтобы MCP мог выбирать минимальное число upstream-запросов, учитывать реальные лимиты, пагинацию, сроки хранения данных, устаревшие методы и безопасные специализированные маршруты.

## Source of truth

Приоритет источников:

1. Официальный OpenAPI/Swagger или официальная документация маркетплейса.
2. Официальные уведомления об изменениях API.
3. Текущий `wb_mcp/endpoints.yaml` / `ozon_mcp/endpoints.yaml` — только как инвентаризация уже реализованного покрытия, не как окончательная истина.
4. Сторонние зеркала OpenAPI — только временный источник с пометкой `provisional`, если официальный spec технически недоступен.

## Что хранить для каждого метода

- marketplace
- operation_id
- HTTP method / host / path
- category / scope
- read / write / destructive
- назначение
- параметры и допустимые диапазоны дат
- batch limits / maximum objects per request
- pagination type and continuation key
- rate limit period / requests / interval / burst
- freshness / retention
- response size or row limits
- deprecated / shutdown date / replacement
- official source and verification date
- current MCP coverage
- preferred specialized tool
- generic fallback allowed or forbidden
- strategy notes: можно ли один пользовательский запрос свести к одному upstream HTTP request

## Первый подтверждённый кейс — WB Orders

`GET https://statistics-api.wildberries.ru/api/v1/supplier/orders`

Подтверждено официальной документацией WB:

- лимит: 1 запрос в минуту на один кабинет продавца;
- данные обновляются примерно раз в 30 минут;
- история заказов хранится не более 90 дней;
- `flag=0` или отсутствие `flag`: выборка по `lastChangeDate >= dateFrom`;
- один ответ при `flag=0` условно ограничен 80 000 строками, после чего требуется продолжение от `lastChangeDate` последней строки;
- это означает, что короткий многодневный пользовательский запрос потенциально можно агрегировать из одного upstream-вызова, если ответ не упёрся в границу 80 000 строк.

Это будет использовано для проверки/оптимизации `wb_get_orders_summary`.

## Важный текущий риск Ozon

В production-каталоге проекта всё ещё присутствуют:

- `/v3/finance/transaction/list`
- `/v3/finance/transaction/totals`

По уведомлению Ozon Seller API от 14.07.2026 эти методы устаревают и должны быть отключены 08.09.2026. Указанные замены:

- `/v1/finance/accrual/postings`
- `/v1/finance/accrual/types`
- `/v1/finance/accrual/by-day`

До прямой верификации актуального официального Ozon Swagger эта запись помечается как `high_priority_verify`, но миграцию нужно считать срочной.

## Следующие этапы

1. Снять полный inventory существующих WB/Ozon endpoint-каталогов проекта.
2. Нормализовать официальные лимиты и batching/pagination.
3. Сверить весь WB inventory с официальной документацией.
4. Получить актуальный Ozon Swagger; если прямой автоматический доступ останется заблокирован, запросить у пользователя только конкретный официальный swagger-файл.
5. Построить machine-readable `api_catalog.json` / YAML.
6. Добавить автоматический drift-check: endpoint из production-каталога, отсутствующий/устаревший в актуальном spec, должен попадать в отчёт.

Production `main` этой research-веткой не изменяется.
