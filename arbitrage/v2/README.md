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
stored text. The human review service never calculates, purchases, lists, or resumes work.

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

# Checkpoint 4: bounded offline evaluation workflow

`OfflineWorkflow` in `workflow.py` connects the existing V2 writers with explicit,
data-only synthetic fixtures. It is not wired to the terminal application or SDK
agents. No live research, prompts, tools, scheduler, commercial action, or economic
decision policy is added. Existing V1C behavior and engine code remain unchanged.

## Entry points and exact context

- `accept(submission, limits, operation_key=...)` atomically creates the existing
  Opportunity/Evaluation/initial review subject and an ACCEPTED workflow event.
  It reuses repository acceptance helpers, tables, append-only artifacts, and
  idempotency receipts. A crash inside acceptance rolls back the whole operation.
- `run(evaluation_id, steps, operation_key=...)` executes only the explicitly
  supplied finite batch. Steps have stable IDs, roles, exact context references,
  context text, and either a synthetic fixture or a calculation scenario reference.
- `repository.get_workflow(evaluation_id)` returns a consistent read of the
  Evaluation, workflow events, and attempts, with `unresolved_runs` and
  `unfinished_attempts`. It does not select another/latest Evaluation.
- `finish(evaluation_id, state, reason=..., operation_key=...)` explicitly records
  FINISHED, PARTIAL, FAILED, or CANCELLED in the existing execution-state vocabulary.
  It cannot finish an open run or resume a terminal Evaluation. A new linked
  Evaluation may be explicitly created using the earlier repository API and
  explicitly initialized with `initialize_workflow`; no automatic restoration.

Batches can be incremental. For example, record research, then supply a new batch
whose fixtures cite those exact returned artifacts. There are no symbolic latest
aliases, implicit dependency selection, or automatic downstream step generation.
A batch requiring outputs that do not yet exist must be supplied in later explicit
calls after those references are known. The original submission, Evaluation and
Opportunity IDs, exact context refs/text, and exact fixture bytes are durably stored
in each invocation manifest before execution.

Meaningful specialist roles are RESEARCH, COMPARABLE, ASSESSMENT, ASSUMPTION,
SCENARIO, and SUMMARY. CALCULATION is application-owned and has no specialist.
`OfflineFixture` is explicitly SYNTHETIC_OFFLINE with a stable fixture ID and
response JSON; `invoke_offline_fixture` only parses those supplied bytes. Typed
responses carry matching ownership and one role-specific draft or a structured
specialist-returned failure. No callable, database handle, review service, or
external tool is passed to the adapter. All nested output references must appear
in the manifest and belong to the selected Evaluation. Fabricated refs and
cross-Evaluation substitutions are rejected. Comparable fixtures must declare
SYNTHETIC_FIXTURE capture. Persisted outputs have synthetic application attribution.
Fixtures are attributed assertions, never independently verified research.

## Durable progress and bounds

All four limits are mandatory positive integers: maximum specialist invocations,
maximum attempts per step, maximum distinct workflow steps, and maximum calculation
attempts. They apply cumulatively to this Evaluation and survive restarts. Durable
STARTED records consume the relevant budget conservatively even if a crash occurs
before the actual invocation. Step and attempt counts are checked before a batch
is claimed. No automatic retries or loops beyond the supplied finite batch exist.

The existing Evaluation stays RUNNING while its workflow may be ACCEPTED, RUNNING,
PARTIAL, or INTERRUPTED/awaiting recovery. This permits explicit bounded continuation
without weakening the earlier terminal-state contract. Explicit finish maps to
FINISHED/COMPLETED, PARTIAL, FAILED, or CANCELLED. These progress phases are not
economic-quality or human-review decisions. FINISHED does not mean profitable,
well-supported, or commercially authorized, and no summary is invented to finish.

Workflow events and STARTED/completion attempt artifacts use the existing V2
artifact store. Attempts preserve application IDs, start/end timestamps, exact
input manifests, producer, output refs, and failure details. Outcomes distinguish
SUCCEEDED, SPECIALIST_FAILURE, INVALID_OUTPUT, PERSISTENCE_FAILURE,
COORDINATOR_ERROR, INTERRUPTED, CALCULATION_INSUFFICIENT, CALCULATION_INVALID, and
CALCULATION_ERROR. Existing calculation artifacts retain their separate statuses.
Invalid or failed steps do not fabricate accepted output artifacts. Earlier output
and valid published summaries survive later failures. Calculation/summary writers
remain the sole validated persistence paths for those outputs.

## Restart and recovery

