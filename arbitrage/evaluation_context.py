"""Deterministic inspection only: no assembly, arithmetic, model calls, or writes."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from arbitrage.contracts import (
    CandidateRecord, CandidateEvaluation, EconomicCostFindingsResult,
    EvaluationArtifactType, EvaluationSelectionEvent, IdentityResolution,
    ProductIdentity, ProfitabilityRequest, ProfitabilityResult, ProfitabilityToolResult,
    ResaleResult, SourceReference, SourcingResult, SourcingTextReport,
)
from arbitrage.identity_resolution import resolution_status
from arbitrage.persistence import EvaluationContextRefused, EvaluationContextSnapshot, PersistenceDataError
from arbitrage.profitability_request_assembly import (
    ArtifactProvenance, IdentityAcceptanceDecision, assess_identity_with_resolution,
)
from arbitrage.shipping_coverage import validate_outbound_shipping
from arbitrage.fee_scope import validate_marketplace_fee_scope


class ContextModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Applicability(str, Enum):
    CURRENTLY_ESTABLISHED = "CURRENTLY_ESTABLISHED"
    NOT_CURRENT = "NOT_CURRENT"
    UNDETERMINED = "UNDETERMINED"


class BlockerState(str, Enum):
    ESTABLISHED_BLOCKER = "ESTABLISHED_BLOCKER"
    POTENTIAL_BLOCKER = "POTENTIAL_BLOCKER"
    NOT_EVALUATED = "NOT_EVALUATED"


class ContextFinding(ContextModel):
    category: str
    state: BlockerState
    reason: str
    artifact_references: list[ArtifactProvenance] = Field(default_factory=list)


class ArtifactInventoryItem(ContextModel):
    reference: ArtifactProvenance
    evaluation_id: str
    created_at: datetime
    provenance_kind: str
    success: bool | None = None
    source_references: list[SourceReference] = Field(default_factory=list)
    existing_parent_references: list[ArtifactProvenance] = Field(default_factory=list)


class EvidenceContext(ContextModel):
    selected: list[ArtifactProvenance]
    acquisition_ready: bool
    selling_ready: bool
    readiness_scope: str = "Implemented sequence rules only; ordering does not establish exact parentage or external-world freshness."
    sourcing_report: SourcingTextReport | None
    resale_result: ResaleResult | None
    acquisition_findings: EconomicCostFindingsResult | None
    selling_findings: EconomicCostFindingsResult | None


class RecordedResaleTarget(ContextModel):
    origin: str
    candidate_id: str
    evaluation_id: str
    product_identity: ProductIdentity


class ResolutionContext(ContextModel):
    reference: ArtifactProvenance
    record: IdentityResolution
    confirmation_applicable: bool
    applicability_reason: str


class IdentityContext(ContextModel):
    candidate_identity: ProductIdentity
    authoritative_resale_target: RecordedResaleTarget | None
    target_matches_current_candidate: bool | None
    researched_identity: ProductIdentity | None
    assessment: IdentityAcceptanceDecision | None
    one_sided_missing: list[str]
    both_absent: list[str]
    other_unresolved: list[str]
    nonblocking_unknowns: list[str]
    resolutions: list[ResolutionContext]
    resolution_summary: str
    assessment_scope: str = "New inspection-time assessment using current implemented identity rules; not historical verification."


class ProfitabilityAttempt(ContextModel):
    reference: ArtifactProvenance
    result_envelope: ProfitabilityToolResult
    recorded_failure_stage: str | None
    recorded_error_type: str | None
    recorded_failure_categories: list[str]
    stored_request: ProfitabilityRequest | None
    stored_context_result: ProfitabilityResult | None
    recorded_input_references: list[ArtifactProvenance]
    applicability: Applicability
    applicability_reasons: list[str]


class ProfitabilityHistory(ContextModel):
    attempts: list[ProfitabilityAttempt]
    historical_success_exists: bool
    current_success_applicability: Applicability
    applicability_reason: str
    persisted_result_cache: ProfitabilityResult | None
    cache_matching_attempt_ids: list[int]


class HumanInputContext(ContextModel):
    reference: ArtifactProvenance
    text: str


class EvaluationContext(ContextModel):
    candidate: CandidateRecord
    evaluation: CandidateEvaluation
    human_inputs: list[HumanInputContext]
    artifact_inventory: list[ArtifactInventoryItem]
    evidence: EvidenceContext
    identity: IdentityContext
    profitability: ProfitabilityHistory
    findings: list[ContextFinding]
    guards_not_evaluated: list[str]
    limitations: list[str]
    scope: str = "Read-only inspection snapshot, not permission to resume, calculate, finalize, or approve."


class EvaluationContextResult(ContextModel):
    success: bool
    context: EvaluationContext | None = None
    error_code: str | None = None
    message: str


def reference(artifact):
    return ArtifactProvenance(artifact_type=artifact.artifact_type,
                             artifact_id=artifact.artifact_id, sequence_number=artifact.sequence_number)


def _metadata(artifact):
    value = json.loads(artifact.context_json) if artifact.context_json is not None else {}
    return value if isinstance(value, dict) else {}


def _parse_inventory(snapshot):
    validators = {
        EvaluationArtifactType.SOURCING_REPORT: SourcingTextReport,
        EvaluationArtifactType.SOURCING: SourcingResult,
        EvaluationArtifactType.RESALE: ResaleResult,
        EvaluationArtifactType.ACQUISITION_COST_FINDINGS: EconomicCostFindingsResult,
        EvaluationArtifactType.SELLING_COST_FINDINGS: EconomicCostFindingsResult,
        EvaluationArtifactType.PROFITABILITY: ProfitabilityToolResult,
        EvaluationArtifactType.IDENTITY_RESOLUTION: IdentityResolution,
        EvaluationArtifactType.EVALUATION_SELECTION: EvaluationSelectionEvent,
    }
    parsed, inventory, human_inputs = {}, [], []
    for artifact in snapshot.artifacts:
        try:
            metadata = _metadata(artifact)
            if artifact.artifact_type == EvaluationArtifactType.HUMAN_INPUT:
                value = json.loads(artifact.payload_json)
                if not isinstance(value, dict) or not isinstance(value.get("text"), str):
                    raise ValueError("Invalid human input")
                human_inputs.append(HumanInputContext(reference=reference(artifact), text=value["text"]))
            else:
                value = validators[artifact.artifact_type].model_validate_json(artifact.payload_json)
            parsed[artifact.artifact_id] = value
            sources = []
            if isinstance(value, EconomicCostFindingsResult):
                sources = [source for finding in value.findings for source in finding.source_references]
            elif isinstance(value, ResaleResult):
                sources = [SourceReference(url=item.url, description=item.listing_title) for item in value.evidence_items]
            elif isinstance(value, IdentityResolution):
                sources = value.supporting_evidence
            provenance = metadata.get("provenance")
            parent_refs = (provenance.get("artifact_references", []) if isinstance(provenance, dict) else [])
            kind = ("HUMAN-SUPPLIED" if artifact.artifact_type == EvaluationArtifactType.HUMAN_INPUT else
                    "APPLICATION-ENFORCED" if artifact.artifact_type in {
                        EvaluationArtifactType.IDENTITY_RESOLUTION, EvaluationArtifactType.EVALUATION_SELECTION} else
                    "DETERMINISTIC RESULT / RECORDED FAILURE" if isinstance(value, ProfitabilityToolResult) else
                    "AGENT-REPORTED")
            inventory.append(ArtifactInventoryItem(
                reference=reference(artifact), evaluation_id=artifact.evaluation_id,
                created_at=artifact.created_at, provenance_kind=kind,
                success=value.success if isinstance(value, ProfitabilityToolResult) else None,
                source_references=sources,
                existing_parent_references=[ArtifactProvenance.model_validate(item) for item in parent_refs],
            ))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise EvaluationContextRefused("INVALID_ARTIFACT", f"Artifact {artifact.artifact_id} is malformed; no record was repaired") from None
    return parsed, inventory, human_inputs


def _identity_value(identity, path):
    value = identity
    for name in path.split("."):
        value = getattr(value, name, None)
    return None if value == "UNKNOWN" or (isinstance(value, str) and not value.strip()) else value


def _identity_context(snapshot, parsed):
    candidate, evidence = snapshot.candidate, snapshot.evidence
    resale = parsed[evidence.resale.artifact_id] if evidence.resale else None
    decision, applicable = (assess_identity_with_resolution(candidate, snapshot.evaluation.evaluation_id, evidence, resale)
                            if resale else (None, ()))
    target = None
    if evidence.resale:
        recorded = _metadata(evidence.resale).get("authoritative_target")
        if recorded is not None:
            try:
                target = RecordedResaleTarget.model_validate(recorded)
            except (ValueError, TypeError):
                raise EvaluationContextRefused("INVALID_ARTIFACT", "Recorded Resale target metadata is malformed") from None
    target_matches = (target.candidate_id == candidate.candidate_id
                      and target.evaluation_id == snapshot.evaluation.evaluation_id
                      and target.origin == "active_persisted_candidate"
                      and target.product_identity == candidate.product_identity) if target else None
    one_sided, both_absent, other = [], [], []
    if decision:
        for path in decision.unresolved_fields:
            left, right = _identity_value(candidate.product_identity, path), _identity_value(resale.researched_identity, path)
            if left is None and right is None:
                both_absent.append(path)
            elif (left is None) != (right is None):
                one_sided.append(path)
            else:
                other.append(path)
    applicable_ids = {artifact.artifact_id for artifact in applicable}
    resolutions = [ResolutionContext(
        reference=reference(artifact), record=parsed[artifact.artifact_id],
        confirmation_applicable=(artifact.artifact_id in applicable_ids and parsed[artifact.artifact_id].status == "CONFIRMED"),
        applicability_reason=("Exact current binding recognized by existing rules" if artifact.artifact_id in applicable_ids
                              else "Not an applicable confirmation: proposed, rejected, stale, superseded, or untrusted binding"),
    ) for artifact in evidence.identity_resolutions]
    return IdentityContext(
        candidate_identity=candidate.product_identity, authoritative_resale_target=target,
        target_matches_current_candidate=target_matches,
        researched_identity=resale.researched_identity if resale else None, assessment=decision,
        one_sided_missing=one_sided, both_absent=both_absent, other_unresolved=other,
        nonblocking_unknowns=[path for path in decision.unresolved_fields if path not in decision.blocking_fields] if decision else [],
        resolutions=sorted(resolutions, key=lambda item: item.reference.sequence_number),
        resolution_summary=resolution_status(evidence),
    )


def _profitability_history(snapshot, parsed, selected, identity):
    attempts = []
    current = {(item.artifact_type, item.artifact_id, item.sequence_number) for item in selected}
    current_resolutions = {(item.reference.artifact_id, item.reference.sequence_number)
                           for item in identity.resolutions
                           if identity.assessment and item.reference.artifact_id in identity.assessment.resolution_artifact_ids}
    for artifact in snapshot.artifacts:
        if artifact.artifact_type != EvaluationArtifactType.PROFITABILITY:
            continue
        envelope, metadata = parsed[artifact.artifact_id], _metadata(artifact)
        provenance = metadata.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        try:
            refs = [ArtifactProvenance.model_validate(item) for item in provenance.get("artifact_references", [])]
            request = ProfitabilityRequest.model_validate(metadata["request"]) if metadata.get("request") is not None else None
            stored_result = ProfitabilityResult.model_validate(metadata["result"]) if metadata.get("result") is not None else None
        except (ValueError, TypeError):
            raise EvaluationContextRefused("INVALID_ARTIFACT", f"Profitability artifact {artifact.artifact_id} has malformed stored provenance") from None
        reasons = []
        if not envelope.success or envelope.result is None:
            state, reasons = Applicability.NOT_CURRENT, ["This is a failed attempt, not a successful calculation."]
        else:
            state = Applicability.UNDETERMINED
            for item in refs:
                if item.artifact_type in {EvaluationArtifactType.SOURCING_REPORT, EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
                                          EvaluationArtifactType.RESALE, EvaluationArtifactType.SELLING_COST_FINDINGS}:
                    if (item.artifact_type, item.artifact_id, item.sequence_number) not in current:
                        reasons.append("A recorded input artifact is not the selected current evidence.")
                elif (item.artifact_type == EvaluationArtifactType.IDENTITY_RESOLUTION
                      and (item.artifact_id, item.sequence_number) not in current_resolutions):
                    reasons.append("A recorded identity resolution is no longer applicable.")
            if request is not None and (request.acquisition.product_identity != snapshot.candidate.product_identity
                                        or request.sale.marketplace_name != snapshot.candidate.resale_destination.name):
                reasons.append("Stored request identity or destination differs from the authoritative scenario.")
            if stored_result is not None and stored_result != envelope.result:
                reasons.append("Stored context result conflicts with the result envelope.")
            if reasons:
                state = Applicability.NOT_CURRENT
            else:
                reasons = ["Historical success is preserved, but no implemented complete freshness/applicability proof exists; matching or absent references do not certify current success."]
        failures = metadata.get("assembly_failures", [])
        categories = [item["code"] for item in failures if isinstance(item, dict) and isinstance(item.get("code"), str)] if isinstance(failures, list) else []
        attempts.append(ProfitabilityAttempt(
            reference=reference(artifact), result_envelope=envelope,
            recorded_failure_stage=metadata.get("failure_stage") if isinstance(metadata.get("failure_stage"), str) else None,
            recorded_error_type=metadata.get("error_type") if isinstance(metadata.get("error_type"), str) else None,
            recorded_failure_categories=categories, stored_request=request, stored_context_result=stored_result,
            recorded_input_references=refs, applicability=state, applicability_reasons=reasons,
        ))
    successes = [item for item in attempts if item.result_envelope.success and item.result_envelope.result is not None]
    aggregate = Applicability.UNDETERMINED if any(item.applicability == Applicability.UNDETERMINED for item in successes) else Applicability.NOT_CURRENT
    cache = snapshot.evaluation.profitability_result
    return ProfitabilityHistory(
        attempts=attempts, historical_success_exists=bool(successes), current_success_applicability=aggregate,
        applicability_reason="Inspection only: known provenance differences are reported; no new eligibility or finalization freshness policy is applied.",
        persisted_result_cache=cache,
        cache_matching_attempt_ids=[item.reference.artifact_id for item in successes if cache is not None and item.result_envelope.result == cache],
    )


def build_evaluation_context(snapshot: EvaluationContextSnapshot) -> EvaluationContext:
    """Build only from the already-materialized read transaction; never reload."""
    parsed, inventory, humans = _parse_inventory(snapshot)
    evidence = snapshot.evidence
    artifacts = [item for item in (evidence.sourcing_report, evidence.acquisition_cost_findings,
                                  evidence.resale, evidence.selling_cost_findings) if item is not None]
    selected = [reference(item) for item in artifacts]
    identity = _identity_context(snapshot, parsed)
    history = _profitability_history(snapshot, parsed, selected, identity)
    findings, not_evaluated = [], []

    def add(category, state, reason, refs=()):
        findings.append(ContextFinding(category=category, state=state, reason=reason, artifact_references=list(refs)))

    for research, capture in ((evidence.sourcing_report, evidence.acquisition_cost_findings),
                              (evidence.resale, evidence.selling_cost_findings)):
        if research is None:
            add("MISSING_RESEARCH", BlockerState.ESTABLISHED_BLOCKER, "A required current research artifact is absent.")
        if capture is None:
            add("MISSING_CAPTURE", BlockerState.ESTABLISHED_BLOCKER, "A required economic capture is absent.")
        elif research is not None and capture.sequence_number <= research.sequence_number:
            add("STALE_CAPTURE", BlockerState.ESTABLISHED_BLOCKER, "Capture does not follow the latest research under existing sequence rules.", [reference(research), reference(capture)])
    decision = identity.assessment
    if decision is not None and not decision.accepted:
        remaining = [name for name in decision.conflicting_fields if name not in decision.human_confirmed_fields]
        if remaining:
            add("CONDITION_CONFLICT" if "condition" in remaining else "IDENTITY_CONFLICT",
                BlockerState.ESTABLISHED_BLOCKER, "Unresolved contradictory fields: " + ", ".join(remaining), selected)
        missing = [name for name in decision.blocking_fields if name not in decision.conflicting_fields]
        if missing:
            add("INSUFFICIENT_IDENTITY_EVIDENCE", BlockerState.ESTABLISHED_BLOCKER,
                "Blocking evidence gaps/assessment: " + ", ".join(missing), selected)
    if identity.target_matches_current_candidate is False:
        add("RECORD_MISMATCH", BlockerState.ESTABLISHED_BLOCKER, "Recorded Resale target differs from the selected Candidate/Evaluation.", selected)
    acquisition = parsed[evidence.acquisition_cost_findings.artifact_id] if evidence.acquisition_cost_findings else None
    selling = parsed[evidence.selling_cost_findings.artifact_id] if evidence.selling_cost_findings else None
    purchase = [finding for finding in acquisition.findings if finding.cost_id == "acquisition_purchase_price"] if acquisition else []
    currency = purchase[0].currency if len(purchase) == 1 else None
    can_check = (evidence.economically_ready and decision is not None and decision.accepted
                 and identity.target_matches_current_candidate is not False and currency is not None
                 and currency == parsed[evidence.resale.artifact_id].currency)
    if can_check:
        shipping = validate_outbound_shipping(selling, currency)
        if not shipping.valid:
            add("SHIPPING_COVERAGE", BlockerState.ESTABLISHED_BLOCKER, "Independent inspection-time existing shipping policy: " + shipping.reason, selected)
            not_evaluated.append("MARKETPLACE_FEE_SCOPE")
        else:
            fee = validate_marketplace_fee_scope(selling)
            if not fee.valid:
                add(fee.category, BlockerState.ESTABLISHED_BLOCKER, "Independent inspection-time existing fee policy: " + fee.reason, selected)
    else:
        not_evaluated.extend(["SHIPPING_COVERAGE", "MARKETPLACE_FEE_SCOPE"])
    for artifact, capture in ((evidence.acquisition_cost_findings, acquisition), (evidence.selling_cost_findings, selling)):
        if capture:
            unknowns = [finding.cost_id for finding in capture.findings if finding.unresolved_materiality == "MATERIAL"]
            if unknowns:
                add("MATERIAL_ECONOMIC_UNKNOWN", BlockerState.POTENTIAL_BLOCKER,
                    "Captured MATERIAL unknowns; arithmetic was not run: " + ", ".join(unknowns), [reference(artifact)])
    add("NO_APPLICABLE_PROFITABILITY_RESULT", BlockerState.POTENTIAL_BLOCKER if history.current_success_applicability == Applicability.UNDETERMINED else BlockerState.ESTABLISHED_BLOCKER,
        "No currently established calculation; historical cache/success is not current approval.")
    not_evaluated.extend(["PROFITABILITY_REQUEST_ASSEMBLY", "PROFITABILITY_ARITHMETIC", "FINALIZATION"])
    for guard in not_evaluated:
        add(guard, BlockerState.NOT_EVALUATED, "Not executed by inspection; no hypothetical downstream success is assumed.")
    return EvaluationContext(
        candidate=snapshot.candidate, evaluation=snapshot.evaluation, human_inputs=humans,
        artifact_inventory=inventory,
        evidence=EvidenceContext(selected=selected, acquisition_ready=evidence.acquisition_ready, selling_ready=evidence.selling_ready,
            sourcing_report=parsed[evidence.sourcing_report.artifact_id] if evidence.sourcing_report else None,
            resale_result=parsed[evidence.resale.artifact_id] if evidence.resale else None,
            acquisition_findings=acquisition, selling_findings=selling),
        identity=identity, profitability=history, findings=findings, guards_not_evaluated=not_evaluated,
        limitations=[
            "All rule assessments are new inspection-time assessments, not reinterpretations of historical provenance.",
            "Agent research and human assumptions are not independent verification of physical inventory, condition, packaging, availability, or prices.",
            "Capture ordering does not prove exact parent binding. External evidence age is not validated.",
            "Historical profitability success/cache is not evidence of current applicability; CURRENTLY_ESTABLISHED is not certified in this checkpoint.",
            "No context was injected into a Manager prompt and no continuation was authorized.",
        ] + (["Historical Resale target metadata is unavailable."] if identity.authoritative_resale_target is None else []),
    )


def inspect_selected_context(workflow) -> EvaluationContextResult:
    pair = (workflow.active.candidate_id, workflow.active.evaluation_id)
    if not workflow.active.is_active or workflow._context_selection != pair:
        return EvaluationContextResult(success=False, error_code="NO_SELECTED_EVALUATION", message="Explicit human Evaluation selection is required in this process.")
    try:
        snapshot = workflow.repository.evaluation_context_snapshot(*pair)
        context = build_evaluation_context(snapshot)
        return EvaluationContextResult(success=True, context=context, message="Inspection snapshot only; no work or writes performed.")
    except EvaluationContextRefused as error:
        return EvaluationContextResult(success=False, error_code=error.code, message=str(error))
    except (ValueError, TypeError, KeyError, AttributeError, PersistenceDataError):
        return EvaluationContextResult(success=False, error_code="INVALID_PERSISTED_DATA", message="Persisted context is malformed; no record was repaired.")
    except sqlite3.Error:
        return EvaluationContextResult(success=False, error_code="SNAPSHOT_UNAVAILABLE", message="A consistent read snapshot could not be obtained; no retry or write performed.")


def render_evaluation_context(context: EvaluationContext) -> str:
    """Bounded terminal summary; no raw JSON, tool arguments, or error payload dumps."""
    def short(value):
        return " ".join(str(value).split())[:160]

    c, e, identity, history = context.candidate, context.evaluation, context.identity, context.profitability
    provenance = e.intake_snapshot.provenance if e.intake_snapshot else None
    lines = ["EVALUATION CONTEXT — inspection only; no continuation authorized",
             f"Candidate {c.candidate_id} [{c.lifecycle_status.value}]",
             f"Evaluation {e.evaluation_id} [{e.status.value}]; completed: {e.completed_at}",
             f"HUMAN-SUPPLIED scenario: {short(c.product_identity.brand)} {short(c.product_identity.product_name)} / {short(c.product_identity.model_number)}; {short(c.product_identity.variant)}",
             f"Source: {short(c.acquisition_source.name)}; destination: {short(c.resale_destination.name)}",
             f"Intake: {c.intake_origin.value}; opportunity: {provenance.opportunity_status.value if provenance else 'UNKNOWN / UNVERIFIED'}",
             f"Assumptions: {len(e.assumptions)}; explicit uncertainties: {len(e.uncertainties)}; human-input artifacts: {len(context.human_inputs)}",
             "HUMAN-SUPPLIED recorded assumptions: " + short(e.assumptions),
             "UNKNOWN / UNVERIFIED recorded uncertainties: " + short(e.uncertainties),
             "AGENT-REPORTED current research/captures: " + "; ".join(f"{item.artifact_type.value} #{item.artifact_id}/s{item.sequence_number}" for item in context.evidence.selected),
             f"APPLICATION-ENFORCED sequence readiness: acquisition={context.evidence.acquisition_ready}, selling={context.evidence.selling_ready}; exact parentage not proven.",
             "DETERMINISTICALLY VALIDATED identity (inspection-time): " + (identity.assessment.comparison_outcome.value if identity.assessment else "NOT EVALUATED"),
             "Conflicts: " + short(identity.assessment.conflicting_fields if identity.assessment else []),
             "One-sided evidence gaps: " + short(identity.one_sided_missing),
             "Both-absent unknowns: " + short(identity.both_absent),
             "Other blocking fields: " + short(identity.assessment.blocking_fields if identity.assessment else []),
             "Identity resolution: " + short(identity.resolution_summary),
             "Applicable human-confirmed fields: " + short(identity.assessment.human_confirmed_fields if identity.assessment else []),
             f"Profitability attempts: {len(history.attempts)}; historical_success_exists={history.historical_success_exists}; cache_present={history.persisted_result_cache is not None}"]
    for attempt in history.attempts[-5:]:
        lines.append(f"  #{attempt.reference.artifact_id}/s{attempt.reference.sequence_number}: success={attempt.result_envelope.success}; stage={short(attempt.recorded_failure_stage)}; categories={short(attempt.recorded_failure_categories)}; applicability={attempt.applicability.value}")
    lines.append("Current-success applicability: " + history.current_success_applicability.value)
    for finding in [item for item in context.findings if item.state != BlockerState.NOT_EVALUATED][:10]:
        lines.append(f"{finding.state.value}: {finding.category}: {short(finding.reason)}")
    lines.extend(["NOT EVALUATED: " + ", ".join(context.guards_not_evaluated),
                  "UNKNOWN / UNVERIFIED: physical facts and external freshness; recorded sources/assumptions are not proof.",
                  "Complete typed context retains inventory, provenance, attempts, and limitations; no raw artifact dump shown."])
    return "\n".join(lines)


def handle_evaluation_context_command(workflow, text: str) -> EvaluationContextResult | None:
    if not text.lstrip().startswith("/evaluation context"):
        return None
    if text != "/evaluation context":
        return EvaluationContextResult(success=False, error_code="INVALID_COMMAND", message="Use exactly /evaluation context with no arguments.")
    return workflow.inspect_evaluation_context()
