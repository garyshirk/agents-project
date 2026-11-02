import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from arbitrage.contracts import ProfitabilityRequest, ProfitabilityResult
from arbitrage.tools.profitability import _calculate_profitability
from arbitrage.v2.contracts import (
    Artifact, ArtifactType, AssessmentDraft, ComparableDraft, ComparableProduct, ComparableSource,
    EconomicInput, HumanActor, OpportunitySubmission, PriceEvidence, Producer, RawResearchReport,
    ScenarioDraft, SummaryDraft, SupportingRecord, UnknownEconomicInput,
)
from arbitrage.v2.economics import assemble_scenario, calculate_scenario
from arbitrage.v2.persistence import IdempotencyConflict, V2PersistenceError, V2Repository
from tests.v2_economic_fixtures import assumed_input, provenance, replace_input, scenario


class V2EconomicsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = V2Repository(self.root / "v2.db", session_database_path=self.root / "sessions.db")
        self.repo.initialize_schema()
        self.producer = Producer(kind="HUMAN", name="Synthetic fixture author")
        self.accepted = self.repo.accept_opportunity(OpportunitySubmission(original_text="Fictional economics only.",
            scenario_kind="HYPOTHETICAL", human_actor="Fixture"), operation_key="accept")
        self.eid = self.accepted.evaluation.evaluation_id
        self.raw = self.repo.record_artifact(self.eid, RawResearchReport(report_text="Entirely synthetic prices and costs."),
            producer=self.producer, operation_key="raw")
        self.comp_draft = ComparableDraft(product=ComparableProduct(description="Fictional comparable, different color."),
            price_evidence=PriceEvidence(evidence_type="COMPLETED_SALE", amount="100", currency="USD",
                quantity_or_unit_basis="one item", completion_basis="Fictional completed sale fixture.",
                realized_price_basis="Fictional explicit realized price fixture."),
            source=ComparableSource(source_category="MARKETPLACE", capture_method="SYNTHETIC_FIXTURE",
                other_reference="Fictional source, not live evidence.", raw_artifact_references=(self.raw.reference,)))
        self.comp = self.repo.record_comparable(self.eid, self.comp_draft, producer=self.producer, operation_key="comparable")
        self.draft = scenario(self.raw.reference, self.comp.reference)

    def record(self, draft=None, key="scenario", **options):
        return self.repo.record_scenario(self.eid, draft or self.draft, producer=self.producer, operation_key=key, **options)

    def calculate(self, artifact=None, key="calculation"):
        return self.repo.calculate_scenario((artifact or self.record()).reference, producer=self.producer, operation_key=key)

    def summary(self, calculation=None, state="INSUFFICIENT_INFORMATION", key="summary", **changes):
        draft = SummaryDraft(**(dict(assessment_state=state, state_decision_rationale="Explicit synthetic human judgment.",
            consequential_uncertainties=("All evidence is fictional.",), known_economic_facts=(),
            unknown_economic_inputs=(), assumptions=(), limitations=("Not a commercial recommendation.",),
            policy_version="fixture-summary-v1", calculation_references=(calculation.reference,) if calculation else ()) | changes))
        return self.repo.record_summary(self.eid, draft, producer=self.producer, operation_key=key)

    def state(self):
        with closing(sqlite3.connect(self.repo.database_path)) as c:
            return tuple((table, tuple(c.execute(f"SELECT * FROM {table} ORDER BY rowid"))) for table in
                ("v2_evaluations", "v2_artifacts", "v2_operations", "v2_reviews"))

    def unknown(self, key="shipping", **changes):
        return replace_input(self.draft, key, disposition="UNKNOWN", amount=None, basis=None,
                             rationale="Fictional material cost is unresolved.", **changes)

    def review(self, key):
        return self.repo.record_review(self.eid, self.repo.get_evaluation(self.eid).current_review_subject,
            "WORTH_INVESTIGATING", "Synthetic review only.", human=HumanActor(actor="Fixture"), operation_key=key)

    def test_scenario_persistence_and_exact_revision(self):
        artifact = self.record()
        self.assertEqual(artifact, self.repo.get_scenario(artifact.reference))
        self.assertEqual(artifact.payload.inputs, self.draft.inputs)
        self.assertEqual(artifact.payload.created_at, artifact.created_at)
        self.assertEqual(artifact.reference.evaluation_id, self.eid)
        self.assertEqual(artifact.payload.revision, 1)

    def test_multiple_scenarios_are_independent(self):
        first = self.record()
        second = self.record(self.draft.model_copy(update={"name":"Separate explicit scenario"}), key="second")
        self.assertNotEqual(first.payload.scenario_id, second.payload.scenario_id)
        self.assertEqual(self.repo.list_current_scenarios(self.eid), (first, second))

    def test_revisions_preserve_history_and_reject_forks(self):
        first = self.record()
        second = self.repo.record_scenario_revision(first.reference, self.draft.model_copy(update={"name":"Revised explicit name"}),
            producer=self.producer, operation_key="revision")
        self.assertEqual(second.payload.scenario_id, first.payload.scenario_id)
        self.assertEqual(second.payload.supersedes, first.reference)
        self.assertEqual(second.payload.revision, 2)
        self.assertEqual(self.repo.list_scenario_history(self.eid), (first, second))
        self.assertEqual(self.repo.get_current_scenario(self.eid, first.payload.scenario_id), second)
        with self.assertRaises(V2PersistenceError):
            self.record(key="fork", predecessor=first.reference)
        with self.assertRaises(ValidationError):
            first.payload.name = "Mutate"

    def test_evidence_supported_vertical_calculation_and_summary(self):
        calculation = self.calculate()
        self.assertEqual(calculation.payload.status, "CALCULATED")
        output = ProfitabilityResult.model_validate_json(calculation.payload.engine_output_json)
        self.assertEqual(output.low_case.net_profit, Decimal("40.00"))
        self.assertIsNone(output.expected_case)
        summary = self.summary(calculation, "EVIDENCE_BASED_ESTIMATE", numerical_estimate=True)
        self.assertEqual(summary.payload.calculation_references, (calculation.reference,))
        self.assertEqual(summary.payload.assessment_state, "EVIDENCE_BASED_ESTIMATE")

    def test_conditional_asking_price_vertical_fixture(self):
        asking = self.repo.record_comparable(self.eid, self.comp_draft.model_copy(update={"price_evidence":PriceEvidence(
            evidence_type="ACTIVE_ASK", amount="120", currency="USD", quantity_or_unit_basis="one item")}),
            producer=self.producer, operation_key="asking")
        draft = self.draft.model_copy(update={"comparable_references":(asking.reference,)})
        draft = replace_input(draft, "resale", evidence_references=(asking.reference,))
        draft = assumed_input(self.repo,self.eid,self.producer,draft,"resale","90","resale-assumption")
        draft = assumed_input(self.repo,self.eid,self.producer,draft,"shipping","9","shipping-assumption")
        calculation = self.calculate(self.record(draft))
        self.assertEqual(calculation.payload.status,"CALCULATED")
        self.assertEqual(self.repo.get_comparable(asking.reference).payload.price_evidence.evidence_type,"ACTIVE_ASK")
        self.assertEqual(len(calculation.payload.assumption_references),2)
        assumptions = tuple(self.repo.get_artifact(r).payload.assumption for r in calculation.payload.assumption_references)
        self.summary(calculation,"CONDITIONAL_ESTIMATE",numerical_estimate=True,assumptions=assumptions)
        self.assertEqual(ProfitabilityResult.model_validate_json(calculation.payload.engine_output_json).low_case.net_profit,Decimal("30.00"))

    def test_all_assumption_sources_and_historical_exact_provenance(self):
        historical = self.repo.accept_opportunity(OpportunitySubmission(original_text="Fictional earlier context.",
            scenario_kind="HYPOTHETICAL",human_actor="Fixture"),operation_key="historical")
        for category in ("MODEL_GENERAL_KNOWLEDGE","CURRENT_EVALUATION_EVIDENCE","HISTORICAL_EVALUATIONS","HUMAN_PROVIDED"):
            refs = () if category in {"MODEL_GENERAL_KNOWLEDGE","HUMAN_PROVIDED"} else (SupportingRecord(source=category,
                artifact=historical.evaluation.current_review_subject if category == "HISTORICAL_EVALUATIONS" else self.raw.reference),)
            p = provenance(True,sources=(category,),supporting_records=refs)
            draft = assumed_input(self.repo,self.eid,self.producer,self.draft,"shipping","9",category,support=p)
            calc = self.calculate(self.record(draft,key=category+"-scenario"),key=category+"-calc")
            self.assertEqual(calc.payload.status,"CALCULATED")
            self.assertEqual(calc.payload.scenario_manifest.inputs[4].provenance,p)

    def test_multiple_knowledge_sources_no_automatic_accuracy_claim(self):
        p = provenance(True,sources=("MODEL_GENERAL_KNOWLEDGE","HUMAN_PROVIDED"))
        draft = assumed_input(self.repo,self.eid,self.producer,self.draft,"shipping","9","multi",support=p)
        self.assertEqual(self.calculate(self.record(draft)).payload.status,"CALCULATED")
        self.assertIsNone(self.repo.get_current_summary(self.eid))

    def test_general_knowledge_assumption_needs_no_url(self):
        draft = assumed_input(self.repo,self.eid,self.producer,self.draft,"shipping","9","assumed")
        self.assertEqual(self.calculate(self.record(draft)).payload.status,"CALCULATED")

    def test_assumption_cannot_be_promoted_to_observed_evidence(self):
        with self.assertRaises(ValidationError):
            replace_input(self.draft,"shipping",provenance=provenance(True))
        draft = assumed_input(self.repo,self.eid,self.producer,self.draft,"shipping","9","assumed")
        ref = draft.inputs[4].assumption
        bad = replace_input(self.draft,"shipping",evidence_references=(ref,))
        with self.assertRaises(V2PersistenceError):
            self.record(bad)

    def test_direct_resale_cannot_average_or_invent_price(self):
        for amount in ("99","110"):
            with self.subTest(amount=amount),self.assertRaises(V2PersistenceError):
                self.record(replace_input(self.draft,"resale",amount=amount))

    def test_incomplete_comparable_never_supplies_missing_price(self):
        comp = self.repo.record_comparable(self.eid,self.comp_draft.model_copy(update={"price_evidence":PriceEvidence(
            evidence_type="ACTIVE_ASK")}),producer=self.producer,operation_key="incomplete")
        draft = self.unknown("resale").model_copy(update={"comparable_references":(comp.reference,)})
        draft = replace_input(draft,"resale",evidence_references=(comp.reference,))
        calc = self.calculate(self.record(draft))
        self.assertEqual(calc.payload.status,"INSUFFICIENT_INPUTS")
        self.assertIsNone(calc.payload.engine_output_json)

    def test_material_unknown_prevents_arithmetic_and_remains_visible(self):
        with patch("arbitrage.v2.economics._calculate_profitability") as engine:
            calculation = self.calculate(self.record(self.unknown()))
        engine.assert_not_called()
        self.assertEqual(calculation.payload.status,"INSUFFICIENT_INPUTS")
        self.assertTrue(calculation.payload.unresolved_inputs)
        self.assertIsNone(calculation.payload.engine_output_json)
        self.summary(calculation)
        self.assertEqual(self.repo.list_review_queue().total,1)

    def test_missing_categories_are_unknown_not_zero(self):
        draft = self.draft.model_copy(update={"inputs":self.draft.inputs[:2]})
        outcome = calculate_scenario(draft)
        self.assertEqual(outcome.status,"INSUFFICIENT_INPUTS")
        self.assertTrue(any("SELLER_SHIPPING" in s for s in outcome.unresolved_inputs))

    def test_explicit_assumption_resolves_only_named_scenario_unknown(self):
        draft = self.unknown().model_copy(update={"known_unknowns":(UnknownEconomicInput(name="shipping",reason="Unquoted"),)})
        draft = assumed_input(self.repo,self.eid,self.producer,draft,"shipping","9","assumption")
        calc = self.calculate(self.record(draft))
        self.assertEqual(calc.payload.status,"CALCULATED")
        self.assertEqual(calc.payload.scenario_manifest.known_unknowns[0].name,"shipping")
        self.assertEqual(self.raw.payload.report_text,"Entirely synthetic prices and costs.")

    def test_unrelated_assumption_does_not_erase_unknown(self):
        draft = self.draft.model_copy(update={"known_unknowns":(UnknownEconomicInput(name="other-material-cost",reason="Unknown"),)})
        self.assertEqual(calculate_scenario(draft).status,"INSUFFICIENT_INPUTS")

    def test_explicit_nonapplicability_does_not_synthesize_zero_row(self):
        outcome = calculate_scenario(self.draft)
        request = ProfitabilityRequest.model_validate_json(outcome.request_json)
        self.assertNotIn("processing",[c.cost_id for c in request.sale.selling_costs])
        with self.assertRaises(ValidationError):
            replace_input(self.draft,"processing",rationale=" ")

    def test_integrated_fees_require_coverage_and_are_counted_once(self):
        draft = replace_input(self.draft,"marketplace",covers=("processing",))
        draft = replace_input(draft,"processing",disposition="INCLUDED",covered_by="marketplace")
        draft = draft.model_copy(update={"fee_relationship":"INTEGRATED","fee_relationship_rationale":"Fictional composite fee includes processing."})
        self.assertEqual(calculate_scenario(draft).status,"CALCULATED")
        bad = replace_input(draft,"processing",disposition="EVIDENCE",covered_by=None,amount="3",currency="USD",
            basis="ESTIMATED",evidence_references=(self.raw.reference,))
        outcome = self.calculate(self.record(bad))
        self.assertEqual(outcome.payload.status,"INVALID_INPUTS")
        self.assertIsNone(outcome.payload.engine_output_json)

    def test_separate_fees_need_explicit_scope(self):
        draft = replace_input(self.draft,"processing",disposition="EVIDENCE",amount="3",currency="USD",
            basis="ESTIMATED",evidence_references=(self.raw.reference,))
        self.assertEqual(calculate_scenario(draft).status,"INVALID_INPUTS")
        draft = draft.model_copy(update={"fee_relationship":"SEPARATE","fee_relationship_rationale":"Explicit fictional separate fees."})
        self.assertEqual(calculate_scenario(draft).status,"CALCULATED")

    def test_shipping_included_and_additive_conflict(self):
        draft = replace_input(self.draft,"marketplace",covers=("shipping",))
        self.assertEqual(calculate_scenario(draft).status,"INVALID_INPUTS")
        draft = replace_input(draft,"shipping",disposition="INCLUDED",amount=None,basis=None,covered_by="marketplace")
        self.assertEqual(calculate_scenario(draft).status,"CALCULATED")

    def test_cross_side_coverage_rejected(self):
        draft = replace_input(self.draft,"purchase",covers=("shipping",))
        draft = replace_input(draft,"shipping",disposition="INCLUDED",amount=None,basis=None,covered_by="purchase")
        self.assertEqual(calculate_scenario(draft).status,"INVALID_INPUTS")

    def test_shipping_in_sale_revenue_does_not_remove_seller_expense(self):
        draft = replace_input(self.draft,"resale",covers=("shipping",))
        draft = replace_input(draft,"shipping",disposition="INCLUDED",amount=None,basis=None,covered_by="resale")
        self.assertEqual(calculate_scenario(draft).status,"INVALID_INPUTS")

    def test_currency_mismatch_is_durable_invalid_result(self):
        calculation = self.calculate(self.record(replace_input(self.draft,"shipping",currency="EUR")))
        self.assertEqual(calculation.payload.status,"INVALID_INPUTS")
        self.assertIn("Currency mismatch",str(calculation.payload.validation_errors))

    def test_price_unit_and_order_mismatches_are_unsupported(self):
        for draft in (replace_input(self.draft,"purchase",cost_type="FIXED_PER_BATCH"),
                      self.draft.model_copy(update={"order_count":2})):
            outcome = calculate_scenario(draft)
            self.assertEqual(outcome.status,"INVALID_INPUTS")
            self.assertTrue(outcome.unsupported_mappings)

    def test_quantity_and_batch_semantics_use_unchanged_engine(self):
        draft = self.draft.model_copy(update={"quantity":2})
        outcome = calculate_scenario(draft)
        result = ProfitabilityResult.model_validate_json(outcome.engine_output_json)
        self.assertEqual(result.low_case.net_profit,Decimal("88.00"))
        self.assertEqual(result.low_case.total_selling_cost,Decimal("28.00"))
        self.assertEqual(ProfitabilityRequest.model_validate_json(outcome.request_json).acquisition.quantity,2)

    def test_decimal_precision_and_engine_identity(self):
        draft = replace_input(self.draft,"purchase",amount="40.005")
        outcome = calculate_scenario(draft)
        request = ProfitabilityRequest.model_validate_json(outcome.request_json)
        self.assertEqual(request.acquisition.unit_purchase_price,Decimal("40.005"))
        self.assertEqual(outcome.engine_output_json,_calculate_profitability(request).model_dump_json())
        with self.assertRaises(ValidationError):
            replace_input(self.draft,"purchase",amount=40.005)

    def test_engine_error_persists_failure_not_profit(self):
        artifact = self.record()
        with patch("arbitrage.v2.economics._calculate_profitability",side_effect=ArithmeticError("Synthetic engine failure")):
            calculation = self.calculate(artifact)
        self.assertEqual(calculation.payload.status,"ERROR")
        self.assertEqual(calculation.payload.failure.category,"ArithmeticError")
        self.assertEqual(calculation.payload.failure.message,"Synthetic engine failure")
        self.assertIsNone(calculation.payload.engine_output_json)
        self.assertIsNotNone(calculation.payload.request_json)
        self.assertEqual(self.repo.get_latest_calculation(artifact.reference),calculation)

    def test_exact_manifest_and_json_round_trip(self):
        scenario_artifact = self.record()
        calculation = self.calculate(scenario_artifact)
        self.assertEqual(calculation.payload.scenario_manifest,scenario_artifact.payload)
        self.assertEqual(calculation.payload.scenario,scenario_artifact.reference)
        self.assertEqual(Artifact.model_validate_json(calculation.model_dump_json()),calculation)
        self.assertIn(self.comp.reference,calculation.payload.evidence_references)
        self.assertTrue(calculation.payload.engine_version.startswith("sha256:"))

    def test_old_revision_calculation_remains_historical_and_not_rerun(self):
        old = self.record()
        first = self.calculate(old)
        new = self.repo.record_scenario_revision(old.reference,self.draft.model_copy(update={"name":"Revision 2"}),
            producer=self.producer,operation_key="revision")
        self.assertIsNone(self.repo.get_latest_calculation(new.reference))
        historical = self.calculate(old,key="historical-calc")
        self.assertEqual(self.repo.list_calculation_history(self.eid,scenario=old.reference),(first,historical))
        self.assertEqual(self.repo.list_calculation_history(self.eid),(first,historical))

    def test_two_explicit_assumptions_produce_independent_calculations(self):
        calculations = []
        for n, amount in enumerate(("7","12")):
            draft = assumed_input(self.repo,self.eid,self.producer,self.draft,"shipping",amount,f"assumption-{n}")
            calculations.append(self.calculate(self.record(draft,key=f"scenario-{n}"),key=f"calc-{n}"))
        results = [ProfitabilityResult.model_validate_json(c.payload.engine_output_json).low_case.net_profit for c in calculations]
        self.assertEqual(results,[Decimal("41"),Decimal("36")])
        self.assertNotEqual(calculations[0].payload.scenario,calculations[1].payload.scenario)

    def test_summary_rejects_wrong_or_incomplete_calculation(self):
        with self.assertRaises(V2PersistenceError):
            self.summary(self.raw,"CONDITIONAL_ESTIMATE",numerical_estimate=True)
        calc = self.calculate(self.record(self.unknown()))
        with self.assertRaises(V2PersistenceError):
            self.summary(calc,"CONDITIONAL_ESTIMATE",numerical_estimate=True)
        with self.assertRaises(V2PersistenceError):
            self.summary(None,"CONDITIONAL_ESTIMATE",numerical_estimate=True)
        with self.assertRaises(V2PersistenceError):
            self.summary(calc,"INSUFFICIENT_INFORMATION",numerical_estimate=True)

    def test_adapter_never_assigns_assessment_state(self):
        self.calculate()
        self.assertIsNone(self.repo.get_current_summary(self.eid))
        self.assertIsNone(self.repo.review_status(self.eid).assessment_state)

    def test_scenario_and_calculation_each_invalidate_old_review(self):
        first = self.review("review-before-scenario")
        scenario_artifact = self.record()
        self.assertFalse(self.repo.review_is_current(first.review_id))
        second = self.review("review-before-calc")
        self.calculate(scenario_artifact)
        self.assertFalse(self.repo.review_is_current(second.review_id))
        self.assertEqual(self.repo.review_status(self.eid).review_state,"PENDING")

    def test_new_calculation_does_not_replace_summary(self):
        summary = self.summary()
        calc = self.calculate()
        self.assertEqual(self.repo.get_current_summary(self.eid),summary)
        self.assertFalse(self.repo.review_status(self.eid).summary_matches_current_subject)
        self.assertIn(calc,self.repo.get_review_evidence(self.eid).artifacts)

    def test_all_outcomes_remain_reviewable(self):
        scenarios = (self.draft,self.unknown(),replace_input(self.draft,"shipping",currency="EUR"))
        for n,draft in enumerate(scenarios):
            calc = self.calculate(self.record(draft,key=f"s-{n}"),key=f"c-{n}")
            self.assertIn(calc,self.repo.get_review_evidence(self.eid).artifacts)
        self.assertEqual(self.repo.list_review_queue().total,1)

    def test_idempotent_retry_returns_same_scenario_and_calculation(self):
        first = self.record()
        self.assertEqual(self.record(),first)
        calculation = self.calculate(first)
        before = self.state()
        with patch("arbitrage.v2.economics._calculate_profitability",side_effect=AssertionError("must not recalculate")):
            self.assertEqual(self.calculate(first),calculation)
        self.assertEqual(self.state(),before)

    def test_conflicting_keys_rejected(self):
        self.record()
        with self.assertRaises(IdempotencyConflict):
            self.record(self.draft.model_copy(update={"name":"Different"}))
        self.calculate()
        second = self.record(key="second")
        with self.assertRaises(IdempotencyConflict):
            self.calculate(second)

    def test_atomic_rollback_after_artifact_before_subject(self):
        before = self.state()
        with patch.object(self.repo,"_partial_subject",side_effect=RuntimeError("Synthetic write fault")):
            with self.assertRaises(RuntimeError):
                self.record()
        self.assertEqual(self.state(),before)
        artifact = self.record()
        before = self.state()
        with patch.object(self.repo,"_partial_subject",side_effect=RuntimeError("Synthetic write fault")):
            with self.assertRaises(RuntimeError):
                self.calculate(artifact)
        self.assertEqual(self.state(),before)

    def test_restart_reconstruction(self):
        scenario_artifact = self.record()
        calculation = self.calculate(scenario_artifact)
        repo = V2Repository(self.repo.database_path,session_database_path=self.repo.paths.sessions)
        self.assertEqual(repo.get_scenario(scenario_artifact.reference),scenario_artifact)
        self.assertEqual(repo.get_latest_calculation(scenario_artifact.reference),calculation)

    def test_receipt_failure_rolls_back_calculation_and_review_subject(self):
        artifact = self.record()
        with closing(sqlite3.connect(self.repo.database_path)) as c:
            c.execute("CREATE TRIGGER fail_calculation_receipt BEFORE INSERT ON v2_operations "
                      "WHEN NEW.operation='calculate_scenario' BEGIN SELECT RAISE(ABORT,'Synthetic receipt failure'); END")
            c.commit()
        before = self.state()
        with self.assertRaises(sqlite3.IntegrityError):
            self.calculate(artifact)
        self.assertEqual(self.state(),before)

    def test_no_models_network_or_real_databases(self):
        protected = [Path(__file__).resolve().parents[1]/name for name in ("arbitrage.db","sessions.db")]
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in protected]
        with patch("socket.socket",side_effect=AssertionError("No network")),patch("agents.Runner.run_sync",side_effect=AssertionError("No model")):
            self.calculate()
        self.assertEqual(before,[hashlib.sha256(p.read_bytes()).hexdigest() for p in protected])
        self.assertFalse(self.repo.paths.sessions.exists())

    def test_reference_ownership_and_sequence_tampering(self):
        for ref in (self.comp.reference.model_copy(update={"evaluation_id":"another"}),
                    self.comp.reference.model_copy(update={"sequence":999})):
            with self.subTest(ref=ref),self.assertRaises(V2PersistenceError):
                self.record(self.draft.model_copy(update={"comparable_references":(ref,)}))

    def test_assumption_value_and_unit_must_match_persisted_record(self):
        draft = assumed_input(self.repo,self.eid,self.producer,self.draft,"shipping","9","assumed")
        with self.assertRaises(V2PersistenceError):
            self.record(replace_input(draft,"shipping",amount="10"))
        with self.assertRaises(V2PersistenceError):
            self.record(replace_input(draft,"shipping",cost_type="FIXED_PER_UNIT"))

    def test_unsubstantiated_historical_provenance_rejected(self):
        draft = assumed_input(self.repo,self.eid,self.producer,self.draft,"shipping","9","assumed",
            support=provenance(True,sources=("HISTORICAL_EVALUATIONS",)))
        with self.assertRaises(V2PersistenceError):
            self.record(draft)

    def test_duplicate_ids_produce_durable_invalid_result(self):
        draft = self.draft.model_copy(update={"inputs":(*self.draft.inputs,self.draft.inputs[4])})
        calc = self.calculate(self.record(draft))
        self.assertEqual(calc.payload.status,"INVALID_INPUTS")

    def test_nonmaterial_unknown_retained_in_engine_quality(self):
        outcome = calculate_scenario(self.unknown(material=False))
        self.assertEqual(outcome.status,"CALCULATED")
        output = ProfitabilityResult.model_validate_json(outcome.engine_output_json)
        self.assertEqual(output.status,"PARTIAL")
        self.assertTrue(output.input_quality.unknown_inputs)

    def test_concurrent_idempotent_calculations_create_one_record(self):
        artifact = self.record()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _:self.calculate(artifact),range(2)))
        self.assertEqual(results[0],results[1])
        self.assertEqual(len(self.repo.list_calculation_history(self.eid)),1)

    def test_raw_calculation_payload_cannot_bypass_validated_writer(self):
        calculation = self.calculate()
        with self.assertRaises(V2PersistenceError):
            self.repo.record_artifact(self.eid,calculation.payload,producer=self.producer,operation_key="bypass")
        with self.assertRaises(ValidationError):
            Artifact.model_validate(calculation.model_dump() | {"payload":calculation.payload.model_dump() | {"engine_output_json":None}})

    def test_unexpected_float_currency_or_quantity_rejected(self):
        for changes in ({"quantity":0},{"quantity":1.5},{"quantity":True},{"inputs":()}):
            if "inputs" in changes:
                self.assertEqual(calculate_scenario(ScenarioDraft.model_validate(self.draft.model_dump() | changes)).status,"INSUFFICIENT_INPUTS")
            else:
                with self.assertRaises(ValidationError):
                    ScenarioDraft.model_validate(self.draft.model_dump() | changes)

    def test_pure_assembly_is_ready_without_executing_engine_or_mutating_input(self):
        before = self.draft.model_dump_json()
        with patch("arbitrage.v2.economics._calculate_profitability",side_effect=AssertionError("No arithmetic in assembly")):
            assembly = assemble_scenario(self.draft)
        self.assertEqual(assembly.status,"READY")
        self.assertEqual(self.draft.model_dump_json(),before)
        self.assertIsNotNone(assembly.request_json)

    def test_insufficient_vertical_preserves_partial_acquisition_and_resale(self):
        draft = self.unknown("purchase")
        draft = replace_input(draft,"resale",disposition="UNKNOWN",amount=None,basis=None)
        draft = draft.model_copy(update={"known_unknowns":(UnknownEconomicInput(name="resale",reason="No useful price."),)})
        calculation = self.calculate(self.record(draft))
        summary = self.summary(calculation,unknown_economic_inputs=draft.known_unknowns)
        self.assertEqual(calculation.payload.status,"INSUFFICIENT_INPUTS")
        self.assertEqual(summary.payload.unknown_economic_inputs,draft.known_unknowns)
        self.assertIsNone(calculation.payload.engine_output_json)
        self.assertTrue(self.repo.get_review_evidence(self.eid).artifacts)

    def test_assessment_is_bound_to_exact_comparable_revision(self):
        assessment = self.repo.record_assessment(self.eid,self.comp.reference,AssessmentDraft(
            candidate_context=self.accepted.evaluation.current_review_subject,
            candidate_identity=ComparableProduct(description="Fictional differing candidate"),category="WEAK",
            explanation="Weak comparable explicitly selected for a synthetic scenario.",
            material_known_differences=("Different color",),provenance=provenance(),policy_version="fixture-v1"),
            producer=self.producer,operation_key="assessment")
        draft = self.draft.model_copy(update={"assessment_references":(assessment.reference,)})
        calc = self.calculate(self.record(draft))
        self.assertEqual(calc.payload.status,"CALCULATED")
        self.assertIn(assessment.reference,calc.payload.evidence_references)
        revised = self.repo.record_comparable_revision(self.comp.reference,self.comp_draft,
            producer=self.producer,operation_key="comparable-revision")
        bad = draft.model_copy(update={"comparable_references":(revised.reference,)})
        with self.assertRaises(V2PersistenceError):
            self.record(bad,key="wrong-assessment-revision")

    def test_comparable_revision_does_not_automatically_rerun_calculation(self):
        first = self.calculate()
        self.repo.record_comparable_revision(self.comp.reference,self.comp_draft,
            producer=self.producer,operation_key="comparable-revision")
        self.assertEqual(self.repo.list_calculation_history(self.eid),(first,))
        self.assertEqual(first.payload.scenario_manifest.comparable_references,(self.comp.reference,))

    def test_summary_cannot_cite_calculation_from_another_evaluation(self):
        calc = self.calculate()
        other = self.repo.create_evaluation(self.accepted.opportunity.opportunity_id,operation_key="other-evaluation")
        draft = SummaryDraft(assessment_state="CONDITIONAL_ESTIMATE",state_decision_rationale="Synthetic estimate.",
            consequential_uncertainties=(),known_economic_facts=(),unknown_economic_inputs=(),assumptions=(),
            limitations=(),policy_version="fixture-v1",numerical_estimate=True,calculation_references=(calc.reference,))
        with self.assertRaises(V2PersistenceError):
            self.repo.record_summary(other.evaluation_id,draft,producer=self.producer,operation_key="other-summary")

    def test_nonapplicability_cannot_hide_amount_or_prices(self):
        with self.assertRaises(ValidationError):
            replace_input(self.draft,"shipping",disposition="NOT_APPLICABLE")
        outcome = calculate_scenario(replace_input(self.draft,"purchase",disposition="NOT_APPLICABLE",amount=None,basis=None))
        self.assertEqual(outcome.status,"INVALID_INPUTS")

    def test_foreign_currency_cannot_be_silently_converted_by_assumption(self):
        draft = assumed_input(self.repo,self.eid,self.producer,self.draft,"shipping","9","assumed")
        outcome = calculate_scenario(replace_input(draft,"shipping",currency="EUR"))
        self.assertEqual(outcome.status,"INVALID_INPUTS")

    def test_mutating_decoded_engine_request_cannot_change_calculation(self):
        calc = self.calculate()
        encoded = calc.model_dump_json()
        decoded = ProfitabilityRequest.model_validate_json(calc.payload.request_json)
        decoded.acquisition.unit_purchase_price = Decimal("0")
        self.assertEqual(calc.model_dump_json(),encoded)
        self.assertEqual(self.repo.get_latest_calculation(calc.payload.scenario),calc)

    def test_summary_author_selects_evidence_quality_not_adapter(self):
        draft = assumed_input(self.repo,self.eid,self.producer,self.draft,"shipping","9","assumed")
        calc = self.calculate(self.record(draft))
        # Python checks actual arithmetic, not whether the author's label is economically justified.
        summary = self.summary(calc,"EVIDENCE_BASED_ESTIMATE",numerical_estimate=True)
        self.assertEqual(summary.payload.assessment_state,"EVIDENCE_BASED_ESTIMATE")
        self.assertTrue(calc.payload.assumption_references)

    def test_pre_checkpoint_summary_receipt_remains_idempotently_readable(self):
        summary = self.summary(key="qualitative-summary")
        with closing(sqlite3.connect(self.repo.database_path)) as c:
            operation,request,result = c.execute("SELECT operation,request_json,result_json FROM v2_operations WHERE operation_key=?",
                ("qualitative-summary",)).fetchone()
            self.assertNotIn("numerical_estimate",json.loads(request)["draft"])
            old_result = json.loads(result)
            old_result["payload"].pop("numerical_estimate")
            c.execute("INSERT INTO v2_operations VALUES (?,?,?,?)",("old-receipt",operation,request,json.dumps(old_result)))
            c.commit()
        before = self.state()
        self.assertEqual(self.summary(key="old-receipt"),summary)
        self.assertEqual(self.state(),before)


if __name__ == "__main__":
    unittest.main()
