import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from arbitrage import orchestration
from arbitrage.acquisition_capture import (
    acquisition_capture_agent,
    validate_acquisition_purchase_price,
)
from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    CandidateRecord, CandidateSource, EconomicCostFindingsResult, InputBasis,
    UnknownMateriality,
)
from arbitrage.persistence import CandidateRepository
from arbitrage.profitability_request_assembly import assemble_profitability_request
from arbitrage.specialists.sourcing import sourcing_agent
from arbitrage.tools.profitability import _calculate_profitability
from tests.test_lead_decision_contract import candidate_request
from tests import test_profitability_request_assembly as assembly_fixtures


class RetailerLevelIntakeTests(unittest.TestCase):
    def source(self, location=None, name="Ross"):
        return CandidateSource(source_kind="PHYSICAL_STORE", name=name,
                               physical_location=location, url=None, seller_identity=None)

    def test_null_location_validates_and_round_trips(self):
        source = self.source()
        self.assertIsNone(CandidateSource.model_validate_json(source.model_dump_json()).physical_location)

    def test_blank_names_and_supplied_blank_locations_still_reject(self):
        for name in ("", "  "):
            with self.assertRaises(ValidationError):
                self.source(name=name)
        with self.assertRaises(ValidationError):
            self.source("  ")

    def test_supplied_location_is_preserved_exactly(self):
        location = "  Crystal Lake, IL  "
        self.assertEqual(self.source(location).physical_location, location)

    def test_scenario_a_and_b_persist_without_fabricated_location(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = CandidateRepository(Path(directory) / "test.db")
            for location in ("Crystal Lake, IL", None):
                workflow = CandidateWorkflow(repository)
                request = candidate_request()
                request.acquisition_source = self.source(location)
                request.assumptions = ["Purchase price is hypothetical"]
                request.uncertainties = ["Inventory and costs unverified"]
                result = workflow.start_candidate_evaluation(request)
                candidate = repository.get_candidate(result.candidate_id)
                evaluation = repository.get_evaluation(result.evaluation_id)
                self.assertEqual(candidate.acquisition_source.physical_location, location)
                self.assertEqual(candidate.lifecycle_status.value, "INVESTIGATING")
                self.assertEqual(evaluation.status.value, "IN_PROGRESS")
                self.assertEqual(evaluation.uncertainties, request.uncertainties)
                # Historical known-location JSON has no new fields or migration.
                self.assertEqual(CandidateRecord.model_validate_json(candidate.model_dump_json()), candidate)
                self.assertEqual(json.loads(candidate.model_dump_json())["acquisition_source"]["physical_location"], location)

    def test_lead_instructions_allow_preliminary_retailer_not_invented_store(self):
        instructions = orchestration.lead_qualifier.instructions
        for phrase in ("named acquisition retailer is sufficient", "physical_location null",
                       "not an unconditional Candidate prerequisite", "Never invent an address",
                       "reasonably answerable", "HUMAN_ASSUMED", "Select exactly one",
                       "product-first sourcing with no named acquisition source is outside"):
            self.assertIn(phrase, instructions)
        self.assertNotIn("human's current named store", instructions)

    def test_manager_preserves_uncertainty_and_nonactionable_scenario(self):
        instructions = orchestration.agent.instructions
        for phrase in ("retailer-level preliminary opportunity", "unknown location as null",
                       "ASSUMED, not a verified offer", "inventory, tax, shipping, and pickup",
                       "does not establish confirmed retailer inventory"):
            self.assertIn(phrase, instructions)

    def test_sourcing_distinguishes_assumptions_from_observations(self):
        instructions = sourcing_agent.instructions
        for phrase in ("not automatically HUMAN_OBSERVED", "hypothetical purchase price remains ASSUMED",
                       "unknown prices remain unknown", "without an exact store address",
                       "Never turn unknown store location into a specific inventory claim"):
            self.assertIn(phrase, instructions)

    def test_capture_instructions_preserve_assumed_price_and_unknown_costs(self):
        instructions = acquisition_capture_agent.instructions
        for phrase in ("ASSUMED basis", "null unresolved_materiality", "not a verified retailer offer",
                       "Do not duplicate the exact assumed amount in modeled_value",
                       "Do not infer confirmed inventory from requested quantity",
                       "remain unresolved, never zero"):
            self.assertIn(phrase, instructions)

    def test_hypothetical_price_assembles_without_inventing_inventory_or_tax(self):
        fixtures = assembly_fixtures.ProfitabilityRequestAssemblyTests
        acquisition = fixtures.acquisition_findings()
        price = acquisition.findings[0]
        self.assertEqual(price.cost_id, "acquisition_purchase_price")
        values = price.model_dump()
        values.update(value="59.99", basis=InputBasis.ASSUMED,
                      limitations=["Hypothetical; not a verified retailer offer"])
        assumed = type(price).model_validate(values)
        validate_acquisition_purchase_price([assumed])
        self.assertIsNone(assumed.modeled_value)
        self.assertIsNone(assumed.unresolved_materiality)
        findings = EconomicCostFindingsResult(findings=[assumed, *acquisition.findings[1:]])
        candidate = fixtures.candidate()
        candidate.acquisition_source = self.source()
        evaluation = fixtures.evaluation().model_copy(update={"intake_snapshot": None})
        result = assemble_profitability_request(candidate, evaluation,
                                               fixtures.snapshot(acquisition=findings))
        self.assertTrue(result.success, result.failures)
        request = result.request
        self.assertEqual(request.acquisition.unit_purchase_price, assumed.value)
        self.assertEqual(request.acquisition.purchase_price_basis, InputBasis.ASSUMED)
        self.assertEqual(request.acquisition.quantity, 1)
        self.assertIsNone(request.acquisition.quantity_available)
        self.assertIsNone(request.acquisition.quantity_available_basis)
        self.assertTrue(any(item.materiality == UnknownMateriality.MATERIAL
                            for item in request.acquisition.unknowns))
        self.assertEqual(_calculate_profitability(request).status.value, "INSUFFICIENT_INPUTS")


if __name__ == "__main__":
    unittest.main()
