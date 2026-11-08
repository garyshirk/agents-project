# Current status, evidence and roadmap

**PROJECT STATUS: PAUSED BY USER.** Updated 2026-11-08. Not abandoned.
Read the [overview](../README.md) first and [resumption guide](RESUMPTION_GUIDE.md)
before any execution. This is the canonical current status; the
[V2 checkpoint notes](../arbitrage/v2/README.md) preserve implementation history.

## Milestones

Code checkpoint: `05360e9c43da4023eaf5920398e781f3921d7b6b`, `v2-human-review`.
Baseline: **1,033 passing deterministic tests**, Python 3.13.5.
Live-only verifications did not require code commits.

| Phase | Implemented / verified | Remaining limit |
| --- | --- | --- |
| 1 (`e13c2cb`) | Opportunity/Evaluation, independent execution state, append-only storage, human review foundation; temporary-DB tests | Human service is assessment-only, not authentication or production UI |
| 2 (`162574b`) | Comparable products, price evidence, qualitative assessments, immutable revisions | Assertions are attributed, not independently verified |
| 3 (`f4c53d0`) | Explicit scenarios, deterministic engine adapter, persisted calculation/summary references | No automatic missing-cost guesses or FX conversion |
| 4 (`340df8f`) | Offline bounded workflow, manifests, retries only explicitly authorized, restart recovery and idempotency tests | No multi-process lease/automatic provider replay |
| 5 (`18584fc`) | Offline Manager context, proposal validation and application; five intents: research, assessment, assumption, scenario, summary | No complete live economic loop |
| 6 (`55af61c`) through 6H | Live Manager adapter; 6B smoke exposed reference boundaries; 6C (`dd9fa4d`) corrected references; 6D retest; 6E (`87abefd`) routing/scope; 6F retest; 6G (`9900916`) full per-intent strict schemas; 6H structured-output verification | Bounded synthetic smoke tests, not arbitrary-task reliability proof |
| 7A (`04e4143`) | Offline Resale contract, accepted-decision delegation, atomic comparable publication, context refresh | Fake-provider tests only at this milestone |
| 7B-1 (`06043a3`) | Single-call hosted web-search + full strict output, local provider audit, budget/attribution validation | Initially offline implementation, not live evidence |
| 7B-2A (`89c049e`) | Fail-closed diagnostic, fresh private temporary paths, two live flags, fixed budgets | Preparation is not authorization |
| 7B-2B (runtime only) | Provider completed; application rejected domain-invalid output | No published evidence |
| 7B-2C (`05360e9`) | Explicit null/equality/timestamp instructions; 19 new regressions; 1,033 full-suite tests | Prompt cannot guarantee factual correctness |
| 7B-2D (runtime only) | One call, two web tool actions; schema/domain/attribution accepted; 3 comparables published | PARTIAL research, LIMITED_OR_AMBIGUOUS_EVIDENCE |

Implementation phases/commits are verified by Git. The 6-series live outcomes
are preserved in local temporary audits and prior checkpoint notes; they are
not independently reproducible from committed code alone. Temporary audit paths
below are machine-local and may disappear; Git does not back them up.

## Latest Resale experiments

### 7B-2B: rejection preserved

Database: `/tmp/v2-resale-live.yWO18s/arbitrage.db`.
Evaluation: `9f4e06cc-bc67-4172-a4f4-a9a93d47240b`.
Database SHA-256: `8af6781e0971a7c3210589306b403b6c7324e43382d56cdf596214779206b80d`.
Raw provider response SHA-256:
`a31b83558c9c4fdd7a5ad08d397fba45b12ed31544b9100ed60e5d1e81ea070e`.

Provider accepted web_search plus strict output, but three ACTIVE_ASK records
put “Active ask only; not a completed-sale price.” in `completion_basis`.
The JSON Schema allowed a string; Pydantic cross-field validation rejected it.
Duplicated source objects also differed in limitations; observation timestamps
were unsupported. No comparables were published. Do not repair or replay this run.

7B-2C advanced the prompt to `v2-resale-public-research-v2`; the schema remains
`v2-resale-research-result-v1`. Existing domain/equality validators were preserved.

### 7B-2D: technical acceptance, limited evidence

Database: `/tmp/v2-resale-live.kPTg7f/arbitrage.db`.
SHA-256: `ba71a49c7b87fa813f8890b7d4caa3d1ab12276c8a30bd23e5cd779e3e80899b`.
Evaluation: `bd9aaf1a-72d9-4395-be51-16495d627274`.
Publication: `c10f4765-02d3-4937-8f88-a5b04e053b8a`.
Raw response SHA-256: `b07b939afc78f0b888c5af3e75a6c34e97d6d6e09b3e36781668706aa2055eab`.

One gpt-5.4-mini invocation returned model `gpt-5.4-mini-2026-03-17`.
Two completed tool actions: search (two queries), then open_page. Usage:
16,507 input / 3,519 output / 20,026 total tokens. JSON Schema, domain,
binding and URL-attribution validation passed. Sources matched exactly and all
observed_at/transaction_at fields were null. Three comparable revisions and their
raw-report links were atomically persisted; audit stages were
STARTED → RECEIVED → VALIDATED → FINISHED (ACCEPTED).

