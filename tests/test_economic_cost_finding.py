import unittest

from pydantic import ValidationError

from arbitrage.contracts import (
    CostType,
    EconomicCostFinding,
    InputBasis,
    ManagerEvaluationJudgment,
    ResaleRequest,
    ResaleResult,
    SourceReference,
    SourcingResult,
    UnknownMateriality,
)
from arbitrage.orchestration import (
    agent,
    lead_qualifier,
    resale_agent_tool,
    sourcing_agent_tool,
)
from arbitrage.specialists.resale import resale_agent
from arbitrage.specialists.sourcing import sourcing_agent


class EconomicCostFindingTests(unittest.TestCase):
    @staticmethod
    def finding(**changes) -> EconomicCostFinding:
        values = {
            "cost_id": "outbound_shipping",
            "name": "Outbound shipping",
            "cost_type": CostType.FIXED_PER_UNIT,
            "value": None,
            "estimated_low": "20",
            "estimated_high": "30",
            "currency": "USD",
            "basis": InputBasis.ESTIMATED,
            "modeled_value": "30",
            "modeled_value_basis": InputBasis.ASSUMED,
            "modeled_value_is_conservative": True,
            "unresolved_materiality": UnknownMateriality.NON_MATERIAL,
            "source_references": [
                SourceReference(
                    url="https://example.com/shipping",
                    description="Carrier estimate for the packaged product.",
                )
            ],
            "limitations": ["The destination ZIP code is not yet known."],
            "notes": "Use $30 as a conservative modeled value.",
        }
        values.update(changes)
        return EconomicCostFinding(**values)

    def test_known_fixed_and_percentage_costs(self):
        fixed = self.finding(
            value="8.25",
            estimated_low=None,
            estimated_high=None,
            basis=InputBasis.VERIFIED,
            modeled_value=None,
            modeled_value_basis=None,
            modeled_value_is_conservative=False,
            unresolved_materiality=None,
        )
        percentage = self.finding(
            cost_id="marketplace_fee",
            name="Marketplace fee",
            cost_type=CostType.PERCENT_OF_UNIT_PRICE,
            value="13.6",
            estimated_low=None,
            estimated_high=None,
            currency=None,
            basis=InputBasis.VERIFIED,
            modeled_value=None,
            modeled_value_basis=None,
            modeled_value_is_conservative=False,
            unresolved_materiality=None,
        )

        self.assertEqual(str(fixed.value), "8.25")
        self.assertEqual(percentage.cost_type, CostType.PERCENT_OF_UNIT_PRICE)
        self.assertEqual(str(percentage.value), "13.6")

    def test_estimated_range_and_conservative_modeled_value(self):
        finding = self.finding()

        self.assertEqual(str(finding.estimated_low), "20")
        self.assertEqual(str(finding.estimated_high), "30")
        self.assertEqual(finding.basis, InputBasis.ESTIMATED)
        self.assertEqual(str(finding.modeled_value), "30")
        self.assertEqual(finding.modeled_value_basis, InputBasis.ASSUMED)
        self.assertTrue(finding.modeled_value_is_conservative)
        self.assertEqual(len(finding.source_references), 1)
        self.assertEqual(len(finding.limitations), 1)

    def test_unresolved_material_cost_requires_no_invented_value(self):
        finding = self.finding(
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
            limitations=["Package dimensions are unavailable."],
            notes=None,
        )

        self.assertIsNone(finding.value)
        self.assertIsNone(finding.basis)
        self.assertEqual(finding.unresolved_materiality, UnknownMateriality.MATERIAL)

    def test_json_round_trip_preserves_provenance(self):
        finding = self.finding()

        self.assertEqual(
            EconomicCostFinding.model_validate_json(finding.model_dump_json()),
            finding,
        )

    def test_blank_identifiers_and_names_are_rejected(self):
        for field in ("cost_id", "name"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.finding(**{field: "   "})

    def test_ranges_must_be_complete_ordered_and_exclusive_with_value(self):
        invalid_changes = (
            {"estimated_low": None},
            {"estimated_high": None},
            {"estimated_low": "31", "estimated_high": "30"},
            {"value": "25"},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.finding(**changes)

    def test_evidence_value_and_basis_must_be_consistent(self):
        invalid_changes = (
            {"basis": None},
            {
                "value": None,
                "estimated_low": None,
                "estimated_high": None,
                "basis": InputBasis.VERIFIED,
            },
            {"basis": InputBasis.VERIFIED},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.finding(**changes)

    def test_modeled_value_metadata_must_be_consistent(self):
        invalid_changes = (
            {"modeled_value_basis": InputBasis.VERIFIED},
            {"modeled_value": None},
            {
                "modeled_value": None,
                "modeled_value_basis": None,
                "modeled_value_is_conservative": True,
            },
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.finding(**changes)

    def test_contradictory_or_empty_findings_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.finding(
                value="8.25",
                estimated_low=None,
                estimated_high=None,
                basis=InputBasis.VERIFIED,
            )
        with self.assertRaises(ValidationError):
            self.finding(
                value=None,
                estimated_low=None,
                estimated_high=None,
                basis=None,
                modeled_value=None,
                modeled_value_basis=None,
                modeled_value_is_conservative=False,
                unresolved_materiality=None,
            )

    def test_agent_boundaries_are_unchanged(self):
        self.assertNotIn("acquisition_cost_findings", SourcingResult.model_fields)
        self.assertNotIn("selling_cost_findings", ResaleResult.model_fields)
        self.assertNotIn(
            "supporting_profitability_reference",
            ManagerEvaluationJudgment.model_fields,
        )
        self.assertIsNone(lead_qualifier.output_type)
        self.assertIsNone(sourcing_agent.output_type)
        self.assertEqual(sourcing_agent_tool.params_json_schema["title"], "AgentAsToolInput")
        self.assertIs(resale_agent.output_type, ResaleResult)
        self.assertEqual(resale_agent_tool.params_json_schema["title"], "ResaleRequest")
        self.assertEqual(
            set(resale_agent_tool.params_json_schema["properties"]),
            set(ResaleRequest.model_fields),
        )
        self.assertIs(agent.output_type, ManagerEvaluationJudgment)


if __name__ == "__main__":
    unittest.main()
