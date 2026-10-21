import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agents.agent_output import AgentOutputSchema

from arbitrage.contracts import (
    CandidateIntake, CandidateIntakeSource, IntakeChannel, IntakeProvenance,
    OpportunityEvidenceStatus, IdentityConfidence, ProductIdentity,
    LeadDecision, CandidateRelation, LeadDisposition, ManagerEvaluationJudgment,
)
from arbitrage.intake_provenance import bind_human_intake
from arbitrage.coordinator import ArbitrageCoordinator
from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.persistence import CandidateRepository
from arbitrage.orchestration import lead_qualifier, agent
from arbitrage.lead_routing import route_candidate_ready
from arbitrage.specialists.sourcing import sourcing_agent
from arbitrage.acquisition_capture import acquisition_capture_agent, validate_acquisition_purchase_price
from tests.test_lead_decision_contract import candidate_request
from tests import test_acquisition_capture as economic_fixtures


class IntakeProvenanceTests(unittest.TestCase):
    def request(self, status=None, excerpts=None, channel="SYSTEM_DISCOVERY"):
        request = candidate_request()
        request.intake_origin = CandidateIntakeSource.HUMAN_ONLINE
        request.intake_snapshot = CandidateIntake(
            intake_source=CandidateIntakeSource.AUTONOMOUS_DISCOVERY,
            product_identity=None, identity_confidence=IdentityConfidence.AMBIGUOUS,
            identity_basis=None, acquisition=None, resale=None, notes=None,
            provenance=IntakeProvenance(channel=channel, opportunity_status=status,
                                        supporting_user_excerpts=excerpts or []))
        return request

    def test_hypothetical_ross_is_human_submission_not_online_observation(self):
        text = "Suppose Ross sells this item for $59.99."
        result = bind_human_intake(self.request("HYPOTHETICAL", [text]), text)
        self.assertEqual(result.intake_origin, CandidateIntakeSource.HUMAN_SUBMISSION)
        self.assertEqual(result.intake_snapshot.provenance.opportunity_status.value, "HYPOTHETICAL")

    def test_in_store_and_online_reports_are_distinct(self):
        for status, text in (("HUMAN_REPORTED_IN_STORE", "I saw this item at Ross today for $59.99."),
                             ("HUMAN_REPORTED_ONLINE", "I found an online listing offering this item for $59.99.")):
            result = bind_human_intake(self.request(status, [text]), text)
            self.assertEqual(result.intake_snapshot.provenance.opportunity_status.value, status)
            self.assertEqual(result.intake_snapshot.provenance.channel, IntakeChannel.HUMAN_SUBMISSION)

    def test_product_first_hypothesis_provenance_does_not_create_source_discovery(self):
        text = "Could this product be profitable if I could acquire it for $59.99?"
        result = bind_human_intake(self.request("HYPOTHETICAL", [text]), text)
        self.assertEqual(result.intake_snapshot.provenance.opportunity_status.value, "HYPOTHETICAL")
        self.assertIsNone(result.intake_snapshot.acquisition)
        # This tests provenance only; named-source Candidate requirements remain unchanged.
        self.assertEqual(result.acquisition_source.name, "Ross")

    def test_agent_cannot_override_application_owned_channel_or_origin(self):
        request = self.request()
        result = bind_human_intake(request, "Ross")
        self.assertEqual(result.intake_snapshot.provenance.channel, IntakeChannel.HUMAN_SUBMISSION)
        self.assertEqual(result.intake_origin, CandidateIntakeSource.HUMAN_SUBMISSION)
        self.assertEqual(result.intake_snapshot.intake_source, CandidateIntakeSource.HUMAN_SUBMISSION)
        self.assertEqual(request.intake_snapshot.provenance.channel, IntakeChannel.SYSTEM_DISCOVERY)

    def test_retailer_and_number_alone_do_not_automatically_infer_status(self):
        result = bind_human_intake(candidate_request(), "Ross $59.99 eBay")
        self.assertIsNone(result.intake_snapshot.provenance.opportunity_status)
        self.assertEqual(result.intake_snapshot.provenance.supporting_user_excerpts, [])

    def test_missing_fabricated_or_blank_support_cannot_establish_status(self):
        for excerpts in ([], ["I saw it"], ["  "], ["Ross", "invented observation"]):
            result = bind_human_intake(self.request("HUMAN_REPORTED_IN_STORE", excerpts), "Ross")
            self.assertIsNone(result.intake_snapshot.provenance.opportunity_status)

    def test_historical_online_json_is_not_reclassified(self):
        old = self.request().intake_snapshot.model_dump(mode="json")
        old.pop("provenance")
        old["intake_source"] = "HUMAN_ONLINE"
        parsed = CandidateIntake.model_validate_json(json.dumps(old))
        self.assertIsNone(parsed.provenance)
        self.assertEqual(parsed.intake_source, CandidateIntakeSource.HUMAN_ONLINE)

    def test_absent_and_explicit_unknown_provenance_remain_unknown(self):
        parsed = IntakeProvenance()
        self.assertIsNone(parsed.channel)
        self.assertIsNone(parsed.opportunity_status)
        request = self.request()
        request.intake_snapshot.provenance = None
        result = bind_human_intake(request, "Unspecified lead")
        self.assertIsNone(result.intake_snapshot.provenance.opportunity_status)

    def test_coordinator_enforces_and_persists_provenance_without_mutating_proposal(self):
        text = "Suppose Ross sells this item for $59.99."
        request = self.request("HYPOTHETICAL", [text])
        decision = LeadDecision(disposition=LeadDisposition.CANDIDATE_READY,
            relation_to_active=CandidateRelation.NO_ACTIVE_CANDIDATE, reasoning="Specified scenario",
            continue_substantive_evaluation=True, candidate_request=request,
            clarification_question=None, user_message=None, unresolved_uncertainties=[])
        with tempfile.TemporaryDirectory() as directory:
            repository = CandidateRepository(Path(directory) / "test.db")
            workflow = CandidateWorkflow(repository)
            coordinator = ArbitrageCoordinator(workflow)
            judgment = ManagerEvaluationJudgment(evaluation_outcome="WAITING_FOR_INPUT",
                candidate_conclusion="INCONCLUSIVE", assumptions=[], uncertainties=[],
                manager_notes=None, user_response="Which condition?")
            outcome = coordinator.coordinate(decision, text, Mock(return_value=judgment))
            candidate = repository.get_candidate(outcome.workflow_result.candidate_id)
            evaluation = repository.get_evaluation(outcome.workflow_result.evaluation_id)
            self.assertEqual(candidate.intake_origin, CandidateIntakeSource.HUMAN_SUBMISSION)
            self.assertEqual(evaluation.intake_snapshot.provenance.opportunity_status.value, "HYPOTHETICAL")
            self.assertEqual(evaluation.intake_snapshot.provenance.supporting_user_excerpts, [text])
            self.assertEqual(request.intake_origin, CandidateIntakeSource.HUMAN_ONLINE)

    def test_instructions_distinguish_channel_status_and_fact_basis(self):
        for text in ("HUMAN_REPORTED_IN_STORE", "HUMAN_REPORTED_ONLINE", "HYPOTHETICAL",
                     "supporting_user_excerpts", "number alone does not establish observation"):
            self.assertIn(text, lead_qualifier.instructions)
        self.assertIn("no intake status promotes", agent.instructions)
        self.assertIn("hypothetical availability unobserved", sourcing_agent.instructions)
        self.assertIn("Never turn hypothetical availability into HUMAN_OBSERVED", acquisition_capture_agent.instructions)

    def test_strict_sdk_schema_constructs_with_optional_provenance(self):
        schema = AgentOutputSchema(CandidateIntake).json_schema()
        self.assertIn("IntakeProvenance", schema["$defs"])
        self.assertIn("HUMAN_SUBMISSION", json.dumps(route_candidate_ready.params_json_schema))

    def test_existing_observed_price_and_hypothetical_basis_remain_separate(self):
        fixture = economic_fixtures.AcquisitionCaptureTests
        observed = fixture.purchase_finding()
        values = observed.model_dump()
        values.update(basis="ASSUMED", value="59.99", limitations=["Explicit hypothetical assumption"])
        hypothetical = type(observed).model_validate(values)
        validate_acquisition_purchase_price([observed])
        validate_acquisition_purchase_price([hypothetical])
        self.assertEqual(observed.basis.value, "HUMAN_OBSERVED")
        self.assertEqual(hypothetical.basis.value, "ASSUMED")
        self.assertEqual(str(hypothetical.value), "59.99")
