"""Offline transport/domain alignment, including a synthetic reconstruction of 6F."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError, create_model
from openai.lib._parsing._responses import parse_text, type_to_text_format_param

from arbitrage.v2.contracts import LiveManagerRequest, ManagerProposal
from arbitrage.v2.live_manager import INTENT_TYPES, LiveManager, ProviderReply, prepare_request, proposal_output_schema
from arbitrage.v2.manager import parse_proposal
from arbitrage.v2.persistence import V2Repository
from tests.v2_live_fixtures import FakeClient, LiveFixture, config, reply


def schema_nodes(value):
    if isinstance(value,dict):
        yield value
        for item in value.values():
            yield from schema_nodes(item)
    elif isinstance(value,list):
        for item in value:
            yield from schema_nodes(item)


def rejected_6f_proposal(fixture):
    """Invented IDs from the fixture; no provider metadata or saved DB dependency."""
    proposal=fixture.proposal.model_dump(mode='json')
    proposal['intent'].update(requested_role='RESALE',
        objective='Find exact or close Nike Pegasus 41 mens size 10 resale evidence.',
        scope=['Separate completed-sale evidence from active asks; disclose variant differences.'],
        source_requirements=['Use public marketplace sources with citations.',
                             'Record completion basis and limitations of realized prices.'])
    return proposal


class ManagerOutputSchemaTests(unittest.TestCase):
    def fixture(self,kind='RESEARCH_REQUEST'):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        root=Path(directory.name)
        repo=V2Repository(root/'business.db',session_database_path=root/'sessions.db');repo.initialize_schema()
        return repo,LiveFixture(repo,kind)

    def test_all_five_schemas_use_sdk_strict_conversion(self):
        for kind,intent_type in INTENT_TYPES.items():
            with self.subTest(kind=kind),patch('socket.socket',side_effect=AssertionError('No network')):
                schema=proposal_output_schema(kind)
                typed=create_model('RequestedManagerProposal',__base__=ManagerProposal,intent=(intent_type,...))
                self.assertEqual(schema,type_to_text_format_param(typed)['schema'])
                for node in schema_nodes(schema):
                    if node.get('type')=='object':
                        self.assertIs(node['additionalProperties'],False)
                        self.assertEqual(set(node['required']),set(node['properties']))
                self.assertNotIn('proposal_json',schema['properties'])
                ref=schema['properties']['intent']['$ref'].split('/')[-1]
                intent=schema['$defs'][ref]
                self.assertEqual(intent['properties']['kind']['const'],kind)
                self.assertFalse(intent['additionalProperties'])
                self.assertNotIn('source_requirements',intent['properties'])
                self.assertNotIn('oneOf',json.dumps(schema))
                self.assertNotIn('discriminator',json.dumps(schema))

    def test_required_nullable_enums_lists_and_nested_references(self):
        schema=proposal_output_schema('RESEARCH_REQUEST')
        self.assertIn('supersedes',schema['required'])
        self.assertIn({'type':'null'},schema['properties']['supersedes']['anyOf'])
        self.assertEqual(schema['properties']['evidence_references']['type'],'array')
        self.assertEqual(schema['properties']['context']['$ref'],'#/$defs/ArtifactRef')
        intent=schema['$defs']['ResearchRequest']
        self.assertIn('priority',intent['required'])
        self.assertIn({'type':'null'},intent['properties']['priority']['anyOf'])
        self.assertIn('RESALE',intent['properties']['requested_role']['enum'])
        self.assertEqual(intent['properties']['scope']['items']['type'],'string')

    def test_all_intents_sdk_parse_round_trip_without_semantic_change(self):
        for kind,intent_type in INTENT_TYPES.items():
            with self.subTest(kind=kind):
                _,fixture=self.fixture(kind)
                typed=create_model('RequestedManagerProposal',__base__=ManagerProposal,intent=(intent_type,...))
                parsed=parse_text(fixture.proposal.model_dump_json(),typed)
                self.assertEqual(parsed.model_dump(mode='json'),fixture.proposal.model_dump(mode='json'))
                self.assertEqual(parse_proposal(parsed.model_dump_json()),fixture.proposal)

    def test_all_intents_valid_through_fake_adapter(self):
        for kind in INTENT_TYPES:
            with self.subTest(kind=kind):
                repo,fixture=self.fixture(kind)
                client=FakeClient(reply(fixture.proposal))
                with patch('socket.socket',side_effect=AssertionError('No network')):
                    result=LiveManager(repo,client=client).execute(fixture.context.reference,'One proposal.',config(kind),
                        step_id='manager',operation_key='run')
                self.assertEqual(result.status,'ACCEPTED',result.failure)
                persisted=repo.get_artifact(result.decision).payload.proposal
                self.assertEqual(persisted,fixture.proposal)
                body=json.loads(client.calls[0][0].api_json)
                self.assertEqual(body['text']['format']['schema'],proposal_output_schema(kind))
                self.assertTrue(body['text']['format']['strict'])
                self.assertEqual(len(client.calls),1)

    def test_6f_extra_field_rejected_by_domain_and_schema_boundary(self):
        repo,fixture=self.fixture()
        bad=rejected_6f_proposal(fixture)
        encoded=json.dumps(bad)
        with self.assertRaises(ValidationError) as error:
            parse_proposal(encoded)
        self.assertEqual(error.exception.errors()[0]['type'],'extra_forbidden')
        schema=proposal_output_schema('RESEARCH_REQUEST')['$defs']['ResearchRequest']
        self.assertNotIn('source_requirements',schema['properties'])
        self.assertFalse(schema['additionalProperties'])
        client=FakeClient(ProviderReply(status='COMPLETED',text=encoded,response_id='resp_synthetic',
            request_id='req_synthetic',usage={'input_tokens':100,'output_tokens':100,'total_tokens':200}))
        with patch('socket.socket',side_effect=AssertionError('No network')):
            result=LiveManager(repo,client=client).execute(fixture.context.reference,'One proposal.',config(),
                step_id='manager',operation_key='run')
        self.assertEqual(result.status,'INVALID_OUTPUT')
        self.assertIsNone(result.decision)
        audits=repo.list_model_calls(fixture.eid)
        self.assertEqual(audits[-1].payload.status,'INVALID_RESPONSE')
        self.assertEqual(audits[1].payload.response_text,encoded)
        self.assertEqual(audits[-1].payload.usage.total_tokens,200)
        self.assertEqual(audits[-1].payload.provider_response_id,'resp_synthetic')
        self.assertEqual(len(client.calls),1)
        self.assertIn('source_requirements',bad['intent'])
        self.assertFalse(any(a.artifact_type=='MANAGER_DECISION' for a in repo.list_artifacts(fixture.eid)))

    def test_legacy_envelope_not_accepted_or_unwrapped(self):
        repo,fixture=self.fixture()
        client=FakeClient(ProviderReply(status='COMPLETED',text=json.dumps({'proposal_json':fixture.proposal.model_dump_json()})))
        result=LiveManager(repo,client=client).execute(fixture.context.reference,'One proposal.',config(),
            step_id='manager',operation_key='run')
        self.assertEqual(result.status,'INVALID_OUTPUT')
        self.assertEqual(len(client.calls),1)

    def test_malformed_required_kind_enum_and_nested_extra_fail_closed(self):
        for change in ('required','kind','enum','nested_extra','root_extra'):
            with self.subTest(change=change):
                _,fixture=self.fixture()
                data=fixture.proposal.model_dump(mode='json')
                if change=='required': del data['intent']['objective']
                elif change=='kind': data['intent']['kind']='UNKNOWN'
                elif change=='enum': data['intent']['requested_role']='WEB_SEARCH'
                elif change=='nested_extra': data['context']['invented']='bad'
                else: data['invented']='bad'
                with self.assertRaises(ValueError): parse_proposal(json.dumps(data))

    def test_request_has_single_authoritative_provider_schema(self):
        _,fixture=self.fixture()
        request=prepare_request(fixture.context,LiveManagerRequest(context=fixture.context.reference,
            objective='One proposal.',config=config()))
        body=json.loads(request.api_json)
        self.assertEqual(body['text']['format']['name'],'manager_proposal')
        data=json.loads(body['input'][1]['content'])
        self.assertNotIn('application_schema',data)
        self.assertIn('authorized_evidence_references',data)
        self.assertIn('source_requirements',body['input'][0]['content'])
        self.assertEqual(body['tools'],[])
