# Canonical API Map — Wildberries + Ozon

Status: WORKING BASELINE
Date: 2026-09-01
Scope: Marketplace MCP routing, API freshness, request minimization and typed-tool design.

## 1. Purpose

The repository already contains low-level endpoint catalogs generated/imported from marketplace API specifications:

- `wb_mcp/endpoints.yaml` — Wildberries;
- `ozon_mcp/endpoints.yaml` — Ozon Seller;
- `ozon_mcp/perf_endpoints.yaml` — Ozon Performance.

This document is a higher-level control map. It must answer a different set of questions:

1. Is the endpoint current on the stated date?
2. Is it deprecated or scheduled for shutdown?
3. What is the official replacement?
4. What user/business question does it answer?
5. Can the answer be obtained with one upstream HTTP request?
6. What pagination is required when one response is not enough?
7. What rate limit/freshness constraint controls the request?
8. Should a dedicated typed MCP tool exist for the task?
9. What fallback is allowed if the preferred tool cannot be used?
10. What evidence proves the route in runtime?

The goal is not to expose hundreds of MCP tools. The preferred surface remains compact:

`SPECIALIZED TOOL > WORKFLOW TOOL > GENERIC MCP TOOL > FAIL CLOSED`

Generic calls are discovery/fallback mechanisms, not the preferred route for frequent business questions.

## 2. Source hierarchy

For freshness and migrations use this trust order:

1. official marketplace OpenAPI/Swagger and official change notifications;
2. current runtime/API responses;
3. repository endpoint catalogs generated from official specs;
4. tests and deployment evidence;
5. third-party mirrors only as a temporary discovery aid, never as sole authority for a destructive migration.

Any item marked `VERIFY_OFFICIAL` must not trigger a production code migration until confirmed by source (1) or (2).

## 3. Canonical row schema

Every business-relevant route should eventually be represented by these fields:

| Field | Meaning |
|---|---|
| marketplace | `wb`, `ozon`, `ozon-performance` |
| business_task | Human question/use case |
| operation_id | Catalog operation id |
| method_path | HTTP method + path |
| status | CURRENT / DEPRECATED / SHUTDOWN / VERIFY_OFFICIAL |
| replacement | Current replacement when applicable |
| safety | read / write / destructive |
| period_limit | Maximum interval per upstream call |
| pagination | none / cursor / offset / lastChangeDate / rrdid / other |
| rate_limit | Marketplace rate limit |
| freshness | Expected source refresh cadence |
| one_call_fit | When one upstream request is enough |
| aggregation | Local aggregation required by MCP |
| preferred_tool | Typed/workflow tool for the user task |
| fallback | Allowed fallback route or FAIL CLOSED |
| evidence | Source/test/runtime evidence |

## 4. P0 freshness findings — Ozon

### 4.1 Finance transaction API migration — deadline 2026-09-08

Current repository catalog still contains:

- `ozon_finance_transactions` → `POST /v3/finance/transaction/list`;
- `ozon_finance_totals` → `POST /v3/finance/transaction/totals`.

Ozon Seller API notification dated 2026-07-14 states that both methods are deprecated and will be disabled on **2026-09-08**. Ozon instructs clients to migrate to:

- `POST /v1/finance/accrual/postings`;
- `POST /v1/finance/accrual/types`;
- `POST /v1/finance/accrual/by-day`.

Current `ozon_mcp/endpoints.yaml` does not yet contain `/v1/finance/accrual/*`.

**Status: P0 — migration required before 2026-09-08.**

Required implementation:

1. import/describe the three accrual endpoints from an official current spec;
2. add them to the canonical Ozon catalog;
3. mark v3 transaction list/totals as deprecated with explicit replacement metadata;
4. add tests for request schema, pagination and response parsing;
5. define typed business tools for common finance totals instead of routing ordinary finance questions to generic calls;
6. acceptance must prove that no user-facing finance route still selects the deprecated methods when a current replacement exists.

### 4.2 Posting list migrations — deadline passed 2026-08-31

Ozon Seller API notifications indicate the following migrations effective **2026-08-31**:

- `/v3/posting/fbs/list` → `/v4/posting/fbs/list`;
- `/v2/posting/fbo/list` → `/v3/posting/fbo/list`;
- `/v3/posting/fbs/unfulfilled/list` → `/v4/posting/fbs/unfulfilled/list`;
- `/v1/posting/digital/list` → `/v2/posting/digital/list`.

The current repository catalog still includes the older FBS/FBO paths above.

**Status: P0 — current date is 2026-09-01; verify current official spec/runtime immediately and migrate where confirmed.**

Important: version upgrades are not assumed to be simple path renames. Request/response envelopes and pagination may change (for example, cursor-based pagination vs offset). Update `items_path` and pagination metadata together with the path.

## 5. P0/P1 request minimization — Wildberries Orders

Current catalog route:

- operation: `wb_stats_orders`;
- `GET https://statistics-api.wildberries.ru/api/v1/supplier/orders`;
- rate limit in repository catalog: `1 req/min`;
- cursor model: `lastChangeDate`;
- parameters: `dateFrom`, `flag`.

The dedicated typed tool `wb_get_orders_summary` is already present. It was introduced specifically to prevent a simple user question from causing repeated generic calls and hidden rate-limit waits.

