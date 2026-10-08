import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall
from pydantic import ValidationError

from arbitrage.candidate_workflow import ApplicationContext, CandidateWorkflow
from arbitrage.contracts import (
    CostType,
    EconomicCostFinding,
    EconomicCostFindingsResult,
    EvaluationArtifactType,
    InputBasis,
    ManagerEvaluationJudgment,
    ResaleResult,
    UnknownMateriality,
)
from arbitrage.economic_findings import (
    record_acquisition_cost_findings,
    record_selling_cost_findings,
)
from arbitrage.orchestration import agent, lead_qualifier, sourcing_agent_tool
from arbitrage.persistence import CandidateRepository
from arbitrage.specialists.resale import resale_agent
from arbitrage.specialists.sourcing import sourcing_agent
from tests.test_lead_decision_contract import candidate_request


class EconomicFindingCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "findings.db"
        self.repository = CandidateRepository(self.database_path)
        self.workflow = CandidateWorkflow(self.repository)
        self.context = ApplicationContext(candidate_workflow=self.workflow)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def finding(cost_id: str = "purchase_tax") -> EconomicCostFinding:
        return EconomicCostFinding(
            cost_id=cost_id,
            name=cost_id.replace("_", " ").title(),
            cost_type=CostType.PERCENT_OF_UNIT_PRICE,
            value="8.25",
            estimated_low=None,
            estimated_high=None,
            currency=None,
            basis=InputBasis.VERIFIED,
            modeled_value=None,
            modeled_value_basis=None,
            modeled_value_is_conservative=False,
            unresolved_materiality=None,
            source_references=[],
            limitations=[],
            notes=None,
        )

    def invoke(self, tool, arguments: dict) -> str:
        encoded = json.dumps(arguments)
        call = ResponseFunctionToolCall(
            arguments=encoded,
            call_id=f"offline-{tool.name}",
            name=tool.name,
            type="function_call",
        )
        context = ToolContext(
            self.context,
            tool_name=tool.name,
            tool_call_id=call.call_id,
            tool_arguments=encoded,
            tool_call=call,
        )
        return asyncio.run(tool.on_invoke_tool(context, encoded))

    @staticmethod
    def arguments(*findings: EconomicCostFinding) -> dict:
        return {"findings": [finding.model_dump(mode="json") for finding in findings]}

    def start(self):
        return self.workflow.start_candidate_evaluation(candidate_request())

    def artifacts(self, evaluation_id: str):
        return self.repository.list_evaluation_artifacts(evaluation_id)

    def test_envelope_requires_findings_and_round_trips(self):
        findings = [self.finding(), self.finding("buyer_fee")]
        result = EconomicCostFindingsResult(findings=findings)

        self.assertEqual(
            EconomicCostFindingsResult.model_validate_json(result.model_dump_json()),
            result,
        )
        with self.assertRaises(ValidationError):
            EconomicCostFindingsResult(findings=[])

    def test_acquisition_and_selling_actions_persist_typed_artifacts(self):
        started = self.start()
        acquisition = self.finding()
        selling = self.finding("marketplace_fee")

        acquisition_output = self.invoke(
            record_acquisition_cost_findings,
            self.arguments(acquisition),
        )
        selling_output = self.invoke(
            record_selling_cost_findings,
            self.arguments(selling),
        )

        self.assertEqual(
            acquisition_output,
            "Recorded 1 acquisition-side economic cost finding(s).",
        )
        self.assertEqual(
            selling_output,
            "Recorded 1 selling-side economic cost finding(s).",
        )
        artifacts = self.artifacts(started.evaluation_id)
        self.assertEqual(
            [artifact.artifact_type for artifact in artifacts],
            [
                EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
                EvaluationArtifactType.SELLING_COST_FINDINGS,
            ],
        )
        self.assertTrue(
            all(artifact.evaluation_id == started.evaluation_id for artifact in artifacts)
        )
        self.assertEqual(
            EconomicCostFindingsResult.model_validate_json(
                artifacts[0].payload_json
            ).findings,
            [acquisition],
        )
        self.assertEqual(
            EconomicCostFindingsResult.model_validate_json(
                artifacts[1].payload_json
            ).findings,
            [selling],
        )
        self.assertEqual(
            json.loads(artifacts[0].context_json)["tool_name"],
            "record_acquisition_cost_findings",
        )

    def test_multiple_findings_and_repeat_calls_preserve_order_and_history(self):
        started = self.start()
        first = [self.finding(), self.finding("buyer_fee")]
        second = [self.finding("inbound_shipping")]

        self.invoke(record_acquisition_cost_findings, self.arguments(*first))
        self.invoke(record_acquisition_cost_findings, self.arguments(*second))

        artifacts = self.artifacts(started.evaluation_id)
        self.assertEqual([artifact.sequence_number for artifact in artifacts], [1, 2])
        self.assertEqual(
            [artifact.artifact_type for artifact in artifacts],
            [EvaluationArtifactType.ACQUISITION_COST_FINDINGS] * 2,
        )
        self.assertEqual(
            EconomicCostFindingsResult.model_validate_json(
                artifacts[0].payload_json
            ).findings,
            first,
        )
        self.assertEqual(
            EconomicCostFindingsResult.model_validate_json(
                artifacts[1].payload_json
            ).findings,
            second,
        )

    def test_no_active_evaluation_and_empty_batch_fail_without_artifacts(self):
        no_active = self.invoke(
            record_acquisition_cost_findings,
            self.arguments(self.finding()),
        )
        self.assertIn("No Candidate Evaluation is currently active", no_active)

        started = self.start()
        empty = self.invoke(record_selling_cost_findings, {"findings": []})
        self.assertIn("findings must not be empty", empty)
        self.assertEqual(self.artifacts(started.evaluation_id), [])

    def test_malformed_finding_creates_no_partial_artifact(self):
        started = self.start()
        valid = self.finding().model_dump(mode="json")
        invalid = self.finding("bad").model_dump(mode="json")
        invalid["cost_id"] = "   "

        output = self.invoke(
            record_acquisition_cost_findings,
            {"findings": [valid, invalid]},
        )

        self.assertIn("Economic cost findings were not recorded", output)
        self.assertIn("Invalid JSON input", output)
        self.assertEqual(self.artifacts(started.evaluation_id), [])

    def test_persistence_failure_is_reported_without_partial_artifact(self):
        started = self.start()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """CREATE TRIGGER fail_economic_capture
                   BEFORE INSERT ON evaluation_artifacts
                   BEGIN SELECT RAISE(ABORT, 'forced economic capture failure'); END"""
            )

        output = self.invoke(
            record_selling_cost_findings,
            self.arguments(self.finding("marketplace_fee")),
        )

        self.assertIn("forced economic capture failure", output)
        self.assertEqual(self.artifacts(started.evaluation_id), [])

    def test_repository_rejects_invalid_direct_payload(self):
        started = self.start()
        with self.assertRaises(ValidationError):
            self.repository.append_evaluation_artifact(
                started.evaluation_id,
                artifact_type=EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
                payload_json=json.dumps({"findings": []}),
            )
        self.assertEqual(self.artifacts(started.evaluation_id), [])

    def test_agent_boundaries_and_step3_manager_policy(self):
        self.assertIsNone(lead_qualifier.output_type)
        self.assertIsNone(sourcing_agent.output_type)
        self.assertEqual(sourcing_agent_tool.params_json_schema["title"], "AgentAsToolInput")
        self.assertIs(resale_agent.output_type, ResaleResult)
        self.assertNotIn("selling_cost_findings", ResaleResult.model_fields)
        self.assertIs(agent.output_type, ManagerEvaluationJudgment)
        self.assertNotIn(
            "supporting_profitability_reference",
            ManagerEvaluationJudgment.model_fields,
        )
        self.assertIn("required application-owned stage", agent.instructions)
        self.assertNotIn("record_selling_cost_findings", agent.instructions)
        self.assertEqual(
            [tool.name for tool in agent.tools],
            [
                "consult_sourcing_agent",
                "consult_resale_agent",
                "calculate_profitability",
                "record_selling_cost_findings",
            ],
        )


if __name__ == "__main__":
    unittest.main()