A RUN_STARTED record claims the batch before any invocation. Only its original
application token may dispatch it; concurrent or restarted same-key callers cannot
silently dispatch again. A completed same-key request returns its original end
record. Different request content with the same key conflicts. Exact output
operation receipts bind accepted output artifacts to their original attempt.

Caught interruptions/unexpected execution errors receive best-effort immutable
interruption reports. Hard process termination cannot write a report: its unresolved
run/STARTED attempt is the durable signal of ambiguity, not proof the specialist
was never called. If storage is unavailable, errors propagate without a claim that
diagnostics were durably recorded. Reviewers can inspect the existing evidence.

After ensuring the original process has stopped, a trusted caller may explicitly
call `recover(run_start_ref, decision="RECONCILE" or "ABANDON", operation_key=...)`.
The decision is persisted before reconciliation. Recovery itself is idempotent and
restartable with its original key; changing its decision conflicts. It never
invokes a specialist, republishes a summary, or reruns arithmetic.

- A durable output receipt is reconciled to its exact attempt and actual result.
  Even ABANDON cannot erase or pretend an already committed output did not happen.
- No output receipt means the attempt becomes INTERRUPTED with explicit uncertainty,
  not a fabricated success. Unstarted planned steps remain visible in the original
  run plan; no new attempt/output is invented for them.
- After recovery closes the old run, the caller may submit a new bounded batch.
  Retrying an unsuccessful step requires `retry_of` pointing to its latest terminal
  attempt and preserves the role. Successful steps cannot be retried as failures;
  a genuinely new research action needs its own explicit step ID. Limits still apply.

Output persistence and attempt completion deliberately have separate receipts.
This makes crashes after output persistence, calculation, or summary publication
reconcilable without duplication. Each individual writer/receipt/review-subject
update is transactional. Persistence failures abort the batch; remaining steps are
not invoked. Any ambiguous run must be explicitly recovered before continuation.

This is a trusted, single-controller offline workflow, not distributed execution
infrastructure. The token prevents duplicate same-key dispatch but is not a worker
lease or authentication mechanism. Recovery must not race a still-running original
process. No exactly-once external-side-effect guarantee is claimed; future live
adapters require a separately scoped cancellation/idempotency/recovery design.

## Economics and human review

Only explicitly authored, persisted scenarios reach the existing calculation API.
No comparable scores, assumptions, resale prices, assessment states, or summaries
are inferred. A supplied summary is validated and published; an invalid one leaves
the previous valid summary intact. An authored insufficient-information summary can
be published even after research failure. Weak or nonmeaningful comparable judgments
are preserved and never used as automatic eligibility filters.

Accepted outputs use the earlier review-currentness rules. Failures and interruption
reports also create new partial review subjects. Bookkeeping starts/completions alone
do not replace a published economic summary. Every accepted Evaluation remains in
the unfiltered queue, including incomplete, failed, interrupted, insufficient,
unattractive, and already human-reviewed Evaluations. Human decisions remain in the
separate review service and cannot trigger workflow or commercial actions.

Tests use fictional fixtures and temporary databases, including complete/conditional,
partial failure, insufficient/invalid/error economics, invalid ownership, restart,
interruption at persistence boundaries, concurrency, rollback, and bounded retries.
No production migration or real database initialization is performed. Earlier V2
payloads remain readable; older code cannot read these new workflow payload kinds.

# Checkpoint 5: offline Manager decision boundary

`OfflineManager` in `manager.py` is a trusted application facade over the existing
offline workflow. It adds no live model, prompts, research execution, planning
loop, UI, or commercial authority. The unchanged terminal app still runs V1C.

## Exact bounded input

`build_context(evaluation_id, references, limits=ManagerContextLimits(...),
operation_key=...)` persists an immutable MANAGER_CONTEXT artifact. The application
explicitly selects exact artifact revisions; no agent receives a repository handle.
Limits bound both artifact count (including explicit historical records and the
captured workflow event) and serialized snapshot characters. Oversized contexts
fail rather than silently dropping evidence. Selection limitations and the count
of available artifacts make omitted context visible.

The typed `ManagerSnapshot` contains the exact original Opportunity/submission,
Evaluation/publication target, selected artifacts with all nested evidence and
provenance, and recorded workflow progress/unresolved attempts. Research, intake
claims, comparable/assessment revisions, scenarios, calculations, summaries,
knowns, unknowns, and failures remain in their existing typed contracts. Prose is
not parsed into invented facts. Artifact snapshots are stored as validated immutable
JSON with a SHA-256 manifest to avoid recursive Artifact/context schemas.
`read_context` returns fresh typed objects and checks manifest consistency.

