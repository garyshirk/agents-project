"""Offline injected Resale boundary. No SDK client, network tools, or database authority in adapter."""

from typing import Protocol

from arbitrage.v2.contracts import Contract, FailureRecord, Producer, ResaleResearchInput, ResaleResearchResult, WorkflowStep
from arbitrage.v2.persistence import V2PersistenceError
from arbitrage.v2.resale_delegation import record_resale_batch, validate_delegation, validate_result
from arbitrage.v2.workflow import OfflineWorkflow, output_operation_key


class ResaleClient(Protocol):
    is_live: bool

    def invoke(self, request: ResaleResearchInput) -> ResaleResearchResult | str: ...


class ResaleReply(Contract):
    result: ResaleResearchResult | None = None
    failure: FailureRecord | None = None


class OfflineResaleAdapter:
    def __init__(self, client: ResaleClient):
        if getattr(client,'is_live',True) is not False:
            raise ValueError('Checkpoint 7A accepts explicitly offline injected clients only')
        self.client=client

    def invoke(self, request: ResaleResearchInput):
        request=ResaleResearchInput.model_validate(request)
        if getattr(self.client,'is_live',True) is not False:
            raise ValueError('Live clients are not authorized in the offline adapter')
        try:
            response=self.client.invoke(request)
        except Exception:
            # Preserve diagnostic category without leaking arbitrary headers/messages/secrets.
            return ResaleReply(failure=FailureRecord(stage='resale_transport',category='TRANSPORT_FAILURE',
                message='Offline specialist failed; completion may be unknown. No automatic retry.'))
        try:
            result=(ResaleResearchResult.model_validate_json(response) if isinstance(response,str)
                    else ResaleResearchResult.model_validate(response))
        except ValueError:
            return ResaleReply(failure=FailureRecord(stage='resale_parse',category='INVALID_OUTPUT',
                message='Malformed or schema-invalid Resale result; no repair.'))
        return ResaleReply(result=result)


class OfflineResaleWorkflow(OfflineWorkflow):
    """Application facade: one explicit persisted delegation per bounded workflow invocation."""
    def __init__(self, repository, *, adapter: OfflineResaleAdapter, specialist_name='Synthetic V2 Resale specialist'):
        super().__init__(repository)
        self.adapter=adapter
        self.producer=Producer(kind='AGENT',name=specialist_name)

    def run(self, evaluation_id, steps, *, operation_key):
        if len(steps)!=1 or steps[0].role!='RESALE_RESEARCH':
            raise V2PersistenceError('Resale execution requires exactly one explicitly authorized delegation')
        return super().run(evaluation_id,steps,operation_key=operation_key)

    def execute(self, delegation, *, operation_key, retry_of=None):
        # Validate before acquiring a run or granting a client any input.
        with self.repository._connection() as c:
            artifact=validate_delegation(self.repository,c,delegation,delegation.evaluation_id)
        step=WorkflowStep(step_id='resale:'+artifact.payload.delegation_id,role='RESALE_RESEARCH',
            context_references=(artifact.reference,),context_text='One authorized offline Resale delegation; no live research.',
            resale_delegation=artifact.reference,retry_of=retry_of)
        return self.run(delegation.evaluation_id,(step,),operation_key=operation_key)

    def _execute(self, attempt):
        if attempt.payload.input_manifest.role!='RESALE_RESEARCH':
            raise V2PersistenceError('This adapter executes only one Resale delegation, not arbitrary workflow steps')
        with self.repository._connection() as c:
            artifact=validate_delegation(self.repository,c,attempt.payload.input_manifest.resale_delegation,
                attempt.reference.evaluation_id)
        request=ResaleResearchInput(delegation=artifact.reference,context=artifact.payload)
        reply=self.adapter.invoke(request)
        if reply.failure:
            return self._complete(attempt,'SPECIALIST_FAILURE' if reply.failure.category=='TRANSPORT_FAILURE'
                else 'INVALID_OUTPUT',failure=reply.failure)
        try:
            validate_result(artifact,reply.result)
        except ValueError:
            return self._complete(attempt,'INVALID_OUTPUT',failure=FailureRecord(stage='resale_binding',
                category='INVALID_BINDING',message='Specialist result changed delegation or source authority.'))
        # Deliberately let persistence errors interrupt the run: no partial batch is accepted;
        # explicit existing recovery resolves an interrupted committed receipt without re-invoking.
        record=record_resale_batch(self.repository,attempt,reply.result,producer=self.producer,
            operation_key=output_operation_key(attempt))
        return self._complete(attempt,'SUCCEEDED',artifact=record)
