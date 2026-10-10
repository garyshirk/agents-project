import asyncio
import inspect
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents import Runner, RunConfig, Usage
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall

from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    CostType,
    EconomicCostFinding,
    EconomicCostFindingsResult,
    EvaluationArtifactType,
    InputBasis,
    SourceReference,
    UnknownMateriality,
)
from arbitrage.persistence import CandidateRepository
from arbitrage.selling_capture import (
    SellingCaptureContext,
    record_captured_selling_cost_findings,
    selling_capture_agent,
)
from arbitrage.selling_capture_state import SellingCaptureError, SellingCaptureState
from tests.test_lead_decision_contract import candidate_request


class RequiredSellingActionModel(Model):
    def __init__(self, arguments: str) -> None:
        self.arguments = arguments
        self.calls = 0
        self.seen_settings = None

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id,
        conversation_id,
        prompt,
    ) -> ModelResponse:
        self.calls += 1
        self.seen_settings = model_settings
        return ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    arguments=self.arguments,
                    call_id="offline-required-selling-action",
                    name="record_selling_cost_findings",
                    type="function_call",
                )
            ],
            usage=Usage(),
            response_id="offline-selling-response",
        )

    def stream_response(self, *args, **kwargs):
        raise AssertionError("streaming is not used in this test")


class SellingCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "selling.db"
        self.repository = CandidateRepository(self.database_path)
        self.workflow = CandidateWorkflow(self.repository)
        self.started = self.workflow.start_candidate_evaluation(candidate_request())
        self.state = SellingCaptureState()
        self.state.begin()
        self.context = SellingCaptureContext(self.workflow, self.state)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def known_marketplace_fee() -> EconomicCostFinding:
        return EconomicCostFinding(
            cost_id="marketplace_final_value_fee",
            name="Marketplace final-value fee",
            cost_type=CostType.PERCENT_OF_UNIT_PRICE,
            value="13.5",
            estimated_low=None,
            estimated_high=None,
            currency=None,
            basis=InputBasis.VERIFIED,
            modeled_value=None,
            modeled_value_basis=None,
            modeled_value_is_conservative=False,
            unresolved_materiality=None,
            source_references=[
                SourceReference(
                    url="https://example.com/marketplace-fees",
                    description="Published marketplace fee schedule",
                )
            ],
            limitations=["Seller category eligibility must match."],
            notes="Inclusive fee; do not duplicate payment processing.",
        )

    @staticmethod
    def unresolved_shipping() -> EconomicCostFinding:
        return EconomicCostFinding(
            cost_id="seller_paid_outbound_shipping",
            name="Seller-paid outbound shipping",
            cost_type=CostType.FIXED_PER_UNIT,
            value=None,
            estimated_low=None,
            estimated_high=None,
            currency="USD",
            basis=None,
            modeled_value=None,
            modeled_value_basis=None,
            modeled_value_is_conservative=False,
            unresolved_materiality=UnknownMateriality.MATERIAL,
            source_references=[],
            limitations=[
                "The Resale result does not establish whether shipping is buyer-paid or seller-paid.",
                "Package dimensions, service, and destination are unavailable.",
            ],
            notes="Do not treat unresolved shipping as zero.",
        )

    def invoke(self, findings: list[EconomicCostFinding]) -> str:
        return self.invoke_arguments(
            {"findings": [finding.model_dump(mode="json") for finding in findings]}
        )

    def invoke_arguments(self, arguments_value: dict) -> str:
        arguments = json.dumps(arguments_value)
        call = ResponseFunctionToolCall(
            arguments=arguments,
            call_id="offline-selling-capture",
            name=record_captured_selling_cost_findings.name,
            type="function_call",
        )
        context = ToolContext(
            self.context,
            tool_name=record_captured_selling_cost_findings.name,
            tool_call_id=call.call_id,
            tool_arguments=arguments,
            tool_call=call,
        )
        return asyncio.run(
            record_captured_selling_cost_findings.on_invoke_tool(context, arguments)
        )

    def artifacts(self):
        return self.repository.list_evaluation_artifacts(self.started.evaluation_id)

    def test_state_requires_exactly_one_successful_action(self):
        with self.assertRaises(SellingCaptureError):
            self.state.complete()

        result = EconomicCostFindingsResult(findings=[self.known_marketplace_fee()])
        self.state.begin_action()
        self.state.select(result)
        self.assertEqual(self.state.complete(), result)

        with self.assertRaises(SellingCaptureError):
            self.state.begin_action()
        with self.assertRaises(SellingCaptureError):
            self.state.complete()

    def test_action_persists_known_fee_and_unresolved_shipping(self):
        findings = [self.known_marketplace_fee(), self.unresolved_shipping()]

        output = self.invoke(findings)

        self.assertEqual(output, "Recorded 2 selling-side economic cost finding(s).")
        self.assertEqual(self.state.complete().findings, findings)
        artifacts = self.artifacts()
        self.assertEqual(
            [artifact.artifact_type for artifact in artifacts],
            [EvaluationArtifactType.SELLING_COST_FINDINGS],
        )
        persisted = EconomicCostFindingsResult.model_validate_json(
            artifacts[0].payload_json
        )
        self.assertEqual(persisted.findings, findings)
        self.assertIsNone(persisted.findings[0].unresolved_materiality)
        self.assertIsNone(persisted.findings[1].value)
        self.assertEqual(
            persisted.findings[1].unresolved_materiality,
            UnknownMateriality.MATERIAL,
        )
        self.assertFalse(any(item.value == 0 for item in findings if item.value is not None))

    def test_invalid_arguments_do_not_persist_or_select_success(self):
        invalid = self.known_marketplace_fee().model_dump(mode="json")
        invalid["cost_id"] = "   "

        output = self.invoke_arguments({"findings": [invalid]})

        self.assertIn("Invalid JSON input", output)
        self.assertEqual(self.state.attempts, 0)
        self.assertEqual(self.state.successful_results, [])
        self.assertEqual(self.artifacts(), [])
        with self.assertRaises(SellingCaptureError):
            self.state.complete()

    def test_duplicate_action_is_rejected_without_second_artifact(self):
        self.invoke([self.known_marketplace_fee()])

        output = self.invoke([self.unresolved_shipping()])

        self.assertIn("exactly one terminal action", output)
        self.assertEqual(len(self.artifacts()), 1)
        with self.assertRaises(SellingCaptureError):
            self.state.complete()

    def test_persistence_failure_does_not_select_success(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """CREATE TRIGGER fail_selling_capture
                   BEFORE INSERT ON evaluation_artifacts
                   BEGIN SELECT RAISE(ABORT, 'forced selling capture failure'); END"""
            )

        output = self.invoke([self.known_marketplace_fee()])

        self.assertIn("forced selling capture failure", output)
        self.assertEqual(self.state.successful_results, [])
        self.assertEqual(self.artifacts(), [])
        with self.assertRaises(SellingCaptureError):
            self.state.complete()

    def test_evidence_bundle_preserves_identity_human_input_and_exact_resale(self):
        from tests.test_candidate_persistence import CandidatePersistenceTests

        raw_input = "Buyer pays shipping according to the listing I saw."
        resale = CandidatePersistenceTests().resale_result()
        self.workflow.record_human_input(raw_input)

        evidence = self.workflow.selling_capture_evidence(resale)

        self.assertIn(candidate_request().product_identity.model_dump_json(), evidence)
        self.assertIn(candidate_request().resale_destination.model_dump_json(), evidence)
        self.assertIn(json.dumps({"text": raw_input}), evidence)
        self.assertTrue(evidence.endswith(resale.model_dump_json()))

    def test_agent_configuration_and_semantic_instructions(self):
        self.assertIsNone(selling_capture_agent.output_type)
        self.assertEqual(
            [tool.name for tool in selling_capture_agent.tools],
            ["record_selling_cost_findings"],
        )
        self.assertEqual(selling_capture_agent.model_settings.tool_choice, "required")
        self.assertFalse(selling_capture_agent.model_settings.parallel_tool_calls)
        self.assertEqual(selling_capture_agent.tool_use_behavior, "stop_on_first_tool")
        instructions = selling_capture_agent.instructions
        for requirement in (
            "Avoid duplicate fee components",
            "seller-paid from buyer-paid shipping",
            "optional from mandatory charges",
            "fixed per-order charge as FIXED_PER_BATCH",
            "unresolved_materiality must be null, not NON_MATERIAL",
            "always use ASSUMED as its basis",
            "never silently convert an unknown to zero",
            "Do not create findings or zero placeholders",
            "do not perform research",
        ):
            self.assertIn(requirement, instructions)
        source = inspect.getsource(
            __import__("arbitrage.selling_capture", fromlist=["run_required_selling_capture"])
        ).lower()
        for forbidden in ("regex", "fallback", "json repair", "model_validate_json"):
            self.assertNotIn(forbidden, source)

    def test_required_action_executes_once_and_stops_without_second_model_turn(self):
        finding = self.known_marketplace_fee()
        model = RequiredSellingActionModel(
            json.dumps({"findings": [finding.model_dump(mode="json")]})
        )
        test_agent = selling_capture_agent.clone(
            model=model,
            model_settings=selling_capture_agent.model_settings,
        )

        result = asyncio.run(
            Runner.run(
                test_agent,
                "Normalize this structured Resale evidence.",
                context=self.context,
                run_config=RunConfig(tracing_disabled=True),
            )
        )

        self.assertEqual(model.calls, 1)
        self.assertEqual(model.seen_settings.tool_choice, "required")
        self.assertFalse(model.seen_settings.parallel_tool_calls)
        self.assertEqual(
            result.final_output,
            "Recorded 1 selling-side economic cost finding(s).",
        )
        self.assertEqual(self.state.complete().findings, [finding])
        self.assertEqual(
            [artifact.artifact_type for artifact in self.artifacts()],
            [EvaluationArtifactType.SELLING_COST_FINDINGS],
        )


if __name__ == "__main__":
    unittest.main()
