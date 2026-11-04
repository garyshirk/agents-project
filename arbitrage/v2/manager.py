"""Typed offline Manager decisions. Authority and transaction ownership stay in the application."""

import json
from hashlib import sha256
from uuid import uuid4

from pydantic import BaseModel

from arbitrage.contracts import CostType
from arbitrage.v2.contracts import (
    ArtifactRef, ArtifactType, AssumptionProposal, AssumptionRecord, ComparableInterpretation,
    EconomicAssumption, ExecutionState, KnowledgeSource, ManagerApplication, ManagerContextLimits,
    ManagerDecision, ManagerProposal, ManagerSnapshot, OfflineFixture, ResearchRequest,
    ScenarioProposal, SummaryProposal, SupportingRecord, WorkflowStep,
)
from arbitrage.v2.economics import assemble_scenario
from arbitrage.v2.persistence import V2PersistenceError, _now


class StaleManagerContext(V2PersistenceError):
    pass


def _walk(value, kind):
    if isinstance(value,kind):
        yield value
    if isinstance(value,BaseModel):
        for name in type(value).model_fields:
            yield from _walk(getattr(value,name),kind)
    elif isinstance(value,(tuple,list)):
        for item in value:
            yield from _walk(item,kind)


def read_context(artifact) -> ManagerSnapshot:
    if artifact.artifact_type != ArtifactType.MANAGER_CONTEXT:
        raise V2PersistenceError("Expected an exact application-owned Manager context artifact")
    payload = artifact.payload
    if sha256(payload.snapshot_json.encode()).hexdigest() != payload.snapshot_sha256:
        raise V2PersistenceError("Manager context snapshot hash mismatch")
    snapshot = ManagerSnapshot.model_validate_json(payload.snapshot_json)
    if (snapshot.evaluation.evaluation_id != payload.evaluation_id
            or snapshot.opportunity.opportunity_id != payload.opportunity_id
            or snapshot.evaluation.opportunity_id != payload.opportunity_id
            or snapshot.evaluation.current_review_subject != payload.review_subject
            or snapshot.evaluation.current_summary != payload.publication_target
            or tuple(a.reference for a in snapshot.artifacts) != payload.selected_references
            or tuple(a.reference for a in snapshot.historical_artifacts) != payload.historical_references):
        raise V2PersistenceError("Manager context manifest and snapshot disagree")
    return snapshot


def _reject_float(value):
    raise ValueError("Manager numerical inputs must use decimal text, not JSON floating-point values")


def parse_proposal(encoded):
    return ManagerProposal.model_validate(json.loads(encoded,parse_float=_reject_float,parse_constant=_reject_float))


def authorized_evidence_references(context):
    """Only explicit application selection grants citation authority; visibility alone does not."""
    return tuple(context.selected_references)


def _validate_evidence_citations(proposal, context):
    authorized=set(authorized_evidence_references(context))
    for value in _walk(proposal,BaseModel):
        for reference in getattr(value,"evidence_references",()):
            if reference not in authorized:
                raise V2PersistenceError("Economic evidence citation is not in the authorized evidence-reference list")
    for support in _walk(proposal,SupportingRecord):
        permitted=context.historical_references if support.source==KnowledgeSource.HISTORICAL_EVALUATIONS else authorized
        if support.artifact not in permitted:
            raise V2PersistenceError("Provenance support must use an explicitly authorized evidence/historical reference")


