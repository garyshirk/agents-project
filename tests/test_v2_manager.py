import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from arbitrage.v2.contracts import (
    Artifact, ArtifactType, AssessmentDraft, AssumptionProposal, ComparableInterpretation,
    ComparableUse, EconomicAssumption, HumanActor, ManagerContextLimits, ManagerProposal,
    OpportunitySubmission, Producer, RawResearchReport, ResearchRequest, ScenarioProposal,
    SummaryDraft, SummaryExplanation, SummaryProposal, SupportingRecord, UnknownEconomicInput, WorkflowLimits, WorkflowStep,
)
from arbitrage.v2.manager import OfflineManager, StaleManagerContext, read_context
from arbitrage.v2.persistence import IdempotencyConflict, V2PersistenceError, V2Repository
from arbitrage.v2.workflow import OfflineWorkflow
from tests.v2_comparable_fixtures import synthetic_comparables
from tests.v2_economic_fixtures import provenance, replace_input, scenario


class ManagerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = V2Repository(self.root/'v2.db',session_database_path=self.root/'sessions.db')
        self.repo.initialize_schema()
        self.workflow = OfflineWorkflow(self.repo)
        self.manager = OfflineManager(self.repo)
        self.accepted = self.workflow.accept(OpportunitySubmission(original_text='Synthetic product opportunity, not verified inventory.',
            scenario_kind='HYPOTHETICAL',human_actor='Fixture human'),WorkflowLimits(max_specialist_invocations=50,
            max_attempts_per_step=3,max_workflow_steps=50,max_calculation_attempts=10),operation_key='accept')
        self.eid = self.accepted.evaluation.evaluation_id
        self.producer = Producer(kind='APPLICATION',name='Synthetic test fixture')
        self.n = 0
        self.limits = ManagerContextLimits(max_artifacts=50,max_characters=500000)

    def key(self):
        self.n += 1
        return str(self.n)

    def context(self,*refs,**kwargs):
        return self.manager.build_context(self.eid,refs,limits=kwargs.pop('limits',self.limits),operation_key=kwargs.pop('operation_key',self.key()),**kwargs)

    def request(self):
        return ResearchRequest(kind='RESEARCH_REQUEST',objective='Investigate fictional seller shipping.',
            target_subject='Synthetic item',requested_role='ECONOMIC_COSTS',scope=('Offline proposal only.',),max_invocations=1)

    def proposal(self,context,intent=None,**changes):
        return ManagerProposal(**(dict(opportunity_id=context.reference.opportunity_id,evaluation_id=self.eid,
            context=context.reference,author_name='Synthetic Manager',rationale='Explicit fictional decision rationale.',
            evidence_references=(),limitations=('No real research.',),uncertainties=('Fictional evidence only.',),
            intent=intent or self.request()) | changes))

    def submit(self,context,proposal=None,**kwargs):
        key=kwargs.pop('operation_key',self.key())
        end = self.manager.submit(context.reference,proposal or self.proposal(context),step_id=kwargs.pop('step_id','manager-'+key),operation_key=key,**kwargs)
        return self.repo.get_artifact(end.payload.attempt_references[0])

    def result(self,attempt):
        return self.repo.get_artifact(attempt.payload.output_references[0])

    def outcome(self,attempt):
        return self.repo.get_artifact(self.result(attempt).payload.outcome)

    def raw(self):
        return self.repo.record_artifact(self.eid,RawResearchReport(report_text='Synthetic evidence only.'),producer=self.producer,operation_key=self.key())

    def comp(self,raw,index=0):
        return self.repo.record_comparable(self.eid,synthetic_comparables(raw.reference)[index],producer=self.producer,operation_key=self.key())

    def scenario_artifact(self,unknown=False):
        raw=self.raw()
        comp=self.comp(raw)
        draft=replace_input(scenario(raw.reference,comp.reference),'resale',amount='101.23')
        if unknown:
            draft=replace_input(draft,'shipping',disposition='UNKNOWN',amount=None,basis=None)
        context=self.context(raw.reference,comp.reference)
        attempt=self.submit(context,self.proposal(context,ScenarioProposal(kind='SCENARIO_PROPOSAL',scenario=draft)))
        self.assertEqual(attempt.payload.status,'SUCCEEDED')
        return raw,comp,self.outcome(attempt)

    def summary(self,context,calc=None,comp=None,assumptions=(),state='INSUFFICIENT_INFORMATION',**changes):
        draft=SummaryDraft(assessment_state=state,state_decision_rationale='Authored synthetic economic judgment.',
            consequential_uncertainties=('Fictional information; applicability uncertain.',),known_economic_facts=(),
            unknown_economic_inputs=(),assumptions=tuple(a.payload.assumption for a in assumptions),
            limitations=('No actual resale prediction.',),policy_version='synthetic-v1',numerical_estimate=calc is not None and state!='INSUFFICIENT_INFORMATION',
            calculation_references=(calc.reference,) if calc else ())
        explanation=SummaryExplanation(opportunity_evaluated='Fictional acquisition opportunity.',evidence_explanation='Explicit fictional comparable evidence.',
            comparables=(ComparableUse(comparable=comp.reference,rationale='Useful fictional comparison despite differences.'),) if comp else (),
            product_differences=('Fictional colorway differs.',),assumptions_explanation='Only explicitly recorded assumptions apply.',
            assumption_references=tuple(a.reference for a in assumptions),uncertain_costs=(),confidence_improvements=('Obtain actual applicable cost evidence.',),
            conditional_usefulness='A bounded sensitivity illustration, not verified profit.' if state=='CONDITIONAL_ESTIMATE' else None)
        return SummaryProposal(**(dict(kind='ECONOMIC_SUMMARY',summary=draft,expected_summary=context.payload.publication_target,explanation=explanation)|changes))

    def test_exact_context_original_intake_and_immutable_references(self):
        raw=self.raw()
        context=self.context(raw.reference)
        snapshot=read_context(context)
        self.assertEqual(snapshot.opportunity,self.accepted.opportunity)
        self.assertEqual(snapshot.artifacts,(raw,))
        self.assertEqual(context.payload.selected_references,(raw.reference,))
        self.assertEqual(snapshot.evaluation.evaluation_id,self.eid)
        with self.assertRaises(ValidationError):
            context.payload.snapshot_json='changed'

    def test_context_hash_round_trip(self):
        context=self.context()
        self.assertEqual(Artifact.model_validate_json(context.model_dump_json()),context)
        self.assertEqual(hashlib.sha256(context.payload.snapshot_json.encode()).hexdigest(),context.payload.snapshot_sha256)

    def test_context_bounds_fail_without_persisting(self):
        raw=self.raw()
        for limits in (ManagerContextLimits(max_artifacts=1,max_characters=500000),ManagerContextLimits(max_artifacts=50,max_characters=10)):
            before=self.repo.list_artifacts(self.eid)
            with self.assertRaises(V2PersistenceError):
                self.context(raw.reference,limits=limits)
            self.assertEqual(self.repo.list_artifacts(self.eid),before)

    def test_context_records_selection_limitations_and_progress(self):
        raw=self.raw()
        context=self.context(selection_limitations=('Raw report intentionally omitted from this decision.',))
        snapshot=read_context(context)
        self.assertNotIn(raw,snapshot.artifacts)
        self.assertGreater(snapshot.available_artifact_count,len(snapshot.artifacts))
        self.assertEqual(snapshot.workflow_event.payload.phase,'ACCEPTED')
        self.assertTrue(context.payload.selection_limitations)

    def test_context_construction_does_not_invalidate_itself(self):
        first=self.context()
        self.context()
        self.assertEqual(self.submit(first).payload.status,'SUCCEEDED')

    def test_research_request_durable_without_research_execution(self):
        context=self.context()
        with patch('socket.socket',side_effect=AssertionError('No network')),patch('agents.Runner.run_sync',side_effect=AssertionError('No model')):
            attempt=self.submit(context)
        result=self.result(attempt)
        self.assertIsNone(result.payload.outcome)
        decision=self.repo.get_artifact(result.payload.decision)
        self.assertEqual(decision.payload.proposal.intent.objective,self.request().objective)
        self.assertFalse(self.repo.list_comparable_history(self.eid))

    def test_research_request_requires_bounded_scope_and_reason(self):
        with self.assertRaises(ValidationError):
            self.request().model_validate(self.request().model_dump()|{'scope':()})
        with self.assertRaises(ValidationError):
            self.request().model_validate(self.request().model_dump()|{'max_invocations':0})
        context=self.context()
        with self.assertRaises(ValidationError):
            self.proposal(context,rationale=' ')

    def test_comparable_interpretation_preserves_differences_and_price_type(self):
        raw=self.raw()
        comp=self.comp(raw,1)
        context=self.context(raw.reference,comp.reference)
        assessment=AssessmentDraft(candidate_context=context.payload.review_subject,candidate_identity=comp.payload.product,
            category='CLOSE_WITH_KNOWN_DIFFERENCES',explanation='Known differences remain useful for a qualified comparison.',
            material_known_differences=('Width unknown.',),provenance=provenance(),policy_version='synthetic-v1')
        intent=ComparableInterpretation(kind='COMPARABLE_INTERPRETATION',comparable=comp.reference,selected_for_analysis=True,assessment=assessment)
        outcome=self.outcome(self.submit(context,self.proposal(context,intent)))
        self.assertEqual(outcome.payload.category,'CLOSE_WITH_KNOWN_DIFFERENCES')
        self.assertEqual(self.repo.get_comparable(comp.reference),comp)
        self.assertEqual(comp.payload.price_evidence.evidence_type,'ACTIVE_ASK')

    def test_weak_evidence_is_not_automatically_excluded(self):
        raw=self.raw(); comp=self.comp(raw,2); context=self.context(comp.reference)
        intent=ComparableInterpretation(kind='COMPARABLE_INTERPRETATION',comparable=comp.reference,selected_for_analysis=True,
            assessment=AssessmentDraft(candidate_context=context.payload.review_subject,candidate_identity=comp.payload.product,
                category='WEAK',explanation='Explicitly retained weak comparison.',provenance=provenance(),policy_version='synthetic-v1'))
        self.assertEqual(self.submit(context,self.proposal(context,intent)).payload.status,'SUCCEEDED')

    def assumption(self,context,**changes):
        return self.proposal(context,AssumptionProposal(kind='ECONOMIC_ASSUMPTION',assumption=EconomicAssumption(**(
            dict(name='shipping',amount='9',unit='USD/FIXED_PER_BATCH',explanation='Explicit fictional cost assumption.',provenance=provenance(True))|changes))))

    def test_general_knowledge_assumption_without_url(self):
        context=self.context()
        outcome=self.outcome(self.submit(context,self.assumption(context)))
        self.assertEqual(outcome.payload.assumption.provenance.sources,('MODEL_GENERAL_KNOWLEDGE',))
        self.assertEqual(outcome.payload.assumption.amount,9)

    def test_current_evidence_assumption_requires_exact_support(self):
        raw=self.raw(); context=self.context(raw.reference)
        support=provenance(True,sources=('CURRENT_EVALUATION_EVIDENCE',),supporting_records=(SupportingRecord(
            source='CURRENT_EVALUATION_EVIDENCE',artifact=raw.reference),))
        self.assertEqual(self.submit(context,self.assumption(context,provenance=support)).payload.status,'SUCCEEDED')

    def test_unsupported_historical_claim_rejected(self):
        context=self.context()
        proposal=self.assumption(context,provenance=provenance(True,sources=('HISTORICAL_EVALUATIONS',)))
        self.assertEqual(self.submit(context,proposal).payload.status,'INVALID_OUTPUT')

    def test_explicit_historical_record_supported_without_automatic_retrieval(self):
        other=self.repo.accept_opportunity(self.accepted.opportunity.submission,operation_key=self.key())
        ref=other.evaluation.current_review_subject
        context=self.context(historical_references=(ref,))
        p=provenance(True,sources=('HISTORICAL_EVALUATIONS',),supporting_records=(SupportingRecord(source='HISTORICAL_EVALUATIONS',artifact=ref),))
        self.assertEqual(self.submit(context,self.assumption(context,provenance=p)).payload.status,'SUCCEEDED')
        self.assertEqual(read_context(context).historical_artifacts[0].reference,ref)

    def test_human_and_multiple_source_assumptions(self):
        context=self.context()
        p=provenance(True,information_kind='HUMAN_ASSUMPTION',sources=('HUMAN_PROVIDED','MODEL_GENERAL_KNOWLEDGE'))
        outcome=self.outcome(self.submit(context,self.assumption(context,provenance=p)))
        self.assertEqual(outcome.payload.assumption.provenance,p)

    def test_assumption_unit_validation(self):
        context=self.context()
        self.assertEqual(self.submit(context,self.assumption(context,unit='USD/unknown')).payload.status,'INVALID_OUTPUT')

    def test_missing_current_support_rejected(self):
        context=self.context()
        self.assertEqual(self.submit(context,self.assumption(context,provenance=provenance(True,sources=('CURRENT_EVALUATION_EVIDENCE',)))).payload.status,'INVALID_OUTPUT')

    def test_scenario_proposal_persists_without_automatic_calculation(self):
        _,_,artifact=self.scenario_artifact()
        self.assertEqual(artifact.artifact_type,'ECONOMIC_SCENARIO')
        self.assertFalse(self.repo.list_calculation_history(self.eid))

    def test_unknown_costs_remain_unknown(self):
        _,_,artifact=self.scenario_artifact(True)
        calc=self.repo.calculate_scenario(artifact.reference,producer=self.producer,operation_key=self.key())
        self.assertEqual(calc.payload.status,'INSUFFICIENT_INPUTS')
        self.assertIsNone(calc.payload.engine_output_json)

    def test_invalid_fee_or_currency_scenario_rejected(self):
        raw=self.raw(); comp=self.comp(raw)
        draft=replace_input(scenario(raw.reference,comp.reference),'shipping',currency='EUR')
        context=self.context(raw.reference,comp.reference)
        result=self.submit(context,self.proposal(context,ScenarioProposal(kind='SCENARIO_PROPOSAL',scenario=draft)))
        self.assertEqual(result.payload.status,'INVALID_OUTPUT')
        self.assertFalse(self.repo.list_scenario_history(self.eid))

    def test_evidence_based_summary_and_exact_calculation(self):
        raw,comp,artifact=self.scenario_artifact()
        end=self.workflow.run(self.eid,(WorkflowStep(step_id='application-calculation',role='CALCULATION',scenario=artifact.reference,
            context_references=(artifact.reference,),context_text='Explicit application calculation request.'),),operation_key=self.key())
        calculation_attempt=self.repo.get_artifact(end.payload.attempt_references[0])
        calc=self.repo.get_artifact(calculation_attempt.payload.output_references[0])
        context=self.context(raw.reference,comp.reference,artifact.reference,calc.reference)
        attempt=self.submit(context,self.proposal(context,self.summary(context,calc,comp,state='EVIDENCE_BASED_ESTIMATE')))
        self.assertEqual(attempt.payload.status,'SUCCEEDED')
        summary=self.outcome(attempt)
        self.assertEqual(summary.payload.assessment_state,'EVIDENCE_BASED_ESTIMATE')
        self.assertEqual(self.repo.get_evaluation(self.eid).current_review_subject,summary.reference)

    def test_conditional_summary_requires_usefulness_and_uncertainties(self):
        context=self.context()
        intent=self.summary(context,state='CONDITIONAL_ESTIMATE')
        with self.assertRaises(ValidationError):
            SummaryProposal.model_validate(intent.model_dump()|{'explanation':intent.explanation.model_dump()|{'conditional_usefulness':None}})

    def test_insufficient_summary_is_authored_without_calculation(self):
        context=self.context()
        result=self.submit(context,self.proposal(context,self.summary(context)))
        self.assertEqual(self.outcome(result).payload.assessment_state,'INSUFFICIENT_INFORMATION')
        self.assertFalse(self.repo.list_calculation_history(self.eid))

    def test_summary_rationale_nonblank_and_next_evidence_required(self):
        context=self.context(); intent=self.summary(context)
        for change in ({'opportunity_evaluated':' '},{'confidence_improvements':()}):
            with self.assertRaises(ValidationError):
                SummaryExplanation.model_validate(intent.explanation.model_dump()|change)

    def test_no_invented_engine_result_field(self):
        context=self.context(); data=self.proposal(context).model_dump(mode='json')
        data['intent']['net_profit']='99'
        attempt=self.submit(context,json.dumps(data))
        self.assertEqual(attempt.payload.status,'INVALID_OUTPUT')

    def test_new_evidence_makes_context_stale(self):
        context=self.context(); self.raw()
        result=self.submit(context)
        self.assertEqual(result.payload.status,'INVALID_OUTPUT')
        self.assertIn('stale',result.payload.failure.message)

    def test_publication_target_changes_are_not_rebound(self):
        first=self.context(); second=self.context()
        self.submit(first,self.proposal(first,self.summary(first)))
        result=self.submit(second,self.proposal(second,self.summary(second)))
        self.assertEqual(result.payload.status,'INVALID_OUTPUT')
        self.assertEqual(self.repo.get_current_summary(self.eid).payload.revision,1)

    def test_cross_evaluation_context_selection_rejected(self):
        other=self.repo.accept_opportunity(self.accepted.opportunity.submission,operation_key=self.key())
        with self.assertRaises(V2PersistenceError):
            self.context(other.evaluation.current_review_subject)

    def test_cross_evaluation_proposal_rejected(self):
        context=self.context()
        self.assertEqual(self.submit(context,self.proposal(context,evaluation_id='other')).payload.status,'INVALID_OUTPUT')

    def test_malformed_proposal_failure_preserves_evidence(self):
        raw=self.raw(); context=self.context(raw.reference)
        attempt=self.submit(context,'not-json')
        self.assertEqual(attempt.payload.status,'INVALID_OUTPUT')
        self.assertEqual(self.repo.get_artifact(raw.reference),raw)

    def test_revision_against_fresh_context_preserves_lineage(self):
        context=self.context(); first=self.result(self.submit(context))
        decision=self.repo.get_artifact(first.payload.decision)
        fresh=self.context(decision.reference)
        second=self.result(self.submit(fresh,self.proposal(fresh,supersedes=decision.reference)))
        revised=self.repo.get_artifact(second.payload.decision)
        self.assertEqual(revised.payload.decision_id,decision.payload.decision_id)
        self.assertEqual(revised.payload.revision,2)
        self.assertEqual(self.repo.get_artifact(decision.reference),decision)

    def test_idempotent_manager_workflow_retry(self):
        context=self.context(); proposal=self.proposal(context)
        first=self.submit(context,proposal,step_id='same',operation_key='same')
        before=self.repo.list_artifacts(self.eid)
        self.assertEqual(self.submit(context,proposal,step_id='same',operation_key='same'),first)
        self.assertEqual(self.repo.list_artifacts(self.eid),before)

    def test_conflicting_manager_operation_key_rejected(self):
        context=self.context(); self.submit(context,operation_key='same',step_id='same')
        with self.assertRaises(IdempotencyConflict):
            self.submit(context,self.proposal(context,rationale='Different authored purpose.'),operation_key='same',step_id='same')

    def test_restart_reconstructs_decision(self):
        context=self.context(); result=self.result(self.submit(context))
        restarted=V2Repository(self.repo.database_path,session_database_path=self.repo.paths.sessions)
        self.assertEqual(restarted.get_artifact(result.reference),result)
        self.assertEqual(read_context(restarted.get_artifact(context.reference)),read_context(context))

    def test_manager_step_is_bounded_and_attributed(self):
        context=self.context(); result=self.submit(context)
        self.assertEqual(result.payload.input_manifest.role,'MANAGER')
        self.assertIn('Synthetic offline fixture',self.result(result).producer.name)
        self.assertEqual(result.payload.input_manifest.context_references,(context.reference,))

    def test_workflow_limit_applies_to_manager_steps(self):
        # A batch exceeding limits is rejected without a Manager invocation.
        other=self.workflow.accept(self.accepted.opportunity.submission,WorkflowLimits(max_specialist_invocations=1,
            max_attempts_per_step=1,max_workflow_steps=2,max_calculation_attempts=1),operation_key=self.key())
        context=self.manager.build_context(other.evaluation.evaluation_id,(),limits=self.limits,operation_key=self.key())
        p=self.proposal(context,evaluation_id=other.evaluation.evaluation_id)
        self.manager.submit(context.reference,p,step_id='one',operation_key=self.key())
        with self.assertRaises(V2PersistenceError):
            self.manager.submit(context.reference,p,step_id='two',operation_key=self.key())

    def test_failed_proposal_does_not_replace_valid_summary(self):
        context=self.context(); original=self.outcome(self.submit(context,self.proposal(context,self.summary(context))))
        fresh=self.context(original.reference)
        self.submit(fresh,'bad-json')
        self.assertEqual(self.repo.get_current_summary(self.eid),original)

    def test_new_decision_invalidates_prior_human_review(self):
        subject=self.repo.get_evaluation(self.eid).current_review_subject
        review=self.repo.record_review(self.eid,subject,'WORTH_INVESTIGATING','Synthetic human review.',human=HumanActor(actor='Human'),operation_key=self.key())
        self.submit(self.context())
        self.assertFalse(self.repo.review_is_current(review.review_id))
        self.assertEqual(self.repo.list_reviews(self.eid)[0],review)

    def test_human_review_and_commercial_intents_not_permitted(self):
        for kind in ('HUMAN_REVIEW','PURCHASE','LIST','PAYMENT'):
            context=self.context()
            data=self.proposal(context).model_dump(mode='json'); data['intent']={'kind':kind}
            self.assertEqual(self.submit(context,json.dumps(data)).payload.status,'INVALID_OUTPUT')
        self.assertFalse(self.repo.list_reviews(self.eid))
        self.assertEqual(self.repo.list_review_queue().total,1)

    def test_no_live_calls_or_database_changes(self):
        root=Path(__file__).resolve().parents[1]
        paths=[root/name for name in ('arbitrage.db','sessions.db','dev_data/v2/arbitrage.db','dev_data/v2/sessions.db')]
        before=[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        with patch('socket.socket',side_effect=AssertionError('No network')),patch('agents.Runner.run_sync',side_effect=AssertionError('No model')):
            self.submit(self.context())
        self.assertEqual(before,[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])

    def test_atomic_domain_failure_rolls_back_accepted_decision(self):
        context=self.context()
        with patch.object(self.repo,'record_artifact',side_effect=sqlite3.OperationalError('Synthetic domain failure')):
            with self.assertRaises(RuntimeError):
                self.submit(context,self.assumption(context))
        self.assertFalse(any(a.artifact_type in {ArtifactType.MANAGER_DECISION,ArtifactType.SCENARIO_ASSUMPTION} for a in self.repo.list_artifacts(self.eid)))

    def test_application_receipt_failure_rolls_back_domain_and_decision(self):
        context=self.context()
        with closing(sqlite3.connect(self.repo.database_path)) as c:
            c.execute("CREATE TRIGGER fail_manager_receipt BEFORE INSERT ON v2_operations WHEN NEW.operation='manager_decision' "
                "BEGIN SELECT RAISE(ABORT,'Synthetic Manager receipt failure'); END")
            c.commit()
        with self.assertRaises(RuntimeError):
            self.submit(context,self.assumption(context))
        self.assertFalse(any(a.artifact_type in {ArtifactType.MANAGER_DECISION,ArtifactType.SCENARIO_ASSUMPTION} for a in self.repo.list_artifacts(self.eid)))

    def test_concurrent_context_conflict_has_one_accepted_decision(self):
        context=self.context(); encoded=self.proposal(context).model_dump_json()
        def apply(n):
            repo=V2Repository(self.repo.database_path,session_database_path=self.repo.paths.sessions)
            try:
                return repo.record_manager_decision(context.reference,encoded,producer=self.producer,operation_key='concurrent-'+str(n))
            except StaleManagerContext:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(apply,range(2)))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_context_idempotency_and_conflict(self):
        first=self.context(operation_key='context')
        self.assertEqual(self.context(operation_key='context'),first)
        with self.assertRaises(IdempotencyConflict):
            self.context(operation_key='context',selection_limitations=('Different selection.',))

    def test_numerical_assumption_cannot_be_observed_fact(self):
        with self.assertRaises(ValidationError):
            EconomicAssumption(name='shipping',amount='9',unit='USD/FIXED_PER_BATCH',explanation='Invalid observation promotion.',provenance=provenance())

    def conditional_fixture(self):
        raw=self.raw(); comp=self.comp(raw,1)
        first=self.context(raw.reference,comp.reference)
        resale=self.outcome(self.submit(first,self.assumption(first,name='resale',amount='100',unit='USD/FIXED_PER_UNIT')))
        second=self.context(raw.reference,comp.reference,resale.reference)
        shipping=self.outcome(self.submit(second,self.assumption(second)))
        draft=scenario(raw.reference,comp.reference)
        draft=replace_input(draft,'resale',disposition='ASSUMPTION',basis='ASSUMED',amount='100',
            provenance=resale.payload.assumption.provenance,assumption=resale.reference)
        draft=replace_input(draft,'shipping',disposition='ASSUMPTION',basis='ASSUMED',amount='9',
            provenance=shipping.payload.assumption.provenance,assumption=shipping.reference)
        draft=draft.model_copy(update={'known_unknowns':(
            UnknownEconomicInput(name='shipping',reason='No actual carrier quote.'),
            UnknownEconomicInput(name='resale',reason='Achievable price unknown; listing remains an asking price.'))})
        refs=(raw.reference,comp.reference,resale.reference,shipping.reference)
        context=self.context(*refs)
        result=self.submit(context,self.proposal(context,ScenarioProposal(kind='SCENARIO_PROPOSAL',scenario=draft)))
        artifact=self.outcome(result)
        calc=self.repo.calculate_scenario(artifact.reference,producer=self.producer,operation_key=self.key())
        context=self.context(*refs,artifact.reference,calc.reference)
        return context,comp,calc,(resale,shipping)

    def test_asking_price_conditional_vertical_slice_preserves_assumptions(self):
        context,comp,calc,assumptions=self.conditional_fixture()
        intent=self.summary(context,calc,comp,assumptions,state='CONDITIONAL_ESTIMATE')
        result=self.submit(context,self.proposal(context,intent))
        self.assertEqual(result.payload.status,'SUCCEEDED')
        self.assertEqual(self.outcome(result).payload.assessment_state,'CONDITIONAL_ESTIMATE')
        self.assertEqual(self.repo.get_comparable(comp.reference).payload.price_evidence.evidence_type,'ACTIVE_ASK')
        self.assertEqual(calc.payload.assumption_references,tuple(a.reference for a in assumptions))

    def test_summary_cannot_omit_calculation_assumptions(self):
        context,comp,calc,assumptions=self.conditional_fixture()
        result=self.submit(context,self.proposal(context,self.summary(context,calc,comp,state='CONDITIONAL_ESTIMATE')))
        self.assertEqual(result.payload.status,'INVALID_OUTPUT')
        self.assertIn('disclose',result.payload.failure.message)

    def test_summary_cannot_relabel_or_change_assumed_value(self):
        context,comp,calc,assumptions=self.conditional_fixture()
        intent=self.summary(context,calc,comp,assumptions,state='CONDITIONAL_ESTIMATE')
        altered=intent.summary.assumptions[0].model_copy(update={'amount':999})
        intent=intent.model_copy(update={'summary':intent.summary.model_copy(update={'assumptions':(altered,assumptions[1].payload.assumption)})})
        self.assertEqual(self.submit(context,self.proposal(context,intent)).payload.status,'INVALID_OUTPUT')

    def test_calculation_context_must_include_exact_scenario(self):
        raw,comp,artifact=self.scenario_artifact()
        calc=self.repo.calculate_scenario(artifact.reference,producer=self.producer,operation_key=self.key())
        context=self.context(raw.reference,comp.reference,calc.reference)
        result=self.submit(context,self.proposal(context,self.summary(context,calc,comp,state='EVIDENCE_BASED_ESTIMATE')))
        self.assertEqual(result.payload.status,'INVALID_OUTPUT')

    def test_summary_must_explain_used_comparables(self):
        raw,comp,artifact=self.scenario_artifact()
        calc=self.repo.calculate_scenario(artifact.reference,producer=self.producer,operation_key=self.key())
        context=self.context(raw.reference,comp.reference,artifact.reference,calc.reference)
        intent=self.summary(context,calc,state='EVIDENCE_BASED_ESTIMATE')
        self.assertEqual(self.submit(context,self.proposal(context,intent)).payload.status,'INVALID_OUTPUT')

    def test_strong_fixture_multiple_completed_comparables(self):
        raw=self.raw(); first=self.comp(raw)
        second_draft=synthetic_comparables(raw.reference)[0]
        second_draft=second_draft.model_copy(update={'source':second_draft.source.model_copy(update={
            'url':'https://market.example.invalid/independent-fictional-listing','seller_name':'Second fictional seller'})})
        second=self.repo.record_comparable(self.eid,second_draft,producer=self.producer,operation_key=self.key())
        draft=replace_input(scenario(raw.reference,first.reference),'resale',amount='101.23')
        draft=draft.model_copy(update={'comparable_references':(first.reference,second.reference)})
        context=self.context(raw.reference,first.reference,second.reference)
        artifact=self.outcome(self.submit(context,self.proposal(context,ScenarioProposal(kind='SCENARIO_PROPOSAL',scenario=draft))))
        calc=self.repo.calculate_scenario(artifact.reference,producer=self.producer,operation_key=self.key())
        context=self.context(raw.reference,first.reference,second.reference,artifact.reference,calc.reference)
        intent=self.summary(context,calc,first,state='EVIDENCE_BASED_ESTIMATE')
        intent=intent.model_copy(update={'explanation':intent.explanation.model_copy(update={'comparables':(
            *intent.explanation.comparables,ComparableUse(comparable=second.reference,rationale='Second fictional completed-sale reference; no averaging.'))})})
        self.assertEqual(self.submit(context,self.proposal(context,intent)).payload.status,'SUCCEEDED')

    def test_partial_failure_context_contains_workflow_failure(self):
        context=self.context(); self.submit(context,'bad-json')
        failed=self.repo.get_workflow(self.eid).attempts[-1]
        fresh=self.context(failed.reference)
        self.assertEqual(read_context(fresh).artifacts[0].payload.status,'INVALID_OUTPUT')
        self.assertEqual(self.submit(fresh,self.proposal(fresh,self.summary(fresh))).payload.status,'SUCCEEDED')

    def test_unselected_reference_rejected_without_rebinding(self):
        raw=self.raw(); context=self.context()
        proposal=self.proposal(context,evidence_references=(raw.reference,))
        self.assertEqual(self.submit(context,proposal).payload.status,'INVALID_OUTPUT')

    def test_json_float_inputs_rejected(self):
        context=self.context(); data=self.assumption(context).model_dump(mode='json')
        data['intent']['assumption']['amount']=9.5
        self.assertEqual(self.submit(context,json.dumps(data)).payload.status,'INVALID_OUTPUT')

    def test_manager_context_has_no_review_mutation_authority(self):
        context=self.context()
        self.assertFalse(hasattr(read_context(context),'repository'))
        self.assertFalse(hasattr(read_context(context),'record_review'))
        self.assertFalse(self.repo.list_reviews(self.eid))

    def test_accepted_manager_output_recovery_does_not_reapply_decision(self):
        context=self.context(); proposal=self.proposal(context)
        with patch('arbitrage.v2.workflow.OfflineWorkflow._complete',side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.submit(context,proposal,step_id='recoverable',operation_key='recoverable')
        snapshot=self.repo.get_workflow(self.eid)
        self.assertEqual(len([a for a in self.repo.list_artifacts(self.eid) if a.artifact_type==ArtifactType.MANAGER_DECISION]),1)
        self.workflow.recover(snapshot.unresolved_runs[0],decision='RECONCILE',operation_key='recover')
        self.assertEqual(len([a for a in self.repo.list_artifacts(self.eid) if a.artifact_type==ArtifactType.MANAGER_DECISION]),1)
        self.assertFalse(self.repo.get_workflow(self.eid).unfinished_attempts)

    def test_insufficient_summary_must_disclose_named_material_unknowns(self):
        raw,comp,artifact=self.scenario_artifact(True)
        calc=self.repo.calculate_scenario(artifact.reference,producer=self.producer,operation_key=self.key())
        context=self.context(raw.reference,comp.reference,artifact.reference,calc.reference)
        intent=self.summary(context,calc,comp)
        self.assertEqual(self.submit(context,self.proposal(context,intent)).payload.status,'INVALID_OUTPUT')
        fresh=self.context(raw.reference,comp.reference,artifact.reference,calc.reference)
        intent=self.summary(fresh,calc,comp)
        intent=intent.model_copy(update={'explanation':intent.explanation.model_copy(update={'uncertain_costs':(
            UnknownEconomicInput(name='shipping',reason='No seller shipping quote.'),)})})
        self.assertEqual(self.submit(fresh,self.proposal(fresh,intent)).payload.status,'SUCCEEDED')


if __name__=='__main__':
    unittest.main()
