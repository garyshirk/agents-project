import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from arbitrage.contracts import (
    CandidateDestination,
    CandidateDestinationKind,
    CandidateEvaluation,
    CandidateIntake,
    CandidateIntakeSource,
    CandidateLifecycleStatus,
    CandidateRecord,
    CandidateSource,
    CandidateSourceKind,
    EvaluationStatus,
    EvaluationTrigger,
    HumanAcquisitionInput,
    HumanInputBasis,
    IdentityConfidence,
    MatchQuality,
    ProductCondition,
    ProductIdentity,
    ResaleResult,
    SourcingResult,
)
from arbitrage.persistence import (
    CandidateRepository,
    InvalidStateTransitionError,
    PersistenceDataError,
    RecordNotFoundError,
    UnsupportedSchemaVersionError,
    initialize_database,
)
from arbitrage.tools.profitability import _calculate_profitability
from arbitrage.contracts import ProfitabilityRequest


class CandidatePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "test.db"
        self.repository = CandidateRepository(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def identity(model: str = "FD2722-001", variant: str = "Men's size 10") -> ProductIdentity:
        return ProductIdentity(
            brand="Nike",
            product_name="Pegasus 41",
            model_number=model,
            upc_gtin=None,
            variant=variant,
            condition=ProductCondition.NEW,
            package_quantity=1,
        )

    @staticmethod
    def source(location: str = "Algonquin, IL") -> CandidateSource:
        return CandidateSource(
            source_kind=CandidateSourceKind.PHYSICAL_STORE,
            name="Ross",
            physical_location=location,
            url=None,
            seller_identity=None,
        )

    @staticmethod
    def destination(name: str = "eBay") -> CandidateDestination:
        return CandidateDestination(
            destination_kind=CandidateDestinationKind.MARKETPLACE,
            name=name,
            url=None,
            seller_account=None,
        )

    def create_candidate(self, **overrides):
        values = {
            "product_identity": self.identity(),
            "acquisition_source": self.source(),
            "resale_destination": self.destination(),
            "intake_origin": CandidateIntakeSource.HUMAN_PHYSICAL,
            "notes": "Human-observed candidate",
        }
        values.update(overrides)
        return self.repository.create_candidate(**values)

    def profitability_result(self):
        request = ProfitabilityRequest.model_validate(
            {
                "acquisition": {
                    "product_identity": self.identity().model_dump(mode="json"),
                    "source_type": "PHYSICAL_RETAILER",
                    "source_name": "Ross",
                    "source_references": [],
                    "condition": "NEW",
                    "currency": "USD",
                    "unit_purchase_price": "39.99",
                    "purchase_price_basis": "ESTIMATED",
                    "quantity": 1,
                    "additional_costs": [],
                    "quantity_available": 1,
                    "quantity_available_basis": "ASSUMED",
                    "purchase_limit": 1,
                    "purchase_requirements": [],
                    "assumptions": [],
                    "unknowns": [],
                    "notes": None,
                },
                "sale": {
                    "marketplace_name": "eBay",
                    "marketplace_references": [],
                    "currency": "USD",
                    "target_condition": "NEW",
                    "resale_price_low": "80",
                    "resale_price_expected": "90",
                    "resale_price_high": "100",
                    "resale_price_basis": "ESTIMATED",
                    "selling_costs": [],
                    "quantity": 1,
                    "assumptions": [],
                    "unknowns": [],
                    "notes": None,
                },
                "sensitivity_inputs": [],
            }
        )
        return _calculate_profitability(request)

    def sourcing_result(self) -> SourcingResult:
        return SourcingResult(
            product_identity=self.identity(),
            identity_confidence="STRONG",
            identity_confidence_reason="Model and variant match the observed label.",
            source_name="Ross",
            seller_name="Ross",
            source_url=None,
            source_references=[],
            item_price=39.99,
            currency="USD",
            shipping_cost=None,
            shipping_cost_known=False,
            other_known_costs=[],
            availability_status="LIMITED",
            inventory_quantity=1,
            purchase_limit=1,
            acquisition_requirements=[],
            unresolved_issues=["Online inventory is not available."],
            research_timestamp="2026-09-25T12:00:00Z",
            notes=None,
        )

    def resale_result(self) -> ResaleResult:
        return ResaleResult(
            researched_identity=self.identity(),
            identity_match=MatchQuality.EXACT,
            identity_match_notes="Exact model and represented variant.",
            evidence_items=[],
            verified_realized_price_low=None,
            verified_realized_price_high=None,
            estimated_achievable_price_low=84.0,
            estimated_achievable_price_high=109.97,
            currency="USD",
            evidence_quality="MODERATE",
            evidence_quality_reason="Representative structured test evidence.",
            demand_assessment="UNKNOWN",
            market_observations=[],
            unresolved_issues=["Size-specific realized price remains uncertain."],
            research_timestamp="2026-09-25T12:05:00Z",
            notes=None,
        )

    def test_initialize_is_idempotent_and_records_schema_version(self):
        candidate = self.create_candidate()
        initialize_database(self.database_path)
        self.assertEqual(self.repository.get_candidate(candidate.candidate_id), candidate)
        with sqlite3.connect(self.database_path) as connection:
            version = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
        self.assertEqual(version, "1")

    def test_unsupported_schema_version_fails_clearly(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE schema_metadata SET value = '999' WHERE key = 'schema_version'"
            )
        with self.assertRaises(UnsupportedSchemaVersionError):
            initialize_database(self.database_path)

    def test_existing_unversioned_candidate_schema_fails_clearly(self):
        other_path = Path(self.temporary_directory.name) / "unversioned.db"
        with sqlite3.connect(other_path) as connection:
            connection.execute("CREATE TABLE candidates(candidate_id TEXT PRIMARY KEY)")
        with self.assertRaises(PersistenceDataError):
            initialize_database(other_path)

    def test_create_get_list_and_filter_candidates(self):
        physical = self.create_candidate()
        online = self.create_candidate(
            acquisition_source=CandidateSource(
                source_kind=CandidateSourceKind.ONLINE_RETAILER,
                name="Best Buy",
                physical_location=None,
                url=None,
                seller_identity=None,
            ),
            intake_origin=CandidateIntakeSource.HUMAN_ONLINE,
        )
        self.assertEqual(physical.lifecycle_status, CandidateLifecycleStatus.INVESTIGATING)
        self.assertIsNone(physical.latest_evaluation_id)
        self.assertEqual(UUID(physical.candidate_id).version, 4)
        self.assertEqual(self.repository.get_candidate(physical.candidate_id), physical)
        listed = self.repository.list_candidates()
        self.assertEqual(len(listed), 2)
        self.assertEqual(listed[0].candidate_id, online.candidate_id)
        filtered = self.repository.list_candidates(
            intake_origin=CandidateIntakeSource.HUMAN_ONLINE, limit=1
        )
        self.assertEqual([item.candidate_id for item in filtered], [online.candidate_id])

    def test_sources_and_destinations_define_independent_candidates(self):
        first = self.create_candidate()
        other_store = self.create_candidate(acquisition_source=self.source("Elgin, IL"))
        other_market = self.create_candidate(resale_destination=self.destination("Facebook"))
        self.assertEqual(len({first.candidate_id, other_store.candidate_id, other_market.candidate_id}), 3)

    def test_physical_source_requires_location_but_online_url_is_optional(self):
        with self.assertRaises(ValidationError):
            self.source("")
        online = CandidateSource(
            source_kind=CandidateSourceKind.ONLINE_RETAILER,
            name="Best Buy",
            physical_location=None,
            url=None,
            seller_identity=None,
        )
        self.assertIsNone(online.url)

    def test_create_evaluation_and_reject_orphan(self):
        candidate = self.create_candidate()
        evaluation = self.repository.create_evaluation(
            candidate.candidate_id, trigger=EvaluationTrigger.HUMAN_REQUEST
        )
        self.assertEqual(evaluation.status, EvaluationStatus.IN_PROGRESS)
        self.assertEqual(UUID(evaluation.evaluation_id).version, 4)
        self.assertIsNone(evaluation.completed_at)
        self.assertIsNone(self.repository.get_candidate(candidate.candidate_id).latest_evaluation_id)
        with self.assertRaises(RecordNotFoundError):
            self.repository.create_evaluation(
                "missing", trigger=EvaluationTrigger.HUMAN_REQUEST
            )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO candidate_evaluations
                    (evaluation_id, candidate_id, trigger, status, started_at,
                     assumptions_json, uncertainties_json)
                    VALUES ('orphan', 'missing', 'HUMAN_REQUEST', 'IN_PROGRESS', ?, '[]', '[]')
                    """,
                    (datetime.now(timezone.utc).isoformat(),),
                )

    def test_wait_and_resume_transitions_synchronize_candidate(self):
        candidate = self.create_candidate()
        evaluation = self.repository.create_evaluation(
            candidate.candidate_id, trigger=EvaluationTrigger.HUMAN_REQUEST
        )
        waiting = self.repository.update_evaluation_status(
            evaluation.evaluation_id, EvaluationStatus.AWAITING_HUMAN_INPUT
        )
        self.assertEqual(waiting.status, EvaluationStatus.AWAITING_HUMAN_INPUT)
        self.assertEqual(
            self.repository.get_candidate(candidate.candidate_id).lifecycle_status,
            CandidateLifecycleStatus.AWAITING_HUMAN_INPUT,
        )
        resumed = self.repository.update_evaluation_status(
            evaluation.evaluation_id, EvaluationStatus.IN_PROGRESS
        )
        self.assertEqual(resumed.status, EvaluationStatus.IN_PROGRESS)
        self.assertEqual(
            self.repository.get_candidate(candidate.candidate_id).lifecycle_status,
            CandidateLifecycleStatus.INVESTIGATING,
        )

    def test_complete_evaluation_updates_candidate_and_is_immutable(self):
        candidate = self.create_candidate()
        evaluation = self.repository.create_evaluation(
            candidate.candidate_id, trigger=EvaluationTrigger.HUMAN_REQUEST
        )
        completed = self.repository.complete_evaluation(
            evaluation.evaluation_id,
            status=EvaluationStatus.COMPLETED,
            profitability_result=self.profitability_result(),
        )
        record = self.repository.get_candidate(candidate.candidate_id)
        self.assertEqual(completed.status, EvaluationStatus.COMPLETED)
        self.assertIsNotNone(completed.completed_at)
        self.assertEqual(record.lifecycle_status, CandidateLifecycleStatus.EVALUATED)
        self.assertEqual(record.latest_evaluation_id, completed.evaluation_id)
        with self.assertRaises(InvalidStateTransitionError):
            self.repository.update_evaluation_status(
                completed.evaluation_id, EvaluationStatus.IN_PROGRESS
            )
        with self.assertRaises(InvalidStateTransitionError):
            self.repository.complete_evaluation(
                completed.evaluation_id, status=EvaluationStatus.COMPLETED
            )

    def test_second_evaluation_preserves_history_and_advances_latest(self):
        candidate = self.create_candidate()
        first = self.repository.create_evaluation(
            candidate.candidate_id, trigger=EvaluationTrigger.HUMAN_REQUEST
        )
        first = self.repository.complete_evaluation(
            first.evaluation_id, status=EvaluationStatus.COMPLETED
        )
        second = self.repository.create_evaluation(
            candidate.candidate_id, trigger=EvaluationTrigger.MANUAL_REFRESH
        )
        self.assertEqual(
            self.repository.get_candidate(candidate.candidate_id).latest_evaluation_id,
            first.evaluation_id,
        )
        second = self.repository.complete_evaluation(
            second.evaluation_id, status=EvaluationStatus.INSUFFICIENT_EVIDENCE
        )
        history = self.repository.list_evaluations(candidate.candidate_id)
        self.assertEqual([item.evaluation_id for item in history], [first.evaluation_id, second.evaluation_id])
        self.assertEqual(history[0], first)
        self.assertEqual(
            self.repository.get_candidate(candidate.candidate_id).latest_evaluation_id,
            second.evaluation_id,
        )

    def test_failed_evaluation_is_historical_without_false_success(self):
        candidate = self.create_candidate()
        failed = self.repository.create_evaluation(
            candidate.candidate_id, trigger=EvaluationTrigger.HUMAN_REQUEST
        )
        failed = self.repository.complete_evaluation(
            failed.evaluation_id, status=EvaluationStatus.FAILED
        )
        record = self.repository.get_candidate(candidate.candidate_id)
        self.assertEqual(failed.status, EvaluationStatus.FAILED)
        self.assertEqual(record.lifecycle_status, CandidateLifecycleStatus.INVESTIGATING)
        self.assertIsNone(record.latest_evaluation_id)

    def test_failed_refresh_preserves_prior_success(self):
        candidate = self.create_candidate()
        success = self.repository.create_evaluation(
            candidate.candidate_id, trigger=EvaluationTrigger.HUMAN_REQUEST
        )
        success = self.repository.complete_evaluation(
            success.evaluation_id, status=EvaluationStatus.COMPLETED
        )
        failed = self.repository.create_evaluation(
            candidate.candidate_id, trigger=EvaluationTrigger.MANUAL_REFRESH
        )
        self.repository.complete_evaluation(failed.evaluation_id, status=EvaluationStatus.FAILED)
        record = self.repository.get_candidate(candidate.candidate_id)
        self.assertEqual(record.lifecycle_status, CandidateLifecycleStatus.EVALUATED)
        self.assertEqual(record.latest_evaluation_id, success.evaluation_id)

    def test_structured_values_round_trip(self):
        candidate = self.create_candidate()
        intake = CandidateIntake(
            intake_source=CandidateIntakeSource.HUMAN_PHYSICAL,
            product_identity=self.identity(),
            identity_confidence=IdentityConfidence.STRONG,
            identity_basis=HumanInputBasis.HUMAN_OBSERVED,
            acquisition=HumanAcquisitionInput(
                seller_or_store="Ross",
                purchase_price="39.99",
                currency="USD",
                quantity_available=1,
                purchase_limit=1,
                availability_status="LIMITED",
                condition="NEW",
                location_description="Algonquin, IL",
                observed_at=datetime.now(timezone.utc),
                price_basis="HUMAN_OBSERVED",
                quantity_basis="HUMAN_OBSERVED",
                condition_basis="HUMAN_OBSERVED",
                availability_basis="HUMAN_OBSERVED",
            ),
            resale=None,
            notes="Observed in person",
        )
        evaluation = self.repository.create_evaluation(
            candidate.candidate_id,
            trigger=EvaluationTrigger.HUMAN_REQUEST,
            intake_snapshot=intake,
            assumptions=["Authenticity assumed"],
            uncertainties=["Size-specific demand unknown"],
        )
        completed = self.repository.complete_evaluation(
            evaluation.evaluation_id,
            status=EvaluationStatus.COMPLETED,
            sourcing_result=self.sourcing_result(),
            resale_result=self.resale_result(),
            profitability_result=self.profitability_result(),
        )
        restored = self.repository.get_evaluation(completed.evaluation_id)
        self.assertEqual(restored.intake_snapshot, intake)
        self.assertEqual(restored.sourcing_result, completed.sourcing_result)
        self.assertEqual(restored.resale_result, completed.resale_result)
        self.assertEqual(restored.profitability_result, completed.profitability_result)
        self.assertEqual(restored.assumptions, ["Authenticity assumed"])
        self.assertEqual(restored.uncertainties, ["Size-specific demand unknown"])

    def test_corrupt_json_fails_visibly(self):
        candidate = self.create_candidate()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE candidates SET product_identity_json = 'not-json' WHERE candidate_id = ?",
                (candidate.candidate_id,),
            )
        with self.assertRaises(PersistenceDataError):
            self.repository.get_candidate(candidate.candidate_id)

    def test_timestamp_and_status_invariants(self):
        now = datetime.now(timezone.utc)
        with self.assertRaises(ValidationError):
            CandidateRecord(
                candidate_id="candidate",
                product_identity=self.identity(),
                acquisition_source=self.source(),
                resale_destination=self.destination(),
                intake_origin=CandidateIntakeSource.HUMAN_PHYSICAL,
                lifecycle_status=CandidateLifecycleStatus.INVESTIGATING,
                created_at=now,
                updated_at=now - timedelta(seconds=1),
                latest_evaluation_id=None,
                notes=None,
            )
        with self.assertRaises(ValidationError):
            CandidateEvaluation(
                evaluation_id="evaluation",
                candidate_id="candidate",
                trigger=EvaluationTrigger.HUMAN_REQUEST,
                status=EvaluationStatus.IN_PROGRESS,
                started_at=now,
                completed_at=now,
                intake_snapshot=None,
                sourcing_result=None,
                resale_result=None,
                profitability_result=None,
                manager_notes=None,
            )
        with self.assertRaises(ValidationError):
            CandidateEvaluation(
                evaluation_id="evaluation",
                candidate_id="candidate",
                trigger=EvaluationTrigger.HUMAN_REQUEST,
                status=EvaluationStatus.COMPLETED,
                started_at=now,
                completed_at=None,
                intake_snapshot=None,
                sourcing_result=None,
                resale_result=None,
                profitability_result=None,
                manager_notes=None,
            )
        with self.assertRaises(ValidationError):
            CandidateEvaluation(
                evaluation_id="evaluation",
                candidate_id="candidate",
                trigger=EvaluationTrigger.HUMAN_REQUEST,
                status=EvaluationStatus.FAILED,
                started_at=now,
                completed_at=now - timedelta(seconds=1),
                intake_snapshot=None,
                sourcing_result=None,
                resale_result=None,
                profitability_result=None,
                manager_notes=None,
            )
        with self.assertRaises(ValidationError):
            CandidateRecord(
                candidate_id="candidate",
                product_identity=self.identity(),
                acquisition_source=self.source(),
                resale_destination=self.destination(),
                intake_origin=CandidateIntakeSource.HUMAN_PHYSICAL,
                lifecycle_status=CandidateLifecycleStatus.INVESTIGATING,
                created_at=datetime.now(),
                updated_at=datetime.now(),
                latest_evaluation_id=None,
                notes=None,
            )

    def test_candidate_status_update_does_not_rewrite_identity(self):
        candidate = self.create_candidate()
        closed = self.repository.update_candidate_status(
            candidate.candidate_id,
            CandidateLifecycleStatus.CLOSED,
            notes="No longer under consideration",
        )
        self.assertEqual(closed.lifecycle_status, CandidateLifecycleStatus.CLOSED)
        self.assertEqual(closed.product_identity, candidate.product_identity)
        self.assertEqual(closed.acquisition_source, candidate.acquisition_source)
        self.assertEqual(closed.resale_destination, candidate.resale_destination)
        with self.assertRaises(InvalidStateTransitionError):
            self.repository.update_candidate_status(
                candidate.candidate_id, CandidateLifecycleStatus.EVALUATED
            )


if __name__ == "__main__":
    unittest.main()
