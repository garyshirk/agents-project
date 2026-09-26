import sqlite3
import tempfile
import unittest
from pathlib import Path

from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    CandidateRelation,
    CandidateWorkflowResult,
    LeadDecision,
    LeadDisposition,
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

    def counts(self):
        with sqlite3.connect(self.database_path) as connection:
            candidates = connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
            evaluations = connection.execute(
                "SELECT COUNT(*) FROM candidate_evaluations"
            ).fetchone()[0]
        return candidates, evaluations

    def test_ready_persists_once_before_substantive_evaluation(self):
        observed = []

        def evaluate(result: CandidateWorkflowResult) -> str:
            observed.append((self.counts(), result, self.workflow.active))
            return "evaluated"

        outcome = self.coordinator.coordinate(self.decision(), evaluate)
        self.assertEqual(outcome.response, "evaluated")
        self.assertEqual(self.counts(), (1, 1))
        counts, received, active = observed[0]
        self.assertEqual(counts, (1, 1))
        self.assertEqual(received.candidate_lifecycle.value, "INVESTIGATING")
        self.assertEqual(received.evaluation_status.value, "IN_PROGRESS")
        self.assertEqual(received.candidate_id, active.candidate_id)
        self.assertEqual(received.evaluation_id, active.evaluation_id)

    def test_repeated_readiness_reuses_active_records(self):
        first = self.coordinator.coordinate(self.decision(), lambda result: "first")
        ids = (first.workflow_result.candidate_id, first.workflow_result.evaluation_id)
        repeated = self.decision(relation_to_active=CandidateRelation.SAME_AS_ACTIVE)
        second = self.coordinator.coordinate(repeated, lambda result: "second")
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(
            (second.workflow_result.candidate_id, second.workflow_result.evaluation_id), ids
        )

    def test_needs_more_info_then_ready(self):
        calls = []
        needs = self.decision(
            LeadDisposition.NEEDS_MORE_INFO,
            continue_substantive_evaluation=False,
            candidate_request=None,
            clarification_question="What is the style number?",
        )
        first = self.coordinator.coordinate(needs, lambda result: calls.append(result))
        self.assertEqual(first.response, "What is the style number?")
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(calls, [])

        second = self.coordinator.coordinate(
            self.decision(), lambda result: calls.append(result) or "continued"
        )
        self.assertTrue(second.proceeded)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(len(calls), 1)

    def test_stop_does_not_persist_or_evaluate(self):
        decision = self.decision(
            LeadDisposition.STOP,
            continue_substantive_evaluation=False,
            candidate_request=None,
            user_message="Cannot establish the product.",
        )
        outcome = self.coordinator.coordinate(
            decision, lambda result: self.fail("evaluation must not run")
        )
        self.assertEqual(outcome.response, "Cannot establish the product.")
        self.assertEqual(self.counts(), (0, 0))

    def test_existing_candidate_reuses_ids(self):
        initial = self.coordinator.coordinate(self.decision(), lambda result: "initial")
        decision = self.decision(
            LeadDisposition.EXISTING_CANDIDATE,
            relation_to_active=CandidateRelation.SAME_AS_ACTIVE,
            candidate_request=None,
        )
        outcome = self.coordinator.coordinate(decision, lambda result: "follow-up")
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(outcome.response, "follow-up")
        self.assertEqual(outcome.workflow_result.candidate_id, initial.workflow_result.candidate_id)
        self.assertEqual(outcome.workflow_result.evaluation_id, initial.workflow_result.evaluation_id)

    def test_clearly_new_while_active_is_blocked(self):
        initial = self.coordinator.coordinate(self.decision(), lambda result: "initial")
        active_ids = (initial.workflow_result.candidate_id, initial.workflow_result.evaluation_id)
        decision = self.decision(relation_to_active=CandidateRelation.CLEARLY_NEW)
        outcome = self.coordinator.coordinate(
            decision, lambda result: self.fail("new evaluation must not run")
        )
        self.assertFalse(outcome.proceeded)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(
            (self.workflow.active.candidate_id, self.workflow.active.evaluation_id), active_ids
        )

    def test_ambiguous_boundary_does_not_mutate(self):
        initial = self.coordinator.coordinate(self.decision(), lambda result: "initial")
        active_ids = (initial.workflow_result.candidate_id, initial.workflow_result.evaluation_id)
        decision = self.decision(
            LeadDisposition.AMBIGUOUS_BOUNDARY,
            relation_to_active=CandidateRelation.AMBIGUOUS,
            continue_substantive_evaluation=False,
            candidate_request=None,
            clarification_question="Is this the current item or a new product?",
        )
        outcome = self.coordinator.coordinate(
            decision, lambda result: self.fail("evaluation must not run")
        )
        self.assertEqual(outcome.response, "Is this the current item or a new product?")
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(
            (self.workflow.active.candidate_id, self.workflow.active.evaluation_id), active_ids
        )

    def test_persistence_failure_is_controlled_and_atomic(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """CREATE TRIGGER fail_evaluation BEFORE INSERT ON candidate_evaluations
                   BEGIN SELECT RAISE(ABORT, 'forced failure'); END"""
            )
        outcome = self.coordinator.coordinate(
            self.decision(), lambda result: self.fail("evaluation must not run")
        )
        self.assertFalse(outcome.proceeded)
        self.assertIsNone(outcome.workflow_result)
        self.assertIn("Candidate persistence failed", outcome.response)
        self.assertEqual(self.counts(), (0, 0))
        self.assertFalse(self.workflow.active.is_active)


if __name__ == "__main__":
    unittest.main()
