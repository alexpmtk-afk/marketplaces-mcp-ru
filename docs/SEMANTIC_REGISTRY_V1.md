# Semantic Registry — WB Weekly Report Semantics (v7)

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

Current approved calculations / historical observations:

- `penalties` → sum `penalty` exactly as reported, with breakdown by report currency and reason from `bonusTypeName` / `sellerOperName`;
- `storage_charge` → sum `paidStorage` exactly as reported by report currency;
- `acceptance_charge` → sum `paidAcceptance` exactly as reported by report currency;
- `sale_and_return_operations` → use `saleDt`, split by `docTypeName`, report sales and returns separately and calculate net as `Продажа - Возврат` for both `retailAmount` and `quantity`;
- `logistics` → use `rrDate`, keep `deliveryService` and `rebillLogisticCost` as separate monetary components, keep `deliveryAmount` and `returnAmount` as logistics counts, and preserve breakdown by operation/reason;
- `deductions_and_adjustments` → use `rrDate`, keep `deduction` and `additionalPayment` separate. `additionalPayment` is the report field for WB-remuneration adjustment and is not renamed to seller payout or netted against `deduction`;
- `commission_and_wb_reward` → use `saleDt`, explicit `Продажа`/`Возврат` buckets and only the monetary report fields `vw` and `vwNds`. The report may expose rate/intermediate fields such as `commissionPercent`, `kvw`, `kvwBase` and `ppvzSalesCommission`, but the money executor does not convert or add them;
- `acquiring_and_payment_processing` → use `saleDt`, explicit `Продажа`/`Возврат` buckets and `acquiringFee`; optional breakdown is by `paymentProcessing` and `acquiringBank`. The returned data class is `PRELIMINARY_WEEKLY_PAYMENT_ACCEPTANCE_WITHHOLDING`, not final monthly acquiring expense;
- `observed_fulfillment_method` → return historical non-empty `deliveryMethod` observations with `officeName`, count and first/last `rrDate`. Data class: `HISTORICAL_OBSERVED_FULFILLMENT`;
- `warehouse_tariff_context` → return historical non-empty fixed coefficient values from `dlvPrc` with `officeName`, `fixTariffDateFrom`, `fixTariffDateTo` and separately reported `warehouseLogisticsCoeff`. Data class: `HISTORICAL_APPLIED_WAREHOUSE_TARIFF_CONTEXT`.

Different currencies are never added into one cross-currency total. Distinct financial components are also never silently netted into one amount. Provider signs are preserved except where a separately approved formula explicitly defines operation-type subtraction, as with sales minus returns.

### Historical fulfillment boundary

`deliveryMethod` and `officeName` are observations attached to rows that actually exist in the weekly archive. They may answer what fulfillment methods/warehouses were observed during a fully covered historical period. They do not prove the seller's or product's current fulfillment configuration.

### Historical warehouse tariff / coefficient boundary

Wildberries documents that the weekly report contains the fixed warehouse coefficient that applied when a supply was planned and the fixation-period start/end dates. The report can continue to show that historical fixed coefficient even after the fixation period has expired.

Therefore:

- historical questions such as “какой коэффициент склада применялся?” may execute from `dlvPrc` with `fixTariffDateFrom`, `fixTariffDateTo`, `officeName` and `warehouseLogisticsCoeff` as historical context;
- values are returned as distinct observations with row count and first/last report dates, not averaged or summed;
- the executor does not infer a live tariff from an expired fixation period;
- questions such as “какой коэффициент сейчас?” or “какой актуальный тариф склада?” must use a suitable current Wildberries tariffs source and never fall back to the weekly archive.

### Commission / WB reward boundary

The phrase “комиссия WB” can mean either a money amount or a rate. The resolver treats these meanings separately:

- monetary reward/commission questions may execute from `vw` and `vwNds`;
- `vw` is treated as the reported WB reward without VAT and `vwNds` as VAT on that reward;
- sale and return buckets remain explicit, and the net amount is `Продажа - Возврат`;
- `commissionPercent`, `kvw` and `kvwBase` are rates/context, not money; a question asking for a percentage/rate does not use the monetary executor;
- acquiring, PVZ service amounts and intermediate commission fields are not silently added to WB reward.

### Acquiring / payment-acceptance boundary

The weekly report exposes `acquiringFee`, but this is not automatically the final actual payment-acceptance expense. The current WB contract distinguishes preliminary weekly withholding/advancing from the final monthly expense reconciliation.

Therefore:

- a normal weekly/historical acquiring question may execute from `acquiringFee` with the data class `PRELIMINARY_WEEKLY_PAYMENT_ACCEPTANCE_WITHHOLDING`;
- `Продажа` and `Возврат` are calculated separately and the net is explicit sale minus return;
- `paymentProcessing` and `acquiringBank` are available as breakdown dimensions;
- a question explicitly asking for final/actual acquiring or the `Отчёт об издержках на приём платежей` does **not** use weekly `acquiringFee`;
- that final monthly report is not currently in the canonical database, so the request fails closed instead of being approximated.

## Resolution order

