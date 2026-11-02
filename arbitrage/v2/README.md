# V2 Checkpoint 1: persistence and review foundation

This package has no agent orchestration, terminal commands, calculations, research,
or commercial actions. Importing it does not open databases or start the app.
The existing launcher/application still runs V1C behavior.

## Explicit database selection and initialization

`V2Repository(business_path, session_database_path=session_path)` requires both
absolute paths. It uses the existing isolation validator, refusing either protected
project-root database, aliases, and SQLite sidecar collisions. It never opens the
session store. Construction validates paths only; `initialize_schema()` explicitly
initializes V2 tables. No real development or historical databases were initialized
by this checkpoint's tests; all fixtures use temporary databases.

Initialization creates `v2_schema_metadata`, `v2_opportunities`, `v2_evaluations`,
`v2_artifacts`, `v2_reviews`, and `v2_operations`. The additional operations table
holds immutable idempotency request/result receipts across operation kinds.
No V1C schema/version is changed. Unsupported V2 schema versions are refused,
not migrated. Record models and artifact payloads have literal schema versions;
opportunity/Evaluation table layouts are governed by V2 schema version 1.

## APIs

- `accept_opportunity(submission, operation_key=...)`: atomically records the exact
  original human text, qualified scenario, Opportunity, Evaluation, and initial
  partial review subject. No exact identity/economic completeness requirement.
- `create_evaluation(opportunity_id, predecessor_evaluation_id=..., operation_key=...)`:
  creates an explicit linked Evaluation, never silently chooses the latest one.
- `set_execution_state(evaluation_id, state, operation_key=...)`: explicit state
  recording only; no execution. Terminal execution is not automatically resumed.
- `record_artifact(...)`: accepts only typed failure or assumption payloads here.
  Checkpoint 2 also permits typed raw report storage. Other future categories
  remain reserved; there is no calculation writer.
- `record_summary(evaluation_id, draft, producer=..., operation_key=...)`: creates
  an immutable next summary revision, linking its predecessor automatically.
- `get_opportunity`, `get_evaluation`, `get_artifact`, `list_artifacts`,
  `get_current_summary`, `list_reviews`, `review_status`, and `list_review_queue`:
  read-only, transaction-consistent retrievals. Missing/malformed data raises an
  error, not a repaired record or a silently filtered queue entry.
- `HumanReviewService`: provides queue/history/status reads and `record_decision`.
  It exposes no generic artifact-writing command. It is not registered as a tool
  or CLI. Its caller must be a trusted human interface; HumanActor metadata is
  provenance, not an authentication system.

## Review and revision semantics

Every accepted Evaluation has a stable `PARTIAL_OR_UNFINISHED` review subject even
before any summary exists. A failure/assumption artifact creates a fresh partial
subject covering the updated record; it does not invent a completed economic
summary. A published summary becomes the exact review subject. Reviews can target
historical subjects, but do not become current by doing so.

`get_current_summary` returns the latest **published** summary. Consult
`review_status.summary_matches_current_subject` to distinguish it from subsequently
recorded information. A new summary or changed partial subject returns the queue
to PENDING unless that exact subject has a review. Execution-state changes alone
do not invalidate an unchanged published economic summary. The execution and human
review dimensions remain independent.

`review_subject_is_current(review_id)` checks exact revision applicability;
`review_is_current(review_id)` additionally checks whether it is the latest decision
for that current subject. Review supersession requires the latest decision for the
same Evaluation and exact subject. All previous decisions remain readable.

The queue includes every Evaluation, with no assessment/review/execution/profit
filter. Ordering is `(created_at, evaluation_id)` with bounded limit/offset paging
(maximum 100). Pages use consistent read transactions; concurrent insertions can
shift later offset-based pages. Cursor paging can be added if needed later.

## Evidence and assessment semantics

All three economic assessment states are supported with a nonblank decision
rationale, explicit consequential uncertainties, facts, unknowns, assumptions, and
limitations. Numerical output is optional. This foundation does not independently
endorse an economic judgment. Descriptive text is not a validated calculation.
Calculation references are refused until a validated calculation payload/writer is
implemented in a later checkpoint.

