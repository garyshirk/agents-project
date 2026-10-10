import copy
import unittest

from arbitrage.contracts import (
    AcquisitionScenario,
    CostType,
    EconomicCostFinding,
    EconomicCostFindingsResult,
    InputBasis,
    ProfitabilityRequest,
    ProfitabilityStatus,
    SaleScenario,
    ScenarioSide,
    SourceType,
    SourceReference,
    UnknownMateriality,
)
from arbitrage.profitability_assembly import (
    PURCHASE_PRICE_COST_ID,
    project_economic_findings,
)
from arbitrage.tools.profitability import _calculate_cost, _calculate_profitability
from tests.test_lead_decision_contract import candidate_request


class ProfitabilityAssemblyTests(unittest.TestCase):
    @staticmethod
    def finding(
        cost_id: str,
        *,
        name: str | None = None,
        cost_type: CostType = CostType.FIXED_PER_UNIT,
        value: str | None = None,
        low: str | None = None,
        high: str | None = None,
        currency: str | None = "USD",
        basis: InputBasis | None = None,
        modeled: str | None = None,
        unresolved: UnknownMateriality | None = None,
        notes: str | None = None,
    ) -> EconomicCostFinding:
        return EconomicCostFinding(
            cost_id=cost_id,
            name=name or cost_id.replace("_", " ").title(),
            cost_type=cost_type,
            value=value,
            estimated_low=low,
            estimated_high=high,
            currency=currency,
            basis=basis,
            modeled_value=modeled,
            modeled_value_basis=InputBasis.ASSUMED if modeled is not None else None,
            modeled_value_is_conservative=modeled is not None,
            unresolved_materiality=unresolved,
            source_references=[
                SourceReference(url="https://example.com/evidence", description="Evidence")
            ],
            limitations=["Representative limitation"],
            notes=notes,
        )

    @classmethod
    def purchase(cls, cost_id: str = PURCHASE_PRICE_COST_ID):
        return cls.finding(
            cost_id,
            value="39.99",
            basis=InputBasis.HUMAN_OBSERVED,
            notes="Observed at Ross",
        )

    @staticmethod
    def result(*findings):
        return EconomicCostFindingsResult(findings=list(findings))

    def project(self, acquisition, selling):
        if not selling:
            selling = [
                self.finding(
                    "unresolved_selling_cost",
                    unresolved=UnknownMateriality.NON_MATERIAL,
                )
            ]
        return project_economic_findings(
            self.result(*acquisition), self.result(*selling), currency="USD"
        )

    def test_projects_ross_fixture_without_fabricating_unknown_costs(self):
        acquisition = [
            self.purchase(),
            self.finding(
                "acquisition_sales_tax",
                cost_type=CostType.PERCENT_OF_UNIT_PRICE,
                currency=None,
                unresolved=UnknownMateriality.MATERIAL,
            ),
        ]
        selling = [
            self.finding(
                "ebay_marketplace_fee",
                cost_type=CostType.PERCENT_OF_UNIT_PRICE,
                currency=None,
                unresolved=UnknownMateriality.MATERIAL,
            ),
            self.finding(
                "seller_paid_outbound_shipping",
                unresolved=UnknownMateriality.MATERIAL,
            ),
        ]

        projected = self.project(acquisition, selling)

        self.assertTrue(projected.valid)
        self.assertEqual(str(projected.unit_purchase_price), "39.99")
        self.assertEqual(projected.purchase_price_basis, InputBasis.HUMAN_OBSERVED)
        self.assertEqual(projected.additional_acquisition_costs, [])
        self.assertEqual(projected.selling_costs, [])
        self.assertEqual(
            [item.related_cost_ids for item in projected.acquisition_unknowns],
            [["acquisition_sales_tax"]],
        )
        self.assertEqual(len(projected.selling_unknowns), 2)
        self.assertFalse(any("0" == str(item.value) for item in projected.selling_costs))

        identity = candidate_request().product_identity
        result = _calculate_profitability(
            ProfitabilityRequest(
                acquisition=AcquisitionScenario(
                    product_identity=identity,
                    source_type=SourceType.PHYSICAL_RETAILER,
                    source_name="Ross",
                    source_references=[],
                    condition=identity.condition,
                    currency=projected.currency,
                    unit_purchase_price=projected.unit_purchase_price,
                    purchase_price_basis=projected.purchase_price_basis,
                    quantity=1,
                    additional_costs=projected.additional_acquisition_costs,
                    quantity_available=None,
                    quantity_available_basis=None,
                    purchase_limit=None,
                    purchase_requirements=[],
                    assumptions=projected.assumptions,
                    unknowns=projected.acquisition_unknowns,
                    notes=None,
                ),
                sale=SaleScenario(
                    marketplace_name="eBay",
                    marketplace_references=[],
                    currency=projected.currency,
                    target_condition=identity.condition,
                    resale_price_low="80",
                    resale_price_expected="90",
                    resale_price_high="100",
                    resale_price_basis=InputBasis.ESTIMATED,
                    selling_costs=projected.selling_costs,
                    quantity=1,
                    assumptions=[],
                    unknowns=projected.selling_unknowns,
                    notes=None,
                ),
                sensitivity_inputs=projected.sensitivity_inputs,
            )
        )
        self.assertEqual(result.status, ProfitabilityStatus.INSUFFICIENT_INPUTS)

    def test_historical_purchase_ids_are_not_silently_reinterpreted(self):
        for historical_id in ("acq_purchase_price", "purchase_price"):
            with self.subTest(historical_id=historical_id):
                projected = self.project([self.purchase(historical_id)], [])
                self.assertFalse(projected.valid)
                self.assertIn(PURCHASE_PRICE_COST_ID, projected.failure_reasons[0])

    def test_missing_and_duplicate_purchase_identifier_are_rejected(self):
        missing = self.project(
            [self.finding("acquisition_tax", value="2", basis=InputBasis.VERIFIED)],
            [],
        )
        duplicate = self.project([self.purchase(), self.purchase()], [])
        self.assertFalse(missing.valid)
        self.assertFalse(duplicate.valid)
        self.assertTrue(any("duplicate cost_id" in item for item in duplicate.failure_reasons))
        self.assertTrue(any("multiple" in item for item in duplicate.failure_reasons))

    def test_exact_additional_and_selling_costs_preserve_type_basis_and_units(self):
        acquisition = [
            self.purchase(),
            self.finding(
                "unit_packaging", value="1.25", basis=InputBasis.VERIFIED
            ),
        ]
        selling = [
            self.finding(
                "marketplace_fee",
                cost_type=CostType.PERCENT_OF_UNIT_PRICE,
                value="13.5",
                currency=None,
                basis=InputBasis.VERIFIED,
            ),
            self.finding(
                "order_fee",
                cost_type=CostType.FIXED_PER_BATCH,
                value="0.30",
                basis=InputBasis.VERIFIED,
            ),
        ]

        projected = self.project(acquisition, selling)

        self.assertTrue(projected.valid)
        self.assertEqual(projected.additional_acquisition_costs[0].value, 1.25)
        self.assertEqual(projected.selling_costs[0].value, 13.5)
        self.assertEqual(
            projected.selling_costs[0].cost_type,
            CostType.PERCENT_OF_UNIT_PRICE,
        )
        self.assertEqual(projected.selling_costs[1].cost_type, CostType.FIXED_PER_BATCH)
        self.assertNotIn(PURCHASE_PRICE_COST_ID, [item.cost_id for item in projected.additional_acquisition_costs])

    def test_existing_engine_preserves_percentage_unit_and_fixed_cost_scope(self):
        projected = self.project(
            [
                self.purchase(),
                self.finding(
                    "unit_cost", value="2", basis=InputBasis.VERIFIED
                ),
            ],
            [
                self.finding(
                    "batch_cost",
                    cost_type=CostType.FIXED_PER_BATCH,
                    value="2",
                    basis=InputBasis.VERIFIED,
                ),
                self.finding(
                    "percentage_cost",
                    cost_type=CostType.PERCENT_OF_UNIT_PRICE,
                    value="13.5",
                    currency=None,
                    basis=InputBasis.VERIFIED,
                ),
            ],
        )

        self.assertEqual(
            _calculate_cost(projected.additional_acquisition_costs[0], 3, 100), 6
        )
        self.assertEqual(_calculate_cost(projected.selling_costs[0], 3, 100), 2)
        self.assertEqual(_calculate_cost(projected.selling_costs[1], 3, 100), 40.5)

    def test_estimated_range_uses_high_baseline_and_preserves_sensitivity(self):
        ranged = self.finding(
            "outbound_shipping",
            low="8",
            high="14",
            basis=InputBasis.ESTIMATED,
        )

        projected = self.project([self.purchase()], [ranged])

        self.assertTrue(projected.valid)
        self.assertEqual(projected.selling_costs[0].value, 14)
        self.assertEqual(projected.selling_costs[0].basis, InputBasis.ESTIMATED)
        sensitivity = projected.sensitivity_inputs[0]
        self.assertEqual(sensitivity.scenario_side, ScenarioSide.SALE)
        self.assertEqual(sensitivity.low_value, 8)
        self.assertEqual(sensitivity.high_value, 14)
        self.assertIn("conservative baseline", projected.assumptions[0])

    def test_modeled_assumption_does_not_erase_material_unknown(self):
        finding = self.finding(
            "outbound_shipping",
            modeled="12",
            unresolved=UnknownMateriality.MATERIAL,
        )

        projected = self.project([self.purchase()], [finding])

        self.assertTrue(projected.valid)
        self.assertEqual(projected.selling_costs[0].value, 12)
        self.assertEqual(projected.selling_costs[0].basis, InputBasis.ASSUMED)
        self.assertEqual(
            projected.selling_unknowns[0].materiality,
            UnknownMateriality.MATERIAL,
        )
        self.assertEqual(
            projected.selling_unknowns[0].related_cost_ids,
            ["outbound_shipping"],
        )

    def test_range_with_modeled_value_uses_assumption_and_range_sensitivity(self):
        finding = self.finding(
            "shipping",
            low="8",
            high="14",
            basis=InputBasis.ESTIMATED,
            modeled="11",
        )
        projected = self.project([self.purchase()], [finding])
        self.assertTrue(projected.valid)
        self.assertEqual(projected.selling_costs[0].value, 11)
        self.assertEqual(projected.selling_costs[0].basis, InputBasis.ASSUMED)
        self.assertEqual(projected.sensitivity_inputs[0].low_value, 8)
        self.assertEqual(projected.sensitivity_inputs[0].high_value, 14)

    def test_nonmaterial_unknown_is_preserved_without_zero_component(self):
        finding = self.finding(
            "optional_packaging",
            unresolved=UnknownMateriality.NON_MATERIAL,
        )
        projected = self.project([self.purchase()], [finding])
        self.assertTrue(projected.valid)
        self.assertEqual(projected.selling_costs, [])
        self.assertEqual(
            projected.selling_unknowns[0].materiality,
            UnknownMateriality.NON_MATERIAL,
        )

    def test_buyer_paid_uncertainty_is_not_converted_to_seller_cost(self):
        buyer_paid_effect = self.finding(
            "buyer_paid_shipping_effect",
            unresolved=UnknownMateriality.MATERIAL,
            notes="Payer and effect on seller proceeds are unresolved.",
        )
        projected = self.project([self.purchase()], [buyer_paid_effect])
        self.assertTrue(projected.valid)
        self.assertEqual(projected.selling_costs, [])
        self.assertEqual(
            projected.selling_unknowns[0].related_cost_ids,
            ["buyer_paid_shipping_effect"],
        )

    def test_duplicate_side_ids_and_currency_mismatches_are_rejected(self):
        duplicate = self.finding("packaging", value="1", basis=InputBasis.VERIFIED)
        wrong_currency = self.finding(
            "shipping", value="5", currency="CAD", basis=InputBasis.VERIFIED
        )
        duplicate_result = self.project([self.purchase()], [duplicate, duplicate])
        currency_result = self.project([self.purchase()], [wrong_currency])
        self.assertFalse(duplicate_result.valid)
        self.assertFalse(currency_result.valid)
        self.assertIn("duplicate cost_id", duplicate_result.failure_reasons[0])
        self.assertIn("currency USD", currency_result.failure_reasons[0])

    def test_ambiguous_purchase_and_exact_modeled_overlap_are_rejected(self):
        wrong_scope = self.purchase().model_copy(
            update={"cost_type": CostType.FIXED_PER_BATCH}
        )
        exact_and_modeled = self.finding(
            "shipping",
            value="10",
            basis=InputBasis.VERIFIED,
            modeled="12",
        )
        self.assertFalse(self.project([wrong_scope], []).valid)
        overlap = self.project([self.purchase()], [exact_and_modeled])
        self.assertFalse(overlap.valid)
        self.assertIn("both an exact value", overlap.failure_reasons[0])

        exact_and_unresolved = self.finding(
            "tax",
            cost_type=CostType.PERCENT_OF_UNIT_PRICE,
            value="8",
            currency=None,
            basis=InputBasis.ESTIMATED,
            unresolved=UnknownMateriality.MATERIAL,
        )
        contradiction = self.project([self.purchase()], [exact_and_unresolved])
        self.assertFalse(contradiction.valid)
        self.assertIn("unresolved materiality", contradiction.failure_reasons[0])

    def test_provenance_is_preserved_and_inputs_are_not_mutated(self):
        purchase = self.purchase()
        selling = self.finding(
            "marketplace_fee",
            cost_type=CostType.PERCENT_OF_UNIT_PRICE,
            value="13.5",
            currency=None,
            basis=InputBasis.VERIFIED,
        )
        acquisition_result = self.result(purchase)
        selling_result = self.result(selling)
        before_acquisition = copy.deepcopy(acquisition_result)
        before_selling = copy.deepcopy(selling_result)

        projected = project_economic_findings(
            acquisition_result, selling_result, currency="USD"
        )

        self.assertEqual(acquisition_result, before_acquisition)
        self.assertEqual(selling_result, before_selling)
        evidence = next(
            item for item in projected.provenance if item.cost_id == "marketplace_fee"
        )
        self.assertEqual(evidence.source_references, selling.source_references)
        self.assertEqual(evidence.limitations, selling.limitations)
        self.assertEqual(evidence.notes, selling.notes)


if __name__ == "__main__":
    unittest.main()
