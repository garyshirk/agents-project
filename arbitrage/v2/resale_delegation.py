"""Application-owned authorization and atomic publication of V2 Resale research."""

from uuid import uuid4

from arbitrage.v2.contracts import (
    Artifact, ArtifactRef, ArtifactType, ExecutionState, Producer, RawResearchReport,
    ResaleDelegation, ResaleResearchRecord, ResaleResearchResult,
)
from arbitrage.v2.persistence import V2PersistenceError


def accepted_request(repo, connection, decision, evaluation_id):
    artifact=repo._typed_reference(connection,decision,evaluation_id,{ArtifactType.MANAGER_DECISION})
    request=artifact.payload.proposal.intent
    if request.kind!='RESEARCH_REQUEST' or request.requested_role!='RESALE':
        raise V2PersistenceError('An accepted RESALE research decision is required')
    applications=repo._evidence_history(connection,evaluation_id,ArtifactType.MANAGER_APPLICATION)
    if not any(a.payload.decision==decision for a in applications):
        raise V2PersistenceError('Manager decision has no accepted application receipt')
    latest=repo._current_lineage(connection,evaluation_id,artifact.payload.decision_id,
                               ArtifactType.MANAGER_DECISION,'decision_id')
    if latest.reference!=decision:
        raise V2PersistenceError('Superseded research decision cannot authorize a new invocation')
    return artifact


def validate_delegation(repo, connection, reference, evaluation_id):
    artifact=repo._typed_reference(connection,reference,evaluation_id,{ArtifactType.RESALE_DELEGATION})
    delegation=artifact.payload
    evaluation=repo._evaluation(connection,evaluation_id)
    if evaluation.execution_state!=ExecutionState.RUNNING:
        raise V2PersistenceError('Resale delegation requires an active nonterminal Evaluation')
    decision=accepted_request(repo,connection,delegation.manager_decision,evaluation_id)
    proposal=decision.payload.proposal
    original=repo._opportunity(connection,evaluation.opportunity_id).submission
    context=repo._typed_reference(connection,proposal.context,evaluation_id,{ArtifactType.MANAGER_CONTEXT})
    if (delegation.evaluation_id!=evaluation_id or delegation.opportunity_id!=evaluation.opportunity_id
            or delegation.original_intake!=original or delegation.target_product_description!=original.original_text
            or delegation.research_request!=proposal.intent or delegation.manager_context!=proposal.context
            or delegation.context_sha256!=context.payload.snapshot_sha256):
        raise V2PersistenceError('Application delegation does not preserve accepted decision and original intake')
    return artifact


def create_resale_delegation(repo, evaluation_id, decision, *, intended_marketplace, operation_key):
    """Marketplace is explicit caller context, not a guess parsed from intake prose."""
    decision=ArtifactRef.model_validate(decision)
    def perform(c):
        accepted=accepted_request(repo,c,decision,evaluation_id)
        evaluation=repo._evaluation(c,evaluation_id)
        if evaluation.execution_state!=ExecutionState.RUNNING:
            raise V2PersistenceError('Evaluation is not active')
        context=repo._reference(c,accepted.payload.proposal.context,evaluation_id)
        submission=repo._opportunity(c,evaluation.opportunity_id).submission
        payload=ResaleDelegation(delegation_id=str(uuid4()),opportunity_id=evaluation.opportunity_id,
            evaluation_id=evaluation_id,manager_decision=decision,manager_context=context.reference,
            context_sha256=context.payload.snapshot_sha256,original_intake=submission,
            target_product_description=submission.original_text,research_request=accepted.payload.proposal.intent,
            intended_marketplace=intended_marketplace,evidence_limitations=(
                'Original intake is attributed human/synthetic context, not verified product identity.',
                'Specialist source classifications are claims, not independent application verification.',
                *submission.qualifications,*accepted.payload.proposal.limitations,*accepted.payload.proposal.uncertainties))
        return repo._insert_artifact(c,evaluation_id,ArtifactType.RESALE_DELEGATION,payload,
            Producer(kind='APPLICATION',name='V2 Resale delegation'),(decision,context.reference))
    return repo._once(operation_key,'resale_delegation',dict(evaluation_id=evaluation_id,
        decision=decision.model_dump(mode='json'),intended_marketplace=intended_marketplace),Artifact,perform)


