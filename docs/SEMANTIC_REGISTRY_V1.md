# Semantic Registry V1

This registry is the first machine-readable layer of Marketplace Semantic Core.

It does not replace `core/registry.py` (provider API endpoint catalog) or `core/business_registry.py` (cabinet/business identity registry).

Current scope:
- register the four approved WB operational metrics from the Business Metrics Contract;
- register approved live API sources for those metrics;
- register the observed `wb_weekly_finance_main` archive dataset, its 92-column schema, deduplication keys and coverage metadata;
- keep all finance-to-business-metric bindings non-executable until their business semantics are proven and explicitly approved.

Safety rule: a recognized source or dataset is not automatically suitable for a business metric. Archive execution requires an APPROVED binding, proven business semantics, and full coverage for the requested period.

This change does not alter `marketplace_business_query` routing yet. The next implementation step is to make the existing business router consume this registry instead of duplicating metric/source rules in code.
