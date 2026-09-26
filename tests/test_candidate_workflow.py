import asyncio
import inspect
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from agents.items import ItemHelpers
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall

from arbitrage.candidate_workflow import (
    ApplicationContext,
    CandidateWorkflow,
    CandidateWorkflowError,
    start_candidate_evaluation,
)
from arbitrage.contracts import (
    CandidateDestination,
    CandidateEvaluation,
    CandidateIntake,
    CandidateIntakeSource,
    CandidateLifecycleStatus,
    CandidateSource,
    CandidateWorkflowResult,
    EvaluationStatus,
    EvaluationTrigger,
    FinishCandidateEvaluationRequest,
    HumanAcquisitionInput,
    HumanInputBasis,
    IdentityConfidence,
    MatchQuality,
    ProductCondition,
    ProductIdentity,
    ProfitabilityRequest,
    ResaleResult,
    SourcingResult,
    StartCandidateEvaluationRequest,
    UpdateCandidateEvaluationRequest,
    WorkflowEvaluationAction,
)
from arbitrage.persistence import CandidateRepository, InvalidStateTransitionError
from arbitrage.tools.profitability import _calculate_profitability


class CandidateWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "workflow.db"
        self.repository = CandidateRepository(self.database_path)
        self.workflow = CandidateWorkflow(self.repository)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def identity(variant: str = "Men's size 10") -> ProductIdentity:
        return ProductIdentity(
            brand="Nike",
            product_name="Pegasus 41",
            model_number="FD2722-001",
            upc_gtin=None,
            variant=variant,
            condition=ProductCondition.NEW,
            package_quantity=1,
        )

    @staticmethod
    def source(location: str = "Algonquin, IL") -> CandidateSource:
        return CandidateSource(
            source_kind="PHYSICAL_STORE",
            name="Ross",
            physical_location=location,
            url=None,
            seller_identity=None,
        )

    @staticmethod
    def destination(name: str = "eBay") -> CandidateDestination:
        return CandidateDestination(
            destination_kind="MARKETPLACE",
            name=name,
            url=None,
            seller_account=None,
        )

    def intake(self) -> CandidateIntake:
        return CandidateIntake(
            intake_source=CandidateIntakeSource.HUMAN_PHYSICAL,
            product_identity=self.identity(),
            identity_confidence=IdentityConfidence.STRONG,
            identity_basis=HumanInputBasis.HUMAN_OBSERVED,
            acquisition=HumanAcquisitionInput(
                seller_or_store="Ross",
                purchase_price="39.99",
                currency="USD",
                quantity_available=1,
                purchase_limit=1,
                availability_status="LIMITED",
                condition="NEW",
                location_description="Algonquin, IL",
                observed_at=datetime.now(timezone.utc),
                price_basis="HUMAN_OBSERVED",
                quantity_basis="HUMAN_OBSERVED",
                condition_basis="HUMAN_OBSERVED",
                availability_basis="HUMAN_OBSERVED",
            ),
            resale=None,
            notes="Style and size observed on the box.",
        )

    def start_request(
        self,
        *,
        source: CandidateSource | None = None,
        destination: CandidateDestination | None = None,
    ) -> StartCandidateEvaluationRequest:
        return StartCandidateEvaluationRequest(
            product_identity=self.identity(),
            acquisition_source=source or self.source(),
            resale_destination=destination or self.destination(),
            intake_origin=CandidateIntakeSource.HUMAN_PHYSICAL,
            intake_snapshot=self.intake(),
            trigger=EvaluationTrigger.HUMAN_REQUEST,
            assumptions=["Authenticity assumed"],
            uncertainties=["Size-specific demand unknown"],
            candidate_notes="Human-supplied physical candidate",
            manager_notes="Initial investigation",
        )

    def sourcing_result(self) -> SourcingResult:
        return SourcingResult(
            product_identity=self.identity(),
            identity_confidence="STRONG",
            identity_confidence_reason="Model and variant match.",
            source_name="Ross",
            seller_name="Ross",
            source_url=None,
            source_references=[],
            item_price=39.99,
            currency="USD",
            shipping_cost=None,
            shipping_cost_known=False,
            other_known_costs=[],
            availability_status="LIMITED",
            inventory_quantity=1,
            purchase_limit=1,
            acquisition_requirements=[],
            unresolved_issues=[],
            research_timestamp="2026-09-26T12:00:00Z",
            notes=None,
        )

    def resale_result(self) -> ResaleResult:
        return ResaleResult(
            researched_identity=self.identity(),
            identity_match=MatchQuality.EXACT,
            identity_match_notes="Exact represented product.",
            evidence_items=[],
            verified_realized_price_low=None,
            verified_realized_price_high=None,
            estimated_achievable_price_low=84,
            estimated_achievable_price_high=109.97,
            currency="USD",
            evidence_quality="MODERATE",
            evidence_quality_reason="Representative workflow test evidence.",
            demand_assessment="UNKNOWN",
            market_observations=[],
            unresolved_issues=[],
            research_timestamp="2026-09-26T12:05:00Z",
            notes=None,
        )

    def profitability_result(self):
        request = ProfitabilityRequest.model_validate(
            {
                "acquisition": {
                    "product_identity": self.identity().model_dump(mode="json"),
                    "source_type": "PHYSICAL_RETAILER",
                    "source_name": "Ross",
                    "source_references": [],
                    "condition": "NEW",
                    "currency": "USD",
                    "unit_purchase_price": "39.99",
                    "purchase_price_basis": "ESTIMATED",
                    "quantity": 1,
                    "additional_costs": [],
                    "quantity_available": 1,
                    "quantity_available_basis": "ASSUMED",
                    "purchase_limit": 1,
                    "purchase_requirements": [],
                    "assumptions": [],
                    "unknowns": [],
                    "notes": None,
                },
                "sale": {
                    "marketplace_name": "eBay",
                    "marketplace_references": [],
                    "currency": "USD",
                    "target_condition": "NEW",
                    "resale_price_low": "84",
                    "resale_price_expected": "89.99",
                    "resale_price_high": "109.97",
                    "resale_price_basis": "ESTIMATED",
                    "selling_costs": [],
                    "quantity": 1,
                    "assumptions": [],
                    "unknowns": [],
                    "notes": None,
                },
                "sensitivity_inputs": [],
            }
        )
        return _calculate_profitability(request)

    def finish_request(
        self, status: EvaluationStatus = EvaluationStatus.COMPLETED
    ) -> FinishCandidateEvaluationRequest:
        return FinishCandidateEvaluationRequest(
            status=status,
            intake_snapshot=self.intake(),
            sourcing_result=self.sourcing_result(),
            resale_result=self.resale_result(),
            profitability_result=self.profitability_result(),
            assumptions=["Authenticity assumed"],
            uncertainties=["Size-specific demand unknown"],
            manager_notes="Evaluation complete",
        )

    def test_new_workflow_has_no_active_investigation(self):
        self.assertFalse(self.workflow.active.is_active)
        self.assertIsNone(self.workflow.active.candidate_id)
        self.assertIsNone(self.workflow.active.evaluation_id)

    def test_start_atomically_creates_candidate_and_evaluation(self):
        request = self.start_request()
        result = self.workflow.start_candidate_evaluation(request)
        candidate = self.repository.get_candidate(result.candidate_id)
        evaluation = self.repository.get_evaluation(result.evaluation_id)
        self.assertEqual(self.workflow.active.candidate_id, candidate.candidate_id)
        self.assertEqual(self.workflow.active.evaluation_id, evaluation.evaluation_id)
        self.assertEqual(candidate.lifecycle_status, CandidateLifecycleStatus.INVESTIGATING)
        self.assertEqual(evaluation.status, EvaluationStatus.IN_PROGRESS)
        self.assertEqual(evaluation.intake_snapshot, request.intake_snapshot)
        self.assertEqual(len(self.repository.list_candidates()), 1)
        self.assertEqual(len(self.repository.list_evaluations(candidate.candidate_id)), 1)

    def test_atomic_start_rolls_back_candidate_when_evaluation_insert_fails(self):
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                """
                CREATE TRIGGER reject_evaluation BEFORE INSERT ON candidate_evaluations
                BEGIN SELECT RAISE(FAIL, 'test failure'); END
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.workflow.start_candidate_evaluation(self.start_request())
        self.assertEqual(self.repository.list_candidates(), [])
        self.assertFalse(self.workflow.active.is_active)

    def test_wait_and_resume_preserve_active_ids(self):
        started = self.workflow.start_candidate_evaluation(self.start_request())
        waiting = self.workflow.update_candidate_evaluation(
            UpdateCandidateEvaluationRequest(
                action=WorkflowEvaluationAction.AWAIT_HUMAN_INPUT
            )
        )
        self.assertEqual(waiting.candidate_id, started.candidate_id)
        self.assertEqual(waiting.evaluation_id, started.evaluation_id)
        self.assertEqual(waiting.candidate_lifecycle, CandidateLifecycleStatus.AWAITING_HUMAN_INPUT)
        self.assertEqual(waiting.evaluation_status, EvaluationStatus.AWAITING_HUMAN_INPUT)
        resumed = self.workflow.update_candidate_evaluation(
            UpdateCandidateEvaluationRequest(action=WorkflowEvaluationAction.RESUME)
        )
        self.assertEqual(resumed.candidate_id, started.candidate_id)
        self.assertEqual(resumed.evaluation_id, started.evaluation_id)
        self.assertEqual(resumed.candidate_lifecycle, CandidateLifecycleStatus.INVESTIGATING)
        self.assertEqual(resumed.evaluation_status, EvaluationStatus.IN_PROGRESS)

    def test_finish_completed_persists_outputs_and_clears_active_state(self):
        started = self.workflow.start_candidate_evaluation(self.start_request())
        request = self.finish_request()
        result = self.workflow.finish_candidate_evaluation(request)
        candidate = self.repository.get_candidate(started.candidate_id)
        evaluation = self.repository.get_evaluation(started.evaluation_id)
        self.assertFalse(result.active)
        self.assertFalse(self.workflow.active.is_active)
        self.assertEqual(candidate.lifecycle_status, CandidateLifecycleStatus.EVALUATED)
        self.assertEqual(candidate.latest_evaluation_id, evaluation.evaluation_id)
        self.assertEqual(evaluation.status, EvaluationStatus.COMPLETED)
        self.assertEqual(evaluation.intake_snapshot, request.intake_snapshot)
        self.assertEqual(evaluation.sourcing_result, request.sourcing_result)
        self.assertEqual(evaluation.resale_result, request.resale_result)
        self.assertEqual(evaluation.profitability_result, request.profitability_result)

    def test_insufficient_evidence_does_not_require_profitability(self):
        started = self.workflow.start_candidate_evaluation(self.start_request())
        request = self.finish_request(EvaluationStatus.INSUFFICIENT_EVIDENCE)
        request.profitability_result = None
        result = self.workflow.finish_candidate_evaluation(request)
        evaluation = self.repository.get_evaluation(started.evaluation_id)
        self.assertEqual(result.evaluation_status, EvaluationStatus.INSUFFICIENT_EVIDENCE)
        self.assertIsNone(evaluation.profitability_result)
        self.assertEqual(
            self.repository.get_candidate(started.candidate_id).latest_evaluation_id,
            started.evaluation_id,
        )

    def test_failed_completion_preserves_v1a_semantics(self):
        started = self.workflow.start_candidate_evaluation(self.start_request())
        request = self.finish_request(EvaluationStatus.FAILED)
        request.sourcing_result = None
        request.resale_result = None
        request.profitability_result = None
        result = self.workflow.finish_candidate_evaluation(request)
        candidate = self.repository.get_candidate(started.candidate_id)
        self.assertEqual(result.evaluation_status, EvaluationStatus.FAILED)
        self.assertEqual(candidate.lifecycle_status, CandidateLifecycleStatus.INVESTIGATING)
        self.assertIsNone(candidate.latest_evaluation_id)
        self.assertFalse(self.workflow.active.is_active)

    def test_terminal_evaluation_cannot_be_modified_through_workflow(self):
        started = self.workflow.start_candidate_evaluation(self.start_request())
        self.workflow.finish_candidate_evaluation(self.finish_request())
        with self.assertRaises(CandidateWorkflowError):
            self.workflow.update_candidate_evaluation(
                UpdateCandidateEvaluationRequest(action=WorkflowEvaluationAction.RESUME)
            )
        with self.assertRaises(CandidateWorkflowError):
            self.workflow.finish_candidate_evaluation(self.finish_request())
        with self.assertRaises(InvalidStateTransitionError):
            self.repository.complete_evaluation(
                started.evaluation_id, status=EvaluationStatus.COMPLETED
            )

    def test_new_lead_is_refused_while_an_evaluation_is_active(self):
        started = self.workflow.start_candidate_evaluation(self.start_request())
        with self.assertRaises(CandidateWorkflowError):
            self.workflow.start_candidate_evaluation(
                self.start_request(source=self.source("Elgin, IL"))
            )
        self.assertEqual(self.workflow.active.candidate_id, started.candidate_id)
        self.assertEqual(len(self.repository.list_candidates()), 1)

    def test_waiting_investigation_is_preserved_when_new_lead_is_attempted(self):
        started = self.workflow.start_candidate_evaluation(self.start_request())
        self.workflow.update_candidate_evaluation(
            UpdateCandidateEvaluationRequest(
                action=WorkflowEvaluationAction.AWAIT_HUMAN_INPUT
            )
        )
        with self.assertRaises(CandidateWorkflowError):
            self.workflow.start_candidate_evaluation(
                self.start_request(destination=self.destination("Facebook Marketplace"))
            )
        evaluation = self.repository.get_evaluation(started.evaluation_id)
        self.assertEqual(evaluation.status, EvaluationStatus.AWAITING_HUMAN_INPUT)
        self.assertEqual(self.workflow.active.evaluation_id, started.evaluation_id)

    def test_separate_candidates_after_completion_preserve_first_history(self):
        first = self.workflow.start_candidate_evaluation(self.start_request())
        self.workflow.finish_candidate_evaluation(self.finish_request())
        first_record = self.repository.get_candidate(first.candidate_id)
        first_evaluation = self.repository.get_evaluation(first.evaluation_id)

        second = self.workflow.start_candidate_evaluation(
            self.start_request(source=self.source("Elgin, IL"))
        )
        self.workflow.finish_candidate_evaluation(self.finish_request())
        third = self.workflow.start_candidate_evaluation(
            self.start_request(destination=self.destination("Facebook Marketplace"))
        )

        self.assertEqual(len({first.candidate_id, second.candidate_id, third.candidate_id}), 3)
        self.assertEqual(len({first.evaluation_id, second.evaluation_id, third.evaluation_id}), 3)
        self.assertEqual(self.repository.get_candidate(first.candidate_id), first_record)
        self.assertEqual(self.repository.get_evaluation(first.evaluation_id), first_evaluation)

    def test_invalid_lifecycle_operations_fail_clearly(self):
        with self.assertRaises(CandidateWorkflowError):
            self.workflow.update_candidate_evaluation(
                UpdateCandidateEvaluationRequest(action=WorkflowEvaluationAction.RESUME)
            )
        self.workflow.start_candidate_evaluation(self.start_request())
        with self.assertRaises(InvalidStateTransitionError):
            self.workflow.update_candidate_evaluation(
                UpdateCandidateEvaluationRequest(action=WorkflowEvaluationAction.RESUME)
            )

    def test_workflow_api_owns_ids_timestamps_and_identity(self):
        start_parameters = inspect.signature(
            self.workflow.start_candidate_evaluation
        ).parameters
        finish_parameters = inspect.signature(
            self.workflow.finish_candidate_evaluation
        ).parameters
        self.assertEqual(list(start_parameters), ["request"])
        self.assertEqual(list(finish_parameters), ["request"])
        for field in ("candidate_id", "evaluation_id", "created_at", "updated_at"):
            self.assertNotIn(field, StartCandidateEvaluationRequest.model_fields)
        self.assertNotIn("started_at", StartCandidateEvaluationRequest.model_fields)
        self.assertNotIn("completed_at", FinishCandidateEvaluationRequest.model_fields)
        self.assertFalse(hasattr(self.workflow, "update_candidate_identity"))

    def test_tests_use_only_injected_temporary_database(self):
        project_database = Path(__file__).resolve().parent.parent / "arbitrage.db"
        self.assertNotEqual(self.database_path, project_database)
        self.assertEqual(self.database_path.parent, Path(self.temporary_directory.name))

    def invoke_start_tool(self, arguments: str):
        tool_call = ResponseFunctionToolCall(
            arguments=arguments,
            call_id="offline-call",
            name="start_candidate_evaluation",
            type="function_call",
        )
        context = ToolContext(
            ApplicationContext(candidate_workflow=self.workflow),
            tool_name=start_candidate_evaluation.name,
            tool_call_id=tool_call.call_id,
            tool_arguments=arguments,
            tool_call=tool_call,
        )
        output = asyncio.run(start_candidate_evaluation.on_invoke_tool(context, arguments))
        raw_item = ItemHelpers.tool_call_output_item(
            tool_call,
            output,
            output_json_schema=start_candidate_evaluation.output_json_schema,
            output_type_adapter=start_candidate_evaluation._output_type_adapter,
        )
        return CandidateWorkflowResult.model_validate_json(raw_item["output"])

    def test_start_function_tool_returns_schema_compatible_json_and_persists(self):
        request = self.start_request()
        result = self.invoke_start_tool(
            json.dumps({"request": request.model_dump(mode="json")})
        )

        self.assertTrue(result.success)
        self.assertIsNone(result.error)
        self.assertEqual(result.candidate_lifecycle, CandidateLifecycleStatus.INVESTIGATING)
        self.assertEqual(result.evaluation_status, EvaluationStatus.IN_PROGRESS)
        self.assertEqual(len(self.repository.list_candidates()), 1)
        self.assertEqual(len(self.repository.list_evaluations(result.candidate_id)), 1)
        self.assertEqual(
            self.repository.get_evaluation(result.evaluation_id).candidate_id,
            result.candidate_id,
        )

    def test_invalid_start_tool_input_returns_schema_compatible_failure_json(self):
        arguments = json.loads(
            json.dumps({"request": self.start_request().model_dump(mode="json")})
        )
        arguments["request"]["acquisition_source"]["physical_location"] = None

        result = self.invoke_start_tool(json.dumps(arguments))

        self.assertFalse(result.success)
        self.assertFalse(result.active)
        self.assertIsNone(result.candidate_id)
        self.assertIsNone(result.evaluation_id)
        self.assertIn("PHYSICAL_STORE", result.error)
        self.assertIn("physical_location", result.error)
        self.assertEqual(self.repository.list_candidates(), [])


if __name__ == "__main__":
    unittest.main()