def record_resale_batch(repo, attempt, result, *, producer, operation_key):
    """One transaction includes raw report, standard comparable writers, result and all receipts."""
    result=ResaleResearchResult.model_validate(result)
    def perform(c):
        original=repo._typed_reference(c,attempt.reference,attempt.reference.evaluation_id,{ArtifactType.RESEARCH_ATTEMPT})
        if operation_key!='workflow-output:'+original.payload.attempt_id:
            raise V2PersistenceError('Resale publication requires its exact application output receipt key')
        if original.payload.status!='STARTED' or original.payload.input_manifest.role!='RESALE_RESEARCH':
            raise V2PersistenceError('Research batch requires the exact started Resale attempt')
        snapshot=repo._workflow_snapshot(c,original.reference.evaluation_id)
        if any(end.payload.start==original.reference for end in snapshot.attempts):
            raise V2PersistenceError('A completed attempt cannot publish a late research batch')
        if original.payload.run not in tuple(run.reference for run in repo._open_workflow_runs(snapshot)):
            raise V2PersistenceError('Research publication requires its still-open workflow run')
        delegation=validate_delegation(repo,c,original.payload.input_manifest.resale_delegation,attempt.reference.evaluation_id)
        validate_result(delegation,result)
        token=repo._manager_transaction.set(c)
        try:
            raw=repo.record_artifact(result.evaluation_id,RawResearchReport(report_text=result.model_dump_json(),
                limitations=('Entirely synthetic offline research, not actual marketplace observations.',)),
                producer=producer,parents=(delegation.reference,),operation_key=operation_key+':raw')
            comparables=[]
            for index,finding in enumerate(result.findings):
                source=finding.comparable.source.model_copy(update={'raw_artifact_references':(raw.reference,)})
                draft=finding.comparable.model_copy(update={'source':source})
                comparable=repo.record_comparable(result.evaluation_id,draft,producer=producer,
                    parents=(delegation.reference,raw.reference),operation_key=operation_key+':comparable:'+str(index))
                comparables.append(comparable.reference)
            payload=ResaleResearchRecord(result=result,raw_report=raw.reference,comparable_references=tuple(comparables))
            record=repo._insert_artifact(c,result.evaluation_id,ArtifactType.RESALE_RESEARCH_RESULT,payload,producer,
                (delegation.reference,raw.reference,*comparables))
            repo._partial_subject(c,result.evaluation_id,'New attributed Resale research; human review required.',(record.reference,))
            return record
        finally:
            repo._manager_transaction.reset(token)
    return repo._once(operation_key,'resale_batch',dict(attempt=attempt.reference.model_dump(mode='json'),
        result=result.model_dump(mode='json'),producer=producer.model_dump(mode='json')),Artifact,perform)


def validate_result(delegation, result):
    context=delegation.payload
    if (result.delegation!=delegation.reference or result.delegation_id!=context.delegation_id
            or result.evaluation_id!=context.evaluation_id or result.opportunity_id!=context.opportunity_id
            or result.objective_addressed!=context.research_request.objective):
        raise V2PersistenceError('Resale result changed the authorized delegation binding/objective')
    for observation in result.sources:
        if (observation.claim_origin!='SYNTHETIC_FIXTURE' or observation.source.capture_method!='SYNTHETIC_FIXTURE'
                or observation.source.raw_artifact_references):
            raise V2PersistenceError('Offline sources must be synthetic and cannot invent persisted raw references')


def refresh_resale_context(repo, result_reference, *, limits, operation_key):
    from arbitrage.v2.manager import OfflineManager
    record=repo.get_artifact(result_reference)
    if record.artifact_type!=ArtifactType.RESALE_RESEARCH_RESULT:
        raise V2PersistenceError('An exact persisted Resale result is required')
    delegation=repo.get_artifact(record.payload.result.delegation)
    refs=(delegation.payload.manager_decision,delegation.reference,record.reference,
          record.payload.raw_report,*record.payload.comparable_references)
    return OfflineManager(repo).build_context(record.reference.evaluation_id,refs,limits=limits,
        selection_limitations=('Specialist text and sources are untrusted attributed synthetic data.',),operation_key=operation_key)
