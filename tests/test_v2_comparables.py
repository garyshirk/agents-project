import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from arbitrage.v2.contracts import (
    Artifact, ArtifactType, AssessmentDraft, ComparableProduct, ComparableSource, ComparabilityCategory,
    FailureRecord, HumanActor, InformationKind, KnowledgeSource, OpportunitySubmission,
    PriceEvidence, Producer, ProductAttribute, Provenance, RawResearchReport, ReviewDecision,
    SupportingRecord,
)
from arbitrage.v2.persistence import IdempotencyConflict, V2PersistenceError, V2Repository
from arbitrage.v2.review import HumanReviewService
from tests import test_v2_foundation as foundation
from tests.v2_comparable_fixtures import synthetic_comparables


class V2ComparableTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name).resolve() / "v2.db"
        self.session_path = self.path.with_name("sessions.db")
        self.repo = V2Repository(self.path, session_database_path=self.session_path)
        self.repo.initialize_schema()
        self.accepted = self.repo.accept_opportunity(OpportunitySubmission(
            original_text="Synthetic hypothetical: Nike Air Max 90, men's US 10, black/white, new, complete box, $59.99. No real offer.",
            scenario_kind="HYPOTHETICAL", human_actor="fixture author"), operation_key="intake")
        self.eid = self.accepted.evaluation.evaluation_id
        self.producer = Producer(kind="HUMAN", name="Trusted synthetic fixture")
        self.raw = self.repo.record_artifact(self.eid, RawResearchReport(report_text="Synthetic report: all products, prices, URLs, and dates are fictional."),
            producer=self.producer, operation_key="raw")
        self.drafts = synthetic_comparables(self.raw.reference)
        self.service = HumanReviewService(self.repo)

    def record(self, index=0, key="comp", **options):
        return self.repo.record_comparable(self.eid, self.drafts[index], producer=self.producer, operation_key=key, **options)

    def assessment_draft(self, category=ComparabilityCategory.CLOSE_WITH_KNOWN_DIFFERENCES):
        return AssessmentDraft(candidate_context=self.accepted.evaluation.current_review_subject,
            candidate_identity=ComparableProduct(description="Hypothetical acquisition Nike Air Max 90 US men's 10, black/white", condition="NEW"),
            category=category, explanation="Attributed synthetic judgment; no economic eligibility decision.",
            material_known_differences=("Color description differs.",), unresolved_questions=("Price effect unknown.",),
            evidence_references=(self.raw.reference,), provenance=Provenance(
                information_kind=InformationKind.REPORTED_INFORMATION, sources=(KnowledgeSource.HUMAN_PROVIDED,),
                rationale="Synthetic test author, not actual research."), policy_version="synthetic-policy-v1")

    def assess(self, comparable, key="assessment", **options):
        return self.repo.record_assessment(self.eid, comparable.reference, self.assessment_draft(),
            producer=self.producer, operation_key=key, **options)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as c:
            return tuple((t, tuple(c.execute(f"SELECT * FROM {t} ORDER BY rowid"))) for t in (
                "v2_opportunities","v2_evaluations","v2_artifacts","v2_reviews","v2_operations"))

    def test_three_synthetic_comparables_and_distinct_judgments_remain_visible(self):
        candidates = [self.record(n, f"comp-{n}") for n in range(3)]
        for comp, category in zip(candidates, (ComparabilityCategory.CLOSE_WITH_KNOWN_DIFFERENCES,
                ComparabilityCategory.WEAK, ComparabilityCategory.NOT_MEANINGFULLY_COMPARABLE)):
            self.repo.record_assessment(self.eid, comp.reference, self.assessment_draft(category), producer=self.producer,
                                        operation_key=f"judgment-{category.value}")
        self.assertEqual(len(self.repo.list_comparable_history(self.eid)), 3)
        evidence = self.service.get_evidence(self.eid)
        self.assertEqual(len([a for a in evidence.artifacts if a.artifact_type == ArtifactType.COMPARABLE_ASSESSMENT]), 3)
        self.assertFalse(any(a.artifact_type == ArtifactType.CALCULATION for a in evidence.artifacts))
        self.assertEqual(self.repo.review_status(self.eid).review_state, "PENDING")
        self.assertIsNone(self.repo.get_current_summary(self.eid))
        self.assertEqual(candidates[0].payload.product_comparisons[0].relation, "KNOWN_DIFFERENCE")
        self.assertNotEqual(candidates[0].payload.source.url, candidates[1].payload.source.url)

    def test_comparable_without_price_or_url_preserves_unknowns(self):
        artifact = self.record(2)
        self.assertIsNone(artifact.payload.price_evidence.amount)
        self.assertIsNone(artifact.payload.source.url)
        self.assertIsNone(artifact.payload.product.packaging)
        self.assertTrue(artifact.payload.source.other_reference)

    def test_completed_sale_and_active_ask_do_not_change_types(self):
        self.assertEqual(self.record().payload.price_evidence.evidence_type, "COMPLETED_SALE")
        asking = self.record(1,"ask")
        self.assertEqual(asking.payload.price_evidence.evidence_type, "ACTIVE_ASK")
        self.assertIsNone(asking.payload.price_evidence.realized_price_basis)
        with self.assertRaises(ValidationError):
            PriceEvidence(evidence_type="ACTIVE_ASK", amount="12", currency="USD", realized_price_basis="not a sale")

    def test_completed_sale_without_price_does_not_invent_realized_amount(self):
        price = PriceEvidence(evidence_type="COMPLETED_SALE", completion_basis="Synthetic sold status; accepted price unavailable.")
        draft = self.drafts[0].model_copy(update={"price_evidence":price})
        artifact = self.repo.record_comparable(self.eid,draft,producer=self.producer,operation_key="unknown-sale-price")
        self.assertIsNone(artifact.payload.price_evidence.amount)

    def test_realized_amount_requires_explicit_support(self):
        for values in ({}, {"completion_basis":"Completed"}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                PriceEvidence(evidence_type="COMPLETED_SALE", amount="12",currency="USD",**values)

    def test_reference_and_human_reported_prices_are_preserved(self):
        for kind in ("PUBLISHED_REFERENCE","HUMAN_REPORTED","OTHER_QUALIFIED"):
            price = PriceEvidence(evidence_type=kind,amount="25",currency="EUR", context=("Fictional reported amount.",))
            comp = self.repo.record_comparable(self.eid,self.drafts[0].model_copy(update={"price_evidence":price}),
                producer=self.producer,operation_key=kind)
            self.assertEqual(comp.payload.price_evidence.currency,"EUR")
            self.assertEqual(comp.payload.price_evidence.amount,25)

    def test_currency_and_url_shape_are_validated_without_verification(self):
        for currency in (None,"usd","US","US$"):
            with self.subTest(currency=currency), self.assertRaises(ValidationError):
                PriceEvidence(evidence_type="ACTIVE_ASK",amount="1",currency=currency)
        for url in ("not a url","file:///tmp/a","https://user:password@example.invalid/a","https://example.invalid:bad/a"):
            with self.subTest(url=url),self.assertRaises(ValidationError):
                ComparableSource(source_category="OTHER",url=url,capture_method="SYNTHETIC_FIXTURE")
        self.assertFalse(self.record().payload.source.application_verified)
        with self.assertRaises(ValidationError):
            ComparableSource(source_category="MARKETPLACE",capture_method="SYNTHETIC_FIXTURE",application_verified=True)

    def test_category_neutral_attributes_and_missing_values(self):
        product = ComparableProduct(description="Synthetic cordless drill bundle",condition=None, package_quantity=2,
            model_identifiers=(ProductAttribute(name="MPN",value=None),),
            other_attributes=(ProductAttribute(name="voltage",value="18V"),ProductAttribute(name="battery included",value=None)))
        comp = self.repo.record_comparable(self.eid,self.drafts[2].model_copy(update={"product":product}),
            producer=self.producer,operation_key="drill")
        self.assertEqual(comp.payload.product,product)
        self.assertIsNone(self.record(1).payload.product.variant_attributes[1].value)

    def test_capture_time_is_application_owned_observation_time_is_distinct(self):
        comp = self.record()
        self.assertEqual(comp.payload.captured_at,comp.created_at)
        self.assertNotEqual(comp.payload.source.observed_at,comp.payload.captured_at)
        self.assertEqual(comp.payload.source.capture_method,"SYNTHETIC_FIXTURE")

    def test_all_categories_are_supplied_not_derived(self):
        comp = self.record()
        for category in ComparabilityCategory:
            result = self.repo.record_assessment(self.eid,comp.reference,self.assessment_draft(category),
                producer=self.producer,operation_key=category.value)
            self.assertEqual(result.payload.category,category)
        self.assertEqual(len(self.repo.get_current_assessments(self.eid,comp.payload.comparable_id)),5)

    def test_assessment_explanation_is_required_and_score_not_supported(self):
        draft = self.assessment_draft().model_dump()
        for changes in ({"explanation":" "},{"similarity_score":0.9}):
            with self.subTest(changes=changes),self.assertRaises(ValidationError):
                AssessmentDraft(**(draft|changes))

    def test_multiple_assessment_identities_for_one_comparable(self):
        comp = self.record()
        a,b = self.assess(comp),self.assess(comp,"second")
        self.assertNotEqual(a.payload.assessment_id,b.payload.assessment_id)
        self.assertEqual(a.payload.revision,b.payload.revision)

    def test_comparable_revision_preserves_identity_and_original_payload(self):
        old = self.record()
        new = self.repo.record_comparable_revision(old.reference,self.drafts[1],producer=self.producer,operation_key="rev")
        self.assertEqual(new.payload.comparable_id,old.payload.comparable_id)
        self.assertEqual(new.payload.revision,2)
        self.assertEqual(new.payload.supersedes,old.reference)
        self.assertEqual(self.repo.get_comparable(old.reference),old)
        self.assertEqual(self.repo.get_current_comparable(self.eid,old.payload.comparable_id),new)

    def test_old_assessment_binding_is_not_rewritten_by_new_comparable(self):
        comp = self.record()
        old = self.assess(comp)
        self.repo.record_comparable_revision(comp.reference,self.drafts[1],producer=self.producer,operation_key="rev")
        history = self.repo.list_assessment_history(self.eid,comp.payload.comparable_id)
        self.assertEqual(history[0].artifact,old)
        self.assertTrue(history[0].latest_assessment_revision)
        self.assertFalse(history[0].current_comparable_revision)
        self.assertEqual(self.repo.get_current_assessments(self.eid,comp.payload.comparable_id),())

    def test_assessment_revision_can_explicitly_target_updated_comparable(self):
        comp = self.record()
        old = self.assess(comp)
        revised = self.repo.record_comparable_revision(comp.reference,self.drafts[1],producer=self.producer,operation_key="comp-rev")
        new = self.repo.record_assessment_revision(old.reference,revised.reference,self.assessment_draft(),
            producer=self.producer,operation_key="assessment-rev")
        self.assertEqual(new.payload.assessment_id,old.payload.assessment_id)
        self.assertEqual(new.payload.supersedes,old.reference)
        statuses = self.repo.list_assessment_history(self.eid,comp.payload.comparable_id)
        self.assertFalse(statuses[0].is_current)
        self.assertTrue(statuses[1].is_current)
        self.assertEqual(self.repo.get_artifact(old.reference),old)

    def test_stale_predecessors_and_changed_lineage_are_rejected(self):
        comp = self.record()
        old = self.assess(comp)
        self.repo.record_comparable_revision(comp.reference,self.drafts[1],producer=self.producer,operation_key="rev")
        with self.assertRaises(V2PersistenceError):
            self.repo.record_comparable_revision(comp.reference,self.drafts[2],producer=self.producer,operation_key="fork")
        other = self.record(2,"other")
        with self.assertRaises(V2PersistenceError):
            self.repo.record_assessment_revision(old.reference,other.reference,self.assessment_draft(),producer=self.producer,operation_key="drift")

    def test_cross_evaluation_and_wrong_type_references_are_rejected(self):
        comp = self.record()
        other = self.repo.create_evaluation(self.accepted.opportunity.opportunity_id,operation_key="other-eval")
        with self.assertRaises(V2PersistenceError):
            self.repo.record_assessment(other.evaluation_id,comp.reference,self.assessment_draft(),producer=self.producer,operation_key="cross")
        for reference in (self.raw.reference,self.accepted.evaluation.current_review_subject):
            with self.subTest(reference=reference),self.assertRaises(V2PersistenceError):
                self.repo.record_assessment(self.eid,reference,self.assessment_draft(),producer=self.producer,operation_key="wrong")

    def test_invalid_raw_source_references_fail_without_partial_writes(self):
        for reference in (self.accepted.evaluation.current_review_subject,
                self.raw.reference.model_copy(update={"artifact_id":"missing"}),self.raw.reference.model_copy(update={"sequence":999})):
            draft = self.drafts[0].model_copy(update={"source":self.drafts[0].source.model_copy(update={"raw_artifact_references":(reference,)})})
            before = self.snapshot()
            with self.subTest(reference=reference),self.assertRaises(V2PersistenceError):
                self.repo.record_comparable(self.eid,draft,producer=self.producer,operation_key="bad")
            self.assertEqual(self.snapshot(),before)

    def test_candidate_context_and_assessment_evidence_type_are_checked(self):
        comp = self.record()
        for draft in (self.assessment_draft().model_copy(update={"candidate_context":self.raw.reference}),
                self.assessment_draft().model_copy(update={"evidence_references":(self.accepted.evaluation.current_review_subject,)})):
            with self.assertRaises(V2PersistenceError):
                self.repo.record_assessment(self.eid,comp.reference,draft,producer=self.producer,operation_key="bad-assessment")

    def test_assessment_provenance_cannot_smuggle_cross_evaluation_evidence(self):
        comp=self.record()
        other=self.repo.create_evaluation(self.accepted.opportunity.opportunity_id,operation_key="other")
        provenance=Provenance(information_kind=InformationKind.REPORTED_INFORMATION,sources=(KnowledgeSource.HISTORICAL_EVALUATIONS,),
            rationale="Cross-Evaluation reference not permitted in comparable assessment.", supporting_records=(SupportingRecord(
                source=KnowledgeSource.HISTORICAL_EVALUATIONS,artifact=other.current_review_subject),))
        with self.assertRaises(V2PersistenceError):
            self.repo.record_assessment(self.eid,comp.reference,self.assessment_draft().model_copy(update={"provenance":provenance}),
                producer=self.producer,operation_key="cross-source")

    def test_history_order_and_restart_reconstruction(self):
        comp=self.record()
        self.assess(comp)
        self.record(1,"second")
        self.assertEqual([a.reference.sequence for a in self.repo.list_comparable_history(self.eid)],sorted(a.reference.sequence for a in self.repo.list_comparable_history(self.eid)))
        reopened=V2Repository(self.path,session_database_path=self.session_path)
        self.assertEqual(reopened.get_review_evidence(self.eid),self.repo.get_review_evidence(self.eid))
        self.assertEqual(reopened.list_assessment_history(self.eid,comp.payload.comparable_id),self.repo.list_assessment_history(self.eid,comp.payload.comparable_id))

    def test_review_visibility_without_or_with_weak_comparables(self):
        self.assertEqual(self.repo.list_review_queue().total,1)
        comp=self.record()
        self.repo.record_assessment(self.eid,comp.reference,self.assessment_draft(ComparabilityCategory.WEAK),producer=self.producer,operation_key="weak")
        self.assertEqual(self.repo.list_review_queue().total,1)
        self.assertIsNone(self.repo.review_status(self.eid).assessment_state)

    def test_each_new_evidence_record_invalidates_prior_human_review(self):
        comp=None
        for operation in (lambda:self.record(),lambda:self.assess(comp)):
            subject=self.repo.get_evaluation(self.eid).current_review_subject
            review=self.service.record_decision(self.eid,subject,ReviewDecision.WORTH_INVESTIGATING,"Review this exact evidence set.",
                human=HumanActor(actor="synthetic reviewer"),operation_key=f"review-{subject.sequence}")
            result=operation()
            if comp is None:comp=result
            self.assertFalse(self.repo.review_subject_is_current(review.review_id))
            self.assertEqual(self.repo.review_status(self.eid).review_state,"PENDING")

    def test_published_economic_summary_is_not_modified_by_comparable(self):
        draft=foundation.V2FoundationTests().draft()
        summary=self.repo.record_summary(self.eid,draft,producer=self.producer,operation_key="economic-summary")
        self.record()
        self.assertEqual(self.repo.get_current_summary(self.eid),summary)
        self.assertFalse(self.repo.review_status(self.eid).summary_matches_current_subject)

    def test_idempotent_calls_do_not_advance_review_subject_twice(self):
        comp=self.record()
        before=self.snapshot()
        self.assertEqual(self.record(),comp)
        self.assertEqual(self.snapshot(),before)
        assessed=self.assess(comp)
        before=self.snapshot()
        self.assertEqual(self.assess(comp),assessed)
        self.assertEqual(self.snapshot(),before)

    def test_conflicting_key_and_bad_parents_are_rejected(self):
        self.record()
        before=self.snapshot()
        with self.assertRaises(IdempotencyConflict):self.record(1)
        other=self.repo.create_evaluation(self.accepted.opportunity.opportunity_id,operation_key="other")
        for parents in ((other.current_review_subject,),(self.raw.reference,self.raw.reference)):
            with self.assertRaises(V2PersistenceError):self.record(1,"bad-parent",parents=parents)
        self.assertEqual(len(self.repo.list_comparable_history(self.eid)),1)

    def test_comparable_and_assessment_rollback_if_review_subject_fails(self):
        comp=self.record()
        for operation in (lambda:self.record(1,"failed-comp"),lambda:self.assess(comp)):
            before=self.snapshot()
            with patch.object(self.repo,"_partial_subject",side_effect=RuntimeError("Synthetic failure")),self.assertRaises(RuntimeError):operation()
            self.assertEqual(self.snapshot(),before)

    def test_concurrent_duplicate_recording_has_one_receipt(self):
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.record(),range(2)))
        self.assertEqual(results[0],results[1])
        self.assertEqual(len(self.repo.list_comparable_history(self.eid)),1)

    def test_artifact_round_trip_and_database_immutability(self):
        comp=self.record(); assessment=self.assess(comp)
        for artifact in (comp,assessment):
            self.assertEqual(Artifact.model_validate_json(artifact.model_dump_json()),artifact)
            with closing(sqlite3.connect(self.path)) as c,self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE v2_artifacts SET payload_json='{}' WHERE artifact_id=?",(artifact.reference.artifact_id,))

    def test_no_model_network_or_profitability_calls(self):
        with patch("agents.Runner.run",side_effect=AssertionError("Agent")),patch("agents.Runner.run_sync",side_effect=AssertionError("Agent")), \
             patch("socket.socket.connect",side_effect=AssertionError("Network")), \
             patch("arbitrage.tools.profitability._calculate_profitability",side_effect=AssertionError("Calculation")):
            comp=self.record();self.assess(comp);self.service.get_evidence(self.eid)
        self.assertFalse(self.session_path.exists())
        self.assertFalse(hasattr(self.service,"record_comparable"))

    def test_revision_idempotency_does_not_duplicate_or_advance_subject(self):
        comp=self.record()
        assessed=self.assess(comp)
        def revise_comp():
            return self.repo.record_comparable_revision(comp.reference,self.drafts[1],producer=self.producer,operation_key="comp-rev")
        new=revise_comp(); before=self.snapshot()
        self.assertEqual(revise_comp(),new)
        self.assertEqual(self.snapshot(),before)
        def revise_assessment():
            return self.repo.record_assessment_revision(assessed.reference,new.reference,self.assessment_draft(),producer=self.producer,operation_key="assessment-rev")
        result=revise_assessment(); before=self.snapshot()
        self.assertEqual(revise_assessment(),result)
        self.assertEqual(self.snapshot(),before)

    def test_assessment_fork_is_refused_without_writes(self):
        comp=self.record(); old=self.assess(comp)
        self.repo.record_assessment_revision(old.reference,comp.reference,self.assessment_draft(),producer=self.producer,operation_key="rev")
        before=self.snapshot()
        with self.assertRaises(V2PersistenceError):
            self.repo.record_assessment_revision(old.reference,comp.reference,self.assessment_draft(),producer=self.producer,operation_key="fork")
        self.assertEqual(self.snapshot(),before)

    def test_cross_evaluation_raw_source_is_refused(self):
        other=self.repo.create_evaluation(self.accepted.opportunity.opportunity_id,operation_key="other")
        raw=self.repo.record_artifact(other.evaluation_id,RawResearchReport(report_text="Other synthetic report."),producer=self.producer,operation_key="other-report")
        draft=self.drafts[0].model_copy(update={"source":self.drafts[0].source.model_copy(update={"raw_artifact_references":(raw.reference,)})})
        before=self.snapshot()
        with self.assertRaises(V2PersistenceError):
            self.repo.record_comparable(self.eid,draft,producer=self.producer,operation_key="cross-raw")
        self.assertEqual(self.snapshot(),before)

    def test_conflicting_assessment_key_rejected_and_inputs_not_mutated(self):
        comp=self.record(); original=self.drafts[0].model_dump_json()
        self.assess(comp)
        with self.assertRaises(IdempotencyConflict):
            self.repo.record_assessment(self.eid,comp.reference,self.assessment_draft(ComparabilityCategory.WEAK),
                producer=self.producer,operation_key="assessment")
        self.assertEqual(self.drafts[0].model_dump_json(),original)

    def test_observation_and_transaction_dates_require_timezone(self):
        from datetime import datetime
        with self.assertRaises(ValidationError):
            ComparableSource(source_category="HUMAN",capture_method="HUMAN_REPORT",observed_at=datetime(2026,1,1))
        with self.assertRaises(ValidationError):
            PriceEvidence(evidence_type="COMPLETED_SALE",completion_basis="Synthetic",transaction_at=datetime(2026,1,1))

    def test_human_reported_transaction_date_remains_reported_not_verified(self):
        from datetime import datetime, timezone
        date=datetime(2026,1,1,tzinfo=timezone.utc)
        price=PriceEvidence(evidence_type="HUMAN_REPORTED",amount="10",currency="USD",transaction_at=date)
        self.assertEqual(price.evidence_type,"HUMAN_REPORTED")
        self.assertEqual(price.transaction_at,date)
        with self.assertRaises(ValidationError):
            PriceEvidence(evidence_type="ACTIVE_ASK",transaction_at=date)

    def test_raw_report_storage_preserves_exact_supplied_text(self):
        report=RawResearchReport(report_text="  Synthetic report with original whitespace.\n")
        artifact=self.repo.record_artifact(self.eid,report,producer=self.producer,operation_key="raw-whitespace")
        self.assertEqual(artifact.payload.report_text,report.report_text)
        with self.assertRaises(ValidationError):RawResearchReport(report_text="  ")


if __name__=="__main__":unittest.main()
