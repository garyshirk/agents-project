import asyncio
import copy
import json
import unittest
from unittest.mock import Mock, patch

from agents.agent_output import AgentOutputSchema

from arbitrage.contracts import (
    EconomicCostFindingsResult, MarketplaceFeeScope, InputBasis, UnknownMateriality,
    EvaluationArtifactType, CostType,
)
from arbitrage.fee_scope import validate_marketplace_fee_scope
from arbitrage.profitability_request_assembly import assemble_profitability_request
from arbitrage.tools.profitability import _calculate_profitability
from arbitrage.coordinator import ArbitrageCoordinator
from arbitrage.selling_capture import record_captured_selling_cost_findings
from tests import test_profitability_request_assembly as fixtures
from tests import test_application_owned_profitability as application_fixture
from tests import test_profitability_finalization_guard as guard_fixture
from tests import test_selling_capture as capture_fixture
from tests.acquisition_readiness_fixtures import append_economic_readiness


class FeeScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = fixtures.ProfitabilityRequestAssemblyTests

    def scope(self, relationship="INTEGRATED", **changes):
        values = dict(relationship=relationship,
            marketplace_fee_cost_ids=["marketplace_fee", "order_fee"],
            processing_fee_cost_ids=[], rationale="Fee terms specify the relationship.",
            evidence=[dict(source_kind="FEE_SCHEDULE",
                reference=dict(url="https://example.com/seller-fee-terms", description="Applicable seller fee schedule"),
                applicability_explanation="Terms explicitly identify inclusion or the additional processing charge.")])
        values.update(changes)
        return MarketplaceFeeScope.model_validate(values)

    def processing(self, **changes):
        values = dict(modeled="2.5", unresolved=None)
        values.update(changes)
        return self.fixtures.finding("payment_processing_charge", **values)

    def selling(self, scope=None, processing=None):
        result = self.fixtures.selling_findings().model_copy(deep=True)
        if processing is not None:
            result.findings.append(processing)
        result.marketplace_fee_scope = scope
        return result

    def assemble(self, selling, resale=None):
        return assemble_profitability_request(self.fixtures.candidate(), self.fixtures.evaluation(),
            self.fixtures.snapshot(selling=selling, resale=resale))

    def test_integrated_percent_and_order_components_project_once(self):
        result = self.assemble(self.selling(self.scope()))
        self.assertTrue(result.success)
        costs = result.request.sale.selling_costs
        self.assertEqual(sum(x.cost_id == "marketplace_fee" for x in costs), 1)
        self.assertEqual(sum(x.cost_id == "order_fee" for x in costs), 1)
        self.assertFalse(any(x.cost_id == "payment_processing_charge" for x in costs))
        self.assertEqual(next(x for x in costs if x.cost_id == "order_fee").cost_type, CostType.FIXED_PER_BATCH)
        self.assertEqual(result.provenance.marketplace_fee_scope["validation"], "INTEGRATED")

    def test_integrated_numeric_processing_is_blocked_not_silently_removed(self):
        scope = self.scope(processing_fee_cost_ids=["payment_processing_charge"])
        selling = self.selling(scope, self.processing())
        before = selling.model_dump(mode="json")
        result = self.assemble(selling)
        self.assertFalse(result.success)
        self.assertEqual(result.failures[0].code.value, "FEE_SCOPE_CONFLICT")
        self.assertEqual(selling.model_dump(mode="json"), before)

    def test_integrated_exact_range_and_zero_processing_are_also_blocked(self):
        scope = self.scope(processing_fee_cost_ids=["payment_processing_charge"])
        for finding in (
                self.processing(modeled=None, value="2", basis=InputBasis.VERIFIED),
                self.processing(modeled=None, low="1", high="3", basis=InputBasis.ESTIMATED),
                self.processing(modeled=None, value="0", basis=InputBasis.ASSUMED)):
            self.assertFalse(self.assemble(self.selling(scope, finding)).success)

    def test_integrated_unknown_processing_is_not_a_second_numeric_cost(self):
        scope = self.scope(processing_fee_cost_ids=["payment_processing_charge"])
        result = self.assemble(self.selling(scope, self.processing(modeled=None, unresolved=UnknownMateriality.MATERIAL)))
        self.assertTrue(result.success)
        self.assertFalse(any(x.cost_id == "payment_processing_charge" for x in result.request.sale.selling_costs))
        self.assertTrue(any("payment_processing_charge" in x.related_cost_ids for x in result.request.sale.unknowns))

    def test_separate_supported_modeled_amount_is_preserved(self):
        scope = self.scope("SEPARATELY_APPLICABLE", processing_fee_cost_ids=["payment_processing_charge"])
        result = self.assemble(self.selling(scope, self.processing()))
        self.assertTrue(result.success)
        processing = [x for x in result.request.sale.selling_costs if x.cost_id == "payment_processing_charge"]
        self.assertEqual(len(processing), 1)
        self.assertEqual(str(processing[0].value), "2.5")
        self.assertEqual(processing[0].basis, InputBasis.ASSUMED)

    def test_separate_unknown_amount_remains_material(self):
        scope = self.scope("SEPARATELY_APPLICABLE", processing_fee_cost_ids=["payment_processing_charge"])
        result = self.assemble(self.selling(scope, self.processing(modeled=None, unresolved=UnknownMateriality.MATERIAL)))
        self.assertTrue(result.success)
        self.assertTrue(any("payment_processing_charge" in x.related_cost_ids for x in result.request.sale.unknowns))
        self.assertEqual(_calculate_profitability(result.request).status.value, "INSUFFICIENT_INPUTS")

    def test_unresolved_both_unknown_preserves_uncertainty_without_numeric_processing(self):
        scope = self.scope("UNRESOLVED", evidence=[], processing_fee_cost_ids=["payment_processing_charge"])
        selling = self.selling(scope, self.processing(modeled=None, unresolved=UnknownMateriality.MATERIAL))
        market = selling.findings[0]
        market.modeled_value = None
        market.modeled_value_basis = None
        market.modeled_value_is_conservative = False
        result = self.assemble(selling)
        self.assertTrue(result.success)
        self.assertFalse(any(x.cost_id in {"marketplace_fee", "payment_processing_charge"}
                             for x in result.request.sale.selling_costs))
        self.assertGreaterEqual(len(result.request.sale.unknowns), 2)

    def test_unresolved_numeric_processing_cannot_be_added(self):
        scope = self.scope("UNRESOLVED", evidence=[], processing_fee_cost_ids=["payment_processing_charge"])
        result = self.assemble(self.selling(scope, self.processing()))
        self.assertFalse(result.success)
        self.assertEqual(result.failures[0].code.value, "FEE_SCOPE_CONFLICT")

    def test_historical_missing_metadata_stays_missing_and_blocks_processing(self):
        selling = self.selling(processing=self.processing())
        raw = selling.model_dump(mode="json")
        raw.pop("marketplace_fee_scope")
        historical = EconomicCostFindingsResult.model_validate(raw)
        self.assertIsNone(historical.marketplace_fee_scope)
        result = self.assemble(historical)
        self.assertFalse(result.success)
        self.assertEqual(result.failures[0].code.value, "FEE_SCOPE_UNRESOLVED")

    def test_missing_metadata_on_lone_marketplace_fee_does_not_infer_relationship(self):
        result = self.assemble(self.selling())
        self.assertTrue(result.success)
        self.assertEqual(result.provenance.marketplace_fee_scope["validation"], "NO_OVERLAP_DECLARED")
        self.assertIsNone(result.provenance.marketplace_fee_scope["declaration"])

    def test_distinct_ids_and_empty_evidence_do_not_establish_separate_charges(self):
        scope = self.scope("SEPARATELY_APPLICABLE", evidence=[], processing_fee_cost_ids=["payment_processing_charge"])
        self.assertFalse(self.assemble(self.selling(scope, self.processing())).success)

    def test_product_listing_and_mislabeled_listing_are_not_fee_documentation(self):
        for kind in ("PRODUCT_LISTING", "FEE_SCHEDULE"):
            scope = self.scope("SEPARATELY_APPLICABLE", processing_fee_cost_ids=["payment_processing_charge"])
            scope.evidence[0].source_kind = kind
            scope.evidence[0].reference.url = "https://www.ebay.com/itm/12345"
            self.assertFalse(self.assemble(self.selling(scope, self.processing())).success)

    def test_malformed_evidence_and_missing_explanation_block(self):
        for url, explanation in (("not-a-url", "explained"), ("ftp://example.com", "explained"),
                                 ("https://example.com:invalid/fees", "explained"),
                                 ("https://example.com/fees", " ")):
            scope = self.scope("SEPARATELY_APPLICABLE", processing_fee_cost_ids=["payment_processing_charge"])
            scope.evidence[0].reference.url = url
            scope.evidence[0].applicability_explanation = explanation
            self.assertFalse(self.assemble(self.selling(scope, self.processing())).success)

    def test_duplicate_crossed_or_missing_ids_reject(self):
        for changes in (dict(marketplace_fee_cost_ids=["marketplace_fee", "marketplace_fee"]),
                        dict(marketplace_fee_cost_ids=["missing"]),
                        dict(marketplace_fee_cost_ids=["payment_processing_charge"], processing_fee_cost_ids=["marketplace_fee"]),
                        dict(processing_fee_cost_ids=[])):
            scope = self.scope("SEPARATELY_APPLICABLE", processing_fee_cost_ids=["payment_processing_charge"])
            for key, value in changes.items():
                setattr(scope, key, value)
            self.assertFalse(self.assemble(self.selling(scope, self.processing())).success)

    def test_custom_independent_additional_charge_can_be_linked(self):
        finding = self.fixtures.finding("account_specific_payment_service", modeled="0.5")
        scope = self.scope("SEPARATELY_APPLICABLE", processing_fee_cost_ids=[finding.cost_id])
        result = self.assemble(self.selling(scope, finding))
        self.assertTrue(result.success)
        self.assertTrue(any(x.cost_id == finding.cost_id for x in result.request.sale.selling_costs))

    def test_identity_and_shipping_failure_precedence_unchanged(self):
        selling = self.selling(processing=self.processing())
        resale = self.fixtures.resale()
        resale.researched_identity.model_number = "different"
        result = self.assemble(selling, resale)
        self.assertEqual(result.failures[0].code.value, "IDENTITY_CONFLICT")
        selling.findings = [x for x in selling.findings if x.cost_id != "seller_paid_outbound_shipping"]
        self.assertEqual(self.assemble(selling).failures[0].code.value, "SHIPPING_COVERAGE")

    def test_same_snapshot_is_pure_and_provenance_contains_exact_evidence(self):
        snapshot = self.fixtures.snapshot(selling=self.selling(self.scope()))
        before = copy.deepcopy(snapshot)
        result = assemble_profitability_request(self.fixtures.candidate(), self.fixtures.evaluation(), snapshot)
        self.assertTrue(result.success)
        self.assertEqual(snapshot, before)
        self.assertEqual(result.provenance.artifact_references[-1].artifact_id, snapshot.selling_cost_findings.artifact_id)
        self.assertEqual(result.provenance.marketplace_fee_scope["declaration"], self.scope().model_dump(mode="json"))

    def test_failed_fee_scope_is_persisted_and_coordinator_does_not_retry(self):
        fixture = application_fixture.ApplicationOwnedProfitabilityTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        append_economic_readiness(fixture.repository, fixture.started.evaluation_id)
        selling = self.selling(processing=self.processing())
        artifact = fixture.repository.append_evaluation_artifact(fixture.started.evaluation_id,
            artifact_type=EvaluationArtifactType.SELLING_COST_FINDINGS, payload_json=selling.model_dump_json())
        with patch("arbitrage.tools.profitability._calculate_profitability") as engine:
            result = fixture.invoke()
        engine.assert_not_called()
        self.assertFalse(result.success)
        self.assertIn("FEE_SCOPE_UNRESOLVED", result.error)
        self.assertIn(str(artifact.artifact_id), result.error)
        self.assertEqual(fixture.successful_profitability_artifacts(), [])
        self.assertIsNone(fixture.repository.get_evaluation(fixture.started.evaluation_id).profitability_result)
        evaluate = Mock(return_value=guard_fixture.ProfitabilityFinalizationGuardTests.judgment("INCONCLUSIVE"))
        outcome = ArbitrageCoordinator(fixture.workflow).coordinate(
            guard_fixture.ProfitabilityFinalizationGuardTests.ready_decision(), "Review", evaluate)
        evaluate.assert_called_once()
        self.assertFalse(outcome.proceeded)
        self.assertIn("FEE_SCOPE_UNRESOLVED", outcome.blocked_details["categories"])

    def test_recording_tool_round_trips_fee_scope_and_strict_schema(self):
        fixture = capture_fixture.SellingCaptureTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        scope = self.scope(marketplace_fee_cost_ids=["marketplace_final_value_fee"])
        output = fixture.invoke_arguments(dict(findings=[fixture.known_marketplace_fee().model_dump(mode="json")],
            marketplace_fee_scope=scope.model_dump(mode="json")))
        self.assertIn("Recorded", output)
        self.assertEqual(fixture.state.complete().marketplace_fee_scope, scope)
        AgentOutputSchema(EconomicCostFindingsResult)
        self.assertIn("marketplace_fee_scope", record_captured_selling_cost_findings.params_json_schema["properties"])


if __name__ == "__main__":
    unittest.main()
