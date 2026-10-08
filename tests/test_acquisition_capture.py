import asyncio
import inspect
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from agents import RunConfig, Runner, Usage
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall

from arbitrage.acquisition_capture import (
    AcquisitionCaptureContext,
    acquisition_capture_agent,
    record_captured_acquisition_cost_findings,
)
from arbitrage.acquisition_capture_state import (
    AcquisitionCaptureError,
    AcquisitionCaptureState,
)
from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    CostType,
    EconomicCostFinding,
    EconomicCostFindingsResult,
    EvaluationArtifactType,
    InputBasis,
    SourceReference,
    SourcingTextReport,
    UnknownMateriality,
)
from arbitrage.persistence import CandidateRepository
from arbitrage import application
from arbitrage.specialists.sourcing import sourcing_agent
from tests.test_lead_decision_contract import candidate_request


FAILED_LIVE_ARGUMENTS = {
    "findings": [
        {
            "cost_id": "acquisition_purchase_price",
            "name": "Ross in-store purchase price",
            "cost_type": "FIXED_PER_UNIT",
            "value": "39.99",
            "estimated_low": None,
            "estimated_high": None,
            "currency": "USD",
            "basis": "HUMAN_OBSERVED",
            "modeled_value": "39.99",
            "modeled_value_basis": "HUMAN_OBSERVED",
            "modeled_value_is_conservative": True,
            "unresolved_materiality": "NON_MATERIAL",
            "source_references": [],
            "limitations": [
                "Price was reported by the human observer and was not independently "
                "verified from a receipt or retailer record.",
                "The report does not establish whether the observed price includes or "
                "excludes sales tax.",
            ],
            "notes": (
                "One pair, package quantity 1; new and in original box per human "
                "observation."
            ),
        },
        {
            "cost_id": "acquisition_sales_tax",
            "name": "Ross sales tax",
            "cost_type": "FIXED_PER_UNIT",
            "value": None,
            "estimated_low": None,
            "estimated_high": None,
            "currency": "USD",
            "basis": None,
            "modeled_value": None,
            "modeled_value_basis": None,
            "modeled_value_is_conservative": False,
            "unresolved_materiality": "MATERIAL",
            "source_references": [],
            "limitations": [
                "Exact Ross store location and receipt were not provided.",
                "Applicable tax rate and whether tax was included in the reported "
                "$39.99 are unknown.",
            ],
            "notes": "Potentially material unresolved acquisition cost; do not treat as zero.",
        },
    ]
}


class RequiredActionModel(Model):
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
                    call_id="offline-required-action",
                    name="record_acquisition_cost_findings",
                    type="function_call",
                )
            ],
            usage=Usage(),
            response_id="offline-response",
        )

    def stream_response(self, *args, **kwargs):
        raise AssertionError("streaming is not used in this test")


class AcquisitionCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "capture.db"
        self.repository = CandidateRepository(self.database_path)
        self.workflow = CandidateWorkflow(self.repository)
        self.started = self.workflow.start_candidate_evaluation(candidate_request())
        self.state = AcquisitionCaptureState()
        self.state.begin()
        self.context = AcquisitionCaptureContext(self.workflow, self.state)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def purchase_finding() -> EconomicCostFinding:
        return EconomicCostFinding(
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
            source_references=[
                SourceReference(
                    url="human-observation://current-store",
                    description="Price observed by the human at Ross",
                )
            ],
            limitations=["Store-specific observation"],
            notes="New in original box",
        )

    @staticmethod
    def unresolved_tax() -> EconomicCostFinding:
        return EconomicCostFinding(
            cost_id="purchase_sales_tax",
            name="Purchase sales tax",
            cost_type=CostType.PERCENT_OF_UNIT_PRICE,
            value=None,
            estimated_low=None,
            estimated_high=None,
            currency=None,
            basis=None,
            modeled_value=None,
            modeled_value_basis=None,
            modeled_value_is_conservative=False,
            unresolved_materiality=UnknownMateriality.MATERIAL,
            source_references=[],
            limitations=["Tax jurisdiction and rate are not known"],
            notes=None,
        )

    def invoke(self, findings: list[EconomicCostFinding]) -> str:
        return self.invoke_arguments(
            {"findings": [finding.model_dump(mode="json") for finding in findings]}
        )

    def invoke_arguments(self, arguments_value: dict) -> str:
        arguments = json.dumps(arguments_value)
        call = ResponseFunctionToolCall(
            arguments=arguments,
            call_id="offline-acquisition-capture",
            name=record_captured_acquisition_cost_findings.name,
            type="function_call",
        )
        context = ToolContext(
            self.context,
            tool_name=record_captured_acquisition_cost_findings.name,
            tool_call_id=call.call_id,
            tool_arguments=arguments,
            tool_call=call,
        )
        return asyncio.run(
            record_captured_acquisition_cost_findings.on_invoke_tool(
                context, arguments
            )
        )

    def artifacts(self):
        return self.repository.list_evaluation_artifacts(self.started.evaluation_id)

    def test_state_begins_clean_and_requires_exactly_one_success(self):
        self.assertEqual(self.state.attempts, 0)
        self.assertEqual(self.state.successful_results, [])
        with self.assertRaises(AcquisitionCaptureError):
            self.state.complete()

        result = EconomicCostFindingsResult(findings=[self.purchase_finding()])
        self.state.begin_action()
        self.state.select(result)
        self.assertEqual(self.state.complete(), result)

        with self.assertRaises(AcquisitionCaptureError):
            self.state.begin_action()
        with self.assertRaises(AcquisitionCaptureError):
            self.state.complete()

    def test_per_invocation_states_are_independent(self):
        other = AcquisitionCaptureState()
        other.begin()
        self.state.begin_action()
        self.state.select(
            EconomicCostFindingsResult(findings=[self.purchase_finding()])
        )
        self.assertEqual(other.attempts, 0)
        self.assertEqual(other.successful_results, [])

    def test_action_persists_human_observed_price_and_unresolved_material_tax(self):
        findings = [self.purchase_finding(), self.unresolved_tax()]

        output = self.invoke(findings)

        self.assertEqual(
            output,
            "Recorded 2 acquisition-side economic cost finding(s).",
        )
        self.assertEqual(self.state.complete().findings, findings)
        artifacts = self.artifacts()
        self.assertEqual(
            [artifact.artifact_type for artifact in artifacts],
            [EvaluationArtifactType.ACQUISITION_COST_FINDINGS],
        )
        persisted = EconomicCostFindingsResult.model_validate_json(
            artifacts[0].payload_json
        )
        self.assertEqual(persisted.findings, findings)
        self.assertNotIn("acquisition_shipping", [item.cost_id for item in findings])
        self.assertFalse(any(item.value == 0 for item in findings if item.value is not None))

    def test_failed_live_payload_exposes_both_independent_semantic_errors(self):
        with self.assertRaisesRegex(
            ValidationError, "known value cannot also have unresolved materiality"
        ):
            EconomicCostFindingsResult.model_validate(FAILED_LIVE_ARGUMENTS)

        corrected_unresolved = json.loads(json.dumps(FAILED_LIVE_ARGUMENTS))
        corrected_unresolved["findings"][0]["unresolved_materiality"] = None
        with self.assertRaisesRegex(
            ValidationError, "modeled_value requires ASSUMED modeled_value_basis"
        ):
            EconomicCostFindingsResult.model_validate(corrected_unresolved)

        corrected = json.loads(json.dumps(corrected_unresolved))
        corrected["findings"][0].update(
            modeled_value=None,
            modeled_value_basis=None,
            modeled_value_is_conservative=False,
        )
        result = EconomicCostFindingsResult.model_validate(corrected)
        self.assertEqual(len(result.findings), 2)
        self.assertIsNone(result.findings[0].unresolved_materiality)
        self.assertIsNone(result.findings[0].modeled_value)

    def test_failed_live_unresolved_sales_tax_is_valid_independently(self):
        finding = EconomicCostFinding.model_validate(FAILED_LIVE_ARGUMENTS["findings"][1])

        self.assertIsNone(finding.value)
        self.assertIsNone(finding.basis)
        self.assertEqual(finding.unresolved_materiality, UnknownMateriality.MATERIAL)

    def test_unresolved_cost_rejects_invented_zero_and_invalid_assumption_metadata(self):
        invented_zero = json.loads(json.dumps(FAILED_LIVE_ARGUMENTS["findings"][1]))
        invented_zero["value"] = "0"
        with self.assertRaisesRegex(ValidationError, "basis is required"):
            EconomicCostFinding.model_validate(invented_zero)

        invalid_assumption = json.loads(json.dumps(FAILED_LIVE_ARGUMENTS["findings"][1]))
        invalid_assumption.update(
            modeled_value="0",
            modeled_value_basis="HUMAN_OBSERVED",
        )
        with self.assertRaisesRegex(
            ValidationError, "modeled_value requires ASSUMED modeled_value_basis"
        ):
            EconomicCostFinding.model_validate(invalid_assumption)

    def test_capture_instructions_state_cross_field_semantics(self):
        instructions = acquisition_capture_agent.instructions

        self.assertIn("unresolved_materiality must be null, not NON_MATERIAL", instructions)
        self.assertIn("Do not duplicate an observed amount in modeled_value", instructions)
        self.assertIn("always use ASSUMED as its basis", instructions)
        self.assertIn("unknown sales tax remains unresolved MATERIAL", instructions)

    def test_multiple_actions_are_rejected_without_second_artifact(self):
        self.invoke([self.purchase_finding()])

        output = self.invoke([self.unresolved_tax()])

        self.assertIn("exactly one terminal action", output)
        self.assertEqual(len(self.artifacts()), 1)
        with self.assertRaises(AcquisitionCaptureError):
            self.state.complete()

    def test_persistence_failure_does_not_select_success(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """CREATE TRIGGER fail_capture
                   BEFORE INSERT ON evaluation_artifacts
                   BEGIN SELECT RAISE(ABORT, 'forced capture failure'); END"""
            )

        output = self.invoke([self.purchase_finding()])

        self.assertIn("forced capture failure", output)
        self.assertEqual(self.state.successful_results, [])
        self.assertEqual(self.artifacts(), [])
        with self.assertRaises(AcquisitionCaptureError):
            self.state.complete()

    def test_invalid_action_arguments_do_not_persist_or_select_success(self):
        invalid = self.purchase_finding().model_dump(mode="json")
        invalid["cost_id"] = "   "

        output = self.invoke_arguments({"findings": [invalid]})

        self.assertIn("Invalid JSON input", output)
        self.assertEqual(self.state.attempts, 0)
        self.assertEqual(self.state.successful_results, [])
        self.assertEqual(self.artifacts(), [])
        with self.assertRaises(AcquisitionCaptureError):
            self.state.complete()

    def test_evidence_bundle_preserves_raw_human_input_and_exact_report(self):
        raw_input = "I observed $39.99 at Ross; tax is not shown yet."
        report = "Exact sourcing report\nhttps://example.com/source"
        self.workflow.record_human_input(raw_input)

        evidence = self.workflow.acquisition_capture_evidence(
            SourcingTextReport(report_text=report)
        )

        self.assertIn(candidate_request().product_identity.model_dump_json(), evidence)
        self.assertIn(candidate_request().acquisition_source.model_dump_json(), evidence)
        self.assertIn(json.dumps({"text": raw_input}), evidence)
        self.assertTrue(evidence.endswith(report))

    def test_agent_boundary_is_plain_text_and_single_action_only(self):
        self.assertIsNone(acquisition_capture_agent.output_type)
        self.assertEqual(
            [tool.name for tool in acquisition_capture_agent.tools],
            ["record_acquisition_cost_findings"],
        )
        self.assertEqual(acquisition_capture_agent.model_settings.tool_choice, "required")
        self.assertFalse(acquisition_capture_agent.model_settings.parallel_tool_calls)
        self.assertEqual(
            acquisition_capture_agent.tool_use_behavior,
            "stop_on_first_tool",
        )
        self.assertIsNone(sourcing_agent.output_type)
        self.assertEqual([tool.name for tool in sourcing_agent.tools], ["web_search"])
        source = inspect.getsource(
            __import__(
                "arbitrage.acquisition_capture", fromlist=["run_required_acquisition_capture"]
            )
        ).lower()
        for forbidden in ("regex", "fallback", "json repair", "model_validate_json"):
            self.assertNotIn(forbidden, source)

    def test_required_action_executes_once_and_stops_without_second_model_turn(self):
        finding = self.purchase_finding()
        arguments = json.dumps(
            {"findings": [finding.model_dump(mode="json")]}
        )
        model = RequiredActionModel(arguments)
        test_agent = acquisition_capture_agent.clone(
            model=model,
            model_settings=acquisition_capture_agent.model_settings,
        )

        result = asyncio.run(
            Runner.run(
                test_agent,
                "Normalize this acquisition evidence.",
                context=self.context,
                run_config=RunConfig(tracing_disabled=True),
            )
        )

        self.assertEqual(model.calls, 1)
        self.assertEqual(model.seen_settings.tool_choice, "required")
        self.assertFalse(model.seen_settings.parallel_tool_calls)
        self.assertEqual(
            result.final_output,
            "Recorded 1 acquisition-side economic cost finding(s).",
        )
        self.assertEqual(self.state.complete().findings, [finding])
        artifacts = self.artifacts()
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(
            artifacts[0].artifact_type,
            EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
        )

    def test_lead_pre_candidate_run_has_no_acquisition_capture_hook(self):
        source = inspect.getsource(application.main)
        qualification_section, substantive_section = source.split("def evaluate", 1)
        self.assertNotIn("EvaluationCaptureHooks", qualification_section)
        self.assertIn("EvaluationCaptureHooks", substantive_section)


if __name__ == "__main__":
    unittest.main()
