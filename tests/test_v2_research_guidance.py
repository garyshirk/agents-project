"""Authored research plans exercise boundaries, not predicted model compliance."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arbitrage.v2.contracts import ResearchRequest
from arbitrage.v2.live_manager import INSTRUCTIONS, PROMPT_VERSION, LiveManager
from arbitrage.v2.manager_evaluation import evaluate_proposal, research_judgment_rubric
from arbitrage.v2.persistence import V2Repository
from tests.v2_live_fixtures import FakeClient, LiveFixture, config, reply


# Synthetic reconstruction, not a dependency on the preserved live database.
RESEARCH_CASES = (
    ('6d_mismatch','HUMAN_INFORMATION',
     'Verify exact identity before any resale assessment and gather eBay prices.',
     ('Confirm model/style mapping.', 'Find sold and active eBay listings.', 'Collect shipping and variant differences.'),
     ('Critique the role/scope mismatch, exact-identity gate and broad combined scope; structural acceptance is not endorsement.',)),
    ('public_resale','RESALE','Find usable eBay price evidence for the reported Nike Pegasus 41 mens US 10.',
     ('Prioritize relevant comparables; disclose condition and variant differences.',
      'Separate sold from active asks; record completion basis and realized-price limitations, including Best Offer.'),
     ('A representative public resale request; no claim that this is the only reasonable plan.',)),
    ('acquisition','SOURCING','Investigate Ross acquisition conditions for this reported shoe.',
     ('Research published purchase/return conditions and acquisition costs; do not claim actual local inventory.',),
     ('Public acquisition research fits SOURCING; actual local stock still needs observation.',)),
    ('physical_inventory','HUMAN_INFORMATION','Ask a human to confirm actual local inventory and packaging.',
     ('Inspect the item/box label and report locally observed stock and price; eBay is the intended destination only.',),
     ('A mention of eBay must not veto a legitimate human observation request.',)),
    ('mixed_needs','RESALE','Research comparable resale prices while local stock remains unconfirmed.',
     ('Use exact or close eBay comparables and disclose differences; do not require a store visit in this scope.',),
     ('Defer local condition/stock confirmation as a separate future human request, not a second delegation.',)),
    ('incomplete_identity','RESALE','Assess whether close comparables offer useful conditional price evidence.',
     ('Disclose unverified style, colorway, size and packaging; explain comparison strength without inventing adjustments.',),
     ('Missing identity attributes need not disqualify all conditional analysis; explain if evidence is too weak.',)),
    ('asking_only','RESALE','Find clearly labeled asking-price evidence for a conditional assessment.',
     ('Do not relabel active/ended listings or unverified sold labels as realized sales.',),
     ('Asking prices can inform cautious estimates; never invent completed transactions.',)),
)


class ResearchGuidanceTests(unittest.TestCase):
    def fixture(self,kind='RESEARCH_REQUEST',topic='insufficient_information'):
        directory=tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        root=Path(directory.name)
        repo=V2Repository(root/'business.db',session_database_path=root/'sessions.db'); repo.initialize_schema()
        return repo,LiveFixture(repo,kind,topic)

    def test_role_descriptions_and_existing_identifiers(self):
        for text in ('SOURCING investigates acquisition','RESALE investigates resale','ECONOMIC_COSTS addresses',
                     'HUMAN_INFORMATION requests observations','not keyword rules','Do not choose HUMAN_INFORMATION',
                     'Not every resale-related question requires RESALE'):
            self.assertIn(text,INSTRUCTIONS)
        roles=set(ResearchRequest.model_json_schema()['properties']['requested_role']['enum'])
        self.assertEqual(roles,{'SOURCING','RESALE','ECONOMIC_COSTS','HUMAN_INFORMATION'})
        self.assertEqual(PROMPT_VERSION,'v2-manager-controlled-v3')

    def test_scope_and_future_needs_are_guidance_not_extra_delegations(self):
        for text in ('one clear primary objective','realistic scope','explicit invocation limit',
                     'Related future needs are not current scope','Do not create or execute multiple autonomous delegations',
                     'Prioritize expected value'):
            self.assertIn(text,INSTRUCTIONS)

    def test_comparability_and_no_fabricated_adjustments(self):
        for text in ('Exact identity is not a universal prerequisite','exact, close or weaker comparables',
                     'colorway, width, size, packaging','Do not invent variant price adjustments',
                     'explain INSUFFICIENT_INFORMATION','never force','perfect-information gates'):
            self.assertIn(text,INSTRUCTIONS)

    def test_completed_sale_and_asking_guidance(self):
        for text in ('COMPLETED_SALE requires evidence','ACTIVE_ASK','not proof of payment',
                     'unverified sold label','cumulative quantity sold','basis of completion',
                     'accepted-offer uncertainty','cautiously interpreted conditional estimate'):
            self.assertIn(text,INSTRUCTIONS)

    def test_authored_research_cases_and_no_rigid_role_veto(self):
        for name,role,objective,scope,expectations in RESEARCH_CASES:
            with self.subTest(case=name):
                repo,fixture=self.fixture()
                intent=ResearchRequest(kind='RESEARCH_REQUEST',requested_role=role,objective=objective,
                    target_subject='Hypothetical Nike Pegasus 41, reported unverified style FD2722-099, mens US 10',
                    scope=scope,max_invocations=1)
                proposal=fixture.proposal.model_copy(update={'intent':intent,'uncertainties':(
                    'Hypothetical offer and identity unverified; local condition/stock need future human confirmation.',
                    'Fees, shipping and tax remain future research needs, not part of this request.')})
                rubric=research_judgment_rubric(name,expectations=expectations,uncertainties=proposal.uncertainties)
                before=repo.list_artifacts(fixture.eid)
                with patch('socket.socket',side_effect=AssertionError('No network')):
                    report=evaluate_proposal(repo,fixture.context,proposal.model_dump_json(),rubric)
                self.assertTrue(report.structural_validity)
                self.assertTrue(report.provenance_scope_validity)
                self.assertTrue(report.application_reference_integrity)
                self.assertEqual(report.judgment_quality,'HUMAN_REVIEW_REQUIRED')
                self.assertEqual(repo.list_artifacts(fixture.eid),before)
                self.assertGreaterEqual(len(report.rubric.human_authored_expectations),7)

    def test_one_fake_call_persists_request_without_second_delegation(self):
        repo,fixture=self.fixture()
        _,role,objective,scope,_=RESEARCH_CASES[1]
        proposal=fixture.proposal.model_copy(update={'intent':ResearchRequest(kind='RESEARCH_REQUEST',
            requested_role=role,objective=objective,target_subject='Hypothetical Nike shoe',scope=scope,max_invocations=1)})
        client=FakeClient(reply(proposal))
        with patch('socket.socket',side_effect=AssertionError('No network')):
            result=LiveManager(repo,client=client).execute(fixture.context.reference,'One research proposal only.',
                config(),step_id='manager',operation_key='run')
        self.assertEqual(result.status,'ACCEPTED')
        self.assertEqual(len(client.calls),1)
        artifacts=repo.list_artifacts(fixture.eid)
        self.assertEqual(sum(a.artifact_type=='MANAGER_DECISION' for a in artifacts),1)
        self.assertEqual(sum(a.artifact_type=='RAW_RESEARCH_REPORT' for a in artifacts),1)
        self.assertFalse(repo.list_reviews(fixture.eid))
        self.assertEqual(repo.list_review_queue().entries[0].review_state,'PENDING')
        body=json.loads(client.calls[0][0].api_json)
        self.assertEqual(body['input'][0]['content'],INSTRUCTIONS)
        self.assertEqual(body['tools'],[])
        self.assertIn('authorized_evidence_references',body['input'][1]['content'])

    def test_close_weak_and_conditional_cases_remain_human_judgments(self):
        cases=(('COMPARABLE_INTERPRETATION','variant_differences','Disclose color differences without a numerical adjustment.'),
               ('ECONOMIC_SUMMARY','insufficient_information','Insufficient information is legitimate; do not force a number.'),
               ('ECONOMIC_ASSUMPTION','asking_only','An explicit shipping assumption is not observed evidence.'),
               ('SCENARIO_PROPOSAL','missing_shipping','Keep shipping unknown rather than fabricate zero or a variant adjustment.'))
        for kind,topic,expectation in cases:
            with self.subTest(kind=kind):
                repo,fixture=self.fixture(kind,topic)
                rubric=research_judgment_rubric(topic,expectations=(expectation,),uncertainties=('Synthetic evidence only.',))
                with patch('socket.socket',side_effect=AssertionError('No network')):
                    report=evaluate_proposal(repo,fixture.context,fixture.proposal.model_dump_json(),rubric)
                self.assertTrue(report.application_reference_integrity)
                self.assertEqual(report.judgment_quality,'HUMAN_REVIEW_REQUIRED')

    def test_reference_and_staleness_guards_still_apply(self):
        for stale in (False,True):
            with self.subTest(stale=stale):
                repo,fixture=self.fixture()
                proposal=fixture.proposal
                if stale:
                    repo.record_artifact(fixture.eid,fixture.raw.payload,producer=fixture.producer,operation_key='later')
                else:
                    ref=fixture.raw.reference.model_copy(update={'artifact_id':'fabricated'})
                    proposal=proposal.model_copy(update={'evidence_references':(ref,)})
                rubric=research_judgment_rubric('boundary',expectations=('Do not repair unauthorized output.',),uncertainties=('Unverified.',))
                report=evaluate_proposal(repo,fixture.context,proposal.model_dump_json(),rubric)
                self.assertTrue(report.structural_validity)
                self.assertFalse(report.application_reference_integrity)
