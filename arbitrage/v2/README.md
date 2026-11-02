# V2 Checkpoint 1: persistence and review foundation

This package has no agent orchestration, terminal commands, live research,
or commercial actions. Checkpoint 3 adds offline calculations through the existing
engine. Importing it does not open databases or start the app.
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
  Checkpoint 2 also permits typed raw report storage. Checkpoint 3 adds dedicated
  validated scenario/calculation writers; generic writes still refuse those types.
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
Checkpoint 3 permits exact validated calculation references; see its structural
numerical-claim rules below. Earlier payloads remain readable.

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

# Checkpoint 3: explicit economic scenarios and calculations

V2 now stores `EconomicScenario` and `CalculationRecord` as immutable, versioned
artifact payloads. No live agents, research, prompts, automatic scenarios, or
commercial actions were added. V1C code and its database schema are unchanged.

Use `record_scenario(evaluation_id, ScenarioDraft(...), producer=..., operation_key=...)`
and `record_scenario_revision(previous_ref, draft, ...)`. Each independent initial
scenario receives an application-owned lineage ID; revisions require its latest
exact predecessor. Names do not implicitly select/merge lineages.

Each `EconomicInput` has an explicit category, side, cost type, currency, rationale,
provenance, and disposition: EVIDENCE, ASSUMPTION, UNKNOWN, NOT_APPLICABLE, or INCLUDED.
Numeric evidence cites exact persisted records. Numeric assumptions cite existing
`AssumptionRecord` artifacts and preserve their name, amount, provenance, and unit
exactly. Assumption units are `CURRENCY/CostType`, for example
`USD/FIXED_PER_BATCH` or `USD/PERCENT_OF_UNIT_PRICE` (percentage points of that
side's unit price, not a monetary charge). Historical supporting references remain
exact references to another Evaluation; no retrieval is performed. General model
knowledge needs no URL and is never treated as an observation.

`assemble_scenario(draft)` is pure and returns READY, INSUFFICIENT_INPUTS, or
INVALID_INPUTS, with unsupported mappings distinguished explicitly. It reads no
database, generates no values, and never executes arithmetic. `calculate_scenario`
in `economics.py` calls the unchanged existing `_calculate_profitability` engine.
Repository `calculate_scenario(exact_scenario_ref, producer=..., operation_key=...)`
performs calculation and stores its immutable outcome, receipt, and new review
subject in one transaction. Persistence faults roll back and do not return success.
Engine exceptions are stored as ERROR outcomes with failure details; they are not
retried. An idempotent retry returns its original receipt without recalculation.

The adapter supports an explicitly authored point resale price, positive integer
quantity, and exactly one modeled order. The engine's required low/high fields
both receive that same supplied point, with no expected price or invented range.
It does not generate midpoint/sensitivity scenarios. Fixed-per-unit costs multiply
by quantity; fixed-per-batch costs apply once; percentage costs use the appropriate
acquisition or resale price and percentage points, exactly as the engine does.
Multi-order scenarios and non-per-unit primary prices are recorded as unsupported
INVALID_INPUTS outcomes rather than guessed conversions. Currency conversion and
multi-currency arithmetic are not implemented. UNKNOWN material inputs prevent
arithmetic; explicit scenario-only assumptions may resolve named known unknowns
without rewriting evidence. Nonmaterial unknowns remain visible in the engine's
PARTIAL result and warnings.

Purchase/resale prices, marketplace/payment fees, seller shipping, acquisition tax,
and travel each require an explicit disposition before a complete calculation.
This is cost coverage, not a product-eligibility policy. Other costs may be named
explicitly. Missing categories are unresolved, never zero. Nonapplicability needs
a rationale and produces no artificial cost row. Composite costs declare `covers`
IDs; covered components must be INCLUDED and point back with `covered_by`, on the
same economic side. Covered costs cannot also be numeric additive charges.
Resale revenue cannot cover/erase a seller expense merely because a buyer-facing
price includes shipping.
Separate/integrated payment fees require an explicit relationship and rationale.
These safeguards validate declarations, not the truth of fee applicability or
semantic overlap hidden behind misleading unrelated IDs. The V1C fee/shipping
utilities require capture-specific contracts and evidence-only shipping policies;
using them wholesale would incorrectly block V2's explicit conditional assumptions.
Their overlap/coverage principles are preserved in V2 declarations instead.

Direct resale evidence must preserve one explicitly cited comparable's price and
currency. Combining or modifying prices requires an explicit assumption, never
automatic averaging. The original evidence classification and exact comparable
revision are retained. Cited assessments must bind that exact comparable revision.
There is no exact-identity, completed-sale, minimum-count, or comparability gate.
Unit descriptions and applicability remain author assertions; Python never parses
free-text comparable units to invent conversions or verify product equivalence.

Calculations carry the full exact scenario manifest, evidence/assumption refs,
validated engine request JSON, validated engine result JSON when calculated,
adapter version, engine source SHA-256, timestamps, and producer. Engine JSON is
stored as immutable text validated against existing engine contracts, avoiding
mutable nested legacy models; decoding produces fresh typed objects. Engine
COMPLETE/PARTIAL maps to CALCULATED, independently of evidence quality. Other
outcomes are INSUFFICIENT_INPUTS, INVALID_INPUTS, and ERROR, without invented profit.

Read APIs: `list_scenario_history`, `get_scenario`, `get_current_scenario`,
`list_current_scenarios`, `list_calculation_history` (optionally exact `scenario=`),
and `get_latest_calculation`. Old revisions/results remain retrievable. Changes to
evidence do not rerun or rewrite calculations.

Summaries can now cite real CALCULATION artifacts from their exact Evaluation.
`SummaryDraft.numerical_estimate=True` requires an estimate assessment state and
CALCULATED references. Estimate summaries cannot cite incomplete calculations as
completed arithmetic. Existing qualitative labels without numerical claims remain
compatible. INSUFFICIENT_INFORMATION may cite failed/insufficient calculations,
but cannot claim a numerical estimate. No deterministic economic-quality judgment
selects or overrides the author's assessment state. Detailed numerical results live
in calculations, not arbitrary summary number fields. Free-text claims are attributed
author statements and are not parsed for semantic truth.

Every new scenario and calculation creates a new partial review subject. Earlier
human decisions become historical; published summaries are not overwritten.
All statuses stay in the review queue. No schema migration, production database
initialization, historical rewrite, or V2 runtime orchestration is performed.
Existing Checkpoint 1/2 JSON remains readable, and earlier qualitative-summary
idempotency receipts remain replayable. Older code cannot read the new payload
kinds; no historical artifacts or receipts are rewritten.
