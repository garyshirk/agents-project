import sqlite3
import tempfile
import unittest
from pathlib import Path

from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    CandidateConclusion,
    CandidateLifecycleStatus,
    CandidateRelation,
    CandidateWorkflowResult,
    EvaluationArtifactType,
    EvaluationStatus,
    LeadDecision,
    LeadDisposition,
    ManagerEvaluationJudgment,
    ManagerEvaluationOutcome,
)
from arbitrage.coordinator import ArbitrageCoordinator
from arbitrage.persistence import CandidateRepository
from tests.test_lead_decision_contract import candidate_request


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "coordinator.db"
        self.repository = CandidateRepository(self.database_path)
        self.workflow = CandidateWorkflow(self.repository)
        self.coordinator = ArbitrageCoordinator(self.workflow)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def decision(self, disposition=LeadDisposition.CANDIDATE_READY, **changes):
        values = {
            "disposition": disposition,
            "relation_to_active": CandidateRelation.NO_ACTIVE_CANDIDATE,
            "reasoning": "Deterministic test decision.",
            "continue_substantive_evaluation": True,
            "candidate_request": candidate_request(),
            "clarification_question": None,
            "user_message": None,
            "unresolved_uncertainties": [],
        }
        values.update(changes)
        return LeadDecision(**values)

    @staticmethod
    def judgment(
        outcome=ManagerEvaluationOutcome.WAITING_FOR_INPUT,
        conclusion=CandidateConclusion.INCONCLUSIVE,
        response="Need one more fact.",
    ):
        return ManagerEvaluationJudgment(
            evaluation_outcome=outcome,
            candidate_conclusion=conclusion,
            assumptions=[],
            uncertainties=[],
            manager_notes=None,
            user_response=response,
        )

    def counts(self):
        with sqlite3.connect(self.database_path) as connection:
            return (
                connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
                connection.execute(
                    "SELECT COUNT(*) FROM candidate_evaluations"
                ).fetchone()[0],
            )

    def test_ready_persists_before_evaluation_and_waits(self):
        observed = []

        def evaluate(result: CandidateWorkflowResult, continuation: str | None):
            observed.append((self.counts(), result))
            return self.judgment()

        outcome = self.coordinator.coordinate(self.decision(), "Initial lead", evaluate)
        self.assertEqual(outcome.response, "Need one more fact.")
        self.assertEqual(observed[0][0], (1, 1))
        self.assertEqual(outcome.workflow_result.evaluation_status, EvaluationStatus.WAITING_FOR_INPUT)
        artifacts = self.repository.list_evaluation_artifacts(
            outcome.workflow_result.evaluation_id
        )
        self.assertEqual([item.artifact_type for item in artifacts], [EvaluationArtifactType.HUMAN_INPUT])

    def test_waiting_evaluation_resumes_same_ids_on_human_input(self):
        first = self.coordinator.coordinate(
            self.decision(), "Initial lead", lambda result, continuation: self.judgment()
        )
        ids = (first.workflow_result.candidate_id, first.workflow_result.evaluation_id)
        follow_up = self.decision(
            LeadDisposition.EXISTING_CANDIDATE,
            relation_to_active=CandidateRelation.SAME_AS_ACTIVE,
            candidate_request=None,
        )

        def complete_viable(result, continuation):
            from tests.test_candidate_persistence import CandidatePersistenceTests

            helper = CandidatePersistenceTests()
            from arbitrage.contracts import ProfitabilityToolResult

            self.workflow.capture_specialist_result(
                EvaluationArtifactType.PROFITABILITY,
                ProfitabilityToolResult(
                    success=True,
                    result=helper.profitability_result(),
                    error=None,
                ).model_dump_json(),
            )
            return self.judgment(
                ManagerEvaluationOutcome.COMPLETED,
                CandidateConclusion.VIABLE,
                "Evaluation complete.",
            )

        second = self.coordinator.coordinate(
            follow_up,
            "Corrected price is $49",
            complete_viable,
        )
        self.assertEqual(
            (second.workflow_result.candidate_id, second.workflow_result.evaluation_id), ids
        )
        self.assertEqual(second.workflow_result.evaluation_status, EvaluationStatus.COMPLETED)
        self.assertEqual(second.workflow_result.candidate_lifecycle, CandidateLifecycleStatus.VIABLE)
        artifacts = self.repository.list_evaluation_artifacts(ids[1])
        self.assertEqual(len(artifacts), 3)
        self.assertIn("Initial lead", artifacts[0].payload_json)
        self.assertIn("$49", artifacts[1].payload_json)

    def test_failure_preserves_captured_result_and_marks_failed(self):
        def fail(result, continuation):
            self.workflow.capture_specialist_result(
                EvaluationArtifactType.SOURCING,
                self._sourcing_json(),
            )
            raise RuntimeError("safe test failure")

        outcome = self.coordinator.coordinate(self.decision(), "Lead", fail)
        self.assertFalse(outcome.proceeded)
        self.assertEqual(outcome.workflow_result.evaluation_status, EvaluationStatus.FAILED)
        self.assertEqual(outcome.workflow_result.candidate_lifecycle, CandidateLifecycleStatus.INVESTIGATING)
        artifacts = self.repository.list_evaluation_artifacts(outcome.workflow_result.evaluation_id)
        self.assertEqual(
            [item.artifact_type for item in artifacts],
            [EvaluationArtifactType.HUMAN_INPUT, EvaluationArtifactType.SOURCING],
        )
        evaluation = self.repository.get_evaluation(outcome.workflow_result.evaluation_id)
        self.assertEqual(
            evaluation.manager_notes, "Substantive evaluation failed (RuntimeError)."
        )

    @staticmethod
    def _sourcing_json():
        from tests.test_candidate_persistence import CandidatePersistenceTests

        helper = CandidatePersistenceTests()
        return helper.sourcing_result().model_dump_json()

    def test_pre_candidate_decisions_do_not_mutate(self):
        for disposition, field, text in (
            (LeadDisposition.NEEDS_MORE_INFO, "clarification_question", "Which model?"),
            (LeadDisposition.AMBIGUOUS_BOUNDARY, "clarification_question", "Same item?"),
            (LeadDisposition.STOP, "user_message", "Stopped."),
        ):
            relation = (
                CandidateRelation.AMBIGUOUS
                if disposition == LeadDisposition.AMBIGUOUS_BOUNDARY
                else CandidateRelation.NO_ACTIVE_CANDIDATE
            )
            decision = self.decision(
                disposition,
                relation_to_active=relation,
                continue_substantive_evaluation=False,
                candidate_request=None,
                **{field: text},
            )
            outcome = self.coordinator.coordinate(
                decision, "input", lambda result, continuation: self.fail("must not run")
            )
            self.assertEqual(outcome.response, text)
        self.assertEqual(self.counts(), (0, 0))

    def test_persistence_failure_is_controlled_and_atomic(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """CREATE TRIGGER fail_evaluation BEFORE INSERT ON candidate_evaluations
                   BEGIN SELECT RAISE(ABORT, 'forced failure'); END"""
            )
        outcome = self.coordinator.coordinate(
            self.decision(), "lead", lambda result, continuation: self.fail("must not run")
        )
        self.assertFalse(outcome.proceeded)
        self.assertIsNone(outcome.workflow_result)
        self.assertIn("Candidate persistence failed", outcome.response)
        self.assertEqual(self.counts(), (0, 0))


if __name__ == "__main__":
    unittest.main()
