import asyncio
import json
import unittest
from unittest.mock import Mock

from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall

from arbitrage.contracts import (
    CandidateIntake, LeadIntakeAssessment, LeadDecision, OpportunityEvidenceStatus,
)
from arbitrage.coordinator import ArbitrageCoordinator
from arbitrage.intake_provenance import bind_human_intake
from arbitrage.lead_routing import route_candidate_ready
from arbitrage.lead_routing_state import LeadRoutingError
from tests import test_lead_routing_reliability as runtime
from tests import test_lead_decision_contract as fixtures
from tests import test_coordinator as coordinator_fixtures


class RequiredIntakeAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.harness = runtime.LeadRoutingReliabilityTests()
        self.harness.setUp()
        self.addCleanup(self.harness.tearDown)
        self.context = self.harness.context
        self.text = "Suppose Ross sells this item for $59.99; I have not visited a store."
        self.context.lead_routing.begin(self.text)

    def assessment(self):
        return LeadIntakeAssessment(status="HYPOTHETICAL", supporting_user_excerpts=[self.text],
                                    unspecified_reason=None, physical_acquisition_proposed=True)

    def arguments(self):
        return dict(candidate_request=fixtures.candidate_request().model_dump(mode="json"),
                    intake_assessment=self.assessment().model_dump(mode="json"),
                    reasoning="Hypothetical evaluation", unresolved_uncertainties=["Inventory unknown"])

    def invoke(self, arguments):
        encoded = json.dumps(arguments)
        call = ResponseFunctionToolCall(name=route_candidate_ready.name, arguments=encoded,
                                       call_id="assessment-test", type="function_call")
        context = ToolContext(self.context, tool_name=route_candidate_ready.name,
                              tool_arguments=encoded, tool_call_id=call.call_id, tool_call=call)
        return asyncio.run(route_candidate_ready.on_invoke_tool(context, encoded))

    def assert_rejected(self, arguments):
        self.context.lead_routing.begin(self.text)
        self.invoke(arguments)
        with self.assertRaises(LeadRoutingError):
            self.context.lead_routing.complete()
        self.assertEqual(self.harness.business_counts(), (0, 0, 0))

    def test_missing_and_null_assessments_do_not_record_terminal_decision(self):
        for value in ("missing", None):
            args = self.arguments()
            if value == "missing":
                args.pop("intake_assessment")
            else:
                args["intake_assessment"] = None
            self.assert_rejected(args)

    def test_missing_blank_and_fabricated_excerpts_reject(self):
        for excerpts in ([], [" "], ["I observed inventory"], [self.text, "fabricated"]):
            args = self.arguments()
            args["intake_assessment"]["supporting_user_excerpts"] = excerpts
            self.assert_rejected(args)

    def test_valid_hypothetical_action_and_null_intake_persist(self):
        args = self.arguments()
        args["candidate_request"]["acquisition_source"]["physical_location"] = None
        self.invoke(args)
        decision = self.context.lead_routing.complete()
        self.assertEqual(decision.intake_assessment.status, OpportunityEvidenceStatus.HYPOTHETICAL)
        workflow = self.context.candidate_workflow
        request = bind_human_intake(decision.candidate_request, self.text, decision.intake_assessment)
        started = workflow.start_candidate_evaluation(request)
        candidate = workflow.repository.get_candidate(started.candidate_id)
        evaluation = workflow.repository.get_evaluation(started.evaluation_id)
        self.assertEqual(candidate.intake_origin.value, "HUMAN_SUBMISSION")
        self.assertEqual(candidate.acquisition_source.source_kind.value, "PHYSICAL_STORE")
        self.assertIsNone(candidate.acquisition_source.physical_location)
        self.assertEqual(evaluation.intake_snapshot.provenance.supporting_user_excerpts, [self.text])
        self.assertEqual(evaluation.intake_snapshot.provenance.opportunity_status.value, "HYPOTHETICAL")
        self.assertTrue(evaluation.intake_snapshot.provenance.physical_acquisition_proposed)
        self.assertIsNone(evaluation.intake_snapshot.acquisition)

    def test_explicit_unspecified_is_distinct_from_historical_null(self):
        args = self.arguments()
        args["intake_assessment"] = fixtures.intake_assessment().model_dump(mode="json")
        self.invoke(args)
        decision = self.context.lead_routing.complete()
        result = bind_human_intake(decision.candidate_request, self.text, decision.intake_assessment)
        self.assertEqual(result.intake_snapshot.provenance.opportunity_status.value, "UNSPECIFIED")
        self.assertTrue(result.intake_snapshot.provenance.unspecified_reason)
        historical = result.intake_snapshot.model_dump(mode="json")
        historical.pop("provenance")
        historical["intake_source"] = "HUMAN_ONLINE"
        parsed = CandidateIntake.model_validate(historical)
        self.assertIsNone(parsed.provenance)
        self.assertEqual(parsed.intake_source.value, "HUMAN_ONLINE")

    def test_contradictory_and_malformed_assessment_rejects(self):
        for changes in ({"status": "UNSPECIFIED", "unspecified_reason": ""},
                        {"status": "UNSPECIFIED", "unspecified_reason": "reason"},
                        {"status": "HYPOTHETICAL", "unspecified_reason": "reason"},
                        {"status": "UNKNOWN"}, {"unexpected": True}):
            args = self.arguments()
            args["intake_assessment"].update(changes)
            self.assert_rejected(args)

    def test_physical_mode_and_other_source_contradiction_rejects(self):
        args = self.arguments()
        args["candidate_request"]["acquisition_source"]["source_kind"] = "OTHER"
        self.assert_rejected(args)

    def test_unspecified_mode_is_not_guessed_from_retailer_name(self):
        args = self.arguments()
        args["candidate_request"]["acquisition_source"]["source_kind"] = "OTHER"
        args["candidate_request"]["acquisition_source"]["physical_location"] = None
        args["intake_assessment"]["physical_acquisition_proposed"] = None
        self.invoke(args)
        decision = self.context.lead_routing.complete()
        self.assertEqual(decision.candidate_request.acquisition_source.source_kind.value, "OTHER")

    def test_nonphysical_or_undeclared_mode_cannot_assert_physical_store(self):
        for declaration in (False, None):
            args = self.arguments()
            args["intake_assessment"]["physical_acquisition_proposed"] = declaration
            self.assert_rejected(args)

    def test_coordinator_revalidates_without_fallback_candidate_creation(self):
        args = self.arguments()
        self.invoke(args)
        decision = self.context.lead_routing.complete()
        decision.intake_assessment = None
        evaluate = Mock()
        result = ArbitrageCoordinator(self.context.candidate_workflow).coordinate(decision, self.text, evaluate)
        self.assertFalse(result.proceeded)
        evaluate.assert_not_called()
        self.assertEqual(self.harness.business_counts(), (0, 0, 0))

    def test_sdk_can_correct_invalid_assessment_within_same_bounded_run(self):
        invalid = self.arguments()
        invalid.pop("intake_assessment")
        lead, model = self.harness.lead([
            [runtime.call("route_candidate_ready", json.dumps(invalid))],
            [runtime.call("route_candidate_ready", json.dumps(self.arguments()))]])
        self.harness.run_lead(lead)
        self.assertEqual(model.calls, 2)
        self.assertEqual(model.decision_counts, [0, 0])
        self.assertEqual(len(self.context.lead_routing.decisions), 1)

    def test_invalid_assessment_exhausts_existing_bound_without_business_records(self):
        args = self.arguments()
        args.pop("intake_assessment")
        lead, model = self.harness.lead([[runtime.call("route_candidate_ready", json.dumps(args))]], repeat=True)
        with self.assertRaises(runtime.MaxTurnsExceeded):
            self.harness.run_lead(lead)
        self.assertEqual(model.calls, 10)
        self.assertEqual(self.context.lead_routing.decisions, [])
        self.assertEqual(self.harness.business_counts(), (0, 0, 0))

    def test_revalidation_rejects_excerpts_against_different_current_message(self):
        self.invoke(self.arguments())
        decision = self.context.lead_routing.complete()
        with self.assertRaises(ValueError):
            bind_human_intake(decision.candidate_request, "Different message", decision.intake_assessment)

    def test_current_strict_action_schema_requires_assessment(self):
        schema = route_candidate_ready.params_json_schema
        self.assertIn("intake_assessment", schema["required"])
        assessment = schema["$defs"]["LeadIntakeAssessment"]
        self.assertFalse(assessment["additionalProperties"])
        self.assertEqual(set(assessment["required"]), set(LeadIntakeAssessment.model_fields))
