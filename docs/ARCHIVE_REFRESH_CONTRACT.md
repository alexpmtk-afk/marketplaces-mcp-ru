# Canonical Archive Refresh Contract

Status: **required for every dataset admitted to the canonical marketplace database**.

## Core rule

An annual archive job is reusable. `COMPLETE` means only that the previous refresh cycle finished successfully. It never means the annual database is permanently final.

A user request such as “обнови базу данных” must enter the common refresh coordinator. The coordinator starts or resumes a reconciliation cycle for every requested registered dataset family.

## Required lifecycle

`REQUEST -> DISCOVER -> COMPARE COVERAGE -> FETCH/RECONCILE -> NORMALIZE -> MERGE -> VERIFY -> PUBLISH -> COMMIT COVERAGE -> COMPLETE -> POST-CHECK`

A client must not report “database updated” at REQUEST/QUEUED time. It may report completion only after the job reaches `COMPLETE` and freshness/coverage is re-checked.

## Required contract for every dataset

Every archive dataset family must register:

1. **Provider discovery rule** — how current provider truth is enumerated.
2. **Coverage/cursor model** — report IDs, bounded request coverage, page cursors, event IDs, or another provider-native completeness proof.
3. **Stable row key** — the exact deduplication/upsert key.
4. **Freshness evidence** — date/high-watermark fields plus provider/coverage evidence.
5. **Merge rule** — append, idempotent insert, or upsert/correction semantics.
6. **Completion invariants** — conditions that must be true before `COMPLETE`.

A dataset without this contract must fail closed and must not be claimed as covered by the common database-update command.

## Date rule

The maximum date in canonical rows is a freshness signal, not a universal identity key. Date comparison is mandatory where meaningful, but it is never allowed to replace a stronger provider-native identity.

Examples:

- WB weekly finance: provider identity `reportId`; row identity `(reportId, rrdId)`; logical/date coverage is checked in addition.
- WB campaign daily advertising: row identity `(date, campaign_id)`.
- WB product daily advertising: `(date, campaign_id, app_type, nm_id)`.
- Event-style datasets: provider event key/fingerprint, with event date used as freshness evidence.

## No duplicates / no silent gaps

Two independent protections are required:

- **coverage protection**: discovery/registry determines what provider units or request ranges are missing;
- **row protection**: annual merge uses the dataset stable key, so a replay cannot create duplicates and a correction can replace the same grain when the contract permits upsert.

`COMPLETE` is forbidden when discovered provider coverage is only partial, when a required canonical file is absent, or when exact publication verification failed.

## Canonical publication order

For large annual files:

1. create the immutable candidate in the **currently configured durable backend**;
2. upload to non-canonical Google Drive staging via resumable upload;
3. verify exact byte count and SHA256;
4. write/verify the configured byte-for-byte durable backup;
5. promote the verified Drive staging file to the canonical name;
6. only then commit report/dataset coverage and job progress.

Google Drive remains canonical. After the REMOTE migration, the durable backend must be proven from the actual REMOTE service environment/config before any mutating refresh or recovery. Legacy Yandex-named classes, files or local paths do not prove that Yandex Cloud is active. If the durable backend cannot be established, fail closed.

This order prevents a registry from claiming data that was not safely published.

## Current registered adapters

### WB finance

Dataset: `wb_weekly_finance_main`

- coverage: `reports_registry.csv`, COMPLETE `reportId` values;
- discovery: WB weekly realization report list;
- stable row key: `(reportId, rrdId)`;
- refresh: rediscover provider report IDs and fetch only report IDs missing from canonical registry;
- date evidence: provider `dateFrom/dateTo`, logical Monday-Sunday week, canonical row dates.

### WB advertising

Datasets: campaign roster, campaign daily, product daily, search-cluster daily, campaign snapshots, expenses, payments.

- coverage: `dataset_coverage_registry.csv` bounded request keys;
- stable keys: dataset-specific, registered in `core/archive_refresh.py` / `core/wb_advertising_archive.py`;
- refresh: reconcile the current closed provider period, then upsert canonical annual datasets by stable key;
- date evidence: closed period ending yesterday Europe/Moscow plus canonical dataset date fields where applicable.

## Extension rule

Any future WB/Ozon archive dataset must be added to the refresh contract/catalog before the generic `marketplace_database_update` tool may include it in `dataset_family=all`.