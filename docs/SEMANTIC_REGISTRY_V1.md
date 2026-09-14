# Marketplace Semantic Core — WB weekly realization archive

Status: COMPLETE FOR 92-COLUMN SEMANTIC CATALOG; PARTIALLY EXECUTABLE RUNTIME
Date: 2026-09-14

## What is complete

The canonical `wb_weekly_finance_main` archive contains 92 physical columns. All 92 columns are present in the semantic registry and each has a documented meaning, role and safe use. The registry validator fails closed if any physical field is missing from the semantic catalog.

This completes the original task of understanding the full weekly realization report: the Semantic Core can determine whether a concept is present in this report, what field(s) represent it, and what limitations apply.

## Runtime calculations already approved

The runtime additionally has coverage-gated executors for selected operations where the server-side formula is safe and explicit: penalties, paid storage, paid acceptance, sales/returns, logistics, deductions/adjustments, monetary WB reward, weekly payment processing/acquiring, historical fulfillment observations, and historical warehouse-tariff context.

These runtime executors are an extension of the 92-column semantic catalog, not a prerequisite for considering the report semantics complete. Attributes, identifiers, flags, percentages and legacy fields do not automatically require an aggregation formula; they remain semantically described and must not be summed or transformed without an approved rule.

## Hard boundaries

The weekly realization archive is authoritative only for operations and attributes actually present in its rows. It is not the source of truth for:

- the complete marketplace order funnel;
- current stock state;
- current product fulfillment configuration;
- current live warehouse tariffs;
- detailed storage drivers beyond `paidStorage`;
- detailed acceptance operations beyond `paidAcceptance`;
- advertising performance;
- Ozon report data.

For those concepts the resolver must require another approved source rather than infer from weekly-report columns.

## Execution safety

Archive execution requires `FULL_COVERAGE` in `reports_registry.csv` and the canonical annual Google Drive files. Monetary calculations do not mix currencies. Historical attributes are never presented as current configuration. `orderDt` / `orderUid` in weekly finance never become a substitute for the complete order flow.

Historical fulfillment is classified as `HISTORICAL_OBSERVED_FULFILLMENT`. Historical warehouse-tariff context is classified as `HISTORICAL_APPLIED_WAREHOUSE_TARIFF_CONTEXT`.

## Source basis

Semantics are based on the observed 92-column canonical archive header plus current Wildberries Seller Help / Finance API documentation for the weekly realization report and its detailed rows.
