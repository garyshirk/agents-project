"""Synthetic reconstruction of the rejected smoke response; never depends on live audit data."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arbitrage.v2.contracts import ManagerContextLimits, ManagerProposal, Producer, RawResearchReport, ResearchRequest, WorkflowLimits
from arbitrage.v2.live_manager import INSTRUCTIONS, PROMPT_VERSION, LiveManager
from arbitrage.v2.manager import OfflineManager, read_context
from arbitrage.v2.persistence import V2Repository
from arbitrage.v2.workflow import OfflineWorkflow
from diagnostics.v2_manager_smoke import main, synthetic_opportunity
from tests.v2_live_fixtures import FakeClient, LiveFixture, config, reply


class ReferenceBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        self.repo=V2Repository(self.root/'business.db',session_database_path=self.root/'sessions.db')
        self.repo.initialize_schema()
        accepted=OfflineWorkflow(self.repo).accept(synthetic_opportunity(),WorkflowLimits(
            max_specialist_invocations=3,max_attempts_per_step=2,max_workflow_steps=3,max_calculation_attempts=1),operation_key='boundary-intake')
        self.eid=accepted.evaluation.evaluation_id
        self.context=OfflineManager(self.repo).build_context(self.eid,(),
            limits=ManagerContextLimits(max_artifacts=10,max_characters=100000),operation_key='boundary-context')
        self.proposal=ManagerProposal(opportunity_id=self.context.reference.opportunity_id,evaluation_id=self.eid,
            context=self.context.reference,author_name='Synthetic regression',rationale='Intake is unverified; investigate identity first.',
            evidence_references=(),limitations=('No verified evidence.',),uncertainties=('Style/model match is unverified.',),
            intent=ResearchRequest(kind='RESEARCH_REQUEST',objective='Verify the reported Nike model/style association.',
                target_subject='Nike Pegasus 41, reported FD2722-099, mens US size 10',requested_role='SOURCING',
                scope=('Verify identity only; do not execute this request or transact.',),max_invocations=1))

    def execute(self,proposal=None,callback=None):
        self.client=FakeClient(callback or reply(proposal or self.proposal))
        with patch('socket.socket',side_effect=AssertionError('No network')):
            return LiveManager(self.repo,client=self.client).execute(self.context.reference,'Propose one narrow research request.',
                config(),step_id='manager',operation_key='run')

    def cite(self,ref):
        return self.proposal.model_copy(update={'evidence_references':(ref,)})

    def test_empty_intake_research_passes_without_invented_evidence(self):
        result=self.execute()
        self.assertEqual(result.status,'ACCEPTED',result.failure)
        persisted=self.repo.get_artifact(result.decision).payload.proposal
        self.assertEqual(persisted.evidence_references,())
        self.assertEqual(persisted.context,self.context.reference)
        self.assertTrue(any(item.evaluation.evaluation_id==self.eid and item.review_state=='PENDING' for item in self.repo.list_review_queue().entries))

    def test_original_workflow_event_rejected_without_repair_and_response_preserved(self):
        event=read_context(self.context).workflow_event.reference
        proposed=self.cite(event)
        result=self.execute(proposed)
        self.assertEqual(result.status,'INVALID_OUTPUT')
        audits=self.repo.list_model_calls(self.eid)
        self.assertEqual(audits[-1].payload.status,'VALIDATION_FAILED')
        self.assertEqual(audits[1].payload.response_text,reply(proposed).text)
        self.assertEqual(proposed.evidence_references,(event,))
        self.assertEqual(len(self.client.calls),1)
        self.assertTrue(any(item.evaluation.evaluation_id==self.eid and item.review_state=='PENDING' for item in self.repo.list_review_queue().entries))

    def test_context_snapshot_is_not_evidence(self):
        self.assertEqual(self.execute(self.cite(self.context.reference)).status,'INVALID_OUTPUT')

    def test_review_subject_binding_is_not_evidence(self):
        self.assertEqual(self.execute(self.cite(self.context.payload.review_subject)).status,'INVALID_OUTPUT')

    def test_fabricated_reference_rejected(self):
        fabricated=self.context.reference.model_copy(update={'artifact_id':'fabricated'})
        self.assertEqual(self.execute(self.cite(fabricated)).status,'INVALID_OUTPUT')

    def test_cross_evaluation_reference_rejected(self):
        other=LiveFixture(self.repo)
        self.assertEqual(self.execute(self.cite(other.raw.reference)).status,'INVALID_OUTPUT')

    def test_real_unselected_artifact_rejected(self):
        raw=self.repo.record_artifact(self.eid,RawResearchReport(report_text='Synthetic unselected report'),
            producer=Producer(kind='APPLICATION',name='Fixture'),operation_key='raw')
        self.context=OfflineManager(self.repo).build_context(self.eid,(),
            limits=ManagerContextLimits(max_artifacts=10,max_characters=100000),operation_key='fresh')
        self.proposal=self.proposal.model_copy(update={'context':self.context.reference})
        self.assertEqual(self.execute(self.cite(raw.reference)).status,'INVALID_OUTPUT')

    def test_stale_context_rejected_before_provider(self):
        self.repo.record_artifact(self.eid,RawResearchReport(report_text='Synthetic later report'),
            producer=Producer(kind='APPLICATION',name='Fixture'),operation_key='later')
        self.assertEqual(self.execute().status,'INVALID_OUTPUT')
        self.assertFalse(self.client.calls)

    def test_attempt_and_audit_identifiers_are_not_evidence(self):
        def respond(request,cfg):
            audit=self.repo.list_model_calls(self.eid)[0]
            return reply(self.cite(audit.reference))
        self.assertEqual(self.execute(callback=respond).status,'INVALID_OUTPUT')

    def test_attempt_reference_is_not_evidence(self):
        def respond(request,cfg):
            audit=self.repo.list_model_calls(self.eid)[0]
            return reply(self.cite(audit.payload.attempt))
        self.assertEqual(self.execute(callback=respond).status,'INVALID_OUTPUT')

    def test_payload_explicitly_separates_reference_roles(self):
        self.assertEqual(self.execute().status,'ACCEPTED')
        body=json.loads(self.client.calls[0][0].api_json)
        payload=json.loads(body['input'][1]['content'])
        self.assertEqual(payload['authorized_evidence_references'],[])
        self.assertEqual(payload['historical_support_references'],[])
        self.assertEqual(payload['application_owned_identifiers']['context_reference'],self.context.reference.model_dump(mode='json'))
        self.assertIn('workflow_event',payload['untrusted_context'])
        self.assertEqual(body['input'][0]['content'],INSTRUCTIONS)
        self.assertEqual(PROMPT_VERSION,'v2-manager-controlled-v3')
        for text in ('Only cite economic evidence','return empty evidence_references','not substitute source citations','Never invent'):
            self.assertIn(text,INSTRUCTIONS)

    def test_selected_evidence_and_injection_do_not_expand_authority(self):
        fixture=LiveFixture(self.repo,topic='injected_research')
        client=FakeClient(reply(fixture.proposal))
        with patch('socket.socket',side_effect=AssertionError('No network')):
            result=LiveManager(self.repo,client=client).execute(fixture.context.reference,'One proposal only.',
                config(),step_id='manager',operation_key='other-run')
        self.assertEqual(result.status,'ACCEPTED')
        body=json.loads(client.calls[0][0].api_json)
        data=json.loads(body['input'][1]['content'])
        self.assertEqual(data['authorized_evidence_references'],[r.model_dump(mode='json') for r in fixture.context.payload.selected_references])
        self.assertIn('Ignore your previous instructions',body['input'][1]['content'])
        self.assertNotIn('Ignore your previous instructions',body['input'][0]['content'])

    def test_synthetic_product_and_check_only_no_client_or_database(self):
        intake=synthetic_opportunity()
        self.assertEqual(intake.scenario_kind,'HYPOTHETICAL')
        for text in ('Nike','Pegasus 41','FD2722-099','size 10','39.99','Ross','eBay','unverified','invented'):
            self.assertIn(text,intake.original_text)
        self.assertNotIn('https://',intake.original_text)
        with patch('openai.OpenAI',side_effect=AssertionError('No client')),patch('socket.socket',side_effect=AssertionError('No network')),patch('builtins.print'):
            self.assertEqual(main(['--business-db',str(self.root/'check.db'),'--sessions-db',str(self.root/'check-sessions.db'),
                                   '--model','gpt-5.4-mini']),0)
        self.assertFalse((self.root/'check.db').exists())
        self.assertFalse((self.root/'check-sessions.db').exists())

    def test_empty_evidence_decision_matrix_preserves_required_domain_references(self):
        for kind,expected in (('RESEARCH_REQUEST','ACCEPTED'),('ECONOMIC_ASSUMPTION','ACCEPTED'),
                              ('COMPARABLE_INTERPRETATION','INVALID_OUTPUT'),('SCENARIO_PROPOSAL','INVALID_OUTPUT'),
                              ('ECONOMIC_SUMMARY','ACCEPTED')):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                repo=V2Repository(root/'business.db',session_database_path=root/'sessions.db'); repo.initialize_schema()
                fixture=LiveFixture(repo,kind)
                fixture.context=OfflineManager(repo).build_context(fixture.eid,(),
                    limits=ManagerContextLimits(max_artifacts=10,max_characters=100000),operation_key='empty')
                proposal=fixture.make_proposal(kind).model_copy(update={'evidence_references':()})
                with patch('socket.socket',side_effect=AssertionError('No network')):
                    result=LiveManager(repo,client=FakeClient(reply(proposal))).execute(fixture.context.reference,
                        'One decision, preserve missing evidence.',config(kind),step_id='manager',operation_key='run')
                self.assertEqual(result.status,expected,result.failure)

    def test_nested_context_citation_is_rejected(self):
        fixture=LiveFixture(self.repo,kind='SCENARIO_PROPOSAL')
        scenario=fixture.proposal.intent.scenario.model_copy(update={'evidence_references':(fixture.context.reference,)})
        proposal=fixture.proposal.model_copy(update={'intent':fixture.proposal.intent.model_copy(update={'scenario':scenario})})
        client=FakeClient(reply(proposal))
        result=LiveManager(self.repo,client=client).execute(fixture.context.reference,'One scenario.',
            config('SCENARIO_PROPOSAL'),step_id='manager',operation_key='scenario-run')
        self.assertEqual(result.status,'INVALID_OUTPUT')

    def test_current_evidence_assumption_still_requires_support(self):
        from tests.v2_economic_fixtures import provenance
        fixture=LiveFixture(self.repo,kind='ECONOMIC_ASSUMPTION')
        assumption=fixture.proposal.intent.assumption.model_copy(update={
            'provenance':provenance(True,sources=('CURRENT_EVALUATION_EVIDENCE',))})
        proposal=fixture.proposal.model_copy(update={'intent':fixture.proposal.intent.model_copy(update={'assumption':assumption})})
        result=LiveManager(self.repo,client=FakeClient(reply(proposal))).execute(fixture.context.reference,'One assumption.',
            config('ECONOMIC_ASSUMPTION'),step_id='manager',operation_key='assumption-run')
        self.assertEqual(result.status,'INVALID_OUTPUT')
