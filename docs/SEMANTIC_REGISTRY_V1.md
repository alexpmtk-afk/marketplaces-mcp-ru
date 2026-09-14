# Semantic Registry — WB Weekly Report Semantics (v5)

This is the machine-readable knowledge, intent-routing and gated archive-calculation layer for the data that actually exists in the canonical archive today.

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

Resolution itself does not automatically authorize SQL. It identifies the allowed capability/fields or refuses the archive route.

## Coverage-gated archive execution

`core/semantic_execution.yaml` is a separate execution allow-list. `core/semantic_archive.py` may reach the canonical archive only when all gates pass:

1. the question resolves to a weekly-report capability;
2. that capability is explicitly approved in `semantic_execution.yaml`;
3. `reports_registry.csv` proves `FULL_COVERAGE` for the entire requested period using `COMPLETE` fragments;
4. the referenced canonical annual CSV exists;
5. the query is generated from registered fields, not free-form LLM SQL.

Current approved calculations:

- `penalties` → sum `penalty` exactly as reported, with breakdown by report currency and reason from `bonusTypeName` / `sellerOperName`;
- `storage_charge` → sum `paidStorage` exactly as reported by report currency;
- `acceptance_charge` → sum `paidAcceptance` exactly as reported by report currency;
- `sale_and_return_operations` → use `saleDt`, split by `docTypeName`, report sales and returns separately and calculate net as `Продажа - Возврат` for both `retailAmount` and `quantity`;
- `logistics` → use `rrDate`, keep `deliveryService` and `rebillLogisticCost` as separate monetary components, keep `deliveryAmount` and `returnAmount` as logistics counts, and preserve breakdown by operation/reason;
- `deductions_and_adjustments` → use `rrDate`, keep `deduction` and `additionalPayment` separate. `additionalPayment` is the report field for WB-remuneration adjustment and is not renamed to seller payout or netted against `deduction`.

Different currencies are never added into one cross-currency total. Distinct financial components are also never silently netted into one amount. Provider signs are preserved except where a separately approved formula explicitly defines operation-type subtraction, as with sales minus returns.

## Resolution order

1. If the user explicitly names a physical column, return that column's registered semantics first.
2. Otherwise match the question to a deterministic semantic route.
3. If the route maps to a weekly-report capability, return only the fields registered for that capability and its guardrail.
4. If the route requires a report/source absent from the database, return `REQUIRES_OTHER_SOURCE`.
5. Explicit current-state questions do not fall back to historical weekly-report fields.
6. Unknown/ambiguous questions fail closed.
7. Archive calculation is allowed only for a capability in `semantic_execution.yaml` and only after `FULL_COVERAGE` is proven.

Examples:

- “По какой схеме отгружался товар?” → `observed_fulfillment_method`, using `deliveryMethod` and related historical fields. This answers what was observed in archived operations, not the seller's current configuration. It is currently knowledge-only, not an approved archive aggregate.
- “Какая схема отгрузки сейчас?” → `REQUIRES_OTHER_SOURCE`; historical `deliveryMethod` is not used as current truth.
- “Сколько штрафов и за что?” → approved archive calculation using `penalty` plus `bonusTypeName` / `sellerOperName`, but only with `FULL_COVERAGE`.
- “Сколько было всех оформленных заказов?” → `REQUIRES_OTHER_SOURCE`; do **not** count `orderDt` or `orderUid` from this financial report. Complete order flow requires the WB Order Feed, which is not currently in the archive.
- “Какая дата заказа у этой продажи?” → `order_context_attribute`; `orderDt` is valid as an attribute of the reported operation, but it is not a complete orders dataset.
- “Сколько списали за хранение?” → approved archive calculation using `paidStorage` with `FULL_COVERAGE`.
- “Как рассчитано хранение по дням?” → `REQUIRES_OTHER_SOURCE`; the dedicated paid-storage report is required and is currently absent.
- “Сколько списали за приёмку?” → approved archive calculation using `paidAcceptance` with `FULL_COVERAGE`.
- “Сколько было продаж?” → approved archive calculation: sale and return buckets are computed separately and the net result is explicit `Продажа - Возврат`.
- “Сколько стоила логистика?” → approved archive calculation, but `deliveryService` and `rebillLogisticCost` are returned separately rather than merged into one guessed total.
- “Какие были удержания?” → approved archive calculation using `deduction`; `additionalPayment` is shown separately as a WB-remuneration adjustment field.
- “Какая комиссия WB сейчас?” → `REQUIRES_OTHER_SOURCE`; a current rate must not be inferred from a historical finance row.
- Any Ozon question → `REQUIRES_OTHER_SOURCE` while no Ozon report dataset exists in the canonical archive.

## Core guardrails

- `orderDt` is an attribute of a reported operation, not a complete orders dataset.
- `deliveryMethod`, `officeName`, tariff coefficients and similar fields describe historical reported operations; they do not prove current configuration.
- Service rows must not be mixed with product sale/return rows without an explicit approved formula.
- A numeric field is not automatically summable for every business question.
- `deliveryService` and `rebillLogisticCost` are separate logistics components and are not automatically netted or merged.
- `deduction` and `additionalPayment` are separate financial components; `additionalPayment` must not be relabeled as a seller payout without explicit evidence.
- Reference-only sources cannot be selected for archive execution.
- Explicit current-state questions cannot silently use historical archive values.
- Unknown or ambiguous questions must not trigger free-form archive SQL.
- Archive execution requires `FULL_COVERAGE` in `reports_registry.csv` and canonical annual-file presence.
- Currencies are not mixed.

## Runtime status

The gated archive executor is wired into `marketplace_business_query` for the explicitly approved capabilities above. The user's original natural-language question is preserved and takes precedence over a conflicting legacy `metric` hint. Unsupported, ambiguous, uncovered or current-state requests fail closed or return the required source rather than falling back to a guessed field or source.

## Evidence

Primary semantic references:

- Wildberries Seller Help: “Детализация еженедельного отчёта реализации”.
- Wildberries Seller Help: “Еженедельные отчёты реализации”.
- Wildberries API documentation: financial reports / detailed realization report.
- Canonical archive header observed on 2026-09-14: 92 columns in `wb_weekly_finance_main`.
- Canonical archive row inspection confirms separate `deliveryService`, `rebillLogisticCost`, `deduction` and `additionalPayment` columns and operation-level values.

Where Wildberries changes field semantics in future API/report versions, the registry, intent routing and execution allow-list must be versioned and reviewed before those changes become executable.
