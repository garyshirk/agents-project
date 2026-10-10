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
    ManagerEvaluationJudgment,
    ManagerEvaluationOutcome,
    ProfitabilityRequest,
    ProfitabilityToolResult,
    UpdateCandidateEvaluationRequest,
    WorkflowEvaluationAction,
)
from arbitrage.persistence import (
    CandidateRepository,
    SellingCapturePrerequisiteError,
)
from arbitrage.tools.profitability import calculate_profitability
from tests.acquisition_readiness_fixtures import (
    append_acquisition_capture,
    append_acquisition_readiness,
    append_economic_readiness,
    append_resale,
    append_selling_capture,
    append_selling_readiness,
    append_sourcing_report,
)
from tests.test_lead_decision_contract import candidate_request


class EconomicEvidenceSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "snapshot.db"
        self.repository = CandidateRepository(self.database_path)
        self.workflow = CandidateWorkflow(self.repository)
        self.started = self.workflow.start_candidate_evaluation(candidate_request())

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def invoke_profitability(self) -> tuple[ProfitabilityToolResult, str]:
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
            call_id="snapshot-profitability",
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
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            raw_output = asyncio.run(
                calculate_profitability.on_invoke_tool(context, arguments)
            )
        item = ItemHelpers.tool_call_output_item(
            call,
            raw_output,
            output_json_schema=calculate_profitability.output_json_schema,
            output_type_adapter=calculate_profitability._output_type_adapter,
        )
        return (
            ProfitabilityToolResult.model_validate_json(item["output"]),
            output.getvalue(),
        )

    def test_current_pairs_produce_durable_snapshot_with_artifact_identity(self):
        append_economic_readiness(self.repository, self.started.evaluation_id)

        snapshot = self.workflow.economic_evidence_snapshot()

        self.assertTrue(snapshot.acquisition_ready)
        self.assertTrue(snapshot.selling_ready)
        self.assertTrue(snapshot.economically_ready)
        artifacts = (
            snapshot.sourcing_report,
            snapshot.acquisition_cost_findings,
            snapshot.resale,
            snapshot.selling_cost_findings,
        )
        self.assertTrue(all(item is not None for item in artifacts))
        self.assertEqual(len({item.artifact_id for item in artifacts}), 4)
        self.assertEqual(
            [item.sequence_number for item in artifacts],
            sorted(item.sequence_number for item in artifacts),
        )

        rebuilt = CandidateWorkflow(CandidateRepository(self.database_path))
        rebuilt.active.candidate_id = self.started.candidate_id
        rebuilt.active.evaluation_id = self.started.evaluation_id
        self.assertEqual(rebuilt.economic_evidence_snapshot(), snapshot)

    def test_missing_resale_and_missing_capture_are_distinct_failures(self):
        append_acquisition_readiness(self.repository, self.started.evaluation_id)

        with self.assertRaisesRegex(
            SellingCapturePrerequisiteError, "substantive Resale result"
        ):
            self.workflow.require_selling_capture()

        append_resale(self.repository, self.started.evaluation_id)
        with self.assertRaisesRegex(
            SellingCapturePrerequisiteError, "does not have a successful"
        ):
            self.workflow.require_selling_capture()

    def test_new_resale_stales_capture_and_new_capture_restores_it(self):
        append_economic_readiness(self.repository, self.started.evaluation_id)
        old = self.workflow.economic_evidence_snapshot()

        append_resale(self.repository, self.started.evaluation_id)
        stale = self.workflow.economic_evidence_snapshot()
        self.assertTrue(stale.acquisition_ready)
        self.assertFalse(stale.selling_ready)
        self.assertGreater(
            stale.resale.sequence_number,
            old.selling_cost_findings.sequence_number,
        )

        append_selling_capture(self.repository, self.started.evaluation_id)
        self.assertTrue(self.workflow.require_economic_readiness().economically_ready)

    def test_cross_evaluation_capture_cannot_satisfy_selling_readiness(self):
        append_acquisition_readiness(self.repository, self.started.evaluation_id)
        append_resale(self.repository, self.started.evaluation_id)
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
        append_selling_capture(self.repository, other.evaluation_id)

        self.assertFalse(self.workflow.economic_evidence_snapshot().selling_ready)
        with self.assertRaises(SellingCapturePrerequisiteError):
            self.workflow.require_selling_capture()

    def test_intervening_other_pair_does_not_invalidate_selling_readiness(self):
        append_selling_readiness(self.repository, self.started.evaluation_id)
        append_sourcing_report(self.repository, self.started.evaluation_id)
        append_acquisition_capture(self.repository, self.started.evaluation_id)

        snapshot = self.workflow.economic_evidence_snapshot()

        self.assertTrue(snapshot.acquisition_ready)
        self.assertTrue(snapshot.selling_ready)
        self.assertLess(
            snapshot.selling_cost_findings.sequence_number,
            snapshot.sourcing_report.sequence_number,
        )

    def test_pairs_can_complete_in_either_relative_order(self):
        append_selling_readiness(self.repository, self.started.evaluation_id)
        append_acquisition_readiness(self.repository, self.started.evaluation_id)
        self.assertTrue(self.workflow.require_economic_readiness().economically_ready)

    def test_wait_resume_reconstructs_readiness(self):
        append_economic_readiness(self.repository, self.started.evaluation_id)
        self.workflow.update_candidate_evaluation(
            UpdateCandidateEvaluationRequest(
                action=WorkflowEvaluationAction.WAIT_FOR_INPUT
            )
        )
        self.workflow.update_candidate_evaluation(
            UpdateCandidateEvaluationRequest(action=WorkflowEvaluationAction.RESUME)
        )

        self.assertTrue(self.workflow.require_economic_readiness().economically_ready)

    def test_selling_prerequisite_blocks_arithmetic_and_false_success(self):
        append_acquisition_readiness(self.repository, self.started.evaluation_id)
        append_resale(self.repository, self.started.evaluation_id)

        result, output = self.invoke_profitability()

        self.assertFalse(result.success)
        self.assertIn("selling-cost capture", result.error)
        self.assertNotIn("[debug] Profitability Tool called", output)
        self.assertNotIn("[debug] Profitability deterministic result", output)
        self.assertNotIn(
            EvaluationArtifactType.PROFITABILITY,
            [
                item.artifact_type
                for item in self.repository.list_evaluation_artifacts(
                    self.started.evaluation_id
                )
            ],
        )

    def test_finalization_rule_and_tool_schema_are_unchanged(self):
        append_acquisition_readiness(self.repository, self.started.evaluation_id)
        judgment = ManagerEvaluationJudgment(
            evaluation_outcome=ManagerEvaluationOutcome.COMPLETED,
            candidate_conclusion=CandidateConclusion.INCONCLUSIVE,
            assumptions=[],
            uncertainties=[],
            manager_notes=None,
            user_response="Insufficient evidence.",
        )

        completed = self.workflow.apply_manager_judgment(judgment)

        self.assertEqual(completed.evaluation_status.value, "COMPLETED")
        self.assertEqual(
            set(calculate_profitability.params_json_schema["properties"]["request"]),
            {"$ref"},
        )
        self.assertEqual(
            set(
                calculate_profitability.params_json_schema["$defs"]
                ["ProfitabilityRequest"]["properties"]
            ),
            set(ProfitabilityRequest.model_fields),
        )


if __name__ == "__main__":
    unittest.main()
