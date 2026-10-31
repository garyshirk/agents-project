import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from arbitrage.v2.contracts import (
    Artifact, ArtifactRef, ArtifactType, AssessmentState, AssumptionRecord, EconomicAssumption,
    EconomicFact, ExecutionState, FailureRecord, HumanActor, InformationKind, KnowledgeSource,
    OpportunitySubmission, Producer, Provenance, ReviewDecision, SubmissionKind,
    SummaryDraft, SupportingRecord, UnknownEconomicInput,
)
from arbitrage.v2.persistence import IdempotencyConflict, V2PersistenceError, V2Repository
from arbitrage.v2.review import HumanReviewService


class V2FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / "business.db"
        self.sessions = self.root / "sessions.db"
        self.repo = V2Repository(self.database, session_database_path=self.sessions)
        self.repo.initialize_schema()
        self.service = HumanReviewService(self.repo)
        self.producer = Producer(kind="AGENT", name="Fixture Manager")
        self.human = HumanActor(actor="local reviewer", detail="Explicit test input")
        self.accepted = self.accept("first")
        self.eid = self.accepted.evaluation.evaluation_id

    def accept(self, key, **changes):
        submission = OpportunitySubmission(original_text="  Suppose a store offered a product for $60.\n",
            scenario_kind=SubmissionKind.HYPOTHETICAL, human_actor="test human", qualifications=("No verified inventory.",))
        return self.repo.accept_opportunity(submission.model_copy(update=changes), operation_key=key)

    def draft(self, state=AssessmentState.INSUFFICIENT_INFORMATION, **changes):
        values = dict(assessment_state=state, state_decision_rationale="The available evidence is limited.",
            consequential_uncertainties=("Resale value unknown.",), known_economic_facts=(),
            unknown_economic_inputs=(UnknownEconomicInput(name="shipping", reason="No amount available."),),
            assumptions=(), limitations=("No live research performed.",), policy_version="fixture-policy-v1")
        return SummaryDraft(**(values | changes))

    def summary(self, key="summary", **changes):
        return self.repo.record_summary(self.eid, self.draft(**changes), producer=self.producer, operation_key=key)

    def review(self, subject=None, key="review", decision=ReviewDecision.WORTH_INVESTIGATING, **changes):
        subject = subject or self.repo.get_evaluation(self.eid).current_review_subject
        return self.service.record_decision(self.eid, subject, decision, "Investigate the remaining questions.",
            human=self.human, operation_key=key, **changes)

    def failure(self, key="failure", parents=()):
        return self.repo.record_artifact(self.eid, FailureRecord(stage="research", category="NO_RESPONSE", message="Fixture failure."),
            producer=self.producer, parents=parents, operation_key=key)

    def counts(self):
        with closing(sqlite3.connect(self.database)) as c:
            return {name:c.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in
                    ("v2_opportunities", "v2_evaluations", "v2_artifacts", "v2_reviews", "v2_operations")}

    def provenance(self, **changes):
        return Provenance(**(dict(information_kind=InformationKind.AGENT_ASSUMPTION,
            sources=(KnowledgeSource.MODEL_GENERAL_KNOWLEDGE,), rationale="Explicit illustrative assumption.") | changes))

    def test_original_submission_and_qualification_round_trip(self):
        opportunity = self.repo.get_opportunity(self.accepted.opportunity.opportunity_id)
        self.assertEqual(opportunity, self.accepted.opportunity)
        self.assertTrue(opportunity.submission.original_text.startswith("  "))
        self.assertTrue(opportunity.submission.original_text.endswith("\n"))
        self.assertEqual(opportunity.submission.scenario_kind, SubmissionKind.HYPOTHETICAL)
        self.assertEqual(opportunity.submission.qualifications, ("No verified inventory.",))

    def test_queue_visible_before_research_and_without_summary(self):
        page = self.service.list_queue()
        self.assertEqual(page.total, 1)
        self.assertEqual(page.entries[0].review_state, "PENDING")
        self.assertIsNone(page.entries[0].assessment_state)
        self.assertIsNone(self.service.get_current_summary(self.eid))
        subject = self.repo.get_artifact(page.entries[0].evaluation.current_review_subject)
        self.assertEqual(subject.artifact_type, ArtifactType.REVIEW_SUBJECT)
        self.assertEqual(subject.payload.label, "PARTIAL_OR_UNFINISHED")

    def test_failure_and_execution_state_remain_reviewable(self):
        self.failure()
        self.repo.set_execution_state(self.eid, ExecutionState.FAILED, operation_key="failed")
        entry = self.service.list_queue().entries[0]
        self.assertEqual(entry.evaluation.execution_state, ExecutionState.FAILED)
        self.assertEqual(entry.review_state, "PENDING")
        self.assertIsNone(entry.assessment_state)
        self.assertTrue(any(a.artifact_type == ArtifactType.FAILURE for a in self.service.get_artifact_history(self.eid)))

    def test_all_assessment_states_allow_no_numerical_calculation(self):
        for state in AssessmentState:
            summary = self.summary(key=state.value, state=state)
            self.assertEqual(summary.payload.assessment_state, state)
            self.assertEqual(summary.payload.calculation_references, ())
            self.assertEqual(self.repo.get_evaluation(self.eid).execution_state, ExecutionState.RUNNING)

    def test_nonblank_rationale_required_for_every_assessment_state(self):
        for state in AssessmentState:
            with self.subTest(state=state), self.assertRaises(ValidationError):
                self.draft(state=state, state_decision_rationale="  ")

    def test_all_source_categories_and_multiple_sources_are_preserved(self):
        for source in KnowledgeSource:
            assumption = EconomicAssumption(name="shipping", explanation="An explicit assumption, not observation.",
                amount="12.00", unit="USD/order", provenance=self.provenance(sources=(source,)))
            summary = self.summary(key=source.value, assumptions=(assumption,))
            self.assertEqual(summary.payload.assumptions[0].provenance.sources, (source,))
        assumption = EconomicAssumption(name="fee", explanation="Combined sources.", provenance=self.provenance(
            sources=(KnowledgeSource.MODEL_GENERAL_KNOWLEDGE, KnowledgeSource.HUMAN_PROVIDED)))
        self.assertEqual(len(self.summary("multiple", assumptions=(assumption,)).payload.assumptions[0].provenance.sources), 2)

    def test_historical_reference_is_exact_existing_record_not_retrieval(self):
        historical = self.accept("historical-fixture").evaluation.current_review_subject
        provenance = self.provenance(sources=(KnowledgeSource.HISTORICAL_EVALUATIONS,), supporting_records=(
            SupportingRecord(source=KnowledgeSource.HISTORICAL_EVALUATIONS, artifact=historical),))
        assumption = EconomicAssumption(name="shipping", amount="12", unit="USD/order",
            explanation="Assumption informed by a prior temporary fixture, not an observed cost.", provenance=provenance)
        artifact = self.summary(assumptions=(assumption,))
        self.assertEqual(artifact.payload.assumptions[0].provenance.supporting_records[0].artifact, historical)

    def test_information_kinds_are_not_silently_promoted(self):
        for kind in (InformationKind.AGENT_ASSUMPTION, InformationKind.HUMAN_ASSUMPTION, InformationKind.HUMAN_CORRECTION):
            source = KnowledgeSource.MODEL_GENERAL_KNOWLEDGE if kind == InformationKind.AGENT_ASSUMPTION else KnowledgeSource.HUMAN_PROVIDED
            assumption = EconomicAssumption(name="cost", explanation="Explicit basis.", provenance=self.provenance(information_kind=kind, sources=(source,)))
            self.assertEqual(assumption.provenance.information_kind, kind)
        for kind in (InformationKind.OBSERVED_COST, InformationKind.ACTUAL_TRANSACTION_OUTCOME):
            provenance = self.provenance(information_kind=kind, sources=(KnowledgeSource.HUMAN_PROVIDED,))
            with self.assertRaises(ValidationError):
                EconomicAssumption(name="cost", explanation="Not an assumption.", provenance=provenance)
            self.assertEqual(EconomicFact(name="reported amount", value="$12", provenance=provenance).provenance.information_kind, kind)

    def test_assumption_cannot_hide_in_known_facts(self):
        with self.assertRaises(ValidationError):
            EconomicFact(name="shipping", value="$12", provenance=self.provenance())

    def test_numerical_assumption_requires_units(self):
        with self.assertRaises(ValidationError):
            EconomicAssumption(name="shipping", explanation="Assumed amount.", amount="12", provenance=self.provenance())

    def test_invalid_or_undeclared_provenance_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.provenance(sources=())
        with self.assertRaises(ValidationError):
            self.provenance(information_kind=InformationKind.HUMAN_ASSUMPTION)
        with self.assertRaises(ValidationError):
            self.provenance(sources=(KnowledgeSource.HUMAN_PROVIDED,), supporting_records=(SupportingRecord(
                source=KnowledgeSource.CURRENT_EVALUATION_EVIDENCE, artifact=self.accepted.evaluation.current_review_subject),))

    def test_current_evidence_cannot_reference_another_evaluation(self):
        other = self.accept("other").evaluation.current_review_subject
        assumption = EconomicAssumption(name="cost", explanation="Bad current reference.", provenance=self.provenance(
            sources=(KnowledgeSource.CURRENT_EVALUATION_EVIDENCE,), supporting_records=(SupportingRecord(
                source=KnowledgeSource.CURRENT_EVALUATION_EVIDENCE, artifact=other),)))
        before = self.counts()
        with self.assertRaises(V2PersistenceError):
            self.summary(assumptions=(assumption,))
        self.assertEqual(self.counts(), before)

    def test_missing_historical_reference_is_rejected(self):
        missing = self.accepted.evaluation.current_review_subject.model_copy(update={"artifact_id":"missing"})
        assumption = EconomicAssumption(name="cost", explanation="Missing reference.", provenance=self.provenance(
            sources=(KnowledgeSource.HISTORICAL_EVALUATIONS,), supporting_records=(SupportingRecord(
                source=KnowledgeSource.HISTORICAL_EVALUATIONS, artifact=missing),)))
        with self.assertRaises(V2PersistenceError):
            self.summary(assumptions=(assumption,))

    def test_historical_assumption_cannot_be_reclassified_as_observed(self):
        old = self.repo.record_artifact(self.eid, AssumptionRecord(assumption=EconomicAssumption(
            name="shipping", explanation="Assumed, not observed.", provenance=self.provenance(), amount="12", unit="USD/order")),
            producer=self.producer, operation_key="old-assumption")
        new_evaluation = self.repo.create_evaluation(self.accepted.opportunity.opportunity_id,
            predecessor_evaluation_id=self.eid, operation_key="new-evaluation")
        provenance = self.provenance(information_kind=InformationKind.OBSERVED_COST,
            sources=(KnowledgeSource.HISTORICAL_EVALUATIONS,), supporting_records=(SupportingRecord(
                source=KnowledgeSource.HISTORICAL_EVALUATIONS, artifact=old.reference),))
        draft = self.draft(known_economic_facts=(EconomicFact(name="shipping", value="$12", provenance=provenance),))
        before = self.counts()
        with self.assertRaises(V2PersistenceError):
            self.repo.record_summary(new_evaluation.evaluation_id, draft, producer=self.producer, operation_key="bad-fact")
        self.assertEqual(self.counts(), before)

    def test_exact_artifact_ownership_and_sequence_are_validated(self):
        reference = self.accepted.evaluation.current_review_subject
        for update in ({"evaluation_id":"wrong"}, {"opportunity_id":"wrong"}, {"sequence":99}):
            with self.subTest(update=update), self.assertRaises(V2PersistenceError):
                self.repo.get_artifact(reference.model_copy(update=update))

    def test_parent_artifacts_require_same_evaluation(self):
        other = self.accept("other").evaluation.current_review_subject
        before = self.counts()
        with self.assertRaises(V2PersistenceError):
            self.failure(parents=(other,))
        self.assertEqual(self.counts(), before)

    def test_exact_parent_reference_is_preserved(self):
        parent = self.accepted.evaluation.current_review_subject
        self.assertEqual(self.failure(parents=(parent,)).parent_references, (parent,))

    def test_duplicate_parent_reference_is_rejected(self):
        parent = self.accepted.evaluation.current_review_subject
        with self.assertRaises(V2PersistenceError):
            self.failure(parents=(parent,parent))

    def test_summary_revision_and_supersession_are_application_owned(self):
        first = self.summary()
        second = self.summary("revision")
        self.assertEqual(first.payload.revision, 1)
        self.assertEqual(second.payload.revision, 2)
        self.assertEqual(second.payload.supersedes, first.reference)
        self.assertIn(first.reference, second.parent_references)
        self.assertEqual(self.repo.get_artifact(first.reference), first)
        self.assertEqual(self.service.get_current_summary(self.eid), second)

    def test_human_review_exact_subject_and_assessment_only(self):
        summary = self.summary()
        decision = self.review(summary.reference, requested_follow_up=("Check packaging later.",))
        self.assertEqual(decision.reviewed_subject, summary.reference)
        self.assertEqual(decision.authority, "ASSESSMENT_ONLY")
        self.assertTrue(self.repo.review_is_current(decision.review_id))
        self.assertEqual(self.service.review_status(self.eid).review_state, ReviewDecision.WORTH_INVESTIGATING)
        self.assertEqual(decision.requested_follow_up, ("Check packaging later.",))

    def test_review_without_summary_uses_stable_partial_subject(self):
        review = self.review()
        self.assertEqual(review.reviewed_subject, self.accepted.evaluation.current_review_subject)
        self.assertIsNone(self.repo.get_current_summary(self.eid))
        self.assertTrue(self.repo.review_subject_is_current(review.review_id))

    def test_old_review_not_current_after_summary_revision(self):
        self.summary()
        review = self.review()
        self.summary("new-summary")
        self.assertFalse(self.repo.review_subject_is_current(review.review_id))
        self.assertFalse(self.repo.review_is_current(review.review_id))
        self.assertEqual(self.service.review_status(self.eid).review_state, "PENDING")
        self.assertEqual(len(self.service.get_review_history(self.eid)), 1)

    def test_new_information_invalidates_old_review_without_rewriting_summary(self):
        summary = self.summary()
        review = self.review()
        self.failure()
        status = self.service.review_status(self.eid)
        self.assertEqual(status.review_state, "PENDING")
        self.assertFalse(status.summary_matches_current_subject)
        self.assertIsNone(status.assessment_state)
        self.assertFalse(self.repo.review_subject_is_current(review.review_id))
        self.assertEqual(self.repo.get_current_summary(self.eid), summary)

    def test_reviewing_historical_subject_does_not_change_current_state(self):
        first = self.summary()
        self.summary("new-summary")
        review = self.review(first.reference)
        self.assertFalse(self.repo.review_subject_is_current(review.review_id))
        self.assertEqual(self.repo.review_status(self.eid).review_state, "PENDING")

    def test_superseding_review_preserves_both_decisions(self):
        first = self.review()
        second = self.review(key="correction", decision=ReviewDecision.REJECTED, supersedes_review_id=first.review_id)
        self.assertEqual(second.supersedes_review_id, first.review_id)
        self.assertEqual(len(self.repo.list_reviews(self.eid)), 2)
        self.assertTrue(self.repo.review_subject_is_current(first.review_id))
        self.assertFalse(self.repo.review_is_current(first.review_id))
        self.assertTrue(self.repo.review_is_current(second.review_id))

    def test_review_supersession_requires_same_subject_and_latest_decision(self):
        first = self.review()
        self.review(key="second")
        with self.assertRaises(V2PersistenceError):
            self.review(key="bad", supersedes_review_id=first.review_id)
        self.summary()
        with self.assertRaises(V2PersistenceError):
            self.review(key="bad-new-subject", supersedes_review_id=first.review_id)

    def test_foreign_or_forged_review_subject_is_rejected(self):
        other = self.accept("other").evaluation.current_review_subject
        for subject in (other, self.accepted.evaluation.current_review_subject.model_copy(update={"sequence":99})):
            with self.subTest(subject=subject), self.assertRaises(V2PersistenceError):
                self.review(subject)

    def test_failure_artifact_is_not_arbitrary_review_subject(self):
        failure = self.failure()
        with self.assertRaises(V2PersistenceError):
            self.review(failure.reference)
        self.assertEqual(self.repo.review_status(self.eid).review_state, "PENDING")

    def test_pending_is_not_a_submitted_decision_and_rationale_required(self):
        with self.assertRaises(ValueError):
            self.review(decision="PENDING")
        with self.assertRaises(ValidationError):
            self.repo.record_review(self.eid, self.accepted.evaluation.current_review_subject,
                ReviewDecision.REJECTED, "  ", human=self.human, operation_key="blank")

    def test_duplicate_recording_returns_same_receipts_without_new_rows(self):
        before = self.counts()
        self.assertEqual(self.accept("first"), self.accepted)
        self.assertEqual(before, self.counts())
        for action in (self.failure, self.summary, self.review):
            first = action()
            before = self.counts()
            self.assertEqual(action(), first)
            self.assertEqual(self.counts(), before)

    def test_conflicting_idempotency_key_does_not_write(self):
        before = self.counts()
        with self.assertRaises(IdempotencyConflict):
            self.accept("first", original_text="Different hypothetical opportunity.")
        with self.assertRaises(IdempotencyConflict):
            self.failure(key="first")
        self.assertEqual(self.counts(), before)

    def test_concurrent_same_key_has_one_atomic_receipt(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.failure(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len([a for a in self.repo.list_artifacts(self.eid) if a.artifact_type == ArtifactType.FAILURE]), 1)

    def test_accept_rolls_back_if_subject_creation_fails(self):
        before = self.counts()
        with patch.object(self.repo, "_partial_subject", side_effect=RuntimeError("fixture failure")), self.assertRaises(RuntimeError):
            self.accept("interrupted")
        self.assertEqual(self.counts(), before)

    def test_artifact_and_operation_roll_back_if_subject_update_fails(self):
        before = self.counts()
        with patch.object(self.repo, "_partial_subject", side_effect=RuntimeError("fixture failure")), self.assertRaises(RuntimeError):
            self.failure()
        self.assertEqual(self.counts(), before)

    def test_summary_pointer_failure_rolls_back_artifact(self):
        before = self.counts()
        with closing(sqlite3.connect(self.database)) as c:
            c.execute("CREATE TRIGGER fixture_fail_pointer BEFORE UPDATE OF current_summary_id ON v2_evaluations BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.summary()
        self.assertEqual(self.counts(), before)
        self.assertIsNone(self.repo.get_current_summary(self.eid))

    def test_review_operation_receipt_failure_rolls_back_review(self):
        before = self.counts()
        with closing(sqlite3.connect(self.database)) as c:
            c.execute("CREATE TRIGGER fixture_fail_receipt BEFORE INSERT ON v2_operations WHEN NEW.operation='review' BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.review()
        self.assertEqual(self.counts(), before)

    def test_artifacts_reviews_and_receipts_are_database_append_only(self):
        self.review()
        for table in ("v2_artifacts", "v2_reviews", "v2_operations"):
            for operation in (f"DELETE FROM {table}", f"UPDATE {table} SET rowid=rowid"):
                with self.subTest(operation=operation), closing(sqlite3.connect(self.database)) as c, self.assertRaises(sqlite3.IntegrityError):
                    c.execute(operation)

    def test_foreign_keys_reject_missing_ownership(self):
        with closing(sqlite3.connect(self.database)) as c:
            c.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE v2_evaluations SET opportunity_id='missing' WHERE evaluation_id=?", (self.eid,))

    def test_restart_reconstructs_queue_artifacts_and_reviews(self):
        self.summary()
        review = self.review()
        reopened = V2Repository(self.database, session_database_path=self.sessions)
        self.assertEqual(reopened.list_review_queue(), self.repo.list_review_queue())
        self.assertEqual(reopened.list_artifacts(self.eid), self.repo.list_artifacts(self.eid))
        self.assertEqual(reopened.list_reviews(self.eid), (review,))

    def test_queue_order_and_bounded_pagination(self):
        for n in range(4):
            self.accept(f"op-{n}")
        all_entries = self.repo.list_review_queue(limit=100).entries
        self.assertEqual(list(all_entries), sorted(all_entries, key=lambda e:(e.evaluation.created_at,e.evaluation.evaluation_id)))
        page = self.repo.list_review_queue(limit=2, offset=2)
        self.assertEqual(page.total, 5)
        self.assertEqual(page.entries, all_entries[2:4])
        self.assertEqual(self.repo.list_review_queue(offset=100).entries, ())
        for values in ((0,0),(101,0),(1,-1),(True,0),(1.5,0)):
            with self.subTest(values=values), self.assertRaises(V2PersistenceError):
                self.repo.list_review_queue(limit=values[0], offset=values[1])

    def test_no_filter_by_review_assessment_or_execution(self):
        for n, state in enumerate(AssessmentState):
            accepted = self.accept(f"state-{n}")
            summary = self.repo.record_summary(accepted.evaluation.evaluation_id, self.draft(state), producer=self.producer, operation_key=f"summary-{n}")
            self.repo.record_review(accepted.evaluation.evaluation_id, summary.reference,
                list(ReviewDecision)[n], "Fixture assessment.", human=self.human, operation_key=f"review-{n}")
            self.repo.set_execution_state(accepted.evaluation.evaluation_id, list(ExecutionState)[n+1], operation_key=f"finish-{n}")
        queue = self.repo.list_review_queue()
        self.assertEqual(queue.total, 4)
        self.assertEqual({e.assessment_state for e in queue.entries if e.assessment_state}, set(AssessmentState))
        self.assertEqual({e.review_state for e in queue.entries}, {"PENDING",*list(ReviewDecision)[:3]})

    def test_execution_and_review_state_are_independent(self):
        self.summary()
        self.review()
        self.repo.set_execution_state(self.eid, ExecutionState.FAILED, operation_key="state")
        self.assertEqual(self.repo.review_status(self.eid).review_state, ReviewDecision.WORTH_INVESTIGATING)
        self.assertEqual(self.repo.get_evaluation(self.eid).execution_state, ExecutionState.FAILED)
        with self.assertRaises(V2PersistenceError):
            self.repo.set_execution_state(self.eid, ExecutionState.RUNNING, operation_key="resume")

    def test_predecessor_relationship_is_same_opportunity_only(self):
        second = self.repo.create_evaluation(self.accepted.opportunity.opportunity_id,
            predecessor_evaluation_id=self.eid, operation_key="reevaluate")
        self.assertEqual(second.predecessor_evaluation_id, self.eid)
        other = self.accept("other")
        with self.assertRaises(V2PersistenceError):
            self.repo.create_evaluation(other.opportunity.opportunity_id, predecessor_evaluation_id=self.eid, operation_key="wrong-parent")

    def test_references_cannot_fabricate_numerical_success(self):
        subject = self.accepted.evaluation.current_review_subject
        with self.assertRaises(V2PersistenceError):
            self.summary(calculation_references=(subject,))
        with self.assertRaises(ValidationError):
            self.draft(net_profit="100")
        with self.assertRaises(V2PersistenceError):
            self.repo.record_artifact(self.eid, {"schema_version":"v2-calculation-v1", "success":True},
                producer=self.producer, operation_key="fabrication")

    def test_assumptions_can_be_recorded_as_typed_artifacts(self):
        assumption = EconomicAssumption(name="shipping", explanation="Explicit scenario assumption.", provenance=self.provenance(), amount="12", unit="USD/order")
        artifact = self.repo.record_artifact(self.eid, AssumptionRecord(assumption=assumption), producer=self.producer, operation_key="assumption")
        self.assertEqual(artifact.artifact_type, ArtifactType.SCENARIO_ASSUMPTION)
        self.assertEqual(Artifact.model_validate_json(artifact.model_dump_json()), artifact)

    def test_human_review_does_not_execute_or_modify_business_content(self):
        before = self.counts()
        original = self.repo.get_evaluation(self.eid)
        # Fail immediately if any standard library network entry point is used.
        with patch("socket.socket.connect", side_effect=AssertionError("Network call")), \
                patch("agents.Runner.run", side_effect=AssertionError("Agent call")), \
                patch("agents.Runner.run_sync", side_effect=AssertionError("Agent call")), \
                patch("arbitrage.tools.profitability._calculate_profitability", side_effect=AssertionError("Calculation")):
            self.review(requested_follow_up=("Research more later, not automatically.",))
        after = self.counts()
        self.assertEqual(after["v2_artifacts"], before["v2_artifacts"])
        self.assertEqual(after["v2_evaluations"], before["v2_evaluations"])
        self.assertEqual(self.repo.get_evaluation(self.eid), original)
        self.assertFalse(hasattr(self.service, "record_artifact"))
        self.assertFalse(self.sessions.exists())

    def test_schema_initialization_is_explicit_idempotent_and_v2_only(self):
        other = self.root / "empty.db"
        repo = V2Repository(other, session_database_path=self.sessions)
        self.assertFalse(other.exists())
        repo.initialize_schema()
        repo.initialize_schema()
        with closing(sqlite3.connect(other)) as c:
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(tables, {"v2_schema_metadata","v2_opportunities","v2_evaluations","v2_artifacts","v2_reviews","v2_operations"})

    def test_production_and_partial_database_aliases_are_refused(self):
        from arbitrage.database_config import DEFAULT_BUSINESS_DATABASE, DEFAULT_SESSION_DATABASE
        for business, sessions in ((DEFAULT_BUSINESS_DATABASE,self.sessions),(self.database,DEFAULT_SESSION_DATABASE)):
            with self.subTest(business=business,sessions=sessions), self.assertRaises(ValueError):
                V2Repository(business, session_database_path=sessions)

    def test_malformed_stored_payload_is_not_silently_skipped(self):
        subject = self.accepted.evaluation.current_review_subject
        with closing(sqlite3.connect(self.database)) as c:
            c.execute("DROP TRIGGER v2_artifacts_no_update")  # Deliberate corruption of temporary fixture only.
            c.execute("UPDATE v2_artifacts SET payload_json='not-json' WHERE artifact_id=?", (subject.artifact_id,))
            c.commit()
        with self.assertRaises(ValueError):
            self.repo.list_review_queue()

    def test_invalid_model_copy_cannot_bypass_validation(self):
        with self.assertRaises(ValidationError):
            self.repo.record_summary(self.eid, self.draft().model_copy(update={"state_decision_rationale":" "}),
                producer=self.producer, operation_key="invalid-model")

    def test_unsupported_schema_version_does_not_migrate_or_repair(self):
        with closing(sqlite3.connect(self.database)) as c:
            c.execute("UPDATE v2_schema_metadata SET value='999' WHERE key='schema_version'")
            c.commit()
        before = self.database.read_bytes()
        with self.assertRaises(V2PersistenceError):
            self.repo.initialize_schema()
        self.assertEqual(self.database.read_bytes(), before)

    def test_empty_intake_and_unknown_summary_properties_are_rejected(self):
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.accept("blank", original_text="  ")
        with self.assertRaises(ValidationError):
            self.draft(success=True)
        self.assertEqual(self.counts(), before)

    def test_invalid_review_authority_cannot_be_serialized(self):
        review = self.review()
        with self.assertRaises(ValidationError):
            type(review).model_validate(review.model_copy(update={"authority":"PURCHASE"}))

    def test_v1c_tables_are_untouched_when_v2_schema_is_added(self):
        from arbitrage.persistence import CandidateRepository
        path = self.root / "coexistence.db"
        CandidateRepository(path)  # Actual V1C schema, in this temporary DB only.
        with closing(sqlite3.connect(path)) as c:
            before = {r[0]:r[1] for r in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
            version = c.execute("SELECT * FROM schema_metadata").fetchall()
        repo = V2Repository(path, session_database_path=self.sessions)
        repo.initialize_schema()
        with closing(sqlite3.connect(path)) as c:
            after = {r[0]:r[1] for r in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
            self.assertEqual(c.execute("SELECT * FROM schema_metadata").fetchall(), version)
        self.assertTrue(all(after[name] == sql for name,sql in before.items()))

    def test_missing_records_and_wrong_evidence_are_refused_without_selection(self):
        with self.assertRaises(V2PersistenceError):
            self.repo.get_evaluation("missing")
        other = self.accept("other").evaluation.current_review_subject
        before = self.counts()
        with self.assertRaises(V2PersistenceError):
            self.summary(evidence_references=(other,))
        self.assertEqual(self.counts(), before)

    def test_queue_tie_breaking_is_deterministic(self):
        from datetime import datetime, timezone
        fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch("arbitrage.v2.persistence._now", return_value=fixed):
            first, second = self.accept("tied-1"), self.accept("tied-2")
        entries = [e for e in self.repo.list_review_queue().entries if e.evaluation.evaluation_id in {
            first.evaluation.evaluation_id, second.evaluation.evaluation_id}]
        self.assertEqual([e.evaluation.evaluation_id for e in entries], sorted(e.evaluation.evaluation_id for e in entries))

    def test_serialized_summary_and_parent_binding_cannot_be_forged(self):
        artifact = self.summary()
        raw = artifact.model_dump(mode="json")
        raw["payload"]["revision"] = 2
        with self.assertRaises(ValidationError):
            Artifact.model_validate(raw)
        raw = artifact.model_dump(mode="json")
        raw["parent_references"] = [artifact.reference.model_dump(mode="json")]
        with self.assertRaises(ValidationError):
            Artifact.model_validate(raw)

    def test_read_services_do_not_change_rows_or_database_bytes(self):
        self.summary()
        review = self.review()
        before, counts = self.database.read_bytes(), self.counts()
        self.service.list_queue()
        self.service.get_evaluation(self.eid)
        self.service.get_current_summary(self.eid)
        self.service.get_artifact_history(self.eid)
        self.service.get_review_history(self.eid)
        self.service.review_status(self.eid)
        self.repo.review_subject_is_current(review.review_id)
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