def _validate_summary(intent, snapshot):
    artifacts = {a.reference:a for a in snapshot.artifacts}
    disclosed_assumptions = set(intent.explanation.assumption_references)
    disclosed_comparables = {item.comparable for item in intent.explanation.comparables}
    for ref in disclosed_assumptions:
        if ref not in artifacts or artifacts[ref].artifact_type != ArtifactType.SCENARIO_ASSUMPTION:
            raise V2PersistenceError("Summary assumption disclosure must identify an actual supplied assumption")
        if artifacts[ref].payload.assumption not in intent.summary.assumptions:
            raise V2PersistenceError("Summary must preserve the exact disclosed assumption value/unit/provenance")
    for ref in disclosed_comparables:
        if ref not in artifacts or artifacts[ref].artifact_type != ArtifactType.RESALE_COMPARABLE:
            raise V2PersistenceError("Summary comparable explanation requires exact supplied comparable revisions")
    for ref in intent.summary.calculation_references:
        artifact = artifacts.get(ref)
        if artifact is None or artifact.artifact_type != ArtifactType.CALCULATION:
            raise V2PersistenceError("Summary must cite actual supplied calculation artifacts")
        calc = artifact.payload
        scenario = artifacts.get(calc.scenario)
        if scenario is None or scenario.artifact_type != ArtifactType.ECONOMIC_SCENARIO or scenario.payload != calc.scenario_manifest:
            raise V2PersistenceError("Summary context must include the exact calculated scenario manifest")
        if not set(calc.assumption_references).issubset(disclosed_assumptions):
            raise V2PersistenceError("Summary must disclose all calculation assumptions without relabeling them")
        if not set(calc.scenario_manifest.comparable_references).issubset(disclosed_comparables):
            raise V2PersistenceError("Summary must explain the explicitly used comparable revisions")
        inputs = calc.scenario_manifest.inputs
        unresolved = {i.input_id for i in inputs if i.disposition == "UNKNOWN" and i.material}
        assumed = {i.input_id for i in inputs if i.disposition == "ASSUMPTION"}
        unresolved.update(i.name for i in calc.scenario_manifest.known_unknowns if i.name not in assumed)
        disclosed = {i.name for i in (*intent.summary.unknown_economic_inputs,*intent.explanation.uncertain_costs)}
        if not unresolved.issubset(disclosed):
            raise V2PersistenceError("Summary must disclose the calculation's named unresolved material inputs")


def apply_manager_decision(repo, connection, context_ref, encoded, producer):
    """Called only inside the repository's composed write transaction."""
    context_artifact = repo._typed_reference(connection,context_ref,context_ref.evaluation_id,{ArtifactType.MANAGER_CONTEXT})
    snapshot = read_context(context_artifact)
    context = context_artifact.payload
    proposal = parse_proposal(encoded)
    if (proposal.context != context_ref or proposal.evaluation_id != context.evaluation_id
            or proposal.opportunity_id != context.opportunity_id):
        raise V2PersistenceError("Manager proposal cannot substitute Evaluation, Opportunity, or context")
    current = repo._evaluation(connection,context.evaluation_id)
    if current.execution_state != ExecutionState.RUNNING:
        raise StaleManagerContext("Manager publication requires a nonterminal Evaluation")
    if current.current_review_subject != context.review_subject or current.current_summary != context.publication_target:
        raise StaleManagerContext("Manager context/publication target is stale; build a fresh context and explicitly revise")
    _validate_evidence_citations(proposal,context)
    allowed = set(context.selected_references) | {context.review_subject}
    if context.publication_target:
        allowed.add(context.publication_target)
    historical = set(context.historical_references)
    historical_support = {r.artifact for r in _walk(proposal,SupportingRecord)
                          if r.source == KnowledgeSource.HISTORICAL_EVALUATIONS}
    for ref in _walk(proposal,ArtifactRef):
        if ref == context_ref:
            continue
        if ref not in allowed and ref not in historical:
            raise V2PersistenceError("Manager decision references data not supplied in its exact context")
        if ref in historical and ref not in historical_support:
            raise V2PersistenceError("Foreign Evaluation records are permitted only as explicitly supplied historical support")
    # A supplied historical record cannot also be smuggled into direct evidence/context fields.
    direct_refs = (*proposal.evidence_references,)
    if any(r.evaluation_id != context.evaluation_id for r in direct_refs):
        raise V2PersistenceError("Direct decision evidence must belong to this Evaluation")
    intent = proposal.intent
    if isinstance(intent,AssumptionProposal):
        amount = intent.assumption
        parts = amount.unit.split("/")
        if (len(parts) != 2 or len(parts[0]) != 3 or not parts[0].isascii()
                or not parts[0].isalpha() or not parts[0].isupper() or parts[1] not in {c.value for c in CostType}):
            raise V2PersistenceError("Assumption unit must be explicit CURRENCY/CostType, matching scenario semantics")
        for source in (KnowledgeSource.CURRENT_EVALUATION_EVIDENCE,KnowledgeSource.HISTORICAL_EVALUATIONS):
            if source in amount.provenance.sources and not any(s.source == source for s in amount.provenance.supporting_records):
                raise V2PersistenceError("Evidence/historical assumption support requires exact supplied records")
    elif isinstance(intent,ScenarioProposal):
        assembly = assemble_scenario(intent.scenario)
        if assembly.status == "INVALID_INPUTS":
            raise V2PersistenceError("Invalid scenario proposal: " + "; ".join((*assembly.validation_errors,*assembly.unsupported_mappings)))
    elif isinstance(intent,SummaryProposal):
        if intent.expected_summary != context.publication_target:
            raise StaleManagerContext("Summary proposal must identify the exact intended publication target")
        _validate_summary(intent,snapshot)
    old = repo._typed_reference(connection,proposal.supersedes,context.evaluation_id,{ArtifactType.MANAGER_DECISION}) if proposal.supersedes else None
    if old and repo._current_lineage(connection,context.evaluation_id,old.payload.decision_id,
            ArtifactType.MANAGER_DECISION,"decision_id").reference != old.reference:
        raise V2PersistenceError("Manager revision requires its latest exact predecessor")
    if old and old.payload.proposal.intent.kind != intent.kind:
        raise V2PersistenceError("A different decision purpose requires a new lineage, not a revision")
    now = _now()
    payload = ManagerDecision(decision_id=old.payload.decision_id if old else str(uuid4()),
        revision=old.payload.revision+1 if old else 1,supersedes=proposal.supersedes,proposal=proposal,created_at=now)
    decision = repo._insert_artifact(connection,context.evaluation_id,ArtifactType.MANAGER_DECISION,payload,producer,
        tuple(dict.fromkeys((context_ref,*proposal.evidence_references,*((proposal.supersedes,) if old else ())))),created_at=now)
    options = dict(producer=producer,operation_key="manager-domain:" + decision.reference.artifact_id)
    outcome = None
    if isinstance(intent,ResearchRequest):
        # The decision itself is the durable request. It has no execution capability.
        repo._partial_subject(connection,context.evaluation_id,"New authored Manager research request; not executed.",(decision.reference,))
    elif isinstance(intent,ComparableInterpretation):
        outcome = repo.record_assessment(context.evaluation_id,intent.comparable,intent.assessment,
            predecessor=intent.predecessor,parents=(decision.reference,),**options)
    elif isinstance(intent,AssumptionProposal):
        outcome = repo.record_artifact(context.evaluation_id,AssumptionRecord(assumption=intent.assumption),
            parents=(decision.reference,),**options)
    elif isinstance(intent,ScenarioProposal):
        outcome = repo.record_scenario(context.evaluation_id,intent.scenario,predecessor=intent.predecessor,**options)
    elif isinstance(intent,SummaryProposal):
        # Link application provenance without changing any authored economic field.
        draft = intent.summary.model_copy(update={"evidence_references":tuple(dict.fromkeys(
            (*intent.summary.evidence_references,decision.reference)))})
        outcome = repo.record_summary(context.evaluation_id,draft,**options)
    now = _now()
    result = ManagerApplication(decision=decision.reference,outcome=outcome.reference if outcome else None,created_at=now)
    return repo._insert_artifact(connection,context.evaluation_id,ArtifactType.MANAGER_APPLICATION,result,producer,
        (decision.reference,*((outcome.reference,) if outcome else ())),created_at=now)


