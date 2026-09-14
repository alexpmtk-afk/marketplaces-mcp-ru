# Marketplaces MCP — canonical architecture

Canonical version: 2026-09-14.v13

Machine-readable source of truth: `core/system_map.py`.

## Runtime and storage

Runtime infrastructure is Yandex Cloud. Google Cloud is not part of the runtime architecture.

Canonical marketplace archive: **Google Drive** under `MCP архив базы данных`. Annual CSV files plus `reports_registry.csv` are the shared source of truth. Google Drive access from Yandex uses the owner's deployed Google Apps Script web-app bridge; the bridge secret is stored in Yandex Lockbox. Yandex Object Storage remains required for durable queue/job state, per-report staging and byte-for-byte backup of canonical Drive files.

## WB Advertising M0

Current advertising scope is Wildberries read-only M0. Credentials use service `wb_ads`. Advertising operational metrics are separate from actual business profit and do not modify campaigns in this phase.

## Semantic Core

The Semantic Core is partially wired into runtime through `marketplace_business_query` and preserves the original natural-language question as the primary intent signal. Legacy `metric` remains compatibility-only and must not override the user's wording.

Canonical components:

- `core/semantic_registry.yaml` — full semantic catalog for the physical WB weekly realization archive;
- `core/semantic_intents.yaml` — deterministic natural-language routing;
- `core/semantic_resolver.py` — fail-closed resolver;
- `core/semantic_execution.yaml` — approved executable contracts;
- `core/semantic_archive.py` — coverage-gated archive execution.

### 92-column weekly-report completion status

The canonical `wb_weekly_finance_main` header has **92 physical columns and all 92 have semantic definitions**. Each field has a documented meaning, role and safe use; the semantic registry fails closed if the physical field set and semantic field catalog diverge. Therefore the original semantic task for the weekly realization report is COMPLETE.

Approved runtime calculations are an additional layer, not a requirement for semantic completeness. Identifiers, flags, historical attributes, percentages and legacy fields must not be turned into totals merely because they exist in the report.

### Approved archive execution

Archive execution requires exact `FULL_COVERAGE` from COMPLETE fragments in `reports_registry.csv` plus presence of the canonical annual file. Current approved archive capabilities are:

- `penalties`;
- `storage_charge`;
- `acceptance_charge`;
- `sale_and_return_operations`;
- `logistics`;
- `deductions_and_adjustments`;
- `commission_and_wb_reward`;
- `acquiring_and_payment_processing`;
- `observed_fulfillment_method`;
- `warehouse_tariff_context`.

Sales/returns use `saleDt` and explicit `docTypeName` buckets, with Продажа minus Возврат for the approved `retailAmount` / `quantity` calculation. Logistics keeps `deliveryService` and `rebillLogisticCost` separate. Deductions keep `deduction` and `additionalPayment` separate and are never silently netted. Monetary WB reward uses `vw` and `vwNds`; it is not derived from `commissionPercent/kvw/kvwBase`. Weekly `acquiringFee` is `PRELIMINARY_WEEKLY_PAYMENT_ACCEPTANCE_WITHHOLDING`, not the final monthly acquiring expense.

Historical fulfillment observations use `deliveryMethod`, `officeName` and `rrDate`; their data class is `HISTORICAL_OBSERVED_FULFILLMENT` and they never confirm current fulfillment configuration. Historical warehouse tariff context uses `dlvPrc`, `fixTariffDateFrom`, `fixTariffDateTo`, `warehouseLogisticsCoeff` and `officeName`; its data class is `HISTORICAL_APPLIED_WAREHOUSE_TARIFF_CONTEXT` and it never confirms the current live warehouse tariff.

## Hard source boundaries

The weekly realization archive is not authoritative for the complete marketplace order funnel, current stock, current fulfillment configuration, current live tariffs, detailed storage drivers, detailed acceptance operations, advertising performance, or Ozon data. Those concepts require another approved source and must fail closed instead of being inferred from weekly rows.

WB Statistics Orders remains operational/preliminary (`PRELIMINARY_NOT_ALL_ORDERS`) and must not be substituted for the complete order flow.