### Current typed-tool behavior

For a single day the tool:

- makes at most one upstream `wb_stats_orders` request when cache/rate state allows;
- excludes `isCancel=true` rows;
- aggregates `finishedPrice`;
- returns `orders_count` and `orders_amount`;
- uses shared cache;
- returns `rate_limit_busy + retry_after_sec` immediately rather than sleeping/queueing inside the interactive MCP request;
- does not expose credentials.

### Next optimization target

The official WB Orders API can return a date range starting at `dateFrom`; therefore multi-day user questions should not automatically become N one-day HTTP requests.

Target design:

1. prefer **one upstream request + local date filtering/aggregation** when the requested period fits one response;
2. if the response reaches the upstream row/page boundary, continue only via the documented `lastChangeDate` cursor;
3. do not perform repeated per-day calls for a multi-day period merely because the current typed tool accepts one day;
4. do not reserve minute-spaced future slots for interactive calls;
5. distinguish local rate-slot contention from a genuine upstream WB `429` cooldown;
6. cache immutable closed-day/range results longer than current-day results.

Candidate implementation:

- extend `wb_get_orders_summary` to safe period mode, or
- add `wb_get_orders_summary_period` and keep the single-day contract stable.

Choose only after tests define exact response-boundary and cursor behavior.

## 6. Rate limiter control rule

Interactive user requests must never create a long hidden queue.

Required semantics:

- if local slot is unavailable: return immediately with a structured status such as `rate_limit_busy`, source=`local_rate_slot`, `retry_after_sec`;
- if WB itself returned `429`: preserve the real upstream cooldown separately, source=`wb_429_cooldown`;
- no automatic retry storm;
- no future reservation chain that can accumulate tens of minutes of debt from cancelled/abandoned requests;
- cache hit must not consume an upstream rate slot.

This remains an open implementation/audit item until a new post-`07a3408` commit and runtime acceptance prove the corrected behavior.

## 7. User-facing routing contract

For the dedicated `marketplaces-mcp-only` Codex project:

1. remote `marketplaces-yandex` is the marketplace data source;
2. direct/local WB/Ozon MCPs are disabled;
3. direct shell network and web search are disabled for marketplace data retrieval;
4. ordinary Russian-language questions must select a specialized tool when one exists;
5. a generic tool must not override a specialized route simply because it can technically call the same endpoint;
6. if the required cabinet is known but credentials are not configured, fail closed with a business-specific configuration error; never substitute another seller cabinet;
7. runtime acceptance should record the actual MCP tool used, not only the final answer text.

## 8. Current known business cabinet registry

Non-secret canonical identities currently include:

- WB `wb_dmitrieva` — ИП Дмитриева, alias `DTE`;
- Ozon `ozon_dmitrieva` — ИП Дмитриева, alias `DTE`;
- WB `wb_novokshenov` — ИП Новокшенов;
- Ozon `ozon_novokshenov` — ИП Новокшенов;
- WB `wb_laser_master` — ООО «Лазер - Мастер»;
- Ozon `ozon_laser_master` — ООО «Лазер - Мастер».

Registry identity and credential storage remain intentionally separate.

## 9. Immediate implementation queue

### P0 — Ozon freshness

- [ ] Obtain/verify current official Ozon Seller OpenAPI source.
- [ ] Import `/v1/finance/accrual/{postings,types,by-day}`.
- [ ] Mark `/v3/finance/transaction/{list,totals}` deprecated and route user finance questions to current APIs.
- [ ] Verify and migrate FBS/FBO/unfulfilled posting-list versions effective 2026-08-31.
- [ ] Add regression tests for new request/response envelopes and pagination.

### P0 — WB rate limiter

- [ ] Audit whether old Redis future-slot reservations can still create long `retry_after_sec` values.
- [ ] Separate local slot state from real WB 429 cooldown.
- [ ] Prove no interactive call sleeps/queues for a minute-scale window.

### P1 — WB multi-day summaries

- [ ] Define one-call multi-day Orders contract using `dateFrom` and local aggregation.
- [ ] Handle `lastChangeDate` continuation only when response boundary requires it.
- [ ] Add tests for multi-day aggregation, boundary/pagination, cancellation filtering and cache behavior.

### P1 — Full business API map

- [ ] Generate a machine-readable map from the existing endpoint YAML catalogs.
- [ ] Enrich it with freshness/deprecation/replacement metadata.
- [ ] Add business-task mappings and `preferred_tool` decisions.
- [ ] Add CI checks that block a route when a known-deprecated endpoint remains preferred past its shutdown date.

## 10. Acceptance criteria for the API-map milestone

The milestone is not complete because a document exists. It is complete when:

1. every frequent marketplace business task has a preferred current route;
2. known shutdown endpoints are machine-detectable and cannot remain preferred after their cutoff date;
3. one-call opportunities are encoded and tested;
4. rate-limit behavior is explicit and fail-fast for interactive MCP use;
5. dedicated tools have unit tests and deployed-runtime acceptance;
6. the production/recovery deployment records the exact app SHA;
7. a clean `marketplaces-mcp-only` chat can answer natural-language WB/Ozon questions through the expected specialized MCP tool without direct API bypass.
