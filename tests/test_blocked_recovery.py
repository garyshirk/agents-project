import json
import unittest
from unittest.mock import Mock

from arbitrage.contracts import CandidateConclusion, EvaluationArtifactType, EvaluationStatus, ProfitabilityToolResult
from arbitrage.coordinator import ArbitrageCoordinator
from arbitrage.persistence import AcquisitionCapturePrerequisiteError, SellingCapturePrerequisiteError
from arbitrage.blocked_evaluation import blocked_evaluation_details
from tests import test_profitability_finalization_guard as fixtures


class BlockedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProfitabilityFinalizationGuardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.workflow = self.fixture.workflow
        self.repository = self.fixture.repository
        self.coordinator = ArbitrageCoordinator(self.workflow)

    def failure(self, code=None, **metadata):
        if code is not None:
            metadata["assembly_failures"] = [{"code": code}]
        return self.workflow.record_profitability_failure(
            ProfitabilityToolResult(success=False, result=None, error="Test failure"),
            context_json=json.dumps(metadata))

    def coordinate(self, callback=None):
        evaluate = Mock(side_effect=callback) if callback else Mock(return_value=self.fixture.judgment(CandidateConclusion.INCONCLUSIVE))
        result = self.coordinator.coordinate(self.fixture.ready_decision(), "Explicit user request", evaluate)
        evaluate.assert_called_once()
        self.assertIsNone(evaluate.call_args.args[1])
        return result

    def test_known_structured_failures_block_without_model_reentry(self):
        for code in ("IDENTITY_CONFLICT", "CONDITION_CONFLICT", "INSUFFICIENT_IDENTITY_EVIDENCE",
                     "RESALE_IDENTITY_IMPERFECT", "SHIPPING_COVERAGE", "MISSING_EVIDENCE", "STALE_EVIDENCE", "PURCHASE_PRICE"):
            with self.subTest(code=code):
                failed = self.failure(code)
                result = self.coordinate()
                self.assertFalse(result.proceeded)
                details = result.blocked_details
                self.assertIn(code, details["categories"])
                self.assertEqual(details["failed_profitability_artifact_id"], failed.artifact_id)
                self.assertEqual(details["candidate_id"], self.fixture.started.candidate_id)
                self.assertEqual(details["evaluation_id"], self.fixture.started.evaluation_id)
                self.assertEqual(details["automatic_continuations"], 0)
                self.fixture.assert_still_active()

    def test_unknown_failure_and_sensitive_metadata_are_not_guessed_or_echoed(self):
        self.failure("FUTURE_CODE", error_type="secret-credential", error="secret-credential")
        result = self.coordinate()
        self.assertEqual(result.blocked_details["categories"], ["UNCLASSIFIED_FAILURE"])
        self.assertNotIn("secret-credential", result.response)

    def test_old_failure_without_metadata_still_blocks(self):
        self.fixture.capture_failed()
        result = self.coordinate()
        self.assertIn("UNCLASSIFIED_FAILURE", result.blocked_details["categories"])
        self.fixture.assert_still_active()

    def test_missing_acquisition_capture_blocks_without_speculative_research(self):
        # New Sourcing makes the existing capture stale.
        from arbitrage.contracts import SourcingTextReport
        source = self.repository.append_evaluation_artifact(self.fixture.started.evaluation_id,
            artifact_type=EvaluationArtifactType.SOURCING_REPORT,
            payload_json=SourcingTextReport(report_text="New report").model_dump_json())
        result = self.coordinate()
        self.assertIn("MISSING_OR_STALE_ACQUISITION_CAPTURE", result.blocked_details["categories"])
        self.assertIsNone(result.blocked_details["failed_profitability_artifact_id"])
        references = result.blocked_details["source_artifacts"]
        self.assertIn({"artifact_id": source.artifact_id, "sequence": source.sequence_number,
                       "type": "SOURCING_REPORT"}, references)
        self.fixture.assert_still_active()

    def test_selling_readiness_error_uses_structured_exception_class(self):
        self.failure(error_type="SellingCapturePrerequisiteError")
        result = self.coordinate()
        self.assertIn("MISSING_OR_STALE_SELLING_CAPTURE", result.blocked_details["categories"])
        with self.assertRaises(SellingCapturePrerequisiteError):
            self.workflow.require_selling_capture()

    def test_missing_research_is_classified_without_invented_references(self):
        # A different empty active workflow, using the same temporary repository.
        from arbitrage.candidate_workflow import CandidateWorkflow
        from tests.test_lead_decision_contract import candidate_request
        workflow = CandidateWorkflow(self.repository)
        workflow.start_candidate_evaluation(candidate_request())
        details = blocked_evaluation_details(workflow, AcquisitionCapturePrerequisiteError())
        self.assertEqual(details["categories"], ["MISSING_RESEARCH"])
        self.assertEqual(details["source_artifacts"], [])
        self.assertIsNone(details["failed_profitability_artifact_id"])

    def test_failed_artifact_and_repeated_judgment_are_not_evidence_changes(self):
        self.failure("SHIPPING_COVERAGE")
        before = self.workflow.economic_evidence_snapshot()
        first = self.coordinate()
        self.failure("SHIPPING_COVERAGE")
        second = self.coordinate()
        self.assertFalse(first.proceeded or second.proceeded)
        self.assertEqual(before, self.workflow.economic_evidence_snapshot())
        self.assertEqual(first.blocked_details["source_artifacts"], second.blocked_details["source_artifacts"])

    def test_successful_insufficient_inputs_can_complete_without_recovery(self):
        self.failure("SHIPPING_COVERAGE")
        self.fixture.capture_successful(self.fixture.insufficient_result())
        result = self.coordinate()
        self.assertTrue(result.proceeded)
        self.assertIsNone(result.blocked_details)
        self.assertEqual(result.workflow_result.evaluation_status, EvaluationStatus.COMPLETED)
        self.assertEqual(result.workflow_result.candidate_lifecycle.value, "INVESTIGATING")

    def test_no_profitability_blocks_viable_without_fake_failed_artifact(self):
        evaluate = Mock(return_value=self.fixture.judgment())
        result = self.coordinator.coordinate(self.fixture.ready_decision(), "Lead", evaluate)
        evaluate.assert_called_once()
        self.assertFalse(result.proceeded)
        self.assertIn("NO_SUCCESSFUL_PROFITABILITY", result.blocked_details["categories"])
        self.assertIsNone(result.blocked_details["failed_profitability_artifact_id"])
        self.fixture.assert_still_active()


if __name__ == "__main__":
    unittest.main()
