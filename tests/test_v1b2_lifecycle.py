import sqlite3
import tempfile
import unittest
from pathlib import Path

from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    CandidateConclusion,
    CandidateLifecycleStatus,
    EvaluationArtifactType,
    EvaluationStatus,
    EvaluationTrigger,
    ManagerEvaluationJudgment,
    ManagerEvaluationOutcome,
    ProfitabilityToolResult,
)
from arbitrage.persistence import (
    CandidateRepository,
    InvalidStateTransitionError,
    initialize_database,
)
from tests.test_lead_decision_contract import candidate_request
from tests import test_candidate_persistence as persistence_fixtures


class V1B2LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "lifecycle.db"
        self.repository = CandidateRepository(self.database_path)
        self.workflow = CandidateWorkflow(self.repository)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def judgment(outcome, conclusion):
        return ManagerEvaluationJudgment(
            evaluation_outcome=outcome,
            candidate_conclusion=conclusion,
            assumptions=["test assumption"],
            uncertainties=["test uncertainty"],
            manager_notes="test judgment",
            user_response="Human-readable result.",
        )

    def finish(self, conclusion):
        started = self.workflow.start_candidate_evaluation(candidate_request())
        if conclusion == CandidateConclusion.VIABLE:
            self.append_successful_profitability(self.workflow, started.evaluation_id)
        result = self.workflow.apply_manager_judgment(
            self.judgment(ManagerEvaluationOutcome.COMPLETED, conclusion)
        )
        return started, result

    @staticmethod
    def append_successful_profitability(workflow, evaluation_id):
        profitability = persistence_fixtures.CandidatePersistenceTests().profitability_result()
        workflow.capture_specialist_result(
            EvaluationArtifactType.PROFITABILITY,
            ProfitabilityToolResult(
                success=True, result=profitability, error=None
            ).model_dump_json(),
        )

    def test_positive_negative_and_inconclusive_completion(self):
        for conclusion, expected in (
            (CandidateConclusion.VIABLE, CandidateLifecycleStatus.VIABLE),
            (CandidateConclusion.REJECTED, CandidateLifecycleStatus.REJECTED),
            (CandidateConclusion.INCONCLUSIVE, CandidateLifecycleStatus.INVESTIGATING),
        ):
            with self.subTest(conclusion=conclusion):
                path = Path(self.temporary_directory.name) / f"{conclusion.value}.db"
                workflow = CandidateWorkflow(CandidateRepository(path))
                started = workflow.start_candidate_evaluation(candidate_request())
                if conclusion == CandidateConclusion.VIABLE:
                    self.append_successful_profitability(
                        workflow, started.evaluation_id
                    )
                result = workflow.apply_manager_judgment(
                    self.judgment(ManagerEvaluationOutcome.COMPLETED, conclusion)
                )
                self.assertEqual(result.evaluation_status, EvaluationStatus.COMPLETED)
                self.assertEqual(result.candidate_lifecycle, expected)
                self.assertIsNotNone(
                    workflow.repository.get_evaluation(started.evaluation_id).completed_at
                )
                self.assertEqual(
                    workflow.repository.get_candidate(started.candidate_id).latest_evaluation_id,
                    started.evaluation_id,
                )

    def test_cancel_and_explicit_close(self):
        for conclusion, expected in (
            (CandidateConclusion.INCONCLUSIVE, CandidateLifecycleStatus.INVESTIGATING),
            (CandidateConclusion.CLOSED, CandidateLifecycleStatus.CLOSED),
        ):
            path = Path(self.temporary_directory.name) / f"cancel-{conclusion.value}.db"
            workflow = CandidateWorkflow(CandidateRepository(path))
            workflow.start_candidate_evaluation(candidate_request())
            result = workflow.apply_manager_judgment(
                self.judgment(ManagerEvaluationOutcome.CANCELLED, conclusion)
            )
            self.assertEqual(result.evaluation_status, EvaluationStatus.CANCELLED)
            self.assertEqual(result.candidate_lifecycle, expected)

    def test_waiting_can_cancel_and_all_terminal_states_are_immutable(self):
        for terminal in (
            EvaluationStatus.COMPLETED,
            EvaluationStatus.FAILED,
            EvaluationStatus.CANCELLED,
        ):
            path = Path(self.temporary_directory.name) / f"terminal-{terminal.value}.db"
            repository = CandidateRepository(path)
            workflow = CandidateWorkflow(repository)
            started = workflow.start_candidate_evaluation(candidate_request())
            if terminal == EvaluationStatus.CANCELLED:
                repository.update_evaluation_status(
                    started.evaluation_id, EvaluationStatus.WAITING_FOR_INPUT
                )
            repository.complete_evaluation(
                started.evaluation_id,
                status=terminal,
                candidate_status=CandidateLifecycleStatus.INVESTIGATING,
            )
            with self.assertRaises(InvalidStateTransitionError):
                repository.update_evaluation_status(
                    started.evaluation_id, EvaluationStatus.IN_PROGRESS
                )

    def test_reevaluation_preserves_history_and_resets_current_assessment(self):
        first, completed = self.finish(CandidateConclusion.VIABLE)
        self.assertEqual(completed.candidate_lifecycle, CandidateLifecycleStatus.VIABLE)
        second = self.workflow.start_existing_candidate_evaluation(
            first.candidate_id, trigger=EvaluationTrigger.MANUAL_REFRESH
        )
        self.assertNotEqual(second.evaluation_id, first.evaluation_id)
        self.assertEqual(second.candidate_lifecycle, CandidateLifecycleStatus.INVESTIGATING)
        history = self.repository.list_evaluations(first.candidate_id)
        self.assertEqual(history[0].status, EvaluationStatus.COMPLETED)
        self.assertEqual(history[1].status, EvaluationStatus.IN_PROGRESS)

    def test_finalization_transaction_rolls_back(self):
        started = self.workflow.start_candidate_evaluation(candidate_request())
        self.append_successful_profitability(self.workflow, started.evaluation_id)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """CREATE TRIGGER reject_viable BEFORE UPDATE ON candidates
                   WHEN NEW.lifecycle_status = 'VIABLE'
                   BEGIN SELECT RAISE(ABORT, 'forced finalization failure'); END"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.complete_evaluation(
                started.evaluation_id,
                status=EvaluationStatus.COMPLETED,
                candidate_status=CandidateLifecycleStatus.VIABLE,
            )
        self.assertEqual(
            self.repository.get_evaluation(started.evaluation_id).status,
            EvaluationStatus.IN_PROGRESS,
        )
        self.assertEqual(
            self.repository.get_candidate(started.candidate_id).lifecycle_status,
            CandidateLifecycleStatus.INVESTIGATING,
        )

    def test_schema_v1_migrates_statuses_without_losing_rows(self):
        started = self.workflow.start_candidate_evaluation(candidate_request())
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE schema_metadata SET value = '1' WHERE key = 'schema_version'"
            )
            connection.execute(
                "UPDATE candidate_evaluations SET status = 'AWAITING_HUMAN_INPUT' WHERE evaluation_id = ?",
                (started.evaluation_id,),
            )
            connection.execute(
                "UPDATE candidates SET lifecycle_status = 'AWAITING_HUMAN_INPUT' WHERE candidate_id = ?",
                (started.candidate_id,),
            )
        initialize_database(self.database_path)
        self.assertEqual(
            self.repository.get_evaluation(started.evaluation_id).status,
            EvaluationStatus.WAITING_FOR_INPUT,
        )
        self.assertEqual(
            self.repository.get_candidate(started.candidate_id).lifecycle_status,
            CandidateLifecycleStatus.INVESTIGATING,
        )


if __name__ == "__main__":
    unittest.main()
