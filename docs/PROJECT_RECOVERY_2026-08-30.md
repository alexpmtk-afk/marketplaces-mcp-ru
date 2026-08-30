# PROJECT RECOVERY — 2026-08-30

## Purpose

This checkpoint records the verified handoff from the Codex chats `Первые шаги mcp 28.08.26` and `Провести тестирование` after the Codex session stopped before the final remote deployment.

## Verified GitHub baseline

- Repository: `alexpmtk-afk/marketplaces-mcp-ru`
- Remote/Yandex baseline on `main`: `4ec3bfaee8cfa56cb6180a5a9f2201d2adbd9020`
- That baseline contains the Streamable HTTP `/mcp` entry point, `/healthz`, Docker image support and Yandex deployment preparation.
- Recovery work is isolated on branch `recovery/provesti-testirovanie-20260830`.

## Recovered Codex checkpoint

A synchronized local Git object proves a later local checkpoint:

- local checkpoint: `7a1ddccb6a6c37ed613022ba19fa19af966849c4`
- parent: `4799d944e6f30cdb13ee752f2c7bba18b45ecfe2`
- message: `checkpoint: preserve current Yandex MCP work before Codex permissions test`

Important: this checkpoint is based on the pre-remote parent, while GitHub `main` contains the later remote/Yandex commit. Therefore the local checkpoint must NOT be force-pushed or blindly used to replace `main`. Its hardening changes must be transplanted onto the current remote baseline.

## Codex work that was prepared locally

The archived `Провести тестирование` conversation records the following work as locally implemented/tested before the external deployment stopped:

- shared request-rate controller for WB, Ozon Seller and Ozon Performance;
- per-cabinet state;
- SQLite coordination for a single host;
- Redis coordination for multiple processes/replicas;
- shared cooldown after HTTP 429;
- production fail-closed mode when Redis is required but unavailable;
- non-secret rate-limit diagnostics;
- remote MCP Bearer authentication;
- multi-cabinet credentials supplied through environment/Lockbox JSON;
- intended remote-only marketplace access invariant;
- support for multiple seller cabinets.

The archived conversation reported offline tests passing during this work, but the final cloud deployment was NOT completed and live marketplace calls were later found to have bypassed the cloud MCP. Those historical local PASS results are therefore not accepted as proof of the final remote system.

## Recovery changes already applied

On `recovery/provesti-testirovanie-20260830`:

- preserved `mcp>=1.2.0,<2` compatibility from the remote baseline;
- added `redis>=5,<7`;
- restored the shared rate-limit environment contract without secrets;
- restored `core/rate_limit.py` from the synchronized Codex working copy.

## Remaining work before remote TEST can be accepted

1. Integrate the recovered controller into every marketplace HTTP path and retry path.
2. Restore/verify rate-limit tests and backend health check.
3. Reconcile the current Streamable HTTP entry point with required Bearer authentication.
4. Restore Lockbox-backed multi-cabinet credential loading without committing any secret values.
5. Make production remote mode fail closed unless shared Redis is configured and reachable.
6. Run repository CI/security tests on the recovery branch.
7. Review diff against `main`; only then merge/push the hardened result.
8. Separately, with explicit approval for paid/external changes, provision/configure Redis and Lockbox in Yandex Cloud, deploy a new container image/revision, and run remote-only WB/Ozon smoke tests.

## Acceptance rule

Do not mark the project complete because a local test or Codex report says PASS. Final TEST acceptance requires evidence of the full route:

`client -> remote MCP -> authenticated container -> shared Redis rate controller -> marketplace API`

with Yandex logs/runtime evidence and no direct marketplace bypass.
