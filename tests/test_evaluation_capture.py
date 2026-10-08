import asyncio
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from arbitrage.candidate_workflow import ApplicationContext, CandidateWorkflow
from arbitrage.acquisition_capture_state import AcquisitionCaptureError
from arbitrage.contracts import (
    CostType,
    EconomicCostFinding,
    EconomicCostFindingsResult,
    EvaluationArtifactType,
    InputBasis,
    ProfitabilityToolResult,
    SourcingTextReport,
)
from arbitrage.evaluation_capture import EvaluationCaptureHooks
from arbitrage.persistence import CandidateRepository
from tests import test_candidate_persistence as persistence_fixtures
from tests.test_lead_decision_contract import candidate_request


class EvaluationCaptureHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name) / "capture.db"
        self.repository = CandidateRepository(path)
        self.workflow = CandidateWorkflow(self.repository)
        self.started = self.workflow.start_candidate_evaluation(candidate_request())
        self.context = SimpleNamespace(
            context=ApplicationContext(candidate_workflow=self.workflow),
            tool_call_id="call-1",
            tool_arguments=json.dumps({"candidate_description": "test"}),
        )
        self.capture_calls = []

        async def capture(workflow, report):
            artifacts = workflow.repository.list_evaluation_artifacts(
                self.started.evaluation_id
            )
            self.assertEqual(
                artifacts[-1].artifact_type,
                EvaluationArtifactType.SOURCING_REPORT,
            )
            self.capture_calls.append(report)
            workflow.record_economic_cost_findings(
                EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
                EconomicCostFindingsResult(
                    findings=[
                        EconomicCostFinding(
                            cost_id="purchase_price",
                            name="Purchase price",
                            cost_type=CostType.FIXED_PER_UNIT,
                            value="39.99",
                            estimated_low=None,
                            estimated_high=None,
                            currency="USD",
                            basis=InputBasis.HUMAN_OBSERVED,
                            modeled_value=None,
                            modeled_value_basis=None,
                            modeled_value_is_conservative=False,
                            unresolved_materiality=None,
                            source_references=[],
                            limitations=[],
                            notes=None,
                        )
                    ]
                ),
            )

        self.hooks = EvaluationCaptureHooks(acquisition_capture=capture)
        self.helpers = persistence_fixtures.CandidatePersistenceTests()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def capture(self, name: str, result: object) -> None:
        asyncio.run(
            self.hooks.on_tool_end(
                self.context,
                SimpleNamespace(name="Arbitrage Manager"),
                SimpleNamespace(name=name),
                result,
            )
        )

    def test_resale_start_marker_distinguishes_nested_failure(self):
        with redirect_stdout(StringIO()) as output:
            asyncio.run(
                self.hooks.on_tool_start(
                    self.context,
                    SimpleNamespace(name="Arbitrage Manager"),
                    SimpleNamespace(name="consult_resale_agent"),
                )
            )
        self.assertIn("Resale Agent tool entered", output.getvalue())

    def test_sourcing_and_resale_results_are_captured_without_mutation(self):
        sourcing = "Sourcing report with evidence and https://example.com/item"
        resale = self.helpers.resale_result().model_dump_json()
        self.capture("consult_sourcing_agent", sourcing)
        self.capture("consult_resale_agent", resale)
        artifacts = self.repository.list_evaluation_artifacts(self.started.evaluation_id)
        self.assertEqual(
            [item.artifact_type for item in artifacts],
            [
                EvaluationArtifactType.SOURCING_REPORT,
                EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
                EvaluationArtifactType.RESALE,
            ],
        )
        self.assertEqual(
            SourcingTextReport.model_validate_json(artifacts[0].payload_json).report_text,
            sourcing,
        )
        self.assertEqual(artifacts[2].payload_json, resale)
        self.assertEqual(json.loads(artifacts[0].context_json)["tool_call_id"], "call-1")
        self.assertEqual(self.capture_calls[0].report_text, sourcing)

    def test_repeated_calls_are_preserved_in_order_and_latest_fields_advance(self):
        first = "First natural-language sourcing report"
        second = "Second natural-language sourcing report"
        profit = ProfitabilityToolResult(
            success=True,
            result=self.helpers.profitability_result(),
            error=None,
        )
        self.capture("consult_sourcing_agent", first)
        self.capture("consult_sourcing_agent", second)
        self.capture("calculate_profitability", profit)
        self.capture("calculate_profitability", profit.model_dump_json())
        artifacts = self.repository.list_evaluation_artifacts(self.started.evaluation_id)
        self.assertEqual([item.sequence_number for item in artifacts], [1, 2, 3, 4, 5, 6])
        self.assertEqual(
            [item.artifact_type for item in artifacts],
            [
                EvaluationArtifactType.SOURCING_REPORT,
                EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
                EvaluationArtifactType.SOURCING_REPORT,
                EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
                EvaluationArtifactType.PROFITABILITY,
                EvaluationArtifactType.PROFITABILITY,
            ],
        )
        evaluation = self.repository.get_evaluation(self.started.evaluation_id)
        self.assertIsNone(evaluation.sourcing_result)
        self.assertEqual(evaluation.profitability_result, profit.result)

    def test_sourcing_hook_propagates_capture_failure_after_preserving_report(self):
        async def fail_capture(_workflow, _report):
            raise AcquisitionCaptureError("required acquisition capture did not complete")

        hooks = EvaluationCaptureHooks(acquisition_capture=fail_capture)
        with self.assertRaisesRegex(
            AcquisitionCaptureError, "required acquisition capture did not complete"
        ):
            asyncio.run(
                hooks.on_tool_end(
                    self.context,
                    SimpleNamespace(name="Arbitrage Manager"),
                    SimpleNamespace(name="consult_sourcing_agent"),
                    "Sourcing evidence that must remain persisted",
                )
            )

        artifacts = self.repository.list_evaluation_artifacts(
            self.started.evaluation_id
        )
        self.assertEqual(
            [item.artifact_type for item in artifacts],
            [EvaluationArtifactType.SOURCING_REPORT],
        )


if __name__ == "__main__":
    unittest.main()
