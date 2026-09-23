# Production Evidence Gate

This runbook prevents a repository-level hypothesis from being presented as a
production fix before the live provider/runtime behavior has actually been observed.

## Why this exists

CI, mocks and regression fixtures validate our code against known inputs. They do
not reveal account-specific or provider-specific facts such as:

- the exact HTTP status/body returned for one real product;
- whether a card is active, in trash, or invisible to a token;
- the token class and provider permissions actually in use;
- current rate-limit state;
- archive coverage or cache state on REMOTE.

Therefore a successful local/CI test is necessary but not sufficient evidence for
a production-specific claim.

## Required gates

### Gate A — contract evidence

Before any live probe, record the evidence for the theory:

- current code path and operation IDs;
- current endpoint/catalog contract;
- provider documentation when the question is provider behavior;
- prior production output when it is directly relevant.

State what is a fact and what is still a hypothesis.

### Gate B — minimal production probe

Use one read-only probe designed to distinguish the remaining hypotheses.

Requirements:

- no writes, no config changes, no secret output;
- minimum number of provider calls;
- respect the proven pacing/quota rule;
- no immediate duplicate call to the same quota bucket;
- print status, safe keys/types/lengths and selected non-secret invariants only;
- do not print complete marketplace payloads when shape metadata is enough;
- isolate one stage at a time.

If the assistant has no direct REMOTE terminal access, the user's first PowerShell
for that theory is a diagnostic probe, not a deploy script.

### Gate C — regression reproduction

Turn the observed production case into a deterministic regression test.

The regression must:

- reproduce the exact failure class;
- demonstrate the old behavior is wrong;
- specify the narrow expected behavior;
- keep unrelated malformed/unexpected cases fail-closed.

### Gate D — patch validation

After the regression exists:

1. apply the smallest safe correction;
2. run the targeted regression;
3. run the complete test matrix/selfchecks;
4. require security, dependency-review and supply-chain checks to pass.

Do not merge because only the targeted test is green.

### Gate E — production deploy and acceptance

Only after Gates A–D pass:

- pin the expected canonical SHA;
- refuse dirty worktrees;
- fast-forward only;
- restart the intended service;
- wait for health rather than assuming startup time;
- execute a single focused acceptance;
- validate semantics/source/completeness, not a guessed business value;
- report PASS only when the live output proves it.

## Failure interpretation

A failure proves the stage that failed, not an unobserved adjacent stage.

Examples:

- an executor saying "no data.items" does not prove malformed JSON;
- an error envelope from a second rate-limited call is not the first provider body;
- "card not found in active cards" does not prove the product is absent from the
  cabinet;
- a green unit test does not prove a real provider returns that fixture.

When evidence is insufficient, add one discriminating diagnostic probe instead of
another speculative patch.

## Script labels

Use these labels consistently in operator-facing instructions:

- **DIAGNOSTIC ONLY** — read-only evidence collection; no patch/deploy.
- **VALIDATION ONLY** — regression/CI verification; no production mutation.
- **DEPLOY + ACCEPTANCE** — allowed only after all preceding gates are satisfied.

This distinction is mandatory for future WB, Ozon, Google Drive archive and REMOTE
runtime incident work.
