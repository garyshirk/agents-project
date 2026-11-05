"""Durable provider intent, budgets and immutable audit events in the existing V2 artifact store."""

from uuid import uuid4

from arbitrage.v2.contracts import Artifact, ArtifactType, Producer, ResaleProviderAudit
from arbitrage.v2.persistence import V2PersistenceError, _now
from arbitrage.v2.resale_delegation import validate_delegation


AUDIT_PRODUCER=Producer(kind='APPLICATION',name='V2 bounded Resale provider boundary')


def history(repo, evaluation_id):
    with repo._connection() as c:
        return repo._evidence_history(c,evaluation_id,ArtifactType.RESALE_PROVIDER_CALL)


def start_call(repo, attempt, prepared, *, live_authorized, offline_client, acknowledge_ambiguous_retry):
    config=attempt.payload.input_manifest.resale_config
    def perform(c):
        original=repo._typed_reference(c,attempt.reference,attempt.reference.evaluation_id,{ArtifactType.RESEARCH_ATTEMPT})
        snapshot=repo._workflow_snapshot(c,attempt.reference.evaluation_id)
        if (original.payload.status!='STARTED' or original.payload.input_manifest.role!='RESALE_RESEARCH'
                or any(a.payload.start==original.reference for a in snapshot.attempts)
                or original.payload.run not in tuple(a.reference for a in repo._open_workflow_runs(snapshot))):
            raise V2PersistenceError('Provider requires an open, durably started Resale attempt')
        delegation=validate_delegation(repo,c,original.payload.input_manifest.resale_delegation,original.reference.evaluation_id)
        if original.payload.input_manifest.resale_config!=config:
            raise V2PersistenceError('Provider configuration differs from durable attempt')
        if not offline_client and not live_authorized:
            raise V2PersistenceError('Explicit live research authorization required')
        records=repo._evidence_history(c,attempt.reference.evaluation_id,ArtifactType.RESALE_PROVIDER_CALL)
        starts=[a for a in records if a.payload.stage=='STARTED']
        if starts and starts[0].payload.config!=config:
            raise V2PersistenceError('Evaluation provider configuration/budget ceilings cannot change')
        if len(starts)>=config.max_provider_invocations:
            raise V2PersistenceError('Provider-call budget exhausted')
        if len({a.payload.research_pass for a in starts})>=config.max_research_passes:
            raise V2PersistenceError('Research-pass budget exhausted')
        if any(a.payload.research_pass==original.reference for a in starts):
            raise V2PersistenceError('Strategy A permits exactly one provider call per research pass')
        if starts:
            latest=[a for a in records if a.payload.call_id==starts[-1].payload.call_id][-1]
            if (latest.payload.stage!='FINISHED' or latest.payload.ambiguous) and not acknowledge_ambiguous_retry:
                raise V2PersistenceError('Explicit acknowledgment required after ambiguous provider completion')
        used=0
        for first in starts:
            observed=[]
            for record in records:
                usage=record.payload.usage
                if record.payload.call_id==first.payload.call_id and usage:
                    total=usage.total_tokens
                    if total is None and usage.input_tokens is not None and usage.output_tokens is not None:
                        total=usage.input_tokens+usage.output_tokens
                    if total is not None:observed.append(total)
            used+=max((first.payload.reserved_tokens,*observed))
        if used+prepared.reserved_tokens>config.max_total_tokens:
            raise V2PersistenceError('Conservative provider token reservation budget exhausted')
        now=_now()
        payload=ResaleProviderAudit(stage='STARTED',call_id=str(uuid4()),call_sequence=len(starts)+1,
            research_pass=original.reference,delegation=delegation.reference,start=None,config=config,
            prompt_version=prepared.prompt_version,schema_version_name=prepared.schema_version_name,
            input_sha256=prepared.input_sha256,context_sha256=delegation.payload.context_sha256,
            request_sha256=prepared.request_sha256,tool_configuration_json=prepared.tools_json,
            reserved_tokens=prepared.reserved_tokens,live_authorized=live_authorized,offline_client=offline_client,
            started_at=now,recorded_at=now,status='INTENDED')
        return repo._insert_artifact(c,original.reference.evaluation_id,ArtifactType.RESALE_PROVIDER_CALL,
            payload,AUDIT_PRODUCER,(original.reference,delegation.reference),created_at=now)
    return repo._once('resale-provider-start:'+attempt.reference.artifact_id,'resale_provider_start',
        dict(attempt=attempt.reference.model_dump(mode='json'),request_sha256=prepared.request_sha256,
             live_authorized=live_authorized,offline_client=offline_client,
             acknowledge_ambiguous_retry=acknowledge_ambiguous_retry),Artifact,perform)


def event(repo, start, stage, **details):
    permitted={'status','ambiguous','provider_request_id','provider_response_id','provider_model','usage',
        'response_sha256','raw_response_json','output_text','citations','tool_metadata_json','result_sha256','publication','failure'}
    if set(details)-permitted or stage not in {'RECEIVED','VALIDATED','FINISHED'}:
        raise V2PersistenceError('Invalid Resale provider audit event')
    def perform(c):
        original=repo._typed_reference(c,start,start.evaluation_id,{ArtifactType.RESALE_PROVIDER_CALL})
        if original.payload.stage!='STARTED':raise V2PersistenceError('Audit requires original STARTED reference')
        records=[a for a in repo._evidence_history(c,start.evaluation_id,ArtifactType.RESALE_PROVIDER_CALL)
                 if a.payload.call_id==original.payload.call_id]
        if any(a.payload.stage in {stage,'FINISHED'} for a in records):
            raise V2PersistenceError('Audit event is already complete')
        if (stage=='RECEIVED' and records[-1].payload.stage!='STARTED'
                or stage=='VALIDATED' and records[-1].payload.stage!='RECEIVED'):
            raise V2PersistenceError('Invalid provider audit transition')
        if details.get('publication'):
            repo._typed_reference(c,details['publication'],start.evaluation_id,{ArtifactType.RESALE_RESEARCH_RESULT})
        now=_now()
        payload=ResaleProviderAudit.model_validate(records[-1].payload.model_dump()|details|
            dict(stage=stage,start=start,recorded_at=now))
        parents=(start,*((payload.publication,) if payload.publication else ()))
        return repo._insert_artifact(c,start.evaluation_id,ArtifactType.RESALE_PROVIDER_CALL,
            payload,AUDIT_PRODUCER,parents,created_at=now)
    encoded={k:([v.model_dump(mode='json') for v in value] if isinstance(value,tuple) else
                value.model_dump(mode='json') if hasattr(value,'model_dump') else value) for k,value in details.items()}
    return repo._once('resale-provider-event:'+start.artifact_id+':'+stage,'resale_provider_event',
        dict(start=start.model_dump(mode='json'),stage=stage,details=encoded),Artifact,perform)