Assumptions retain their classification even when supported by historical records.
Provenance distinguishes model general knowledge, current evidence, historical
Evaluations, and human input. Optional persisted references are checked for exact
ownership/sequence and current-versus-historical scope; there is no historical
search, retrieval agent, inferred history, or transaction-tracking behavior.
Observation/outcome categories represent attributed information, not independent
verification. Human corrections remain append-only new information.

## Transaction and idempotency boundaries

Writes use foreign keys and `BEGIN IMMEDIATE`; reads use read-only connections and
an explicit read transaction. Entity creation, subject/summary changes, reviews,
and their idempotency receipts commit together or roll back together. Keys are
global to the V2 store; reuse with different operation/content is rejected.
Replays return the original receipt snapshot, not an implicitly refreshed record;
use explicit getters for current state.

Artifacts, reviews, and operation receipts have SQLite UPDATE/DELETE rejection
triggers. This is append-only application storage, not tamper-proof protection
against an administrator who can alter the schema. There are no multi-process
execution leases; SQLite serialization only protects these recording operations.
Schema checks validate shape/provenance, not truth of agent/human assertions.

Human decisions always have `ASSESSMENT_ONLY` authority. Follow-up requests are
stored text. Nothing here researches, calculates, purchases, lists, or resumes work.

## Checkpoint 2: comparable evidence and qualitative assessments

`ComparableProduct` uses optional named identifiers, variant/other attributes,
condition, packaging, quantity, and bundle contents. Unknown values stay null;
there are no mandatory shoe attributes or exact-match eligibility rules.

`PriceEvidence` separates completed-sale claims, active asking prices, published
references, human reports, and other qualified evidence. Monetary values use
Decimal strings and explicit currency; no conversions or inferred sale amounts
occur. Completed-sale claims require completion support and, when a numerical
price is supplied, realized-price support. These are attributed source claims,
not independent application verification. `ComparableSource.application_verified`
is always false; URL validation checks syntax, not page contents or identity.

Typed `RawResearchReport` records provide optional exact raw-source artifact
bindings. Storing a supplied report does not fetch a page or perform research.

Repository APIs:
- `record_comparable(evaluation_id, draft, producer=..., operation_key=...)`
- `record_comparable_revision(previous_ref, draft, ...)`
- `record_assessment(evaluation_id, comparable_ref, draft, ...)`
- `record_assessment_revision(previous_ref, comparable_ref, draft, ...)`
- `list_comparable_history(evaluation_id)` and `get_comparable(exact_ref)`
- `get_current_comparable(evaluation_id, comparable_id)`
- `list_assessment_history(evaluation_id, comparable_id)`
- `get_current_assessments(evaluation_id, comparable_id)`
- `get_review_evidence(evaluation_id)`; human service `get_evidence` exposes
  this complete ordered read snapshot, including failures and older judgments.

Comparable and assessment lineage IDs are application-owned. Each revision has
its own immutable ArtifactRef. A correction must name the latest predecessor;
forks are refused. Multiple independent assessment lineages are allowed for the
same comparable. Revising an assessment can explicitly target a newer revision
of the same comparable identity, but never silently changes the old binding.

Assessments preserve an attributed Candidate description anchored to a persisted
same-Evaluation review subject/summary; they do not overwrite or independently
verify the original Opportunity submission. The qualitative category is supplied,
never computed from the attribute differences or used as an eligibility filter.

Assessment history reports separate flags for latest assessment revision and
binding to the current comparable revision. `is_current` requires both. Current
assessment retrieval never filters by qualitative category; the complete history
and review evidence retain stale and NOT_MEANINGFULLY_COMPARABLE assessments.

All comparable/assessment references, raw-source references, and caller parents
must exist in the exact Evaluation. Raw references must identify raw reports;
assessment evidence must identify raw reports or comparables. Exact support
references cannot smuggle other-Evaluation evidence into these records.

Each successful new record/revision atomically advances the existing partial
review subject. A replayed idempotency receipt does not advance it again. A previous
economic summary is preserved, not recomputed or reclassified. Human review
remains PENDING until the expanded evidence subject is actually reviewed.

No tables or schema migration were added. Older checkpoint-1 payloads remain
readable; old application code will not understand new checkpoint-2 payload kinds.
Tests use explicitly synthetic `example.invalid` sources and temporary databases.
