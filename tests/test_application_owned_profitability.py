import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.items import ItemHelpers
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall

from arbitrage.candidate_workflow import ApplicationContext, CandidateWorkflow
from arbitrage.contracts import (
    EvaluationArtifactType,
    ProfitabilityRequest,
    ProfitabilityStatus,
    ProfitabilityToolResult,
)
from arbitrage.orchestration import agent as manager_agent
from arbitrage.persistence import CandidateRepository
from arbitrage.profitability_request_assembly import (
    AssemblyFailure,
    AssemblyFailureCode,
    ProfitabilityRequestAssemblyResult,
)
from arbitrage.tools.profitability import _calculate_profitability, calculate_profitability
from tests.acquisition_readiness_fixtures import append_economic_readiness
from tests.test_lead_decision_contract import candidate_request


class ApplicationOwnedProfitabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = CandidateRepository(
            Path(self.temporary_directory.name) / "application-owned.db"
        )
        self.workflow = CandidateWorkflow(self.repository)
        self.started = self.workflow.start_candidate_evaluation(candidate_request())

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def invoke(
        self,
        arguments: str = "{}",
        *,
        workflow: CandidateWorkflow | None = None,
    ) -> ProfitabilityToolResult:
        call = ResponseFunctionToolCall(
            arguments=arguments,
            call_id="application-owned-profitability",
            name=calculate_profitability.name,
            type="function_call",
        )
        context = ToolContext(
            ApplicationContext(candidate_workflow=workflow or self.workflow),
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

    def profitability_artifacts(self):
        return [
            artifact
            for artifact in self.repository.list_evaluation_artifacts(
                self.started.evaluation_id
            )
            if artifact.artifact_type == EvaluationArtifactType.PROFITABILITY
        ]

    def successful_profitability_artifacts(self):
        return [
            artifact
            for artifact in self.profitability_artifacts()
            if ProfitabilityToolResult.model_validate_json(artifact.payload_json).success
        ]

    def test_manager_tool_is_zero_argument_and_other_tools_remain_registered(self):
        self.assertEqual(calculate_profitability.params_json_schema["properties"], {})
        self.assertEqual(calculate_profitability.params_json_schema["required"], [])
        self.assertEqual(
            [tool.name for tool in manager_agent.tools],
            [
                "consult_sourcing_agent",
                "consult_resale_agent",
                "calculate_profitability",
            ],
        )

    def test_model_authored_economic_inputs_are_rejected(self):
        append_economic_readiness(self.repository, self.started.evaluation_id)

        result = self.invoke('{"request": {"acquisition": {}}}')

        self.assertFalse(result.success)
        self.assertIn("accepts no model-authored arguments", result.error)
        self.assertEqual(self.successful_profitability_artifacts(), [])

    def test_valid_execution_persists_exact_request_provenance_and_result_once(self):
        append_economic_readiness(self.repository, self.started.evaluation_id)
        snapshot = self.workflow.economic_evidence_snapshot()

        with patch(
            "arbitrage.tools.profitability._calculate_profitability",
            wraps=_calculate_profitability,
        ) as engine:
            result = self.invoke()

        self.assertTrue(result.success)
        self.assertEqual(result.result.status, ProfitabilityStatus.INSUFFICIENT_INPUTS)
        artifacts = self.profitability_artifacts()
        self.assertEqual(len(artifacts), 1)
        persisted = ProfitabilityToolResult.model_validate_json(
            artifacts[0].payload_json
        )
        self.assertEqual(persisted, result)
        context = json.loads(artifacts[0].context_json)
        engine.assert_called_once()
        self.assertEqual(
            engine.call_args.args[0],
            ProfitabilityRequest.model_validate(context["request"]),
        )
        self.assertEqual(context["result"], result.result.model_dump(mode="json"))
        self.assertEqual(context["status"], result.result.status.value)
        self.assertEqual(context["success"], True)
        references = context["provenance"]["artifact_references"]
        self.assertEqual(
            [(item["artifact_id"], item["sequence_number"]) for item in references],
            [
                (artifact.artifact_id, artifact.sequence_number)
                for artifact in (
                    snapshot.sourcing_report,
                    snapshot.acquisition_cost_findings,
                    snapshot.resale,
                    snapshot.selling_cost_findings,
                )
            ],
        )
        evaluation = self.repository.get_evaluation(self.started.evaluation_id)
        self.assertEqual(evaluation.profitability_result, result.result)

    def test_snapshot_is_loaded_once_and_not_reconstructed(self):
        append_economic_readiness(self.repository, self.started.evaluation_id)
        original = self.repository.require_economic_readiness

        with patch.object(
            self.repository,
            "require_economic_readiness",
            wraps=original,
        ) as load:
            result = self.invoke()

        self.assertTrue(result.success)
        load.assert_called_once_with(self.started.evaluation_id)

    def test_missing_active_evaluation_is_rejected(self):
        workflow = CandidateWorkflow(self.repository)

        result = self.invoke(workflow=workflow)

        self.assertFalse(result.success)
        self.assertIn("No Candidate Evaluation is currently active", result.error)

    def test_candidate_evaluation_mismatch_is_rejected(self):
        other = self.repository.create_candidate(
            product_identity=candidate_request().product_identity,
            acquisition_source=candidate_request().acquisition_source,
            resale_destination=candidate_request().resale_destination,
            intake_origin=candidate_request().intake_origin,
        )
        self.workflow.active.candidate_id = other.candidate_id

        result = self.invoke()

        self.assertFalse(result.success)
        self.assertIn("do not match", result.error)

    def test_terminal_active_evaluation_is_rejected(self):
        failed = self.workflow.fail_active_evaluation("Test terminal state.")
        self.workflow.active.candidate_id = failed.candidate_id
        self.workflow.active.evaluation_id = failed.evaluation_id

        result = self.invoke()

        self.assertFalse(result.success)
        self.assertIn("IN_PROGRESS", result.error)

    def test_assembly_failure_prevents_arithmetic_and_artifact(self):
        append_economic_readiness(self.repository, self.started.evaluation_id)
        failure = ProfitabilityRequestAssemblyResult(
            success=False,
            request=None,
            provenance=None,
            failures=[
                AssemblyFailure(
                    code=AssemblyFailureCode.RESALE_PRICE,
                    message="No defensible resale bounds.",
                )
            ],
        )

        with patch(
            "arbitrage.tools.profitability.assemble_profitability_request",
            return_value=failure,
        ), patch("arbitrage.tools.profitability._calculate_profitability") as engine:
            result = self.invoke()

        self.assertFalse(result.success)
        self.assertIn("RESALE_PRICE", result.error)
        engine.assert_not_called()
        self.assertEqual(self.successful_profitability_artifacts(), [])
        self.assertEqual(len(self.profitability_artifacts()), 1)
        context = json.loads(self.profitability_artifacts()[0].context_json)
        self.assertEqual(context["failure_stage"], "request_assembly")
        self.assertEqual(context["assembly_failures"][0]["code"], "RESALE_PRICE")

    def test_engine_exception_creates_no_artifact(self):
        append_economic_readiness(self.repository, self.started.evaluation_id)

        with patch(
            "arbitrage.tools.profitability._calculate_profitability",
            side_effect=RuntimeError("deterministic engine failure"),
        ):
            result = self.invoke()

        self.assertFalse(result.success)
        self.assertIn("deterministic engine failure", result.error)
        self.assertEqual(self.successful_profitability_artifacts(), [])
        self.assertEqual(len(self.profitability_artifacts()), 1)

    def test_persistence_failure_does_not_report_success(self):
        append_economic_readiness(self.repository, self.started.evaluation_id)

        with patch.object(
            self.workflow,
            "record_profitability_calculation",
            side_effect=RuntimeError("profitability persistence failure"),
        ):
            result = self.invoke()

        self.assertFalse(result.success)
        self.assertIn("profitability persistence failure", result.error)
        self.assertEqual(self.successful_profitability_artifacts(), [])
        self.assertEqual(len(self.profitability_artifacts()), 1)


if __name__ == "__main__":
    unittest.main()