**Model-reported claims only, not verified realized sale prices:**

| URL | Model classification / amount | Differences and limitations |
| --- | --- | --- |
| https://www.ebay.com/itm/237116600230 | COMPLETED_SALE, USD 15.00; shipping USD 12.23 excluded | Size 10; used/no box, different colorway; sold-label/displayed-price basis; accepted-offer uncertainty |
| https://www.ebay.com/itm/278437097171 | COMPLETED_SALE, CAD 42.00; shipping CAD 18.00 excluded | Size 10, new without box; Canada/variant differences; accepted-offer/payment uncertainty |
| https://www.ebay.com/itm/336473833873 | ACTIVE_ASK, USD 95.00; shipping unknown | FD2722-500, new without box, different variant; no sale assertion |

No independently verified realized transaction prices were established. The two
purported completed sales passed validators because the model supplied basis
strings and supporting classifications; those are not proof of realized payment.
The suggested “lower-bound” interpretation of a used comparable is not a reliable
bound for the synthetic new/original-box product. Model titles/sellers/details
were not corroborated by provider excerpts. Provider-attributed URLs are not
independently verified marketplace facts. No independent browsing occurred.

Research status: PARTIAL. Evidence quality: LIMITED_OR_AMBIGUOUS_EVIDENCE.
Attempt SUCCEEDED, Evaluation/workflow RUNNING, zero unresolved runs, review PENDING.
Bounded Manager context availability was checked read-only (8 references,
117,610 characters); no live Manager response, calculation or human review followed.

## Safeguards, limitations and planned improvements

**Implemented safeguards:** typed domain validators; exact Evaluation/revision
references; strict provider schemas; source equality/URL attribution; no response
repair; transactionally atomic publication; immutable audit stages/raw hashes;
explicit budgets and zero SDK retries; idempotency; stale-context rejection;
human-subject currentness; database isolation; no commercial tool authority.

**Known limitations (not all software defects):**

- Sold labels, ended listings and hidden accepted offers cannot establish the
  realized transaction amount. Current COMPLETED_SALE accepts authored support
  strings without independently evaluating their meaning (next design concern).
- URL attribution is not content verification. Model claims may be wrong or
  fabricated despite valid shape and source links; excerpts may be absent.
- Plausible invented timestamps can pass validation. Freshness is unknown when
  dates are missing; audit time is not publication/observation/transaction time.
- Variant, width, size, condition and packaging differences limit comparability;
  exact synthetic FD2722-099 association is not verified.
- Currency mismatches are not silently converted. There is no FX retrieval or
  conversion service; CAD and USD cannot be combined as a common price range.
- Missing costs stay unknown or explicitly assumed. Fee coverage and shipping
  claims need evidence/assumptions; arithmetic cannot establish their truth.
- Live coverage is a small synthetic sample, not a reliability benchmark.
- Full live Manager → research → judgment → scenario → summary orchestration is
  incomplete. Existing offline APIs/tests are not completed live Evaluations.
- Historical references can be explicitly selected; automated historical
  Evaluation retrieval into Manager reasoning is not integrated.
- Recovery is explicit reconciliation, not unattended restart, cancellation
  proof or distributed execution locking. No production deployment exists.
- Temporary evidence stores are not a durable archival/backup strategy.

**Planned improvements:** evidence-quality semantics first, then controlled
validation and staged orchestration. No purchasing/listing approval is implied.

## Next checkpoint — 7C (NOT STARTED)

**V2 CHECKPOINT 7C — EVIDENCE QUALITY AND REALIZED-PRICE UNCERTAINTY.**
First inspect contracts and propose the smallest coherent change separating
listing sale status from realized-price support. Consider separate sale-status
and price-certainty/basis semantics; preserve imperfect sold-listing comparables
without calling displayed amounts verified transaction prices. Keep provenance,
no-fabrication rules and Manager judgment over imperfect evidence. Add sanitized
regressions based on 7B-2D. A design decision is required before implementation;
no particular new enum/schema or migration is approved by this documentation.
No further live research call is authorized.

## Longer-term sequence (planned, not completed live)

1. Review and implement an approved 7C evidence-quality design.
2. Separately authorize additional bounded Resale validation if warranted.
3. Connect Manager qualitative comparable assessments and explicit assumption
   proposals, preserving evidence and knowledge-source provenance.
4. Connect scenario creation/execution to deterministic profitability and
   calculation-linked economic summaries.
5. Exercise the human review workflow end to end without commercial authority.
6. Design historical Evaluation retrieval and controlled multi-agent orchestration.
7. Expand end-to-end regression/recovery tests and conduct a reliability/safety
   review before considering production use.

Retailer APIs, richer marketplace data, user interfaces and business operations
are possible later enhancements, not implemented or approved milestones.
