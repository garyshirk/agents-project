import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import patch

from pydantic import ValidationError

from arbitrage.v2.contracts import (
    ArtifactType, AssessmentDraft, AssumptionRecord, EconomicAssumption, FailureRecord,
    HumanActor, OfflineFixture, OpportunitySubmission, Producer, RawResearchReport,
    SummaryDraft, WorkflowLimits, WorkflowStep,
)
from arbitrage.v2.persistence import IdempotencyConflict, V2PersistenceError, V2Repository
from arbitrage.v2.review import HumanReviewService
from arbitrage.v2.workflow import (
    AssessmentOutput, AssumptionOutput, ComparableOutput, OfflineResponse, OfflineWorkflow,
    RecoveryRequired, ResearchOutput, ScenarioOutput, SpecialistFailure, SummaryOutput,
    WorkflowPersistenceFailure, invoke_offline_fixture, output_operation_key,
)
from tests.v2_comparable_fixtures import synthetic_comparables
from tests.v2_economic_fixtures import provenance, replace_input, scenario
from tests.v2_workflow_fixtures import fixture_step


class V2WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = V2Repository(self.root / "v2.db",session_database_path=self.root / "sessions.db")
        self.repo.initialize_schema()
        self.workflow = OfflineWorkflow(self.repo)
        self.limits = WorkflowLimits(max_specialist_invocations=30,max_attempts_per_step=3,
            max_workflow_steps=30,max_calculation_attempts=5)
        self.submission = OpportunitySubmission(original_text="Synthetic hypothetical product offered for $40. No real research.",
            scenario_kind="HYPOTHETICAL",human_actor="Fixture author")
        self.accepted = self.workflow.accept(self.submission,self.limits,operation_key="accept")
        self.eid = self.accepted.evaluation.evaluation_id
        self.producer = Producer(kind="APPLICATION",name="Synthetic fixture author")

    def raw_step(self, name="research", **options):
        return fixture_step(self.accepted,name,"RESEARCH",ResearchOutput(role="RESEARCH",
            report=RawResearchReport(report_text="Entirely synthetic offline evidence, not actual research.")),**options)

    def run_step(self, step, key=None):
        end = self.workflow.run(self.eid,(step,),operation_key=key or "run-" + step.step_id)
        return self.repo.get_artifact(end.payload.attempt_references[0])

    def output(self, attempt):
        return self.repo.get_artifact(attempt.payload.output_references[0])

    def research(self):
        return self.output(self.run_step(self.raw_step()))

    def comparables(self, raw):
        outputs = []
        for n,draft in enumerate(synthetic_comparables(raw.reference)):
            outputs.append(self.output(self.run_step(fixture_step(self.accepted,f"comparable-{n}","COMPARABLE",
                ComparableOutput(role="COMPARABLE",draft=draft),refs=(raw.reference,)))))
        return outputs

    def economic_scenario(self, *, unknown=False, invalid=False):
        raw = self.research()
        comparable = self.comparables(raw)[0]
        draft = scenario(raw.reference,comparable.reference)
        draft = replace_input(draft,"resale",amount="101.23")
        if unknown:
            draft = replace_input(draft,"shipping",disposition="UNKNOWN",amount=None,basis=None)
        if invalid:
            draft = replace_input(draft,"shipping",currency="EUR")
        artifact = self.output(self.run_step(fixture_step(self.accepted,"scenario","SCENARIO",
            ScenarioOutput(role="SCENARIO",draft=draft),refs=(raw.reference,comparable.reference))))
        return artifact

    def calculate(self, artifact, step_id="calculate", **options):
        return self.run_step(WorkflowStep(step_id=step_id,role="CALCULATION",scenario=artifact.reference,
            context_references=(artifact.reference,),context_text="Calculate the explicitly supplied synthetic scenario.",**options))

    def summary_step(self, calculation=None, state="INSUFFICIENT_INFORMATION", step_id="summary", **changes):
        draft = SummaryDraft(**(dict(assessment_state=state,state_decision_rationale="Explicit synthetic author judgment.",
            consequential_uncertainties=("All data are fictional.",),known_economic_facts=(),unknown_economic_inputs=(),
            assumptions=(),limitations=("Offline fixture only.",),policy_version="synthetic-workflow-v1",
            calculation_references=(calculation.reference,) if calculation else ()) | changes))
        return fixture_step(self.accepted,step_id,"SUMMARY",SummaryOutput(role="SUMMARY",draft=draft),
            refs=(calculation.reference,) if calculation else ())

    def snapshot(self):
        return self.repo.get_workflow(self.eid)

    def interrupted_run(self, step=None):
        with patch.object(self.workflow,"_execute",side_effect=KeyboardInterrupt("Synthetic interruption")):
            with self.assertRaises(KeyboardInterrupt):
                self.workflow.run(self.eid,(step or self.raw_step(),),operation_key="interrupted-run")
        return self.snapshot().unresolved_runs[0]

    def rows(self):
        with closing(sqlite3.connect(self.repo.database_path)) as c:
            return tuple((t,tuple(c.execute(f"SELECT * FROM {t} ORDER BY rowid"))) for t in
                ("v2_evaluations","v2_artifacts","v2_reviews","v2_operations"))

    def test_durable_acceptance_precedes_any_research(self):
        state = self.snapshot()
        self.assertEqual(state.events[0].payload.phase,"ACCEPTED")
        self.assertEqual(state.evaluation.execution_state,"RUNNING")
        self.assertFalse(state.attempts)
        self.assertEqual(self.repo.list_review_queue().total,1)

    def test_accept_replay_and_conflicting_limits(self):
        self.assertEqual(self.workflow.accept(self.submission,self.limits,operation_key="accept"),self.accepted)
        with self.assertRaises(IdempotencyConflict):
            self.workflow.accept(self.submission,self.limits.model_copy(update={"max_workflow_steps":2}),operation_key="accept")
        self.assertEqual(len(self.snapshot().events),1)

    def test_acceptance_and_workflow_initialization_are_atomic(self):
        with patch.object(self.repo,"_workflow_event",side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.workflow.accept(self.submission,self.limits,operation_key="other-accept")
        self.assertEqual(self.repo.list_review_queue().total,1)
        accepted = self.workflow.accept(self.submission,self.limits,operation_key="other-accept")
        self.assertEqual(self.repo.get_workflow(accepted.evaluation.evaluation_id).events[0].payload.phase,"ACCEPTED")

    def test_attempt_start_and_exact_manifest_durable_before_fixture_invocation(self):
        step = self.raw_step(refs=(self.accepted.evaluation.current_review_subject,))
        def invoke(manifest):
            snapshot = self.snapshot()
            self.assertEqual(len(snapshot.unfinished_attempts),1)
            self.assertEqual(manifest.submission,self.submission)
            self.assertEqual(manifest.evaluation_id,self.eid)
            self.assertEqual(manifest.opportunity_id,self.accepted.opportunity.opportunity_id)
            self.assertEqual(manifest.context_references,step.context_references)
            self.assertEqual(manifest.fixture.response_json,step.fixture.response_json)
            return invoke_offline_fixture(manifest)
        with patch("arbitrage.v2.workflow.invoke_offline_fixture",side_effect=invoke):
            result = self.run_step(step)
        self.assertEqual(result.payload.status,"SUCCEEDED")
        self.assertIsNotNone(result.payload.ended_at)
        self.assertEqual(result.payload.started_at,self.snapshot().attempts[0].payload.started_at)

    def test_raw_evidence_is_synthetic_and_reviewable(self):
        raw = self.research()
        self.assertEqual(raw.artifact_type,"RAW_RESEARCH_REPORT")
        self.assertIn("Synthetic offline fixture",raw.producer.name)
        self.assertIn(raw,HumanReviewService(self.repo).get_evidence(self.eid).artifacts)

    def test_complete_vertical_slice_multiple_comparables_and_assessments(self):
        raw = self.research()
        comparables = self.comparables(raw)
        for n,comp in enumerate(comparables[:2]):
            draft = AssessmentDraft(candidate_context=self.accepted.evaluation.current_review_subject,
                candidate_identity=comp.payload.product,category="WEAK",explanation="Explicitly authored weak evidence judgment.",
                material_known_differences=("Synthetic variant mismatch.",),provenance=provenance(),policy_version="synthetic-v1")
            step = fixture_step(self.accepted,f"assessment-{n}","ASSESSMENT",
                AssessmentOutput(role="ASSESSMENT",comparable=comp.reference,draft=draft),
                refs=(comp.reference,self.accepted.evaluation.current_review_subject))
            assessment = self.output(self.run_step(step))
            self.assertEqual(assessment.payload.category,"WEAK")
        draft = replace_input(scenario(raw.reference,comparables[0].reference),"resale",amount="101.23")
        scenario_artifact = self.output(self.run_step(fixture_step(self.accepted,"scenario","SCENARIO",
            ScenarioOutput(role="SCENARIO",draft=draft),refs=(raw.reference,comparables[0].reference))))
        calculation = self.output(self.calculate(scenario_artifact))
        summary = self.output(self.run_step(self.summary_step(calculation,"EVIDENCE_BASED_ESTIMATE",numerical_estimate=True)))
        self.workflow.finish(self.eid,"FINISHED",reason="Explicit completion of synthetic fixture.",operation_key="finish")
        self.assertEqual(self.snapshot().events[-1].payload.phase,"COMPLETED")
        self.assertEqual(self.snapshot().evaluation.execution_state,"FINISHED")
        self.assertEqual(self.repo.get_current_summary(self.eid),summary)
        self.assertEqual(len(self.repo.list_comparable_history(self.eid)),3)
        self.assertEqual(self.repo.list_review_queue().total,1)

    def test_partial_failure_preserves_success_and_allows_authored_summary(self):
        good = self.raw_step("good")
        bad = fixture_step(self.accepted,"failed","RESEARCH",SpecialistFailure(role="FAILURE",
            failure=FailureRecord(stage="synthetic_research",category="NO_EVIDENCE",message="Fictional source unavailable.")))
        end = self.workflow.run(self.eid,(good,bad),operation_key="batch")
        self.assertEqual(end.payload.phase,"PARTIAL")
        attempts = [self.repo.get_artifact(r) for r in end.payload.attempt_references]
        self.assertEqual([a.payload.status for a in attempts],["SUCCEEDED","SPECIALIST_FAILURE"])
        self.assertEqual(attempts[1].payload.failure.category,"NO_EVIDENCE")
        self.assertEqual(self.output(attempts[0]).artifact_type,"RAW_RESEARCH_REPORT")
        self.run_step(self.summary_step())
        self.assertEqual(self.repo.get_current_summary(self.eid).payload.assessment_state,"INSUFFICIENT_INFORMATION")

    def test_missing_comparable_or_summary_does_not_block_review(self):
        self.research()
        self.workflow.finish(self.eid,"PARTIAL",reason="No more supplied fixtures.",operation_key="partial")
        entry = self.repo.list_review_queue().entries[0]
        self.assertEqual(entry.evaluation.execution_state,"PARTIAL")
        self.assertIsNone(entry.assessment_state)

    def test_calculation_insufficiency_keeps_no_fabricated_profit(self):
        result = self.calculate(self.economic_scenario(unknown=True))
        self.assertEqual(result.payload.status,"CALCULATION_INSUFFICIENT")
        calculation = self.output(result)
        self.assertIsNone(calculation.payload.engine_output_json)
        self.assertTrue(calculation.payload.unresolved_inputs)
        self.run_step(self.summary_step(calculation))
        self.assertEqual(self.repo.list_review_queue().total,1)

    def test_invalid_calculation_outcome_distinct_from_invalid_specialist(self):
        result = self.calculate(self.economic_scenario(invalid=True))
        self.assertEqual(result.payload.status,"CALCULATION_INVALID")
        self.assertEqual(self.output(result).payload.status,"INVALID_INPUTS")

    def test_unexpected_engine_error_is_durable_calculation_outcome(self):
        scenario_artifact = self.economic_scenario()
        with patch("arbitrage.v2.economics._calculate_profitability",side_effect=ArithmeticError("Fictional engine error")):
            result = self.calculate(scenario_artifact)
        self.assertEqual(result.payload.status,"CALCULATION_ERROR")
        self.assertEqual(self.output(result).payload.failure.message,"Fictional engine error")

    def test_invalid_json_output_is_recorded_after_invocation_start(self):
        step = self.raw_step().model_copy(update={"fixture":OfflineFixture(fixture_id="invalid",response_json="not-json")})
        result = self.run_step(step)
        self.assertEqual(result.payload.status,"INVALID_OUTPUT")
        self.assertEqual(result.payload.failure.stage,"specialist_validation")
        self.assertFalse(result.payload.output_references)

    def test_other_evaluation_identity_in_response_rejected(self):
        original = self.raw_step()
        data = json.loads(original.fixture.response_json)
        data["evaluation_id"] = "another-evaluation"
        step = original.model_copy(update={"fixture":OfflineFixture(fixture_id="wrong-owner",response_json=json.dumps(data))})
        result = self.run_step(step)
        self.assertEqual(result.payload.status,"INVALID_OUTPUT")
        self.assertIn("substitute",result.payload.failure.message)

    def test_other_opportunity_identity_rejected(self):
        original = self.raw_step()
        data = json.loads(original.fixture.response_json)
        data["opportunity_id"] = "another-opportunity"
        result = self.run_step(original.model_copy(update={"fixture":OfflineFixture(fixture_id="wrong",response_json=json.dumps(data))}))
        self.assertEqual(result.payload.status,"INVALID_OUTPUT")

    def test_output_cannot_reference_undelegated_evidence(self):
        raw = self.research()
        draft = synthetic_comparables(raw.reference)[0]
        result = self.run_step(fixture_step(self.accepted,"bad-comparable","COMPARABLE",
            ComparableOutput(role="COMPARABLE",draft=draft)))
        self.assertEqual(result.payload.status,"INVALID_OUTPUT")
        self.assertEqual(self.repo.get_artifact(raw.reference),raw)

    def test_bad_reference_sequence_rejected_before_specialist(self):
        ref = self.accepted.evaluation.current_review_subject.model_copy(update={"sequence":900})
        with patch("arbitrage.v2.workflow.invoke_offline_fixture") as invoke:
            with self.assertRaises(V2PersistenceError):
                self.run_step(self.raw_step(refs=(ref,)))
        invoke.assert_not_called()
        self.assertFalse(self.snapshot().attempts)

    def test_live_looking_comparable_cannot_masquerade_as_offline_research(self):
        raw = self.research()
        draft = synthetic_comparables(raw.reference)[0]
        draft = draft.model_copy(update={"source":draft.source.model_copy(update={"capture_method":"PUBLIC_WEB_REPORT"})})
        result = self.run_step(fixture_step(self.accepted,"live-looking","COMPARABLE",ComparableOutput(role="COMPARABLE",draft=draft),refs=(raw.reference,)))
        self.assertEqual(result.payload.status,"INVALID_OUTPUT")

    def test_specialist_output_role_substitution_rejected(self):
        step = self.raw_step()
        response = OfflineResponse(opportunity_id=self.accepted.opportunity.opportunity_id,evaluation_id=self.eid,
            output=SummaryOutput(role="SUMMARY",draft=OfflineResponse.model_validate_json(
                self.summary_step().fixture.response_json).output.draft))
        result = self.run_step(step.model_copy(update={"fixture":OfflineFixture(fixture_id="wrong-role",response_json=response.model_dump_json())}))
        self.assertEqual(result.payload.status,"INVALID_OUTPUT")

    def test_invalid_summary_does_not_replace_prior_valid_summary(self):
        original = self.output(self.run_step(self.summary_step()))
        raw = self.research()
        invalid = self.summary_step(raw,"CONDITIONAL_ESTIMATE",step_id="bad-summary",numerical_estimate=True)
        result = self.run_step(invalid)
        self.assertEqual(result.payload.status,"INVALID_OUTPUT")
        self.assertEqual(self.repo.get_current_summary(self.eid),original)

    def test_persistence_failure_reports_without_output_success(self):
        with patch.object(self.repo,"record_artifact",side_effect=sqlite3.OperationalError("Synthetic disk write failure")):
            with self.assertRaises(WorkflowPersistenceFailure):
                self.run_step(self.raw_step())
        snapshot = self.snapshot()
        self.assertEqual(snapshot.attempts[-1].payload.status,"PERSISTENCE_FAILURE")
        self.assertFalse(snapshot.attempts[-1].payload.output_references)
        self.assertTrue(snapshot.unresolved_runs)

    def test_failure_recording_itself_failing_never_claims_durability(self):
        with patch.object(self.repo,"record_artifact",side_effect=sqlite3.OperationalError("Disk failure")),patch.object(
                self.repo,"complete_workflow_attempt",side_effect=sqlite3.OperationalError("Disk still failed")):
            with self.assertRaisesRegex(WorkflowPersistenceFailure,"failure recording failed"):
                self.run_step(self.raw_step())
        self.assertEqual(self.snapshot().attempts[-1].payload.status,"STARTED")
        self.assertTrue(self.snapshot().unfinished_attempts)

    def test_coordinator_error_distinct_from_specialist_returned_failure(self):
        with patch("arbitrage.v2.workflow.invoke_offline_fixture",side_effect=RuntimeError("Synthetic boundary error")):
            result = self.run_step(self.raw_step())
        self.assertEqual(result.payload.status,"COORDINATOR_ERROR")
        self.assertEqual(result.payload.failure.category,"RuntimeError")

    def test_interrupted_run_restart_never_silently_replays(self):
        run = self.interrupted_run()
        restarted = OfflineWorkflow(V2Repository(self.repo.database_path,session_database_path=self.repo.paths.sessions))
        with patch("arbitrage.v2.workflow.invoke_offline_fixture") as invoke:
            with self.assertRaises(RecoveryRequired):
                restarted.run(self.eid,(self.raw_step(),),operation_key="interrupted-run")
            with self.assertRaises(V2PersistenceError):
                restarted.run(self.eid,(self.raw_step("other"),),operation_key="new-before-recovery")
        invoke.assert_not_called()
        self.assertEqual(restarted.repository.get_workflow(self.eid).unresolved_runs,(run,))

    def test_explicit_recovery_marks_unknown_attempt_then_bounded_retry(self):
        run = self.interrupted_run()
        end = self.workflow.recover(run,decision="RECONCILE",operation_key="recover")
        self.assertEqual(end.payload.phase,"INTERRUPTED")
        interrupted = self.repo.get_artifact(end.payload.attempt_references[0])
        self.assertEqual(interrupted.payload.status,"INTERRUPTED")
        result = self.run_step(self.raw_step(retry_of=interrupted.reference),key="retry")
        self.assertEqual(result.payload.attempt_number,2)
        self.assertEqual(result.payload.status,"SUCCEEDED")
        self.assertFalse(self.snapshot().unresolved_runs)

    def test_abandon_records_interruption_without_any_invocation(self):
        run = self.interrupted_run()
        with patch("arbitrage.v2.workflow.invoke_offline_fixture") as invoke:
            self.workflow.recover(run,decision="ABANDON",operation_key="abandon")
        invoke.assert_not_called()
        self.assertEqual(self.snapshot().attempts[-1].payload.status,"INTERRUPTED")

    def test_crash_after_fixture_before_output_persistence_is_ambiguous(self):
        with patch.object(self.workflow,"_persist_output",side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.run_step(self.raw_step())
        run = self.snapshot().unresolved_runs[0]
        with patch("arbitrage.v2.workflow.invoke_offline_fixture") as invoke:
            self.workflow.recover(run,decision="RECONCILE",operation_key="recover")
        invoke.assert_not_called()
        self.assertEqual(self.snapshot().attempts[-1].payload.status,"INTERRUPTED")

    def test_output_persisted_before_completion_reconciles_without_duplicate(self):
        with patch.object(self.workflow,"_complete",side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.run_step(self.raw_step())
        snapshot = self.snapshot()
        artifact = self.repo.get_operation_artifact(output_operation_key(snapshot.attempts[-1]))
        self.assertIsNotNone(artifact)
        with patch("arbitrage.v2.workflow.invoke_offline_fixture") as invoke:
            self.workflow.recover(snapshot.unresolved_runs[0],decision="RECONCILE",operation_key="recover")
        invoke.assert_not_called()
        self.assertEqual(self.snapshot().attempts[-1].payload.output_references,(artifact.reference,))
        reports = [a for a in self.repo.list_artifacts(self.eid) if a.artifact_type == ArtifactType.RAW_RESEARCH_REPORT]
        self.assertEqual(reports,[artifact])

    def test_calculation_persisted_before_completion_is_not_recalculated(self):
        artifact = self.economic_scenario()
        with patch.object(self.workflow,"_complete",side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.calculate(artifact)
        run = self.snapshot().unresolved_runs[0]
        with patch("arbitrage.v2.economics._calculate_profitability",side_effect=AssertionError("No recalculation")):
            self.workflow.recover(run,decision="RECONCILE",operation_key="recover")
        self.assertEqual(len(self.repo.list_calculation_history(self.eid)),1)

    def test_summary_persisted_before_completion_is_not_republished(self):
        with patch.object(self.workflow,"_complete",side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.run_step(self.summary_step())
        summary = self.repo.get_current_summary(self.eid)
        self.workflow.recover(self.snapshot().unresolved_runs[0],decision="RECONCILE",operation_key="recover")
        self.assertEqual(self.repo.get_current_summary(self.eid),summary)

    def test_crash_after_attempt_completion_before_run_end_reconciles(self):
        with patch.object(self.repo,"end_workflow_run",side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.run_step(self.raw_step())
        run = self.snapshot().unresolved_runs[0]
        self.assertFalse(self.snapshot().unfinished_attempts)
        self.workflow.recover(run,decision="RECONCILE",operation_key="recover")
        self.assertFalse(self.snapshot().unresolved_runs)

    def test_interruption_before_first_attempt_does_not_consume_invocation(self):
        with patch.object(self.repo,"begin_workflow_attempt",side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.run_step(self.raw_step())
        self.assertFalse(self.snapshot().attempts)
        self.workflow.recover(self.snapshot().unresolved_runs[0],decision="ABANDON",operation_key="recover")
        self.assertEqual(self.run_step(self.raw_step(),key="new-run").payload.attempt_number,1)

    def test_completed_run_idempotency_no_duplicate_steps_or_artifacts(self):
        first = self.workflow.run(self.eid,(self.raw_step(),),operation_key="run")
        before = self.rows()
        with patch("arbitrage.v2.workflow.invoke_offline_fixture",side_effect=AssertionError("No replay")):
            self.assertEqual(self.workflow.run(self.eid,(self.raw_step(),),operation_key="run"),first)
        self.assertEqual(self.rows(),before)

    def test_conflicting_run_operation_key_is_rejected(self):
        self.workflow.run(self.eid,(self.raw_step(),),operation_key="run")
        with self.assertRaises(IdempotencyConflict):
            self.workflow.run(self.eid,(self.raw_step("different"),),operation_key="run")

    def test_recovery_idempotency_and_conflicting_decisions(self):
        run = self.interrupted_run()
        end = self.workflow.recover(run,decision="RECONCILE",operation_key="recover")
        before = self.rows()
        self.assertEqual(self.workflow.recover(run,decision="RECONCILE",operation_key="recover"),end)
        self.assertEqual(before,self.rows())
        with self.assertRaises(IdempotencyConflict):
            self.workflow.recover(run,decision="ABANDON",operation_key="recover")

    def test_limits_are_explicit_and_nonzero(self):
        with self.assertRaises(ValidationError):
            WorkflowLimits(max_specialist_invocations=0,max_attempts_per_step=1,max_workflow_steps=1,max_calculation_attempts=1)

    def limited(self, **changes):
        limits = self.limits.model_copy(update=changes)
        accepted = self.workflow.accept(self.submission,limits,operation_key="limited-accept")
        return accepted,limits

    def test_specialist_invocation_limit_survives_restart(self):
        accepted,_ = self.limited(max_specialist_invocations=1)
        first = fixture_step(accepted,"first","RESEARCH",ResearchOutput(role="RESEARCH",report=RawResearchReport(report_text="Synthetic.")))
        self.workflow.run(accepted.evaluation.evaluation_id,(first,),operation_key="first")
        second = first.model_copy(update={"step_id":"second"})
        restarted = OfflineWorkflow(self.repo)
        with self.assertRaisesRegex(V2PersistenceError,"invocation limit"):
            restarted.run(accepted.evaluation.evaluation_id,(second,),operation_key="second")

    def test_unique_workflow_step_limit(self):
        accepted,_ = self.limited(max_workflow_steps=1)
        step = fixture_step(accepted,"first","RESEARCH",ResearchOutput(role="RESEARCH",report=RawResearchReport(report_text="Synthetic.")))
        with self.assertRaisesRegex(V2PersistenceError,"step limit"):
            self.workflow.run(accepted.evaluation.evaluation_id,(step,step.model_copy(update={"step_id":"second"})),operation_key="limited")

    def test_attempt_limit_and_explicit_retry_requirement(self):
        accepted,_ = self.limited(max_attempts_per_step=1)
        step = fixture_step(accepted,"failure","RESEARCH",SpecialistFailure(role="FAILURE",failure=FailureRecord(
            stage="fixture",category="NO_DATA",message="Synthetic failure")))
        end = self.workflow.run(accepted.evaluation.evaluation_id,(step,),operation_key="failure")
        retry = step.model_copy(update={"retry_of":end.payload.attempt_references[0]})
        with self.assertRaisesRegex(V2PersistenceError,"attempt limit"):
            self.workflow.run(accepted.evaluation.evaluation_id,(retry,),operation_key="retry")

    def test_unsolicited_retry_after_failure_rejected(self):
        step = fixture_step(self.accepted,"failure","RESEARCH",SpecialistFailure(role="FAILURE",failure=FailureRecord(
            stage="fixture",category="NO_DATA",message="Synthetic failure")))
        self.run_step(step)
        with self.assertRaisesRegex(V2PersistenceError,"explicitly"):
            self.run_step(step,key="implicit-retry")

    def test_calculation_attempt_limit_is_independent(self):
        accepted,_ = self.limited(max_calculation_attempts=1)
        # Use an actual same-Evaluation scenario via existing validated APIs.
        raw = self.repo.record_artifact(accepted.evaluation.evaluation_id,RawResearchReport(report_text="Synthetic"),
            producer=self.producer,operation_key="limited-raw")
        comp = self.repo.record_comparable(accepted.evaluation.evaluation_id,synthetic_comparables(raw.reference)[0],
            producer=self.producer,operation_key="limited-comp")
        draft = replace_input(scenario(raw.reference,comp.reference),"resale",amount="101.23")
        artifact = self.repo.record_scenario(accepted.evaluation.evaluation_id,draft,producer=self.producer,operation_key="limited-scenario")
        step = WorkflowStep(step_id="calc",role="CALCULATION",scenario=artifact.reference,
            context_references=(artifact.reference,),context_text="Synthetic calculation.")
        self.workflow.run(accepted.evaluation.evaluation_id,(step,),operation_key="first-calc")
        with self.assertRaisesRegex(V2PersistenceError,"Calculation attempt limit"):
            self.workflow.run(accepted.evaluation.evaluation_id,(step.model_copy(update={"step_id":"calc-again"}),),operation_key="second-calc")

    def test_attempt_end_transaction_rolls_back_and_output_survives(self):
        with closing(sqlite3.connect(self.repo.database_path)) as c:
            c.execute("CREATE TRIGGER fail_attempt_receipt BEFORE INSERT ON v2_operations "
                "WHEN NEW.operation='workflow_attempt_end' BEGIN SELECT RAISE(ABORT,'Synthetic end failure'); END")
            c.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_step(self.raw_step())
        self.assertEqual(len(self.snapshot().attempts),1)
        self.assertEqual(self.snapshot().attempts[0].payload.status,"STARTED")
        self.assertIsNotNone(self.repo.get_operation_artifact(output_operation_key(self.snapshot().attempts[0])))

    def test_begin_run_transaction_rollback_does_not_leave_false_claim(self):
        before = self.rows()
        with closing(sqlite3.connect(self.repo.database_path)) as c:
            c.execute("CREATE TRIGGER fail_run_receipt BEFORE INSERT ON v2_operations "
                "WHEN NEW.operation='workflow_run' BEGIN SELECT RAISE(ABORT,'Synthetic start failure'); END")
            c.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_step(self.raw_step())
        self.assertEqual(before,self.rows())

    def test_multiple_evaluations_are_isolated_with_independent_reviews(self):
        raw = self.research()
        other = self.workflow.accept(self.submission,self.limits,operation_key="other")
        step = fixture_step(other,"bad","COMPARABLE",ComparableOutput(role="COMPARABLE",draft=synthetic_comparables(raw.reference)[0]))
        end = self.workflow.run(other.evaluation.evaluation_id,(step,),operation_key="other-run")
        result = self.repo.get_artifact(end.payload.attempt_references[0])
        self.assertEqual(result.payload.status,"INVALID_OUTPUT")
        self.assertEqual(self.repo.list_review_queue().total,2)
        subject = self.repo.get_evaluation(self.eid).current_review_subject
        HumanReviewService(self.repo).record_decision(self.eid,subject,"WORTH_INVESTIGATING","Synthetic assessment only.",
            human=HumanActor(actor="Fixture human"),operation_key="review")
        self.assertEqual(self.repo.review_status(other.evaluation.evaluation_id).review_state,"PENDING")

    def test_cross_evaluation_context_rejected_before_run_claim(self):
        other = self.workflow.accept(self.submission,self.limits,operation_key="other")
        with self.assertRaises(V2PersistenceError):
            self.run_step(self.raw_step(refs=(other.evaluation.current_review_subject,)))
        self.assertFalse(self.snapshot().attempts)

    def test_new_evidence_invalidates_human_review_without_commercial_action(self):
        subject = self.repo.get_evaluation(self.eid).current_review_subject
        decision = HumanReviewService(self.repo).record_decision(self.eid,subject,"WORTH_INVESTIGATING","Synthetic only.",
            human=HumanActor(actor="Fixture human"),operation_key="review")
        before = len(self.snapshot().attempts)
        self.assertEqual(decision.authority,"ASSESSMENT_ONLY")
        self.assertEqual(len(self.snapshot().attempts),before)
        self.research()
        self.assertFalse(self.repo.review_is_current(decision.review_id))
        self.assertEqual(self.repo.review_status(self.eid).review_state,"PENDING")

    def test_no_automatic_assumptions_scenarios_or_assessment_state(self):
        self.research()
        self.assertFalse(self.repo.list_scenario_history(self.eid))
        self.assertFalse(self.repo.list_calculation_history(self.eid))
        self.assertIsNone(self.repo.get_current_summary(self.eid))
        self.assertFalse(any(a.artifact_type == ArtifactType.SCENARIO_ASSUMPTION for a in self.repo.list_artifacts(self.eid)))

    def test_explicit_assumption_and_conditional_summary(self):
        artifact = self.economic_scenario()
        p = provenance(True)
        record = AssumptionRecord(assumption=EconomicAssumption(name="shipping",explanation="Explicit fictional shipping assumption.",
            amount="9",unit="USD/FIXED_PER_BATCH",provenance=p))
        assumption = self.output(self.run_step(fixture_step(self.accepted,"assumption","ASSUMPTION",
            AssumptionOutput(role="ASSUMPTION",record=record))))
        draft = scenario(artifact.payload.evidence_references[0],artifact.payload.comparable_references[0])
        draft = replace_input(draft,"resale",amount="101.23")
        draft = replace_input(draft,"shipping",disposition="ASSUMPTION",amount="9",basis="ASSUMED",provenance=p,assumption=assumption.reference)
        refs = (artifact.payload.evidence_references[0],artifact.payload.comparable_references[0],assumption.reference)
        second = self.output(self.run_step(fixture_step(self.accepted,"conditional-scenario","SCENARIO",
            ScenarioOutput(role="SCENARIO",draft=draft),refs=refs)))
        calc = self.output(self.calculate(second,"conditional-calc"))
        summary = self.output(self.run_step(self.summary_step(calc,"CONDITIONAL_ESTIMATE",numerical_estimate=True,
            assumptions=(record.assumption,))))
        self.assertEqual(summary.payload.assessment_state,"CONDITIONAL_ESTIMATE")
        self.assertEqual(calc.payload.assumption_references,(assumption.reference,))

    def test_nonmeaningful_comparable_is_not_filtered(self):
        raw = self.research()
        comp = self.comparables(raw)[2]
        draft = AssessmentDraft(candidate_context=self.accepted.evaluation.current_review_subject,
            candidate_identity=comp.payload.product,category="NOT_MEANINGFULLY_COMPARABLE",explanation="Authored synthetic judgment.",
            provenance=provenance(),policy_version="synthetic-v1")
        assessment = self.output(self.run_step(fixture_step(self.accepted,"assessment","ASSESSMENT",
            AssessmentOutput(role="ASSESSMENT",comparable=comp.reference,draft=draft),
            refs=(comp.reference,self.accepted.evaluation.current_review_subject))))
        self.assertIn(assessment,self.repo.get_review_evidence(self.eid).artifacts)

    def test_no_network_models_or_protected_database_changes(self):
        root = Path(__file__).resolve().parents[1]
        paths = [root/p for p in ("arbitrage.db","sessions.db","dev_data/v2/arbitrage.db","dev_data/v2/sessions.db")]
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        with patch("socket.socket",side_effect=AssertionError("No network")),patch("agents.Runner.run_sync",side_effect=AssertionError("No model")):
            self.research()
        self.assertEqual(hashes,[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])
        self.assertFalse(self.repo.paths.sessions.exists())

    def test_failed_cancelled_and_completed_workflow_states_are_reviewable(self):
        for n,state in enumerate(("FINISHED","FAILED","CANCELLED","PARTIAL")):
            accepted = self.workflow.accept(self.submission,self.limits,operation_key=f"accept-{n}")
            self.workflow.finish(accepted.evaluation.evaluation_id,state,reason="Explicit offline lifecycle decision.",operation_key=f"finish-{n}")
            self.assertEqual(self.repo.get_evaluation(accepted.evaluation.evaluation_id).execution_state,state)
        self.assertEqual(self.repo.list_review_queue().total,5)

    def test_finish_is_idempotent_and_terminal_evaluation_cannot_resume(self):
        end = self.workflow.finish(self.eid,"FINISHED",reason="Done.",operation_key="finish")
        self.assertEqual(self.workflow.finish(self.eid,"FINISHED",reason="Done.",operation_key="finish"),end)
        with self.assertRaises(V2PersistenceError):
            self.run_step(self.raw_step())

    def test_finish_rejects_ambiguous_open_run(self):
        self.interrupted_run()
        with self.assertRaises(V2PersistenceError):
            self.workflow.finish(self.eid,"FINISHED",reason="Premature.",operation_key="finish")

    def test_concurrent_same_key_only_original_claim_invokes_fixture(self):
        entered, release = Event(),Event()
        def invoke(manifest):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Synthetic concurrent test timed out")
            return invoke_offline_fixture(manifest)
        with patch("arbitrage.v2.workflow.invoke_offline_fixture",side_effect=invoke) as mocked,ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.workflow.run,self.eid,(self.raw_step(),),operation_key="concurrent")
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(RecoveryRequired):
                    OfflineWorkflow(self.repo).run(self.eid,(self.raw_step(),),operation_key="concurrent")
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(mocked.call_count,1)

    def test_interruption_is_durable_without_automatic_recovery(self):
        run = self.interrupted_run()
        snapshot = self.snapshot()
        self.assertEqual(snapshot.events[-1].payload.event,"INTERRUPTION_REPORTED")
        self.assertEqual(snapshot.events[-1].payload.failure.category,"INTERRUPTED")
        self.assertEqual(snapshot.unresolved_runs,(run,))
        self.assertEqual(self.repo.list_review_queue().total,1)

    def test_unexpected_error_before_attempt_is_recorded_and_awaits_recovery(self):
        with patch.object(self.repo,"begin_workflow_attempt",side_effect=RuntimeError("Synthetic coordinator failure")):
            with self.assertRaises(RuntimeError):
                self.run_step(self.raw_step())
        self.assertEqual(self.snapshot().events[-1].payload.failure.category,"COORDINATOR_ERROR")
        self.assertFalse(self.snapshot().attempts)
        self.assertTrue(self.snapshot().unresolved_runs)

    def test_recovery_request_survives_interruption_and_rejects_changed_decision(self):
        run = self.interrupted_run()
        with patch.object(self.workflow,"_complete",side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.workflow.recover(run,decision="RECONCILE",operation_key="recover")
        self.assertEqual(self.snapshot().events[-1].payload.event,"RECOVERY_STARTED")
        with self.assertRaises(IdempotencyConflict):
            self.workflow.recover(run,decision="ABANDON",operation_key="recover")
        with self.assertRaises(V2PersistenceError):
            self.workflow.recover(run,decision="RECONCILE",operation_key="different-key")
        end = self.workflow.recover(run,decision="RECONCILE",operation_key="recover")
        self.assertEqual(end.payload.event,"RECOVERED")

    def test_recovery_itself_can_resume_after_completion_before_run_end(self):
        run = self.interrupted_run()
        with patch.object(self.repo,"end_workflow_run",side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.workflow.recover(run,decision="RECONCILE",operation_key="recover")
        self.assertFalse(self.snapshot().unfinished_attempts)
        before = len(self.snapshot().attempts)
        self.workflow.recover(run,decision="RECONCILE",operation_key="recover")
        self.assertEqual(len(self.snapshot().attempts),before)

    def test_finish_transaction_rolls_back_execution_state(self):
        before = self.rows()
        with closing(sqlite3.connect(self.repo.database_path)) as c:
            c.execute("CREATE TRIGGER fail_finish_receipt BEFORE INSERT ON v2_operations "
                "WHEN NEW.operation='workflow_finish' BEGIN SELECT RAISE(ABORT,'Synthetic finish failure'); END")
            c.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.workflow.finish(self.eid,"FAILED",reason="Synthetic finalization.",operation_key="finish")
        self.assertEqual(self.rows(),before)
        self.assertEqual(self.repo.get_evaluation(self.eid).execution_state,"RUNNING")

    def test_completion_cannot_fabricate_success_from_unrelated_artifact(self):
        run = self.interrupted_run()
        start = self.repo.get_artifact(self.snapshot().unfinished_attempts[0])
        with self.assertRaises(V2PersistenceError):
            self.repo.complete_workflow_attempt(start.reference,"SUCCEEDED",outputs=(self.accepted.evaluation.current_review_subject,),
                operation_key="fabricated-completion")
        self.assertEqual(self.snapshot().unfinished_attempts,(start.reference,))
        self.assertEqual(self.snapshot().unresolved_runs,(run,))

    def test_normal_run_cannot_be_closed_before_planned_steps_attempted(self):
        event = self.repo.begin_workflow_run(self.eid,(self.raw_step(),),owner_token="synthetic-claim",operation_key="run")
        with self.assertRaisesRegex(V2PersistenceError,"every explicitly planned step"):
            self.repo.end_workflow_run(event.reference,reason="Premature.",operation_key="end")

    def test_entire_batch_and_calculation_replay_without_new_outputs(self):
        artifact = self.economic_scenario()
        step = WorkflowStep(step_id="calc",role="CALCULATION",scenario=artifact.reference,
            context_references=(artifact.reference,),context_text="Synthetic calculation.")
        first = self.workflow.run(self.eid,(step,),operation_key="calc-batch")
        before = self.rows()
        with patch("arbitrage.v2.economics._calculate_profitability",side_effect=AssertionError("No replay")):
            self.assertEqual(self.workflow.run(self.eid,(step,),operation_key="calc-batch"),first)
        self.assertEqual(self.rows(),before)

    def test_successful_step_cannot_be_silently_reinvoked_with_new_key(self):
        first = self.run_step(self.raw_step())
        with self.assertRaises(V2PersistenceError):
            self.run_step(self.raw_step(retry_of=first.reference),key="retry-success")

    def test_unstarted_steps_after_interruption_remain_identifiable(self):
        with patch.object(self.workflow,"_execute",side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.workflow.run(self.eid,(self.raw_step("first"),self.raw_step("second")),operation_key="batch")
        snapshot = self.snapshot()
        run = self.repo.get_artifact(snapshot.unresolved_runs[0])
        self.assertEqual([s.step_id for s in run.payload.steps],["first","second"])
        self.assertEqual([a.payload.input_manifest.step_id for a in snapshot.attempts],["first"])


if __name__ == "__main__":
    unittest.main()