Contexts do not advance review-currentness merely by being assembled. Before
application, the stored review subject and summary target must still match the
Evaluation inside the same SQLite write transaction. New evidence, decisions,
failures, or publication changes make the context stale. Ordinary workflow
bookkeeping does not invalidate a context as its own Manager step begins.
Nothing silently rebinds to newer evidence. Explicit fresh contexts and revised
decisions are required. Intentionally selected historical revisions stay exact.

`historical_references=...` only resolves explicitly supplied exact persisted refs;
there is no automatic historical search/retrieval. Historical records belong to
another Evaluation and can support only declared historical provenance, not direct
current-Evaluation evidence, comparable selection, or publication targets.

## Five independent authored intents

`ManagerProposal` binds the selected Evaluation/Opportunity, exact context,
authored rationale, evidence refs, limitations/uncertainties, and one small intent:

- `ResearchRequest`: objective, target, requested sourcing/resale/cost/human-input
  role, scope, bounded proposed invocation count, and optional priority. The durable
  decision is only a request; it executes nothing and consumes no research budget.
- `ComparableInterpretation`: exact comparable, authored selection flag, and the
  existing `AssessmentDraft`. Categories, similarities, differences, unknowns, price
  classification, units, and source limitations are preserved. No score, exact-match
  gate, minimum count, or automatic weak-evidence exclusion is introduced.
- `AssumptionProposal`: existing numerical `EconomicAssumption`, preserving rationale,
  amount, unit, and all four provenance categories. Units must use the Checkpoint 3
  `CURRENCY/CostType` convention. General knowledge needs no URL; current/historical
  evidence claims require exact supplied support. Assumptions cannot be observations.
- `ScenarioProposal`: existing `ScenarioDraft`, with optional exact predecessor.
  Existing pure assembly checks reject invalid/unsupported coverage, units, and
  currency mappings. Insufficient scenarios may still persist. No calculation,
  midpoint, assumption, or price is generated automatically.
- `SummaryProposal`: existing `SummaryDraft`, exact expected publication target,
  and concise structured explanation of the opportunity, important evidence, used
  comparables and their rationale, product differences, assumptions, uncertain
  costs, and useful next evidence. Conditional summaries additionally explain why
  the estimate is useful despite missing information and disclose consequential
  uncertainties.

Each accepted decision has an application-owned lineage ID, revision, producer,
timestamp, and explicit predecessor when revised. A revision must name the latest
decision of the same purpose; a different purpose starts a new lineage. No original
evidence or historical artifact is rewritten.

## Validation, atomicity, and workflow authority

`submit(context_ref, proposal_or_json, step_id=..., operation_key=...)` supplies a
SYNTHETIC_OFFLINE fixture to a MANAGER workflow step. Its invocation, limits,
attempts, errors, interruptions, idempotency, and recovery use Checkpoint 4.
Malformed/forbidden proposals and stale/cross-Evaluation refs become durable failed
attempts when storage is available. Their original bytes remain in the input
fixture; prior valid summaries and evidence survive. There is no unrestricted
tool-data logging or repository capability in fixture data.

The trusted adapter validates only exact records actually supplied in the context.
It uses existing assessment, assumption, scenario, and summary writers. A narrow
context-local transaction scope composes those writers: accepted decision, domain
artifact, review-subject update, application linkage, and idempotency receipts all
commit together or roll back together. SQLite write serialization makes conflicting
publications against one context fail stale after the first succeeds. Existing
V2 calls outside this scope retain their earlier transaction behavior.

MANAGER_APPLICATION is the workflow's validated output receipt. It binds the exact
MANAGER_DECISION to its optional accepted domain artifact. Summary publication adds
the application-owned decision as an origin evidence reference, without changing
the authored economic fields. This lets the current published summary cover the
full structured explanation rather than immediately becoming a partial subject.
Application linkage after publication is bookkeeping, not new economic evidence.

Calculations are still separately requested by the application using the existing
CALCULATION step/API. Proposing a scenario never calculates it, and arithmetic
never automatically publishes a summary. Numerical summaries must cite supplied
validated calculations and their exact scenario manifests. All calculation
assumptions must be disclosed with their exact values/units/provenance, and used
comparable revisions must have authored explanations. No engine result field can
be authored in a Manager proposal. JSON floating-point monetary values are refused;
explicit decimal text preserves existing arithmetic precision.

The author chooses EVIDENCE_BASED_ESTIMATE, CONDITIONAL_ESTIMATE, or
INSUFFICIENT_INFORMATION. The application checks structural calculation links and
disclosures, not whether the economic judgment is persuasive. No universal number
of completed sales or exact product attributes is imposed. An insufficient summary
needs no numerical calculation and may explain partial/failed research. Free-text
interpretations remain attributed assertions; Python does not verify their truth.

