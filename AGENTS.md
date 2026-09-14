# AGENTS.md — repo safety and architecture rules

Guardrails for humans and AI agents working in this repo. Adapted from
[letya999/ai-repo-safety-skill](https://github.com/letya999/ai-repo-safety-skill).

## Canonical architecture — read before architecture/storage/deployment work
- The machine-readable source of truth is `core/system_map.py` (`SYSTEM_MAP`, `SYSTEM_INSTRUCTIONS`).
- Human mirror: `ARCHITECTURE.md`.
- Runtime infrastructure is Yandex Cloud. Google Cloud is not part of the runtime architecture.
- Primary shared marketplace archive/storage is **Google Drive** under `MCP архив базы данных`: annual CSV files and the report registry are the source of truth.
- Google Drive access uses the owner's deployed Google Apps Script web-app bridge. Its shared bridge secret lives in Yandex Lockbox; the MCP must not depend on a Google Cloud OAuth refresh token.
- The Apps Script bridge is only the transport/authentication surface into the fixed archive root; it must not become a parallel source of truth.
- **Yandex Object Storage** remains required for durable archive queue/job state, per-report staging, and a secondary byte-for-byte backup of canonical Drive files.
- Yandex Object Storage runtime auth uses the Serverless Container service account and a temporary IAM token from metadata; do not introduce static archive keys unless the canonical architecture explicitly changes.
- Canonical archive writes must succeed on Google Drive first; do not silently fall back to Yandex as the source of truth.
- Existing canonical files left in Yandex by the prior architecture may be migrated to Drive on read without re-downloading marketplace data.
- Chat-local memory/files are never authoritative shared state.
- Do not introduce a new cloud provider, primary storage path, or parallel architecture without an explicit architecture change.
- Any architecture change must update `core/system_map.py`, `ARCHITECTURE.md`, guardrail tests, and pass CI/security/deployment acceptance in the same change.
- If implementation and canonical architecture conflict, fail closed and surface the conflict instead of silently changing architecture.
- Semantic Core historical fulfillment (`deliveryMethod` / `officeName`) and historical warehouse tariff context (`dlvPrc`, `fixTariffDateFrom`, `fixTariffDateTo`, `warehouseLogisticsCoeff`) are archive observations only. Never promote them to current configuration or live tariff truth; current-state questions require a suitable live source or must fail closed.

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
