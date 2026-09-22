# AGENTS.md — repo safety and architecture rules

Guardrails for humans and AI agents working in this repo. Adapted from
[letya999/ai-repo-safety-skill](https://github.com/letya999/ai-repo-safety-skill).

## Canonical architecture — read before architecture/storage/deployment work
- The machine-readable source of truth is `core/system_map.py` (`SYSTEM_MAP`, `SYSTEM_INSTRUCTIONS`).
- Human mirror: `ARCHITECTURE.md`.
- **Current production runtime is the dedicated Linux REMOTE server, not Yandex Cloud.**
  - host: `VM-684381` / `89.208.14.36`;
  - root: `/opt/mcp/`;
  - Marketplaces service: `mcp-marketplaces.service`;
  - service user: `mcp-marketplaces`;
  - internal MCP bind: `127.0.0.1:8080` (never publish this port directly).
- Approved external client path is:
  `Codex / allowed client -> https://mcp892081436.duckdns.org:13267/mcp -> Keycloak OAuth -> REMOTE GPT-MCP bridge -> internal Marketplaces MCP`.
  The bridge is local on `127.0.0.1:18181`; the internal Marketplaces bearer and server credentials are never issued to ordinary clients.
- The retired production path `marketplaces-yandex -> Yandex API Gateway -> Yandex Serverless Container` must not be restored or used as fallback.
- Server-side Marketplaces secrets belong on REMOTE under `/opt/mcp/secrets/marketplaces/` (runtime file `runtime.env`, restricted ownership/permissions). Do not treat Yandex Lockbox as the current production secret store.
- Shared rate-limit / queue coordination uses the local REMOTE Redis service at `127.0.0.1:6379`; do not publish Redis externally.
- Primary shared marketplace archive/storage is **Google Drive** under `Мой диск/Marketplaces/MCP отчеты МП/MCP архив базы данных`; annual CSV files and the applicable coverage registries are the source of truth.
- Google Drive access uses the owner's deployed **Google Apps Script** web-app bridge as the Google authorization/control plane:
  - small archive operations, reads, metadata/status and folder resolution go through the bridge;
  - large annual CSV writes use the official **Google Drive API resumable upload** path, while Apps Script creates the resumable session using the owner's effective-user OAuth context;
  - Apps Script performs final verified staging-to-canonical promotion after exact Drive size/SHA256 verification and the configured durable-backup verification.
- The Apps Script shared secret is injected server-side on REMOTE. **Do not introduce a Google OAuth refresh token into the Marketplaces runtime for this archive path.** The Google access token stays inside Apps Script; only the opaque resumable session URI is returned to the worker.
- Large annual CSV file bytes must **not** be sent through Apps Script as one base64 JSON POST. Apps Script is control plane only; the REMOTE worker uploads bounded chunks directly to the returned Drive session URI.
- **Never upload resumable large-file chunks directly into the existing canonical annual file.** Upload to a non-canonical staging filename first. The old canonical file must remain untouched until the staged file passes exact Drive size/SHA256 verification and the configured durable backup is verified.
- The strict large-file order is: immutable candidate in the **currently configured durable backend** -> non-canonical Drive resumable staging -> server-confirmed offset/resume -> exact Drive size/SHA256 verification -> durable byte-for-byte backup -> Apps Script verified promotion to canonical + trash explicit previous canonical -> COMMIT registry/job progress.
- A resumable session URI is a bearer-like capability: persist it only in durable job state, never print it or return it to users.
- Treat the Drive `Range` response as authoritative for resumed offsets; never assume all bytes sent were persisted.
- Non-final chunks must be multiples of 256 KiB. Expired/unusable sessions restart from the immutable candidate. Transient failures use bounded exponential backoff with jitter.
- Promotion must be retry-safe. If a crash occurs after the staged file was renamed but before durable state was saved, verify the canonical file by explicit ID/size/SHA256 and continue without re-downloading provider data or repeating PREPARE.
- The Apps Script bridge and the resumable uploader are transport/authentication surfaces only; neither is a parallel source of truth.
- **Archive durable backend after the REMOTE migration must be established from the actual REMOTE runtime before any mutating refresh/recovery.** Legacy file/class/path names containing `Yandex` or `yandex-object-storage` do not prove that Yandex Cloud is active.
- `core/archive_yandex.py` and Yandex-named compatibility paths are legacy-capable implementation surfaces. Do not present them as the current production backend unless a fresh read-only REMOTE audit proves the relevant environment/configuration is active.
- Canonical archive writes must succeed on Google Drive first; do not silently replace Drive as the source of truth.
- Chat-local memory/files, HOME files and WORK files are never authoritative shared state.
- Do not introduce a new runtime provider, primary storage path, or parallel architecture without an explicit architecture change.

### Database refresh guardrails
- Ordinary “обнови базу данных” requests must use the registered common refresh path (`marketplace_database_update` / `core/archive_refresh.py`) rather than inventing a dataset-specific shortcut.
- `COMPLETE` is **refresh-cycle completion only**. It never means an annual archive is permanently final. A later refresh must re-run provider discovery/coverage reconciliation.
- Never report a database refresh as successful merely because work was queued. Wait for every requested job to reach `COMPLETE`, then require `marketplace_database_verify` to pass.
- Every generic-refresh dataset must declare provider discovery, coverage/cursor model, stable row key, freshness/high-watermark evidence, merge semantics and completion invariants. If the contract is absent, fail closed and do not claim the dataset was refreshed.
- Maximum date / last row is an important freshness signal where meaningful, but it is not a universal deduplication key. Prefer provider-native identity such as `reportId`, event key/fingerprint, or bounded request coverage when it is stronger.
- Require both coverage protection and row protection: canonical coverage state determines what is missing/correction-eligible; stable-key merge prevents duplicate logical rows and permits approved corrections at the same grain.
- Post-refresh verification must check canonical-file presence, stable-key duplicates/incomplete keys, registry/coverage consistency and date/high-watermark evidence.
- Finance currently uses provider `reportId` coverage and row key `(reportId, rrdId)`; WB advertising uses dataset-specific stable keys and `dataset_coverage_registry.csv` request coverage.
- Future WB/Ozon archive datasets must be registered in the refresh contract before the generic `dataset_family=all` path may include them.
- Full contract: `docs/ARCHIVE_REFRESH_CONTRACT.md`.

- Any architecture change must update `core/system_map.py`, `ARCHITECTURE.md`, guardrail tests, and pass CI/security/deployment acceptance in the same change.
- If implementation and canonical architecture conflict, fail closed and surface the conflict instead of silently changing architecture.

## Semantic Core boundaries
- Natural-language business questions are normalized by `core/business_query_parser.py` before source selection; parsing the requested measure/grouping/period/filter must remain separate from provider-field selection.
- Prefer `user_message`, `user_reason`, and `user_note` from high-level MCP tools when speaking to users. Keep `technical_message`, `technical_reason`, source-family/status codes, executor names, fail-closed details, and forbidden-substitute diagnostics internal unless the user explicitly asks for technical/source diagnostics. Do not append boilerplate such as «данные не подменял» or «обходные способы не использовал» to ordinary answers.
- A generic current-state marker such as `сегодня`/`сейчас` may be bypassed only by a more specific registered business metric with an explicitly approved operational/live source. It must never make historical archive capabilities look current.
- `ORDERS` is an operational/preliminary business metric from WB Statistics Orders. It may answer ordinary order questions including today, but it must never be presented as the complete marketplace order flow.
- `CURRENT_STOCK` is `CURRENT_OPERATIONAL_STOCK` and uses the live WB Seller Analytics stocks source, with the official asynchronous warehouse-remains report as the Base-token fallback. It is current-snapshot only.
- Historical stock questions must fail closed until a separately approved historical stock source/contract exists. Never answer a past-date stock question with today's `CURRENT_STOCK` snapshot.
- The canonical WB weekly realization dataset has 92 physical columns; all 92 must remain semantically catalogued in `core/semantic_registry.yaml`.
- Weekly realization semantics are complete when every physical field has documented meaning, role, safe uses and explicit limitations where needed. Do not invent a calculation merely because a field exists.
- Approved weekly-finance formulas remain separate from field semantics in `core/semantic_execution.yaml`.
- Additive domain semantics such as WB Advertising are registered through the validated `core/semantic_registry_extensions.yaml`; they must still pass the same fail-closed registry validation as the base catalog.
- Historical fulfillment (`HISTORICAL_OBSERVED_FULFILLMENT`) and historical warehouse-tariff context (`HISTORICAL_APPLIED_WAREHOUSE_TARIFF_CONTEXT`) must never be presented as current configuration or current live tariff.
- `orderDt` / `orderUid` in weekly finance must never be treated as the complete marketplace order flow.
- Historical WB advertising performance uses its own canonical source, not weekly finance: `ads_campaign_daily` plus `ads_campaign_roster_snapshots`, with proof from `dataset_coverage_registry.csv`.
- `advertising_performance` is `ADVERTISING_ATTRIBUTION_OPERATIONAL`: DRR/ROAS, spend and attributed orders are advertising-attribution metrics, not total seller revenue, the complete order flow or business profitability.
- Closed historical advertising execution requires `FULL_COVERAGE` for the requested period: complete roster coverage plus complete fullstats coverage for every expected eligible campaign. Partial advertising coverage must fail closed.
- Advertising Semantic Core V1 is **cabinet-total only**. Product/`nm_id` questions require a separately approved `ads_product_daily` contract; campaign-filtered or campaign-breakdown questions require their own selector/grouping contract. Never substitute cabinet totals for a narrower question.
- Current-day/current-state advertising remains live from the WB Promotion API and must not be inferred from the closed historical archive.

## Before every commit / push
- `pre-commit run --all-files` (or at minimum the two local hooks below).
- `python scripts/security/forbid_sensitive_files.py --all`
- `python scripts/security/scan_mcp_config.py`
- Run the offline test suite: `python -m pytest tests/ -q`.

## Never commit (secrets live locally only)
- `.env`, `.env.*` (keep `.env.example` with placeholders only)
- `cabinets.json` — the local credential store (`~/.marketplace-mcp/`, chmod 600)
- `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `id_ed25519`
- `credentials*.json`, `service-account*.json`, `token.json`, `tokens.json`, `secrets.json`
- `*.ovpn`, `claude_desktop_config.json`

`.mcp.json` IS tracked on purpose — it is the secret-free plugin distribution
manifest. Its contents are verified by `scan_mcp_config.py`; never put a token in it.

## Forbidden without explicit user confirmation
- `git push` (confirm the diff first)
- making a repo/issue/PR public with private context
- printing secrets: `cat .env`, `env`, `printenv`, `cat ~/.marketplace-mcp/cabinets.json`
- adding or changing MCP servers
- weakening the safety gate, auth, or input validation just to make code work
- installing packages suggested only from model memory (see below)

## Dependency policy (trusted packages)
1. Verify the package exists and is maintained before adding it.
2. Prefer the standard library or already-present deps (`mcp`, `httpx`, `pyyaml`).
3. CI runs `pip-audit` and OSV scanning; do not introduce HIGH-severity advisories.
4. Never install hallucinated / typo-squat packages.

## If a secret is exposed
Stop. Rotate/revoke it in the marketplace cabinet FIRST, then clean Git history.
Marketplace keys are scoped and rotatable — rotation is the primary mitigation.