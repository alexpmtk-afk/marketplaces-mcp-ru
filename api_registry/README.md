# Marketplace API Registry

Status: WORKING CANONICAL REGISTRY
Baseline date: 2026-09-01
Branch: `api-map/2026-09-01`

## Purpose

This directory is the permanent, reviewable registry of marketplace API operations used to design and audit Marketplace MCP.

It is intentionally separate from the runtime catalogs. Runtime files remain under `wb_mcp/` and `ozon_mcp/`; this directory is the control copy used for completeness, freshness, migration and routing audits.

## Current materialized registry

| Marketplace | Registry file | Baseline operations | Baseline source |
|---|---|---:|---|
| Wildberries | `catalog/wildberries.yaml` | 307 | schema-driven import from WB OpenAPI/Swagger plus curated records |
| Ozon Seller | `catalog/ozon_seller.yaml` | 441 | schema-driven import from Ozon Seller OpenAPI plus curated records |
| Ozon Performance | `catalog/ozon_performance.yaml` | 45 | Ozon Performance catalog |
| **Total** |  | **793** |  |

These three files are exact blob-level copies of the runtime catalogs from application baseline `07a3408eab9a2abe4ba1e9dbcfdd8c67bed0458d`. They therefore form a reproducible baseline, not a hand-copied table.

## Important distinction: complete baseline vs current API

`793` means "all operations present in the project's imported catalogs at the baseline commit". It does **not** mean every operation is still current on 2026-09-01.

Marketplace APIs change independently of this repository. `status_overrides.yaml` records verified migrations/deprecations discovered after or outside the imported snapshot. `docs/API_MAP.md` is the higher-level business/routing map.

## Source provenance

The predecessor chat planned this structure but did not persist the raw Swagger/OpenAPI files. No raw `swagger.yaml`, `swagger.json` or equivalent source snapshot was found in the archived project files on Google Drive.

The repository does contain the import machinery:

- `scripts/sync_swagger.py` — additive single-spec import;
- `scripts/ingest_specs.py` — host-aware bulk WB OpenAPI ingest;
- `scripts/ingest_ozon.py` — Ozon Seller swagger ingest with safety/deprecation handling.

Raw official snapshots should subsequently be stored under:

```text
api_registry/sources/
  wildberries/
  ozon/
```

Do not put credentials, API keys, Client-Id values or seller data here.

## Canonical fields for the next normalized export

For every operation the normalized registry should expose at least:

- marketplace / service;
- section/category;
- operation_id;
- HTTP method;
- host;
- path;
- purpose/summary;
- request parameters/body hints;
- date parameters and maximum period;
- pagination method;
- response item path / response-size constraints;
- marketplace rate limit and burst;
- source freshness / retention when documented;
- safety class (`read`, `write`, `destructive`);
- status (`CURRENT`, `DEPRECATED`, `SHUTDOWN`, `VERIFY_OFFICIAL`);
- replacement path when applicable;
- preferred MCP typed/workflow tool;
- allowed generic fallback;
- evidence / last verification date.

## Routing rule

Frequent human business questions should follow:

`SPECIALIZED TOOL > WORKFLOW TOOL > GENERIC MCP TOOL > FAIL CLOSED`

The existence of a generic catalog endpoint is not a reason to prefer it over a verified specialized tool.

## Maintenance

1. Acquire the latest official OpenAPI/Swagger or official API documentation/change notice.
2. Preserve the raw source snapshot under `sources/` when technically obtainable.
3. Compare `(HTTP method, host, path)` against the materialized catalog.
4. Add new operations; never silently erase deprecated operations from historical control data.
5. Mark status/replacement in `status_overrides.yaml`.
6. Update request limits, pagination and date-range constraints.
7. Run safety/pagination/registry tests before touching runtime catalogs.
8. Only after review, synchronize approved changes back into runtime `endpoints.yaml` files and deploy.