class OfflineManager:
    """Trusted application facade. Fixture/model data never receives this repository authority."""
    def __init__(self, repository):
        self.repository = repository

    def build_context(self, evaluation_id, references, *, limits: ManagerContextLimits,
                      historical_references=(), selection_limitations=(), operation_key):
        return self.repository.record_manager_context(evaluation_id,references,limits=limits,
            historical_references=historical_references,selection_limitations=selection_limitations,operation_key=operation_key)

    def submit(self, context: ArtifactRef, proposal: ManagerProposal | str, *, step_id, operation_key, retry_of=None):
        from arbitrage.v2.workflow import ManagerOutput, OfflineResponse, OfflineWorkflow
        context_artifact = self.repository.get_artifact(context)
        read_context(context_artifact)
        encoded = proposal.model_dump_json() if isinstance(proposal,ManagerProposal) else proposal
        response = OfflineResponse(opportunity_id=context.opportunity_id,evaluation_id=context.evaluation_id,
            output=ManagerOutput(role="MANAGER",context=context,proposal_json=encoded))
        step = WorkflowStep(step_id=step_id,role="MANAGER",context_references=(context,),
            context_text="Explicit synthetic offline Manager decision against an immutable context.",
            fixture=OfflineFixture(fixture_id="synthetic-manager-" + step_id,response_json=response.model_dump_json()),retry_of=retry_of)
        return OfflineWorkflow(self.repository).run(context.evaluation_id,(step,),operation_key=operation_key)
