import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from arbitrage.candidate_workflow import ApplicationContext, CandidateWorkflow
from arbitrage.contracts import EvaluationArtifactType, ProfitabilityToolResult
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
        self.hooks = EvaluationCaptureHooks()
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

    def test_sourcing_and_resale_results_are_captured_without_mutation(self):
        sourcing = self.helpers.sourcing_result().model_dump_json()
        resale = self.helpers.resale_result().model_dump_json()
        self.capture("consult_sourcing_agent", sourcing)
        self.capture("consult_resale_agent", resale)
        artifacts = self.repository.list_evaluation_artifacts(self.started.evaluation_id)
        self.assertEqual(
            [item.artifact_type for item in artifacts],
            [EvaluationArtifactType.SOURCING, EvaluationArtifactType.RESALE],
        )
        self.assertEqual(artifacts[0].payload_json, sourcing)
        self.assertEqual(artifacts[1].payload_json, resale)
        self.assertEqual(json.loads(artifacts[0].context_json)["tool_call_id"], "call-1")

    def test_repeated_calls_are_preserved_in_order_and_latest_fields_advance(self):
        first = self.helpers.sourcing_result()
        second = first.model_copy(update={"item_price": 49.0})
        profit = ProfitabilityToolResult(
            success=True,
            result=self.helpers.profitability_result(),
            error=None,
        )
        self.capture("consult_sourcing_agent", first.model_dump_json())
        self.capture("consult_sourcing_agent", second.model_dump_json())
        self.capture("calculate_profitability", profit)
        self.capture("calculate_profitability", profit.model_dump_json())
        artifacts = self.repository.list_evaluation_artifacts(self.started.evaluation_id)
        self.assertEqual([item.sequence_number for item in artifacts], [1, 2, 3, 4])
        self.assertEqual(
            [item.artifact_type for item in artifacts],
            [
                EvaluationArtifactType.SOURCING,
                EvaluationArtifactType.SOURCING,
                EvaluationArtifactType.PROFITABILITY,
                EvaluationArtifactType.PROFITABILITY,
            ],
        )
        evaluation = self.repository.get_evaluation(self.started.evaluation_id)
        self.assertEqual(evaluation.sourcing_result.item_price, 49.0)
        self.assertEqual(evaluation.profitability_result, profit.result)


if __name__ == "__main__":
    unittest.main()
