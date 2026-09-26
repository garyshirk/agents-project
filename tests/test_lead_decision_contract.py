import unittest

from pydantic import ValidationError
from agents.agent_output import AgentOutputSchema

from arbitrage.contracts import (
    CandidateDestination,
    CandidateIntakeSource,
    CandidateRelation,
    CandidateSource,
    LeadDecision,
    LeadDisposition,
    ProductCondition,
    ProductIdentity,
    StartCandidateEvaluationRequest,
    EvaluationTrigger,
)


def candidate_request() -> StartCandidateEvaluationRequest:
    return StartCandidateEvaluationRequest(
        product_identity=ProductIdentity(
            brand="Nike",
            product_name="Pegasus 41",
            model_number="FD2722-001",
            upc_gtin=None,
            variant="Men's size 10",
            condition=ProductCondition.NEW,
            package_quantity=1,
        ),
        acquisition_source=CandidateSource(
            source_kind="PHYSICAL_STORE",
            name="Ross",
            physical_location="Human's current Ross; exact location not provided",
            url=None,
            seller_identity=None,
        ),
        resale_destination=CandidateDestination(
            destination_kind="MARKETPLACE",
            name="eBay",
            url=None,
            seller_account=None,
        ),
        intake_origin=CandidateIntakeSource.HUMAN_PHYSICAL,
        intake_snapshot=None,
        trigger=EvaluationTrigger.HUMAN_REQUEST,
        assumptions=[],
        uncertainties=[],
        candidate_notes=None,
        manager_notes=None,
    )


class LeadDecisionContractTests(unittest.TestCase):
    def decision(self, **changes) -> LeadDecision:
        values = {
            "disposition": LeadDisposition.CANDIDATE_READY,
            "relation_to_active": CandidateRelation.NO_ACTIVE_CANDIDATE,
            "reasoning": "The product, source, and destination are sufficiently specific.",
            "continue_substantive_evaluation": True,
            "candidate_request": candidate_request(),
            "clarification_question": None,
            "user_message": None,
            "unresolved_uncertainties": [],
        }
        values.update(changes)
        return LeadDecision(**values)

    def test_valid_dispositions_and_schema(self):
        self.decision()
        self.decision(
            disposition=LeadDisposition.EXISTING_CANDIDATE,
            relation_to_active=CandidateRelation.SAME_AS_ACTIVE,
            candidate_request=None,
        )
        self.decision(
            disposition=LeadDisposition.NEEDS_MORE_INFO,
            continue_substantive_evaluation=False,
            candidate_request=None,
            clarification_question="What is the model or style number?",
        )
        self.decision(
            disposition=LeadDisposition.AMBIGUOUS_BOUNDARY,
            relation_to_active=CandidateRelation.AMBIGUOUS,
            continue_substantive_evaluation=False,
            candidate_request=None,
            clarification_question="Is this about the current item or a new one?",
        )
        self.decision(
            disposition=LeadDisposition.STOP,
            continue_substantive_evaluation=False,
            candidate_request=None,
            user_message="There is not enough information to continue.",
        )
        schema = AgentOutputSchema(LeadDecision).json_schema()
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(set(schema["required"]), set(LeadDecision.model_fields))

    def test_contradictory_decisions_are_rejected(self):
        invalid_changes = [
            {"candidate_request": None},
            {"continue_substantive_evaluation": False},
            {"clarification_question": "Unneeded question"},
            {
                "disposition": LeadDisposition.EXISTING_CANDIDATE,
                "relation_to_active": CandidateRelation.NO_ACTIVE_CANDIDATE,
                "candidate_request": None,
            },
            {
                "disposition": LeadDisposition.NEEDS_MORE_INFO,
                "continue_substantive_evaluation": False,
                "candidate_request": None,
                "clarification_question": None,
            },
            {
                "disposition": LeadDisposition.AMBIGUOUS_BOUNDARY,
                "continue_substantive_evaluation": False,
                "candidate_request": None,
                "clarification_question": "Which item?",
            },
            {
                "disposition": LeadDisposition.STOP,
                "continue_substantive_evaluation": False,
                "candidate_request": None,
                "user_message": None,
            },
        ]
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.decision(**changes)


if __name__ == "__main__":
    unittest.main()