Accepted decisions/evidence advance the existing review subject, keeping older human
decisions historical. Context creation alone does not. Every Evaluation remains in
the review queue. Manager decisions cannot record human reviews, remove Evaluations,
claim purchased/listed state, or authorize commercial actions. Human review remains
a separate assessment-only service.

Tests use temporary databases and explicitly fictional decisions/evidence. No real
database initialization/migration, historical Attempt 7 mutation, V1C source change,
live API call, or external research is part of this checkpoint. Future live adapter
design, context selection UX, human interfaces, and research approval/execution are
separate work. Earlier V2 payloads remain readable; older code cannot read the new
Manager artifact kinds.

# Checkpoint 6: controlled live Manager transport (not yet live-tested)

`live_manager.py` reuses the installed OpenAI Responses SDK and the existing
Checkpoint 4 workflow/Checkpoint 5 application validator. No live Sourcing,
Resale, WebSearch, browser, commercial tool, prompt loop, or V1C change is added.
Importing creates no SDK client and makes no call. `agent.py` is unchanged.

## Explicit client and authority

Construct `LiveManager(repository, client=..., mode="OFFLINE")` explicitly.
OFFLINE mode accepts only an injected client declaring `is_live=False` (the tests
provide deterministic data-only fakes). There is no implicit real-client fallback.
The production `OpenAIResponsesClient(explicit_sdk_client)` declares `is_live=True`.
Each production run requires BOTH `mode="LIVE"` and the trusted caller's
`authorize_live=True`. Environment variables, API keys, proposal content, and
Codex access cannot authorize a call. Fake clients are trusted test dependencies,
not a sandbox against arbitrary caller-supplied Python code.

`execute(context_ref, objective, config, step_id=..., operation_key=...)` requests
one explicitly selected decision type. `ManagerModelConfig` requires model ID,
decision type, output-token cap, timeout, invocation/attempt/total-token budgets,
request/response byte bounds, and an explicit monetary-budget disposition.
No model is selected automatically, and account availability is not assumed.

The production client uses `with_options(max_retries=0, timeout=...)` and one
non-streaming `responses.create` call. It supplies no tools, disables parallel
tool calls and automatic truncation, and requests `store=False`. A process-local
lock rejects overlapping provider dispatch. This is a trusted single-controller
workflow, not distributed worker coordination. Do not run recovery concurrently
with an original process that is still alive.

## Prompt, response schema, and application validation

The constant instruction set is versioned as `v2-manager-controlled-v1`. It asks
for useful defensible human-reviewed assessments, preserves uncertainty and
assumptions, forbids invented sources/sales/calculations or commercial actions,
and does not impose universal exact-identity or completed-sale gates.
Opportunity/research text, source notes, objectives, exact context, and the
decision-specific application schema are JSON data in a separate user message;
they are never interpolated into system instructions. Embedded evidence
instructions cannot grant tools or repository authority.

The hosted strict response schema is deliberately small: an object containing
only `proposal_json`, a string. That string must parse as the existing typed
ManagerProposal and match the requested intent. The request supplies the exact
existing schema specialized to that one intent; it does not ask for five outputs.
No JSON repair, inferred defaults for missing economic evidence, replacement
references, or alternative judgment is generated by the parser. Checkpoint 5
remains authoritative for ownership, provenance, freshness, assumptions,
scenario/calculation links, publication targets, and review isolation.

## Persistent bounds and honest limits

Each LIVE_MANAGER step consumes the existing durable workflow budgets as well as
model-call budgets. The first dispatched call fixes the Evaluation's model
invocation/attempt/total-token ceilings; later requests cannot silently expand
them. The request is byte-bounded and reserves its serialized UTF-8 bytes plus
the requested output-token cap plus a 4,096-token framing allowance. Reservations
are never refunded after ambiguous failures or missing usage. Returned usage
above a reservation increases the accounted consumption rather than being hidden.
An observed usage-limit violation is retained and prevents proposal application.

The API supports `max_output_tokens`, but exposes no hard total-input-plus-output
ceiling in this installed request interface. Reservations are conservative local
accounting, not a contractual provider token/billing guarantee: opaque framing,
tokenization, and metadata can differ. No authoritative prices are configured,
so a non-null `max_estimated_cost_usd` fails closed. There is no invented price
table or monetary ceiling. Inspect actual returned usage after a later live test;
missing metadata remains null, never a fabricated zero or cost.

