import json
import unittest
from unittest.mock import patch

from agents.agent_output import AgentOutputSchema

from arbitrage.contracts import (
    CostType, EconomicCostFindingsResult, InputBasis, OutboundShippingNonapplicability,
    SourceReference, UnknownMateriality, EvaluationArtifactType,
)
from arbitrage.profitability_assembly import project_economic_findings
from arbitrage.profitability_request_assembly import assemble_profitability_request
from arbitrage.shipping_coverage import validate_outbound_shipping, OUTBOUND_SHIPPING_COST_ID
from arbitrage.selling_capture import record_captured_selling_cost_findings, selling_capture_agent
from arbitrage.tools.profitability import _calculate_profitability
from tests import test_profitability_request_assembly as fixtures
from tests import test_application_owned_profitability as application_fixture
from tests import test_selling_capture as capture_fixture
from tests.acquisition_readiness_fixtures import append_economic_readiness


class ShippingCoverageTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = fixtures.ProfitabilityRequestAssemblyTests
        self.fee = self.fixtures.finding("fee", value="1", basis=InputBasis.VERIFIED)

    def shipping(self, **kwargs):
        return self.fixtures.finding(OUTBOUND_SHIPPING_COST_ID, **kwargs)

    def result(self, *findings, disposition=None):
        return EconomicCostFindingsResult(findings=[self.fee, *findings],
                                         outbound_shipping_nonapplicability=disposition)

    def assemble(self, selling):
        return assemble_profitability_request(self.fixtures.candidate(), self.fixtures.evaluation(),
                                              self.fixtures.snapshot(selling=selling))

    def test_missing_ambiguous_and_buyer_shipping_block(self):
        for name in (None, "shipping", "buyer_paid_shipping", "acquisition_shipping"):
            findings = [] if name is None else [self.fixtures.finding(name, value="5", basis=InputBasis.VERIFIED)]
            result = self.assemble(self.result(*findings))
            self.assertFalse(result.success)
            self.assertEqual(result.failures[0].code.value, "SHIPPING_COVERAGE")
            self.assertIn("artifact 14 sequence 4", result.failures[0].message)
            self.assertTrue(any(status in result.failures[0].message for status in ("MISSING_COVERAGE", "INVALID_COVERAGE")))

    def test_material_unresolved_is_valid_and_preserved(self):
        selling = self.result(self.shipping(unresolved=UnknownMateriality.MATERIAL))
        self.assertEqual(validate_outbound_shipping(selling, "USD").status.value, "COVERED_MATERIAL_UNRESOLVED")
        assembled = self.assemble(selling)
        self.assertTrue(assembled.success)
        self.assertTrue(any(OUTBOUND_SHIPPING_COST_ID in item.related_cost_ids
                            for item in assembled.request.sale.unknowns))
        self.assertEqual(_calculate_profitability(assembled.request).status.value, "INSUFFICIENT_INPUTS")

    def test_supported_exact_cost_is_projected_once(self):
        finding = self.shipping(value="8", basis=InputBasis.HUMAN_OBSERVED)
        projected = project_economic_findings(self.fixtures.acquisition_findings(), self.result(finding), currency="USD")
        matches = [x for x in projected.selling_costs if x.cost_id == OUTBOUND_SHIPPING_COST_ID]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].value, 8)
        self.assertEqual(matches[0].basis, InputBasis.HUMAN_OBSERVED)
        self.assertTrue(self.assemble(self.result(finding)).success)

    def test_supported_range_keeps_conservative_projection_and_sensitivity(self):
        result = self.assemble(self.result(self.shipping(low="5", high="9", basis=InputBasis.ESTIMATED)))
        self.assertTrue(result.success)
        cost = next(x for x in result.request.sale.selling_costs if x.cost_id == OUTBOUND_SHIPPING_COST_ID)
        self.assertEqual(cost.value, 9)
        self.assertEqual(cost.basis, InputBasis.ESTIMATED)
        self.assertEqual(result.request.sensitivity_inputs[0].low_value, 5)
        self.assertEqual(result.request.sensitivity_inputs[0].high_value, 9)

    def test_supported_modeled_cost_preserves_assumed_basis_and_batch(self):
        finding = self.shipping(modeled="7", cost_type=CostType.FIXED_PER_BATCH)
        result = self.assemble(self.result(finding))
        self.assertTrue(result.success)
        cost = next(x for x in result.request.sale.selling_costs if x.cost_id == OUTBOUND_SHIPPING_COST_ID)
        self.assertEqual(cost.value, 7)
        self.assertEqual(cost.basis, InputBasis.ASSUMED)
        self.assertEqual(cost.cost_type, CostType.FIXED_PER_BATCH)

    def disposition(self, **kwargs):
        values = dict(fulfillment_mode="LOCAL_IN_PERSON_NO_SHIPMENT", rationale="Local in-person handover, no shipment.",
                      basis=InputBasis.HUMAN_OBSERVED, source_references=[SourceReference(
                          url="https://example.com/local-agreement", description="Explicit pickup fulfillment agreement")])
        values.update(kwargs)
        return OutboundShippingNonapplicability(**values)

    def test_nonapplicability_has_provenance_without_zero_expense(self):
        result = self.assemble(self.result(disposition=self.disposition()))
        self.assertTrue(result.success)
        self.assertFalse(any(x.cost_id == OUTBOUND_SHIPPING_COST_ID for x in result.request.sale.selling_costs))
        provenance = result.provenance.model_dump(mode="json")["outbound_shipping_coverage"]
        self.assertEqual(provenance["status"], "COVERED_NOT_APPLICABLE")
        self.assertEqual(provenance["nonapplicability"]["source_references"][0]["url"],
                         "https://example.com/local-agreement")
        json.dumps(result.provenance.model_dump(mode="json"))

    def test_nonapplicability_can_stand_alone_without_fabricated_findings(self):
        selling = EconomicCostFindingsResult(findings=[], outbound_shipping_nonapplicability=self.disposition())
        result = self.assemble(selling)
        self.assertTrue(result.success)
        self.assertEqual(result.request.sale.selling_costs, [])

    def test_ambiguous_alias_cannot_double_count_canonical_cost(self):
        canonical = self.shipping(value="5", basis=InputBasis.VERIFIED)
        alias = self.fixtures.finding("shipping", value="5", basis=InputBasis.VERIFIED)
        self.assertFalse(validate_outbound_shipping(self.result(canonical, alias), "USD").valid)

    def test_unsupported_nonapplicability_fails(self):
        for changes in ({"rationale": " "}, {"source_references": []}, {"basis": InputBasis.ASSUMED}):
            result = self.assemble(self.result(disposition=self.disposition(**changes)))
            self.assertFalse(result.success)
            self.assertIn("INVALID_COVERAGE", result.failures[0].message)

    def test_duplicates_and_contradictions_fail(self):
        cost = self.shipping(value="5", basis=InputBasis.VERIFIED)
        for selling in (self.result(cost, cost), self.result(cost, disposition=self.disposition()),
                        self.result(self.shipping(unresolved=UnknownMateriality.MATERIAL), disposition=self.disposition())):
            self.assertFalse(validate_outbound_shipping(selling, "USD").valid)

    def test_zero_requires_supported_basis_and_evidence(self):
        for basis in (InputBasis.ASSUMED, InputBasis.ESTIMATED):
            self.assertFalse(validate_outbound_shipping(self.result(self.shipping(value="0", basis=basis)), "USD").valid)
        finding = self.shipping(value="0", basis=InputBasis.VERIFIED)
        self.assertTrue(validate_outbound_shipping(self.result(finding), "USD").valid)
        finding.source_references = []
        self.assertFalse(validate_outbound_shipping(self.result(finding), "USD").valid)

    def test_currency_percentage_and_nonmaterial_unknown_fail(self):
        for finding in (self.shipping(value="5", basis=InputBasis.VERIFIED, currency="CAD"),
                        self.shipping(value="5", basis=InputBasis.VERIFIED, cost_type=CostType.PERCENT_OF_UNIT_PRICE),
                        self.shipping(unresolved=UnknownMateriality.NON_MATERIAL)):
            self.assertFalse(validate_outbound_shipping(self.result(finding), "USD").valid)

    def test_historical_json_reads_but_missing_coverage_fails(self):
        old = json.dumps({"findings": [self.fee.model_dump(mode="json")]})
        result = EconomicCostFindingsResult.model_validate_json(old)
        self.assertIsNone(result.outbound_shipping_nonapplicability)
        self.assertFalse(self.assemble(result).success)

    def test_failure_persists_without_engine_or_success_result(self):
        fixture = application_fixture.ApplicationOwnedProfitabilityTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        append_economic_readiness(fixture.repository, fixture.started.evaluation_id)
        fixture.repository.append_evaluation_artifact(fixture.started.evaluation_id,
            artifact_type=EvaluationArtifactType.SELLING_COST_FINDINGS, payload_json=self.result().model_dump_json())
        with patch("arbitrage.tools.profitability._calculate_profitability") as engine:
            result = fixture.invoke()
        engine.assert_not_called()
        self.assertFalse(result.success)
        self.assertIn("SHIPPING_COVERAGE", result.error)
        self.assertEqual(fixture.successful_profitability_artifacts(), [])
        self.assertIsNone(fixture.repository.get_evaluation(fixture.started.evaluation_id).profitability_result)
        self.assertEqual(len(fixture.profitability_artifacts()), 1)

    def test_strict_sdk_capture_schema_and_instructions(self):
        AgentOutputSchema(EconomicCostFindingsResult)
        self.assertIn("outbound_shipping_nonapplicability", record_captured_selling_cost_findings.params_json_schema["properties"])
        self.assertIn(OUTBOUND_SHIPPING_COST_ID, selling_capture_agent.instructions)

    def test_nonapplicability_round_trips_through_actual_recording_tool(self):
        fixture = capture_fixture.SellingCaptureTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        disposition = self.disposition()
        output = fixture.invoke_arguments({"findings": [],
            "outbound_shipping_nonapplicability": disposition.model_dump(mode="json")})
        self.assertIn("Recorded 0", output)
        result = fixture.state.complete()
        self.assertEqual(result.outbound_shipping_nonapplicability, disposition)
        captured = next(item for item in fixture.artifacts()
                        if item.artifact_type == EvaluationArtifactType.SELLING_COST_FINDINGS)
        self.assertEqual(EconomicCostFindingsResult.model_validate_json(captured.payload_json), result)


if __name__ == "__main__":
    unittest.main()
