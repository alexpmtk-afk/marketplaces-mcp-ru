# Semantic Registry — WB Weekly Report Semantics (v2)

This registry is the machine-readable knowledge layer for the data that actually exists in the canonical archive today.

## Current database truth

At this stage the only business report treated as present in the canonical archive is:

- `wb_weekly_finance_main` — Wildberries weekly realization detail, report type 1.

Other Wildberries and Ozon reports are registered only as `REFERENCE_ONLY / NOT_IN_DATABASE`. Their existence must never be interpreted as data availability.

## What changed from the first draft

The registry is no longer centered on four pre-selected order/sales metrics. It is centered on the weekly report itself.

For every one of the 92 physical archive columns, `field_catalog` now records:

- business meaning;
- semantic role (`identifier`, `date`, `dimension`, `measure`, `operation`, `flag`, `legacy`);
- safe uses;
- explicit limitations where a field can be misread.

The physical 92-column schema and the semantic catalog must match exactly.

## How a business question should be resolved

1. Recognize the requested concept.
2. Check whether a registered capability can answer it from `wb_weekly_finance_main`.
3. Use only the fields declared for that capability and respect its guardrails.
4. Check full archive coverage for the requested period.
5. If the weekly report cannot answer the concept, return `REQUIRES_OTHER_SOURCE` rather than guessing from a similar field.

Examples:

- “По какой схеме отгружался товар?” → use `deliveryMethod` and related historical fields. This answers the scheme observed in archived operations, not the seller's current configuration.
- “Сколько штрафов и за что?” → use `penalty` for the amount and `bonusTypeName` / `sellerOperName` for the reason.
- “Сколько было всех оформленных заказов?” → do **not** count `orderDt` or `orderUid` from this financial report. The complete order flow requires another source such as the WB Order Feed, which is not currently in the archive.
- “Сколько списали за хранение?” → `paidStorage` can answer the charge present in the weekly report.
- “Почему и как рассчитано хранение по дням?” → requires the dedicated paid-storage report, currently absent from the archive.

## Core guardrails

- `orderDt` is an attribute of a reported operation, not a complete orders dataset.
- `deliveryMethod`, `officeName`, tariff coefficients and similar fields describe historical reported operations; they do not prove current configuration.
- Service rows (logistics, storage, penalties, deductions, acceptance, etc.) must not be mixed with product sale/return rows without an explicit operation filter.
- A numeric field is not automatically summable for every business question.
- Reference-only sources cannot be selected for archive execution.
- Archive execution requires `FULL_COVERAGE` in `reports_registry.csv`.

## Current semantic capabilities from the weekly report

The registry explicitly covers report metadata, product identity, sale/return operations, order context, observed fulfillment method, warehouse/tariff context, logistics, penalties, storage charges, paid acceptance, deductions/adjustments, WB commission and reward, acquiring/payment processing, pickup-point context, promotions/loyalty/cashback, B2B attributes, traceability/marking, and geography attached to reported operations.

This still does **not** wire the registry into `marketplace_business_query`; runtime routing remains unchanged until a later approved step.

## Evidence

Primary semantic references:

- Wildberries Seller Help: “Детализация еженедельного отчёта реализации”.
- Wildberries Seller Help: “Еженедельные отчёты реализации”.
- Wildberries API documentation: financial reports / detailed realization report.
- Canonical archive header observed on 2026-09-14: 92 columns in `wb_weekly_finance_main`.

Where Wildberries changes field semantics in future API/report versions, the registry must be versioned and reviewed before those changes become executable.
