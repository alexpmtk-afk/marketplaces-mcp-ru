# Semantic Registry — WB Weekly Report Semantics (v3)

This is the machine-readable knowledge and intent-routing layer for the data that actually exists in the canonical archive today.

## Current database truth

At this stage the only business report treated as present in the canonical archive is:

- `wb_weekly_finance_main` — Wildberries weekly realization detail, report type 1.

Other Wildberries and Ozon reports are registered only as `REFERENCE_ONLY / NOT_IN_DATABASE`. Their existence must never be interpreted as data availability.

## Weekly report field passport

The registry is centered on the weekly report itself rather than on a small pre-selected metric list.

For every one of the 92 physical archive columns, `field_catalog` records:

- business meaning;
- semantic role (`identifier`, `date`, `dimension`, `measure`, `operation`, `flag`, `legacy`);
- safe uses;
- explicit limitations where a field can be misread.

The physical 92-column schema and the semantic catalog must match exactly.

## Question resolver

`core/semantic_intents.yaml` contains deterministic intent routes. `core/semantic_resolver.py` resolves a natural-language business question into one of these outcomes:

- `AVAILABLE` — the weekly archive contains the required semantic capability;
- `AVAILABLE_WITH_LIMITATION` — the archive can answer only within an explicit historical/reporting limitation;
- `REQUIRES_OTHER_SOURCE` — another report/API is required and the weekly archive must not be queried as a substitute;
- `AMBIGUOUS` — several incompatible semantic routes match;
- `UNKNOWN` — no confirmed semantic route exists.

Resolution itself does not execute SQL. It identifies the allowed capability/fields or refuses the archive route. A later query planner must still verify period coverage before execution.

## Resolution order

1. If the user explicitly names a physical column, return that column's registered semantics first.
2. Otherwise match the question to a deterministic semantic route.
3. If the route maps to a weekly-report capability, return only the fields registered for that capability and its guardrail.
4. If the route requires a report/source absent from the database, return `REQUIRES_OTHER_SOURCE`.
5. Explicit current-state questions do not fall back to historical weekly-report fields.
6. Unknown/ambiguous questions fail closed.
7. Before any future archive execution, require `FULL_COVERAGE` for the requested period.

Examples:

- “По какой схеме отгружался товар?” → `observed_fulfillment_method`, using `deliveryMethod` and related historical fields. This answers what was observed in archived operations, not the seller's current configuration.
- “Какая схема отгрузки сейчас?” → `REQUIRES_OTHER_SOURCE`; historical `deliveryMethod` is not used as current truth.
- “Сколько штрафов и за что?” → `penalties`, using `penalty` plus `bonusTypeName` / `sellerOperName`.
- “Сколько было всех оформленных заказов?” → `REQUIRES_OTHER_SOURCE`; do **not** count `orderDt` or `orderUid` from this financial report. Complete order flow requires the WB Order Feed, which is not currently in the archive.
- “Какая дата заказа у этой продажи?” → `order_context_attribute`; `orderDt` is valid as an attribute of the reported operation.
- “Сколько списали за хранение?” → `storage_charge`; `paidStorage` can answer the charge present in the weekly report.
- “Как рассчитано хранение по дням?” → `REQUIRES_OTHER_SOURCE`; the dedicated paid-storage report is required and is currently absent.
- “Какая комиссия WB сейчас?” → `REQUIRES_OTHER_SOURCE`; a current rate must not be inferred from a historical finance row.
- “Что означает поле deliveryMethod?” → direct field semantics from the 92-column field passport.
- Any Ozon question → `REQUIRES_OTHER_SOURCE` while no Ozon report dataset exists in the canonical archive.

## Core guardrails

- `orderDt` is an attribute of a reported operation, not a complete orders dataset.
- `deliveryMethod`, `officeName`, tariff coefficients and similar fields describe historical reported operations; they do not prove current configuration.
- Service rows (logistics, storage, penalties, deductions, acceptance, etc.) must not be mixed with product sale/return rows without an explicit operation filter.
- A numeric field is not automatically summable for every business question.
- Reference-only sources cannot be selected for archive execution.
- Explicit current-state questions cannot silently use historical archive values.
- Unknown or ambiguous questions must not trigger free-form archive SQL.
- Archive execution requires `FULL_COVERAGE` in `reports_registry.csv`.

## Current semantic capabilities from the weekly report

The registry explicitly covers report metadata, product identity, sale/return operations, order context, observed fulfillment method, warehouse/tariff context, logistics, penalties, storage charges, paid acceptance, deductions/adjustments, WB commission and reward, acquiring/payment processing, pickup-point context, promotions/loyalty/cashback, B2B attributes, traceability/marking, and geography attached to reported operations.

This still does **not** wire the resolver into `marketplace_business_query`; runtime execution remains unchanged until a later controlled integration step.

## Evidence

Primary semantic references:

- Wildberries Seller Help: “Детализация еженедельного отчёта реализации”.
- Wildberries Seller Help: “Еженедельные отчёты реализации”.
- Wildberries API documentation: financial reports / detailed realization report.
- Canonical archive header observed on 2026-09-14: 92 columns in `wb_weekly_finance_main`.

Where Wildberries changes field semantics in future API/report versions, the registry and intent routing must be versioned and reviewed before those changes become executable.
