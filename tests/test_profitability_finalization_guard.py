import tempfile
import unittest
from pathlib import Path

from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    CandidateConclusion,
    CandidateLifecycleStatus,
    CandidateRelation,
    EvaluationArtifactType,
    EvaluationStatus,
    LeadDecision,
    LeadDisposition,
    ManagerEvaluationJudgment,
    ManagerEvaluationOutcome,
    ProfitabilityStatus,
    ProfitabilityToolResult,
)
from arbitrage.coordinator import ArbitrageCoordinator
from arbitrage.persistence import (
    CandidateRepository,
    ProfitabilityCorrectionRequiredError,
    ViableFinalizationPrerequisiteError,
)
from tests import test_candidate_persistence as persistence_fixtures
from tests.test_lead_decision_contract import candidate_request
from tests.acquisition_readiness_fixtures import append_acquisition_readiness


class ProfitabilityFinalizationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "guard.db"
        self.repository = CandidateRepository(path)
        self.workflow = CandidateWorkflow(self.repository)
        self.started = self.workflow.start_candidate_evaluation(candidate_request())
        append_acquisition_readiness(self.repository, self.started.evaluation_id)
        self.profitability = (
            persistence_fixtures.CandidatePersistenceTests().profitability_result()
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def judgment(conclusion=CandidateConclusion.VIABLE):
        return ManagerEvaluationJudgment(
            evaluation_outcome=ManagerEvaluationOutcome.COMPLETED,
            candidate_conclusion=conclusion,
            assumptions=[],
            uncertainties=[],
            manager_notes=None,
            user_response="Test judgment.",
        )

    def capture_failed(self, message="validation failed", workflow=None):
        target = workflow or self.workflow
        target.capture_specialist_result(
            EvaluationArtifactType.PROFITABILITY,
            ProfitabilityToolResult(
                success=False, result=None, error=message
            ).model_dump_json(),
        )

    def capture_successful(self, result=None):
        self.workflow.capture_specialist_result(
            EvaluationArtifactType.PROFITABILITY,
            ProfitabilityToolResult(
                success=True, result=result or self.profitability, error=None
            ).model_dump_json(),
        )

    def insufficient_result(self):
        return self.profitability.model_copy(
            update={
                "status": ProfitabilityStatus.INSUFFICIENT_INPUTS,
                "low_case": None,
                "expected_case": None,
                "high_case": None,
                "sensitivity_results": [],
                "warnings": ["Material inputs are unavailable."],
            }
        )

    def assert_still_active(self):
        candidate = self.repository.get_candidate(self.started.candidate_id)
        evaluation = self.repository.get_evaluation(self.started.evaluation_id)
        self.assertEqual(candidate.lifecycle_status, CandidateLifecycleStatus.INVESTIGATING)
        self.assertEqual(evaluation.status, EvaluationStatus.IN_PROGRESS)
        self.assertIsNone(evaluation.completed_at)

    def test_no_profitability_does_not_support_viable(self):
        with self.assertRaises(ViableFinalizationPrerequisiteError):
            self.workflow.apply_manager_judgment(self.judgment())
        self.assert_still_active()

    def test_failed_profitability_does_not_support_viable_and_remains(self):
        self.capture_failed()
        with self.assertRaises(ProfitabilityCorrectionRequiredError):
            self.workflow.apply_manager_judgment(self.judgment())
        self.assert_still_active()
        artifacts = self.repository.list_evaluation_artifacts(self.started.evaluation_id)
        profitability = [
            item for item in artifacts
            if item.artifact_type == EvaluationArtifactType.PROFITABILITY
        ]
        self.assertEqual(len(profitability), 1)
        self.assertFalse(
            ProfitabilityToolResult.model_validate_json(
                profitability[0].payload_json
            ).success
        )

    def test_failed_profitability_does_not_support_inconclusive_completion(self):
        self.capture_failed()
        with self.assertRaises(ProfitabilityCorrectionRequiredError):
            self.workflow.apply_manager_judgment(
                self.judgment(CandidateConclusion.INCONCLUSIVE)
            )
        self.assert_still_active()

    def test_multiple_failed_profitability_calls_do_not_support_viable(self):
        self.capture_failed("first failure")
        self.capture_failed("second failure")
        with self.assertRaises(ProfitabilityCorrectionRequiredError):
            self.workflow.apply_manager_judgment(self.judgment())
        with self.assertRaises(ProfitabilityCorrectionRequiredError):
            self.workflow.apply_manager_judgment(
                self.judgment(CandidateConclusion.INCONCLUSIVE)
            )
        self.assert_still_active()
        self.assertEqual(
            sum(
                item.artifact_type == EvaluationArtifactType.PROFITABILITY
                for item in self.repository.list_evaluation_artifacts(
                    self.started.evaluation_id
                )
            ),
            2,
        )

    def test_successful_profitability_supports_viable(self):
        self.capture_successful()
        result = self.workflow.apply_manager_judgment(self.judgment())
        self.assertEqual(result.evaluation_status, EvaluationStatus.COMPLETED)
        self.assertEqual(result.candidate_lifecycle, CandidateLifecycleStatus.VIABLE)
        self.assertIsNotNone(
            self.repository.get_evaluation(self.started.evaluation_id).completed_at
        )

    def test_successful_partial_satisfies_tool_prerequisite(self):
        partial = self.profitability.model_copy(
            update={
                "status": ProfitabilityStatus.PARTIAL,
                "warnings": ["A non-material input was omitted."],
            }
        )
        self.capture_successful(partial)
        result = self.workflow.apply_manager_judgment(self.judgment())
        self.assertEqual(result.candidate_lifecycle, CandidateLifecycleStatus.VIABLE)

    def test_successful_insufficient_inputs_supports_inconclusive(self):
        self.capture_successful(self.insufficient_result())
        result = self.workflow.apply_manager_judgment(
            self.judgment(CandidateConclusion.INCONCLUSIVE)
        )
        self.assertEqual(result.evaluation_status, EvaluationStatus.COMPLETED)
        self.assertEqual(result.candidate_lifecycle, CandidateLifecycleStatus.INVESTIGATING)

    def test_failed_then_successful_preserves_both_and_supports_viable(self):
        self.capture_failed()
        self.capture_successful()
        result = self.workflow.apply_manager_judgment(self.judgment())
        self.assertEqual(result.candidate_lifecycle, CandidateLifecycleStatus.VIABLE)
        artifacts = self.repository.list_evaluation_artifacts(self.started.evaluation_id)
        outcomes = [
            ProfitabilityToolResult.model_validate_json(item.payload_json).success
            for item in artifacts
            if item.artifact_type == EvaluationArtifactType.PROFITABILITY
        ]
        self.assertEqual(outcomes, [False, True])

    def test_failed_then_successful_insufficient_inputs_supports_inconclusive(self):
        self.capture_failed()
        self.capture_successful(self.insufficient_result())
        result = self.workflow.apply_manager_judgment(
            self.judgment(CandidateConclusion.INCONCLUSIVE)
        )
        self.assertEqual(result.evaluation_status, EvaluationStatus.COMPLETED)
        artifacts = self.repository.list_evaluation_artifacts(self.started.evaluation_id)
        self.assertEqual(
            [
                ProfitabilityToolResult.model_validate_json(item.payload_json).success
                for item in artifacts
                if item.artifact_type == EvaluationArtifactType.PROFITABILITY
            ],
            [False, True],
        )

    def test_nonviable_completions_do_not_require_profitability(self):
        for conclusion, expected in (
            (CandidateConclusion.REJECTED, CandidateLifecycleStatus.REJECTED),
            (CandidateConclusion.INCONCLUSIVE, CandidateLifecycleStatus.INVESTIGATING),
        ):
            path = Path(self.temporary_directory.name) / f"{conclusion.value}.db"
            workflow = CandidateWorkflow(CandidateRepository(path))
            started = workflow.start_candidate_evaluation(candidate_request())
            append_acquisition_readiness(workflow.repository, started.evaluation_id)
            result = workflow.apply_manager_judgment(self.judgment(conclusion))
            self.assertEqual(result.candidate_lifecycle, expected)

    def test_failed_profitability_does_not_block_wait_failure_or_cancel(self):
        cases = (
            ("waiting", ManagerEvaluationOutcome.WAITING_FOR_INPUT, EvaluationStatus.WAITING_FOR_INPUT),
            ("cancel", ManagerEvaluationOutcome.CANCELLED, EvaluationStatus.CANCELLED),
        )
        for name, outcome, expected in cases:
            path = Path(self.temporary_directory.name) / f"{name}.db"
            workflow = CandidateWorkflow(CandidateRepository(path))
            workflow.start_candidate_evaluation(candidate_request())
            self.capture_failed(workflow=workflow)
            result = workflow.apply_manager_judgment(
                ManagerEvaluationJudgment(
                    evaluation_outcome=outcome,
                    candidate_conclusion=CandidateConclusion.INCONCLUSIVE,
                    assumptions=[],
                    uncertainties=[],
                    manager_notes=None,
                    user_response="Test lifecycle outcome.",
                )
            )
            self.assertEqual(result.evaluation_status, expected)

        path = Path(self.temporary_directory.name) / "failed.db"
        workflow = CandidateWorkflow(CandidateRepository(path))
        workflow.start_candidate_evaluation(candidate_request())
        self.capture_failed(workflow=workflow)
        result = workflow.fail_active_evaluation("Test technical failure.")
        self.assertEqual(result.evaluation_status, EvaluationStatus.FAILED)

    def test_coordinator_performs_one_bounded_same_evaluation_continuation(self):
        coordinator = ArbitrageCoordinator(self.workflow)
        calls = []

        def evaluate(result, continuation):
            calls.append((result.candidate_id, result.evaluation_id, continuation))
            if continuation is None:
                self.capture_failed()
            else:
                self.assertIn("no invocation completed successfully", continuation)
                self.capture_successful()
            return self.judgment()

        outcome = coordinator.coordinate(self.ready_decision(), "Lead", evaluate)
        self.assertTrue(outcome.proceeded)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({(item[0], item[1]) for item in calls}), 1)
        self.assertEqual(outcome.workflow_result.evaluation_status, EvaluationStatus.COMPLETED)
        artifacts = self.repository.list_evaluation_artifacts(self.started.evaluation_id)
        self.assertEqual(
            [item.artifact_type for item in artifacts],
            [
                EvaluationArtifactType.SOURCING_REPORT,
                EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
                EvaluationArtifactType.HUMAN_INPUT,
                EvaluationArtifactType.PROFITABILITY,
                EvaluationArtifactType.PROFITABILITY,
            ],
        )

    def test_second_unsupported_viable_judgment_leaves_evaluation_active(self):
        coordinator = ArbitrageCoordinator(self.workflow)
        calls = []

        def evaluate(result, continuation):
            calls.append(continuation)
            self.capture_failed()
            return self.judgment()

        outcome = coordinator.coordinate(self.ready_decision(), "Lead", evaluate)
        self.assertFalse(outcome.proceeded)
        self.assertEqual(len(calls), 2)
        self.assertIn("remains active", outcome.response)
        self.assert_still_active()

    def test_inconclusive_failed_call_uses_same_bounded_continuation(self):
        coordinator = ArbitrageCoordinator(self.workflow)
        calls = []

        def evaluate(result, continuation):
            calls.append((result.candidate_id, result.evaluation_id, continuation))
            if continuation is None:
                self.capture_failed()
            else:
                self.assertIn("not equivalent", continuation)
                self.capture_successful(self.insufficient_result())
            return self.judgment(CandidateConclusion.INCONCLUSIVE)

        outcome = coordinator.coordinate(self.ready_decision(), "Lead", evaluate)
        self.assertTrue(outcome.proceeded)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({(item[0], item[1]) for item in calls}), 1)
        self.assertEqual(outcome.workflow_result.evaluation_status, EvaluationStatus.COMPLETED)
        self.assertEqual(outcome.workflow_result.candidate_lifecycle, CandidateLifecycleStatus.INVESTIGATING)

    @staticmethod
    def ready_decision():
        return LeadDecision(
            disposition=LeadDisposition.EXISTING_CANDIDATE,
            relation_to_active=CandidateRelation.SAME_AS_ACTIVE,
            reasoning="Continue active test Candidate.",
            continue_substantive_evaluation=True,
            candidate_request=None,
            clarification_question=None,
            user_message=None,
            unresolved_uncertainties=[],
        )


if __name__ == "__main__":
    unittest.main()