No automatic retry occurs. A timeout or transport/response loss may already have
been processed and is marked ambiguous. A separately authorized retry requires
the existing latest-attempt `retry_of`, remaining budgets, a fresh context, and
explicit `acknowledge_ambiguous_retry=True` when appropriate. SDK HTTP timeouts
bound transport waits; they are not proof that remote work was cancelled.

## Audit and restart semantics

Immutable MODEL_CALL artifacts use existing V2 tables and receipts, without a
migration. STARTED is committed before dispatch and binds the exact workflow
attempt, decision objective/type, context hash, instruction version/hash, request
hash, requested model/configuration, reservation, authorization and timestamps.
RECEIVED records the transport outcome, bounded response snapshot/hash, returned
response/request IDs, model, and actual usage where available. A timeout outcome
may have no provider response at all. FINISHED records acceptance/application
references or separate parsing, validation, refusal, rate-limit, incomplete,
transport, and size/usage failures. Provider success with application rejection
remains distinguishable. API secrets and arbitrary exception headers/messages
are not logged. Selected context and bounded proposal responses can contain the
user's research data; the later authorized call sends that selected data to OpenAI.

Accepted outputs still commit through Checkpoint 5's atomic decision/domain
transaction. If persistence fails after a provider return, the adapter claims no
durable success and leaves receipts/attempts available for explicit recovery.
`LiveManager.recover(run_start_ref, decision="RECONCILE" or "ABANDON",
operation_key=...)` reuses Checkpoint 4 recovery, never contacts a provider,
and updates unfinished model audits:

- Existing output receipt: reconcile the accepted decision without reapplying it.
- Stored response but no accepted receipt: retain NOT_APPLIED; inspect it manually.
- No durable response: retain AMBIGUOUS. Do not assume the provider was never called.

Recovery never replays a provider call or silently rebases an old proposal on a
fresh context. A new decision needs explicit caller authorization and remaining
budgets. Context freshness may have changed after interruption, so manual review
and a newly assembled context are required before any subsequent proposal.

## Offline evaluation

`manager_evaluation.evaluate_proposal` uses existing validators in an explicitly
rolled-back transaction over an isolated fixture repository. It reports schema
validity, provenance-reference scope, full application/reference integrity, and
separate HUMAN_REVIEW_REQUIRED judgment quality. Reference scope is not independent
verification of a source's truth. Human-authored rubrics identify expected behavior
and uncertainties; passing a schema does not establish a correct economic judgment.
The report is serializable JSON for later live-response evaluation.

Synthetic preloaded tests cover completed-sale evidence, imperfect/variant
comparables, asking-only evidence, unknown shipping/fees, insufficient/conflicting
information, injection text, and fabricated references. No harness call invokes a
provider. All integration tests use fake clients and temporary databases.

## Separately authorized smoke test — do not run during implementation

Review and authorize ONE paid call first, and choose a model available to the
project. Use a fresh directory and preserve it for inspection; do not point the
smoke test at production or the existing V2 development stores. The script refuses
existing database paths and never starts research, calculations, or commercial work.

From the project root, after selecting the approved model:

    V2_SMOKE_MODEL='YOUR_APPROVED_MODEL_ID'
    V2_SMOKE_DIR="$(mktemp -d /tmp/v2-manager-smoke.XXXXXX)"
    .venv/bin/python diagnostics/v2_manager_smoke.py \
      --business-db "$V2_SMOKE_DIR/arbitrage.db" \
      --sessions-db "$V2_SMOKE_DIR/sessions.db" --model "$V2_SMOKE_MODEL"

This default check prints configuration only, creates no database, and constructs
no OpenAI client. Only AFTER separate explicit authorization, run exactly once:

    .venv/bin/python diagnostics/v2_manager_smoke.py \
      --business-db "$V2_SMOKE_DIR/arbitrage.db" \
      --sessions-db "$V2_SMOKE_DIR/sessions.db" --model "$V2_SMOKE_MODEL" \
      --run --authorize-live

The authorized path reads the existing root `.env` without printing the key,
creates a new isolated Evaluation/context, requests only one bounded RESEARCH_REQUEST,
and prints a structured result. Limits: one invocation, one attempt, 1,500 output
tokens, 45-second SDK timeout, 100,000 local reserved total tokens, 80,000 request
bytes, and 30,000 response bytes. No session conversation store is opened.

Inspect `get_workflow(evaluation_id)`, `list_model_calls(evaluation_id)`, the accepted
decision/application references, and `list_review_queue()`. If interrupted or
ambiguous, STOP: preserve the directory and inspect/recover its recorded run before
considering any new call. Do not rerun with a different directory merely to bypass
uncertainty or budgets. No live smoke test has been performed in this checkpoint.
