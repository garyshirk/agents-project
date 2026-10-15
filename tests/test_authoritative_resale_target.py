import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents import RunContextWrapper, Runner

from arbitrage.candidate_workflow import ApplicationContext, CandidateWorkflow
from arbitrage.contracts import EvaluationStatus, ProductPackaging, ResaleResult
from arbitrage.evaluation_capture import EvaluationCaptureHooks
from arbitrage.orchestration import resale_agent_tool, sourcing_agent_tool
from arbitrage.profitability_request_assembly import evaluate_identity_acceptance
from tests.test_lead_decision_contract import candidate_request
from tests import test_candidate_persistence as fixtures


class AuthoritativeResaleTargetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = fixtures.CandidateRepository(Path(self.directory.name) / "test.db")
        self.workflow = CandidateWorkflow(self.repository)
        self.started = self.workflow.start_candidate_evaluation(candidate_request())
        self.context = RunContextWrapper(ApplicationContext(candidate_workflow=self.workflow))
        self.intent = dict(identity_confidence="STRONG", identity_uncertainty=None,
                           research_constraints=[], research_objectives=[],
                           sourcing_context=None, additional_context=None)
        self.result = fixtures.CandidatePersistenceTests().resale_result()

    def invoke(self, values=None):
        return asyncio.run(resale_agent_tool.on_invoke_tool(
            self.context, json.dumps(self.intent if values is None else values)))

    def runner(self):
        return patch.object(Runner, "run", new_callable=AsyncMock, return_value=SimpleNamespace(
            final_output=self.result, final_output_as=lambda *a, **k: self.result, interruptions=[]))

    def target(self, run):
        value = run.call_args.kwargs["input"]
        return json.loads(value)

    def test_omitted_identity_loads_exact_persisted_target_with_nulls(self):
        with self.runner() as run:
            self.invoke()
        target = self.target(run)["product_identity"]
        self.assertEqual(target, candidate_request().product_identity.model_dump(mode="json"))
        self.assertEqual(target["material_variant"]["packaging"], "COMPLETE_ORIGINAL")
        self.assertIsNone(target["material_variant"]["width"])
        self.assertIsNone(target["upc_gtin"])
        self.assertNotIn("product_identity", resale_agent_tool.params_json_schema["properties"])

    def test_all_manager_identity_substitutions_reject_before_resale(self):
        for field, value in (("packaging", "ORIGINAL_INCOMPLETE"), ("colorway", "Red"),
                             ("size_value", "11"), ("condition", "USED"),
                             ("package_quantity", 2), ("model_number", "different")):
            with self.subTest(field=field), self.runner() as run:
                values = dict(self.intent, product_identity={field: value})
                self.assertIn("rejected", self.invoke(values))
                run.assert_not_awaited()

    def test_deep_copy_and_no_candidate_mutation(self):
        with self.runner() as run:
            self.invoke()
        target = self.target(run)
        target["product_identity"]["material_variant"]["packaging"] = "NO_ORIGINAL"
        self.context.resale_target_provenance["product_identity"]["brand"] = "changed"
        candidate = self.repository.get_candidate(self.started.candidate_id)
        self.assertEqual(candidate.product_identity.brand, "Nike")
        self.assertEqual(candidate.product_identity.material_variant.packaging.value, "COMPLETE_ORIGINAL")

    def test_missing_mismatched_and_terminal_active_records_fail(self):
        for candidate_id, evaluation_id in ((None, None), ("missing", self.started.evaluation_id)):
            self.workflow.active.candidate_id = candidate_id
            self.workflow.active.evaluation_id = evaluation_id
            with self.runner() as run:
                self.assertIn("rejected", self.invoke())
                run.assert_not_awaited()
        self.workflow.active.candidate_id = self.started.candidate_id
        self.workflow.active.evaluation_id = self.started.evaluation_id
        with patch.object(self.repository, "get_evaluation", return_value=SimpleNamespace(
                candidate_id="another", status=EvaluationStatus.IN_PROGRESS)), self.runner() as run:
            self.assertIn("rejected", self.invoke())
            run.assert_not_awaited()
        with patch.object(self.repository, "get_evaluation", return_value=SimpleNamespace(
                candidate_id=self.started.candidate_id, status=EvaluationStatus.WAITING_FOR_INPUT)), self.runner() as run:
            self.assertIn("rejected", self.invoke())
            run.assert_not_awaited()

    def test_independent_disagreement_and_target_provenance_persist(self):
        self.result.researched_identity.material_variant.packaging = ProductPackaging.ORIGINAL_INCOMPLETE
        with self.runner():
            output = self.invoke()
        async def no_capture(*args):
            pass
        hooks = EvaluationCaptureHooks(selling_capture=no_capture)
        asyncio.run(hooks.on_tool_end(self.context, None, resale_agent_tool, output))
        artifact = self.repository.list_evaluation_artifacts(self.started.evaluation_id)[0]
        persisted = ResaleResult.model_validate_json(artifact.payload_json)
        self.assertEqual(persisted.researched_identity.material_variant.packaging.value, "ORIGINAL_INCOMPLETE")
        provenance = json.loads(artifact.context_json)["authoritative_target"]
        self.assertEqual(provenance["candidate_id"], self.started.candidate_id)
        self.assertEqual(provenance["evaluation_id"], self.started.evaluation_id)
        self.assertEqual(provenance["product_identity"]["material_variant"]["packaging"], "COMPLETE_ORIGINAL")
        decision = evaluate_identity_acceptance(candidate_request().product_identity, persisted)
        self.assertFalse(decision.accepted)
        self.assertIn("material_variant.packaging", decision.conflicting_fields)

    def test_rejected_call_does_not_create_artifact(self):
        output = self.invoke(dict(self.intent, product_identity={}))
        asyncio.run(EvaluationCaptureHooks().on_tool_end(self.context, None, resale_agent_tool, output))
        self.assertEqual(self.repository.list_evaluation_artifacts(self.started.evaluation_id), [])

    def test_concurrent_sourcing_and_resale_keep_target(self):
        async def run_both():
            return await asyncio.gather(
                resale_agent_tool.on_invoke_tool(self.context, json.dumps(self.intent)),
                sourcing_agent_tool.on_invoke_tool(RunContextWrapper(self.context.context), '{"input":"research"}'))
        async def fake_run(**kwargs):
            await asyncio.sleep(0)
            if kwargs["starting_agent"].name == "Sourcing Agent":
                return SimpleNamespace(final_output="Sourcing report", interruptions=[])
            return SimpleNamespace(final_output=self.result, final_output_as=lambda *a, **k: self.result,
                                   interruptions=[])
        with patch.object(Runner, "run", side_effect=fake_run):
            output = asyncio.run(run_both())
        self.assertEqual(output[1], "Sourcing report")
        self.assertEqual(self.context.resale_target_provenance["product_identity"],
                         candidate_request().product_identity.model_dump(mode="json"))
