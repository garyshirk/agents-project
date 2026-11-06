"""Sanitized output-boundary regressions; fictional sources, fake provider, no network."""

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from arbitrage.v2.contracts import ManagerContextLimits, ResaleResearchInput, ResaleResearchResult
from arbitrage.v2.live_resale import (
    INSTRUCTIONS, PROMPT_VERSION, SCHEMA_VERSION, LiveResaleAdapter, LiveResaleWorkflow,
    output_schema, prepare_request, source_metadata,
)
from arbitrage.v2.persistence import V2Repository
from arbitrage.v2.resale_audit import history
from arbitrage.v2.resale_delegation import refresh_resale_context
from tests.v2_live_resale_fixtures import FakeProvider, config, provider_reply, result_from
from tests.v2_resale_fixtures import ResaleFixture


def active_result(prepared):
    """Fresh fictional response, not a repaired/replayed historical payload."""
    data=result_from(prepared)
    data['sources']=data['sources'][:1]
    data['findings']=data['findings'][:1]
    data['sources'][0]['source']['observed_at']=None
    data['findings'][0]['comparable']['source']=copy.deepcopy(data['sources'][0]['source'])
    return data


class ResaleOutputBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        self.repo=V2Repository(root/'business.db',session_database_path=root/'sessions.db')
        self.repo.initialize_schema();self.fixture=ResaleFixture(self.repo)
        request=ResaleResearchInput(delegation=self.fixture.delegation.reference,context=self.fixture.delegation.payload)
        self.prepared=prepare_request(request,config())
        self.data=active_result(self.prepared)

    def execute(self,data,*,reply_change=None):
        reply=provider_reply(data)
        if reply_change:reply=reply_change(reply)
        client=FakeProvider(reply)
        workflow=LiveResaleWorkflow(self.repo,adapter=LiveResaleAdapter(client))
        with patch('socket.socket',side_effect=AssertionError('No network permitted')):
            end=workflow.execute(self.fixture.delegation.reference,config(),operation_key='run')
        self.assertEqual(len(client.calls),1)
        self.assertFalse(self.repo.list_calculation_history(self.fixture.eid))
        self.assertFalse(self.repo.list_reviews(self.fixture.eid))
        self.assertEqual(self.repo.list_review_queue().entries[0].review_state,'PENDING')
        self.assertEqual(history(self.repo,self.fixture.eid)[1].payload.response_sha256,
                         sha256(reply.raw_response_json.encode()).hexdigest())
        return self.repo.get_artifact(end.payload.attempt_references[0]),workflow,client

    def test_historical_active_ask_denial_rejected_without_repair(self):
        price=self.data['findings'][0]['comparable']['price_evidence']
        price['completion_basis']='Active ask only; not a completed-sale price.'
        original=copy.deepcopy(self.data)
        with self.assertRaisesRegex(ValidationError,'non-transaction'):ResaleResearchResult.model_validate(self.data)
        attempt,_,_=self.execute(self.data)
        self.assertEqual(attempt.payload.failure.category,'DOMAIN_VALIDATION')
        self.assertEqual(self.data,original)
        self.assertFalse(self.repo.list_comparable_history(self.fixture.eid))
        self.assertEqual([a.payload.stage for a in history(self.repo,self.fixture.eid)],['STARTED','RECEIVED','FINISHED'])

    def test_corrected_active_ask_published_and_context_refreshed(self):
        self.data['findings'][0]['comparable']['price_evidence']['context']=['Active ask only; not a completed-sale price.']
        attempt,workflow,client=self.execute(self.data)
        self.assertEqual(attempt.payload.status,'SUCCEEDED')
        record=self.repo.get_artifact(attempt.payload.output_references[0])
        self.assertEqual(record.payload.result,ResaleResearchResult.model_validate(self.data))
        context=refresh_resale_context(self.repo,record.reference,
            limits=ManagerContextLimits(max_artifacts=20,max_characters=200000),operation_key='refresh')
        self.assertIn(record.payload.provider_audits[0],context.payload.selected_references)
        self.assertEqual([a.payload.stage for a in history(self.repo,self.fixture.eid)],
                         ['STARTED','RECEIVED','VALIDATED','FINISHED'])
        audit=history(self.repo,self.fixture.eid)[0].payload
        self.assertEqual(audit.prompt_version,PROMPT_VERSION)
        self.assertEqual(audit.schema_version_name,SCHEMA_VERSION)
        with patch('socket.socket',side_effect=AssertionError('No network')):
            workflow.execute(self.fixture.delegation.reference,config(),operation_key='run')
        self.assertEqual(len(client.calls),1)

    def test_source_copies_must_match_in_every_supported_field(self):
        # There is no source title field; source_name is the supported name/title-like field.
        changes={'url':'https://other.invalid/listing','source_name':'Different title/name',
                 'source_category':'RETAILER','observed_at':'2026-01-02T00:00:00Z',
                 'limitations':['Different source limitations'],'seller_name':'Different seller',
                 'capture_method':'OTHER','other_reference':'Different reference'}
        for field,value in changes.items():
            with self.subTest(field=field):
                data=copy.deepcopy(self.data)
                data['findings'][0]['comparable']['source'][field]=value
                with self.assertRaisesRegex(ValidationError,'exact declared source'):
                    ResaleResearchResult.model_validate(data)

    def test_source_mismatch_rejected_before_publication(self):
        self.data['findings'][0]['comparable']['source']['limitations']=['Different qualification']
        attempt,_,_=self.execute(self.data)
        self.assertEqual(attempt.payload.failure.category,'DOMAIN_VALIDATION')
        self.assertFalse(self.repo.list_comparable_history(self.fixture.eid))

    def test_missing_source_fields_and_unknown_title(self):
        for field in ('source_category','capture_method'):
            data=copy.deepcopy(self.data);del data['sources'][0]['source'][field]
            with self.subTest(field=field),self.assertRaises(ValidationError):ResaleResearchResult.model_validate(data)
        data=copy.deepcopy(self.data);data['sources'][0]['source']['title']='Not a contract field'
        with self.assertRaises(ValidationError):ResaleResearchResult.model_validate(data)
        # Optional omission parses as its declared default; strict provider schema requires the key.
        data=copy.deepcopy(self.data);del data['sources'][0]['source']['observed_at']
        self.assertIsNone(ResaleResearchResult.model_validate(data).sources[0].source.observed_at)
        self.assertIn('observed_at',output_schema()['$defs']['ComparableSource']['required'])

    def test_unsupported_attribution_rejected_not_repaired(self):
        attempt,_,_=self.execute(self.data,reply_change=lambda r:r.model_copy(update={'citations':()}))
        self.assertEqual(attempt.payload.failure.category,'INVALID_SOURCE_ATTRIBUTION')
        self.assertFalse(self.repo.list_comparable_history(self.fixture.eid))

    def test_supported_url_is_attribution_not_truth(self):
        attempt,_,_=self.execute(self.data)
        record=self.repo.get_artifact(attempt.payload.output_references[0])
        self.assertFalse(record.payload.result.sources[0].source.application_verified)
        for citation in history(self.repo,self.fixture.eid)[1].payload.citations:
            self.assertFalse(citation.application_visited)
            self.assertIsNone(citation.excerpt)

    def test_null_observation_time_with_missing_provider_time(self):
        attempt,_,_=self.execute(self.data)
        record=self.repo.get_artifact(attempt.payload.output_references[0])
        self.assertIsNone(record.payload.result.sources[0].source.observed_at)
        audit=history(self.repo,self.fixture.eid)[1].payload
        self.assertIsNotNone(audit.recorded_at)
        self.assertNotIn('timestamp',json.loads(audit.tool_metadata_json)[0])

    def test_plausible_model_timestamp_cannot_be_verified_by_current_boundary(self):
        for source in (self.data['sources'][0]['source'],self.data['findings'][0]['comparable']['source']):
            source['observed_at']='2026-01-01T00:00:00Z'
        attempt,_,_=self.execute(self.data)
        self.assertEqual(attempt.payload.status,'SUCCEEDED')
        # Deliberate limit: attribution has no provider observation-time field to corroborate it.
        self.assertFalse(self.repo.get_artifact(attempt.payload.output_references[0]).payload.result.sources[0].source.application_verified)

    def test_supported_observation_is_preserved_not_promoted_to_verification(self):
        # Fixture declares an observation time; provider citation metadata itself cannot prove it.
        for source in (self.data['sources'][0]['source'],self.data['findings'][0]['comparable']['source']):
            source['observed_at']='2026-01-03T12:34:56Z'
            source['limitations']=['Synthetic source explicitly reports observation at 2026-01-03T12:34:56Z.']
        typed=ResaleResearchResult.model_validate(self.data)
        self.assertEqual(typed.sources[0].source.observed_at.isoformat(),'2026-01-03T12:34:56+00:00')
        self.assertFalse(typed.sources[0].source.application_verified)

    def test_publication_date_not_substituted_for_observation(self):
        self.data['findings'][0]['comparable']['price_evidence']['context']=['Source publication date: 2025-12-31; observation time unknown.']
        typed=ResaleResearchResult.model_validate(self.data)
        self.assertIsNone(typed.sources[0].source.observed_at)
        self.assertIsNone(typed.findings[0].comparable.price_evidence.transaction_at)

    def test_detectably_invalid_timestamps_rejected(self):
        for value in ('invented-date','2026-13-99T00:00:00Z','2026-01-01T00:00:00'):
            data=copy.deepcopy(self.data)
            for source in (data['sources'][0]['source'],data['findings'][0]['comparable']['source']):source['observed_at']=value
            with self.subTest(value=value),self.assertRaises(ValidationError):ResaleResearchResult.model_validate(data)

    def test_completed_sale_transaction_date_distinct_from_observation(self):
        data=result_from(self.prepared)
        data['findings'][1]['comparable']['price_evidence']['transaction_at']='2025-12-30T12:00:00Z'
        attempt,_,_=self.execute(data)
        self.assertEqual(attempt.payload.status,'SUCCEEDED')
        typed=self.repo.get_artifact(attempt.payload.output_references[0]).payload.result
        self.assertNotEqual(typed.findings[1].comparable.price_evidence.transaction_at,typed.sources[1].source.observed_at)

    def test_hidden_accepted_offer_has_no_invented_realized_amount(self):
        self.data['sources'][0]['listing_status']='ACCEPTED_OFFER'
        self.data['findings'][0]['comparable']['price_evidence'].update(evidence_type='OTHER_QUALIFIED',amount=None,
            context=['Accepted offer hidden; actual transaction amount and completion unknown.'])
        attempt,_,_=self.execute(self.data)
        self.assertEqual(attempt.payload.status,'SUCCEEDED')

    def test_all_nontransaction_categories_reject_both_basis_fields(self):
        for kind in ('ACTIVE_ASK','PUBLISHED_REFERENCE','HUMAN_REPORTED','OTHER_QUALIFIED'):
            for field in ('completion_basis','realized_price_basis'):
                data=copy.deepcopy(self.data)
                data['findings'][0]['comparable']['price_evidence'].update(evidence_type=kind,**{field:'Not a sale.'})
                with self.subTest(kind=kind,field=field),self.assertRaises(ValidationError):ResaleResearchResult.model_validate(data)

    def test_schema_preserves_nested_strict_nullable_contract(self):
        schema=output_schema()
        def walk(node):
            if isinstance(node,dict):
                if node.get('type')=='object':
                    self.assertIs(node['additionalProperties'],False)
                    self.assertEqual(set(node['required']),set(node['properties']))
                for value in node.values():walk(value)
            elif isinstance(node,list):
                for value in node:walk(value)
        walk(schema)
        price=schema['$defs']['PriceEvidence']['properties']
        for field in ('completion_basis','realized_price_basis','transaction_at'):
            self.assertIn({'type':'null'},price[field]['anyOf'])
        self.assertEqual(schema['$defs']['PriceEvidenceType']['enum'],
                         ['COMPLETED_SALE','ACTIVE_ASK','PUBLISHED_REFERENCE','HUMAN_REPORTED','OTHER_QUALIFIED'])
        self.assertNotIn('proposal_json',schema['properties'])
        self.assertEqual(schema['$defs']['ResaleResearchFinding']['properties']['comparable']['$ref'],'#/$defs/ComparableDraft')
        # JSON Schema constrains individual objects, not cross-copy equality/custom validators.
        self.assertIn('source',schema['$defs']['ComparableDraft']['properties'])

    def test_prompt_and_audit_versions_preserve_contract(self):
        self.assertEqual(PROMPT_VERSION,'v2-resale-public-research-v2')
        self.assertEqual(SCHEMA_VERSION,'v2-resale-research-result-v1')
        for text in ('completion_basis and realized_price_basis MUST be null','OTHER_QUALIFIED',
                     'MUST reproduce','Leave observed_at null','publication date','transaction date'):
            self.assertIn(text,INSTRUCTIONS)
        citations,_,_=source_metadata({'output':[{'type':'web_search_call','status':'completed',
            'action':{'sources':[{'type':'url','url':'https://example.invalid/a','published_at':'2025-12-31'}]}}]})
        self.assertNotIn('observed_at',type(citations[0]).model_fields)
        self.assertIsNone(citations[0].excerpt)

    def test_no_evidence_partial_and_provider_failure_are_bounded(self):
        # Each case owns fresh state and one fake invocation, never an automatic second pass.
        for status in ('NO_EVIDENCE','PARTIAL','PROVIDER_ERROR'):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as root:
                repo=V2Repository(Path(root)/'b.db',session_database_path=Path(root)/'s.db');repo.initialize_schema()
                fixture=ResaleFixture(repo)
                client=FakeProvider(lambda p,c:provider_reply(result_from(p,status if status!='PROVIDER_ERROR' else 'COMPLETED'),
                                                              status=status if status=='PROVIDER_ERROR' else 'COMPLETED'))
                workflow=LiveResaleWorkflow(repo,adapter=LiveResaleAdapter(client))
                with patch('socket.socket',side_effect=AssertionError('No network')):
                    end=workflow.execute(fixture.delegation.reference,config(),operation_key='run')
                attempt=repo.get_artifact(end.payload.attempt_references[0])
                self.assertEqual(attempt.payload.status,'SPECIALIST_FAILURE' if status=='PROVIDER_ERROR' else 'SUCCEEDED')
                self.assertEqual(len(client.calls),1)
                self.assertFalse(repo.list_calculation_history(fixture.eid));self.assertFalse(repo.list_reviews(fixture.eid))

    def test_atomic_rollback_and_restart_never_reinvoke_provider(self):
        with closing(sqlite3.connect(self.repo.database_path)) as c:
            c.execute("CREATE TRIGGER fail_comp BEFORE INSERT ON v2_artifacts WHEN NEW.artifact_type='RESALE_COMPARABLE' BEGIN SELECT RAISE(ABORT,'Synthetic rollback'); END")
        client=FakeProvider(lambda p,c:provider_reply(active_result(p)))
        workflow=LiveResaleWorkflow(self.repo,adapter=LiveResaleAdapter(client))
        with patch('socket.socket',side_effect=AssertionError('No network')),self.assertRaises(sqlite3.Error):
            workflow.execute(self.fixture.delegation.reference,config(),operation_key='run')
        self.assertFalse(self.repo.list_comparable_history(self.fixture.eid))
        self.assertEqual(history(self.repo,self.fixture.eid)[-1].payload.stage,'VALIDATED')
        run=self.repo.get_workflow(self.fixture.eid).unresolved_runs[0]
        forbidden=FakeProvider(AssertionError('No second invocation'))
        restarted=LiveResaleWorkflow(self.repo,adapter=LiveResaleAdapter(forbidden))
        restarted.recover(run,decision='RECONCILE',operation_key='recover')
        self.assertEqual(history(self.repo,self.fixture.eid)[-1].payload.status,'NOT_APPLIED')
        self.assertEqual(len(client.calls),1);self.assertEqual(forbidden.calls,[])
        self.assertFalse(self.repo.list_calculation_history(self.fixture.eid));self.assertFalse(self.repo.list_reviews(self.fixture.eid))