1. If the user explicitly names a physical column, return that column's registered semantics first.
2. Otherwise match the question to a deterministic semantic route.
3. If the route maps to a weekly-report capability, return only the fields registered for that capability and its guardrail.
4. If the route requires a report/source absent from the database, return `REQUIRES_OTHER_SOURCE` or an explicit fail-closed source boundary.
5. Explicit current-state questions do not fall back to historical weekly-report fields.
6. Unknown/ambiguous questions fail closed.
7. Archive calculation is allowed only for a capability in `semantic_execution.yaml` and only after `FULL_COVERAGE` is proven.

Examples:

- “По какой схеме отгружался товар?” → approved `observed_fulfillment_method`; answer historical `deliveryMethod` plus warehouse context only.
- “Какая схема отгрузки сейчас?” → `REQUIRES_OTHER_SOURCE`; historical `deliveryMethod` is not used as current truth.
- “Какой коэффициент склада применялся к товару?” → approved `warehouse_tariff_context`; return historical fixed coefficient and fixation dates/context.
- “Какой актуальный коэффициент склада?” → fail closed for the weekly archive; use a live Wildberries tariffs source.
- “Сколько штрафов и за что?” → approved archive calculation using `penalty` plus `bonusTypeName` / `sellerOperName`, but only with `FULL_COVERAGE`.
- “Сколько было всех оформленных заказов?” → `REQUIRES_OTHER_SOURCE`; do **not** count `orderDt` or `orderUid` from this financial report. Complete order flow requires the WB Order Feed, which is not currently in the archive.
- “Какая дата заказа у этой продажи?” → `order_context_attribute`; `orderDt` is valid as an attribute of the reported operation, but it is not a complete orders dataset.
- “Сколько списали за хранение?” → approved archive calculation using `paidStorage` with `FULL_COVERAGE`.
- “Как рассчитано хранение по дням?” → `REQUIRES_OTHER_SOURCE`; the dedicated paid-storage report is required and is currently absent.
- “Сколько списали за приёмку?” → approved archive calculation using `paidAcceptance` with `FULL_COVERAGE`.
- “Сколько было продаж?” → approved archive calculation: sale and return buckets are computed separately and the net result is explicit `Продажа - Возврат`.
- “Сколько стоила логистика?” → approved archive calculation, but `deliveryService` and `rebillLogisticCost` are returned separately rather than merged into one guessed total.
- “Какие были удержания?” → approved archive calculation using `deduction`; `additionalPayment` is shown separately as a WB-remuneration adjustment field.
- “Какая сумма комиссии WB за период?” → approved monetary reward calculation from `vw` and `vwNds`, with explicit sale/return buckets.
- “Какой процент комиссии WB был?” → fail closed for the monetary executor; rate fields are not money and require their own approved rate analysis.
- “Сколько было эквайринга за неделю?” → approved `acquiringFee` calculation marked `PRELIMINARY_WEEKLY_PAYMENT_ACCEPTANCE_WITHHOLDING`.
- “Какие окончательные издержки на приём платежей?” → do not use weekly `acquiringFee`; the separate final monthly report is required and is absent from the canonical archive.
- “Какая комиссия WB сейчас?” → current-state request does not infer a live rate from historical finance rows.
- Any Ozon question → `REQUIRES_OTHER_SOURCE` while no Ozon report dataset exists in the canonical archive.

## Core guardrails

- `orderDt` is an attribute of a reported operation, not a complete orders dataset.
- `deliveryMethod`, `officeName`, tariff coefficients and similar fields describe historical reported operations; they do not prove current configuration.
- `dlvPrc`, `fixTariffDateFrom`, `fixTariffDateTo` and `warehouseLogisticsCoeff` may support historical coefficient context but never current tariff truth.
- Service rows must not be mixed with product sale/return rows without an explicit approved formula.
- A numeric field is not automatically summable for every business question.
- `deliveryService` and `rebillLogisticCost` are separate logistics components and are not automatically netted or merged.
- `deduction` and `additionalPayment` are separate financial components; `additionalPayment` must not be relabeled as a seller payout without explicit evidence.
- Monetary WB reward uses `vw` / `vwNds`; percentage fields are not silently converted to money.
- Weekly `acquiringFee` is not silently upgraded to final monthly payment-acceptance expense.
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
- Wildberries Seller Help: “Фиксация тарифов: тарифы на поставку и на остаток” — historical fixed warehouse coefficient and fixation dates in the weekly detail; current tariffs are viewed separately.
- Wildberries Seller Help: “Раздел «Тарифы» на портале продавца” — current warehouse logistics/storage coefficients are current-state tariff data.
- Wildberries current offer / payment-acceptance terms: preliminary weekly withholding versus final monthly `Отчёт об издержках на приём платежей`.
- Wildberries API documentation: financial reports / detailed realization report.
- Canonical archive header observed on 2026-09-14: 92 columns in `wb_weekly_finance_main`.
- Canonical archive row inspection confirms separate `deliveryService`, `rebillLogisticCost`, `deduction`, `additionalPayment`, `vw`, `vwNds`, `acquiringFee`, `paymentProcessing`, `acquiringBank`, `deliveryMethod`, `officeName`, `dlvPrc`, fixation-date and warehouse coefficient fields.

Where Wildberries changes field semantics in future API/report versions, the registry, intent routing and execution allow-list must be versioned and reviewed before those changes become executable.
