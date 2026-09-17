# Metric Registry V1

`core/metric_registry.yaml` is the canonical semantic dictionary for business metrics understood by Marketplaces MCP.

Its job is to connect user vocabulary to a canonical Russian business meaning and then to the already-approved semantic target. It does **not** grant execution or arithmetic permission.

Each entry may define:

- `canonical_name_ru` — primary human-facing Russian name;
- `abbreviation` — accepted industry abbreviation, for example `CTR`, `CPC`, `CPO`, `ROAS`, or Russian `ДРР`;
- `aliases_ru` / `aliases_en` — natural-language and technical aliases accepted from users;
- `semantic_target` — existing `BUSINESS_METRIC` or `CAPABILITY` that owns routing/execution semantics;
- `provider_mappings` — known provider/source fields and aliases; unknown Ozon mappings are explicitly `NOT_MAPPED` and must not be inferred;
- `formula_ref` — for derived metrics, a reference to the authoritative domain calculation contract rather than a second executable formula;
- `guardrail` — meaning restrictions that must survive into resolution/provenance.

Example: `AD_DRR` is canonically **«Доля рекламных расходов»**, abbreviation **«ДРР»**, with accepted alias `DRR`. For the current WB advertising contract it maps to canonical fields `spend` and `attributed_order_amount` and references `wb_ads_m0.v1/drr_order_pct`. It must not be treated as overall business profitability.

The Semantic Core composes this registry under `metric_dictionary`. `semantic_resolver.py` uses it as a terminology fallback, so wording such as `DRR`, `ДРР`, `доля рекламных расходов`, `CTR`, and `кликабельность рекламы` converge on the same canonical semantic targets without bypassing existing fail-closed execution, coverage, join, or calculation controls.
