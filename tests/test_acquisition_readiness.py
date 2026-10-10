import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agents.items import ItemHelpers
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall

from arbitrage.candidate_workflow import ApplicationContext, CandidateWorkflow
from arbitrage.contracts import (
    CandidateConclusion,
    EvaluationArtifactType,
    EvaluationStatus,
    ManagerEvaluationJudgment,
    ManagerEvaluationOutcome,
    ProfitabilityToolResult,
    UpdateCandidateEvaluationRequest,
    WorkflowEvaluationAction,
)
from arbitrage.persistence import (
    AcquisitionCapturePrerequisiteError,
    CandidateRepository,
)
from arbitrage.tools.profitability import calculate_profitability
from tests.acquisition_readiness_fixtures import (
    append_acquisition_capture,
    append_acquisition_readiness,
    append_economic_readiness,
    append_selling_capture,
    append_selling_readiness,
    append_sourcing_report,
)
from tests.test_lead_decision_contract import candidate_request


class AcquisitionReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = CandidateRepository(
            Path(self.temporary_directory.name) / "readiness.db"
        )
        self.workflow = CandidateWorkflow(self.repository)
        self.started = self.workflow.start_candidate_evaluation(candidate_request())

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def completed_judgment() -> ManagerEvaluationJudgment:
        return ManagerEvaluationJudgment(
            evaluation_outcome=ManagerEvaluationOutcome.COMPLETED,
            candidate_conclusion=CandidateConclusion.INCONCLUSIVE,
            assumptions=[],
            uncertainties=[],
            manager_notes="Deterministic readiness test.",
            user_response="Evaluation complete.",
        )

    def invoke_profitability(self) -> ProfitabilityToolResult:
        from tests.test_profitability_tool import ProfitabilityFunctionToolTests

        arguments = json.dumps(
            {
                "request": ProfitabilityFunctionToolTests.request().model_dump(
                    mode="json"
                )
            }
        )
        call = ResponseFunctionToolCall(
            arguments=arguments,
            call_id="readiness-profitability",
            name=calculate_profitability.name,
            type="function_call",
        )
        context = ToolContext(
            ApplicationContext(candidate_workflow=self.workflow),
            tool_name=calculate_profitability.name,
            tool_call_id=call.call_id,
            tool_arguments=arguments,
            tool_call=call,
        )
        output = asyncio.run(calculate_profitability.on_invoke_tool(context, arguments))
        item = ItemHelpers.tool_call_output_item(
            call,
            output,
            output_json_schema=calculate_profitability.output_json_schema,
            output_type_adapter=calculate_profitability._output_type_adapter,
        )
        return ProfitabilityToolResult.model_validate_json(item["output"])

    def artifact_types(self) -> list[EvaluationArtifactType]:
        return [
            artifact.artifact_type
            for artifact in self.repository.list_evaluation_artifacts(
                self.started.evaluation_id
            )
        ]

    def test_latest_sourcing_and_capture_allow_profitability_and_completion(self):
        append_economic_readiness(self.repository, self.started.evaluation_id)

        result = self.invoke_profitability()
        completed = self.workflow.apply_manager_judgment(self.completed_judgment())

        self.assertTrue(result.success)
        self.assertEqual(completed.evaluation_status, EvaluationStatus.COMPLETED)

    def test_missing_capture_blocks_actual_profitability_and_preserves_sourcing(self):
        append_sourcing_report(self.repository, self.started.evaluation_id)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = self.invoke_profitability()

        self.assertFalse(result.success)
        self.assertIn("does not have a successful acquisition-cost capture", result.error)
        self.assertNotIn("[debug] Profitability Tool called", output.getvalue())
        self.assertEqual(self.artifact_types(), [EvaluationArtifactType.SOURCING_REPORT])

    def test_missing_capture_blocks_actual_completed_finalization(self):
        append_sourcing_report(self.repository, self.started.evaluation_id)

        with self.assertRaises(AcquisitionCapturePrerequisiteError):
            self.workflow.apply_manager_judgment(self.completed_judgment())

        evaluation = self.repository.get_evaluation(self.started.evaluation_id)
        self.assertEqual(evaluation.status, EvaluationStatus.IN_PROGRESS)
        self.assertEqual(self.artifact_types(), [EvaluationArtifactType.SOURCING_REPORT])

    def test_failed_capture_leaves_no_success_artifact_and_blocks_both_boundaries(self):
        append_sourcing_report(self.repository, self.started.evaluation_id)

        profitability = self.invoke_profitability()
        with self.assertRaises(AcquisitionCapturePrerequisiteError):
            self.workflow.apply_manager_judgment(self.completed_judgment())

        self.assertFalse(profitability.success)
        self.assertEqual(self.artifact_types(), [EvaluationArtifactType.SOURCING_REPORT])

    def test_newer_sourcing_stales_prior_capture_and_new_capture_restores_readiness(self):
        append_economic_readiness(self.repository, self.started.evaluation_id)
        append_sourcing_report(
            self.repository,
            self.started.evaluation_id,
            "Newer substantive sourcing evidence.",
        )

        stale = self.invoke_profitability()
        self.assertFalse(stale.success)
        with self.assertRaises(AcquisitionCapturePrerequisiteError):
            self.workflow.apply_manager_judgment(self.completed_judgment())

        append_acquisition_capture(self.repository, self.started.evaluation_id)
        restored = self.invoke_profitability()
        completed = self.workflow.apply_manager_judgment(self.completed_judgment())

        self.assertTrue(restored.success)
        self.assertEqual(completed.evaluation_status, EvaluationStatus.COMPLETED)

    def test_other_evaluation_capture_does_not_satisfy_active_evaluation(self):
        append_sourcing_report(self.repository, self.started.evaluation_id)
        other_candidate = self.repository.create_candidate(
            product_identity=candidate_request().product_identity,
            acquisition_source=candidate_request().acquisition_source,
            resale_destination=candidate_request().resale_destination,
            intake_origin=candidate_request().intake_origin,
        )
        other = self.repository.create_evaluation(
            other_candidate.candidate_id,
            trigger=candidate_request().trigger,
        )
        append_acquisition_readiness(self.repository, other.evaluation_id)

        result = self.invoke_profitability()

        self.assertFalse(result.success)
        self.assertEqual(self.artifact_types(), [EvaluationArtifactType.SOURCING_REPORT])

    def test_wait_resume_uses_persisted_readiness(self):
        append_economic_readiness(self.repository, self.started.evaluation_id)
        self.workflow.update_candidate_evaluation(
            UpdateCandidateEvaluationRequest(
                action=WorkflowEvaluationAction.WAIT_FOR_INPUT
            )
        )
        self.workflow.update_candidate_evaluation(
            UpdateCandidateEvaluationRequest(action=WorkflowEvaluationAction.RESUME)
        )

        result = self.invoke_profitability()

        self.assertTrue(result.success)

    def test_new_resale_stales_selling_readiness_until_new_capture(self):
        from tests.test_candidate_persistence import CandidatePersistenceTests

        append_acquisition_readiness(self.repository, self.started.evaluation_id)
        append_selling_readiness(self.repository, self.started.evaluation_id)
        resale = CandidatePersistenceTests().resale_result()
        self.repository.append_evaluation_artifact(
            self.started.evaluation_id,
            artifact_type=EvaluationArtifactType.RESALE,
            payload_json=resale.model_dump_json(),
        )
        self.assertFalse(self.invoke_profitability().success)
        append_selling_capture(self.repository, self.started.evaluation_id)
        self.assertTrue(self.invoke_profitability().success)

    def test_no_sourcing_blocks_successful_finalization_but_not_failure(self):
        with self.assertRaises(AcquisitionCapturePrerequisiteError):
            self.workflow.apply_manager_judgment(self.completed_judgment())

        failed = self.workflow.fail_active_evaluation("Test technical failure.")
        self.assertEqual(failed.evaluation_status, EvaluationStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
