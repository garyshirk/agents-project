# Resumption guide

**PROJECT STATUS: PAUSED BY USER.** Resume only on a new user instruction.
Read [Project overview](../README.md) and [Current status / proposed 7C](CURRENT_STATUS.md).
This guide authorizes no live research or commercial action.

## 1. Establish state before changing anything

Repository: `/Users/garyshirk/agents-project`.
Remote: `https://github.com/garyshirk/agents-project.git`.
Active development branch: `v2-human-review`.
Implementation checkpoint: `05360e9c43da4023eaf5920398e781f3921d7b6b`.
The subsequent pause-documentation commit is the handoff checkpoint; use Git
history to obtain its full hash rather than embedding a self-referential hash.

```sh
cd /Users/garyshirk/agents-project
git status --short
git branch --show-current
git log -3 --oneline
git remote -v
git rev-parse HEAD origin/v2-human-review main v1c-from-text-baseline
```

Do not reset, switch branches, reconcile unexpected changes, migrate data or
resume a historical Evaluation automatically. `main` is the preserved stable
baseline; `v1c-from-text-baseline` is preserved V1C (exact pointers in overview).
Historical reverse-debug branches/experiments are not the current V2 baseline.
If pointers or hashes differ, inspect and ask before destructive action.

## 2. Environment and offline validation

Verified Python: 3.13.5. Existing environment: `.venv/bin/python`.
Requirements pin `openai-agents==0.22.2` and `python-dotenv==1.2.3`.
At inspection, installed transitive packages were openai 3.13.0 and Pydantic
2.13.5; requirements are minimal, not a full transitive lockfile. A fresh install
may resolve different transitive versions and must be revalidated. Do not upgrade
an existing environment merely to resume.

For a missing environment only, after deciding installation is appropriate:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

These commands can access the network; they were not run for this handoff.
Offline checks from the project root:

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest tests.test_v2_resale_output_boundary tests.test_v2_live_resale tests.test_v2_resale_integration tests.test_v2_resale_diagnostic -v
.venv/bin/python -m compileall -q agent.py arbitrage diagnostics tests
git diff --check
```

Pause baseline: 1,033 passing tests; focused Resale selection: 120 tests.
Tests use temporary databases and fake providers. Do not run `agent.py` as a test:
it is still the V1C terminal entry point, not a complete V2 runtime.

## 3. Data and secrets

Do not alter root `arbitrage.db` / `sessions.db`, `dev_data/v2` databases or
historical smoke stores. Expected protected SHA-256 values:

| File relative to repository | SHA-256 |
| --- | --- |
| arbitrage.db | 6c267c89847b9ef4de9f5f93a57de8a2baae47a69836b1aa716298022ce3d2b8 |
| sessions.db | 4d078431e222279dddd9bdca343add5c827036424b724bd95581b1dbf8359603 |
| dev_data/v2/arbitrage.db | 76f4cf7193ed40b25ccec0ee4e12c5d82b0169c059ba7c95cea07665a7e12e7c |
| dev_data/v2/sessions.db | ac7a65aff3c7183760594c4617ab69739b59b8b13585d0e283fa21eeedbaa865 |

Use `shasum -a 256` to verify before/after. Historical Resale databases/hashes
are in [Current status](CURRENT_STATUS.md). Preserve raw audits unchanged; never
repair/replay their responses or remove a claim marker to make a run reusable.
`/tmp` paths are not guaranteed to survive cleanup/reboot. If evidence is missing,
report that limitation; do not silently regenerate it. Durable archival needs a
separately reviewed backup action, not an automatic new live test.

V2Repository requires explicit absolute business/session paths, rejects protected
root aliases/sidecar collisions, and never opens the session database itself.
Use fresh OS temporary directories for tests and initialize only their test
fixtures; do not use persistent development databases as scratch storage.
No V1C/V2 history migration or import is automatic.

`ARBITRAGE_DATABASE_PATH` and `ARBITRAGE_SESSIONS_DATABASE_PATH` are paired
legacy application overrides. Set both or neither. `ARBITRAGE_SESSION_ID`
selects a conversation, not a database; legacy default is `default_conversation`.
The development launcher defaults that ID to `v2_development`. See
[launcher caveats](../V2_DEVELOPMENT.md) before using it.

`.env` contains OPENAI_API_KEY for authorized live use. Do not print, commit,
copy into fixtures or include credentials in reports. Existing loading uses
python-dotenv; live diagnostic constructs its client only after authorization.
Offline tests/check-only need no key. Keep databases, sidecars, caches and logs
out of Git. `.gitignore` is not a substitute for staged-diff inspection.

## 4. Safe diagnostic preflight only

The Resale diagnostic defaults to CHECK-ONLY. This creates only a new empty
private directory; it does not initialize persistent databases or call providers:

```sh
FRESH_DIR="$(mktemp -d /tmp/v2-resale-live.XXXXXX)"
.venv/bin/python diagnostics/v2_resale_smoke.py --business-db "$FRESH_DIR/arbitrage.db" --sessions-db "$FRESH_DIR/sessions.db" --model gpt-5.4-mini
```

Require `CHECK_ONLY_NO_CALL_NO_DB_WRITES`, exit 0 and an empty directory. Do not
reuse historical or earlier check-only directories for a future authorized test.
No live command is recommended as the first resumption action.

Live mode requires BOTH `--run` and `--authorize-live` plus separate human
approval. Do not supply them during resumption checks. Fixed Resale policy:
one pass, one provider invocation, gpt-5.4-mini, at most three hosted web tool
calls, 6,000 output tokens, 45-second timeout, zero retries. No alternate model,
second extraction, follow-up research, live Manager call or commercial tools.
A timeout is ambiguous, not proof of cancellation. Preserve the entire claimed
directory and inspect/reconcile explicitly; never retry automatically.

## 5. Read implementation before designing 7C

- [Current contracts](../arbitrage/v2/contracts.py): PriceEvidence,
  ComparableSource, ResaleResearchResult, economic/review/status models.
- [Live prompt and validation](../arbitrage/v2/live_resale.py): current prompt v2,
  schema v1, domain and attribution boundaries.
- [Publication](../arbitrage/v2/resale_delegation.py),
  [audit](../arbitrage/v2/resale_audit.py),
  [workflow](../arbitrage/v2/workflow.py),
  [storage](../arbitrage/v2/persistence.py).
- [Boundary regressions](../tests/test_v2_resale_output_boundary.py),
  [live adapter fake-provider tests](../tests/test_v2_live_resale.py),
  [offline Resale integration](../tests/test_v2_resale_integration.py),
  [diagnostic safeguards](../tests/test_v2_resale_diagnostic.py).
- [Historical implementation notes](../arbitrage/v2/README.md) and Git history.
  Earlier diagnostics are experiments, not production entry points; inspect
  their flags before running anything. No independent requirements/agent-spec
  documents were found in the tracked repository at handoff; current contracts,
  instructions, tests and these docs are the available in-repository authority.

Recommended first development task: review and propose the 7C evidence-quality
contract change, including backward compatibility, realized-price uncertainty
and deterministic tests. Do not start implementation, broaden orchestration or
make another live call without the next scoped instruction. Keep commercial
execution outside current authority.
