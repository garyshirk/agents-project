import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arbitrage.contracts import (
    AvailabilityStatus,
    CandidateDestination,
    CandidateEvaluation,
    CandidateIntake,
    CandidateIntakeSource,
    CandidateLifecycleStatus,
    CandidateRecord,
    CandidateSource,
    CandidateSourceKind,
    CostType,
    EconomicCostFinding,
    EconomicCostFindingsResult,
    EvaluationArtifact,
    EvaluationArtifactType,
    EvaluationStatus,
    EvaluationTrigger,
    EvidenceType,
    HumanAcquisitionInput,
    HumanInputBasis,
    IdentityConfidence,
    InputBasis,
    MatchQuality,
    ProductCondition,
    ProductIdentity,
    ProfitabilityStatus,
    ResaleEvidenceItem,
    ResaleResult,
    SourceReference,
    SourceType,
    SourcingTextReport,
    UnknownMateriality,
)
from arbitrage.persistence import CandidateRepository, EconomicEvidenceSnapshot
from arbitrage.profitability_assembly import PURCHASE_PRICE_COST_ID
from arbitrage.profitability_request_assembly import (
    ASSEMBLY_POLICY_VERSION,
    AssemblyFailureCode,
    assemble_profitability_request,
)
from arbitrage.tools.profitability import _calculate_cost, _calculate_profitability


class ProfitabilityRequestAssemblyTests(unittest.TestCase):
    @staticmethod
    def identity(**changes) -> ProductIdentity:
        values = {
            "brand": "Nike",
            "product_name": "Pegasus 41",
            "model_number": "FD2722-001",
            "upc_gtin": None,
            "variant": "Men's size 10",
            "condition": ProductCondition.NEW,
            "package_quantity": 1,
        }
        values.update(changes)
        return ProductIdentity(**values)

    @classmethod
    def candidate(
        cls,
        *,
        source_kind=CandidateSourceKind.PHYSICAL_STORE,
        source_url=None,
        destination_name="eBay",
        destination_url=None,
    ) -> CandidateRecord:
        now = datetime.now(timezone.utc)
        return CandidateRecord(
            candidate_id="candidate-1",
            product_identity=cls.identity(),
            acquisition_source=CandidateSource(
                source_kind=source_kind,
                name="Ross",
                physical_location=(
                    "Algonquin, IL"
                    if source_kind == CandidateSourceKind.PHYSICAL_STORE
                    else None
                ),
                url=source_url,
                seller_identity=None,
            ),
            resale_destination=CandidateDestination(
                destination_kind="MARKETPLACE",
                name=destination_name,
                url=(
                    destination_url
                    if destination_url is not None
                    else f"https://www.{destination_name.casefold()}.com"
                ),
                seller_account=None,
            ),
            intake_origin=CandidateIntakeSource.HUMAN_PHYSICAL,
            lifecycle_status=CandidateLifecycleStatus.INVESTIGATING,
            created_at=now,
            updated_at=now,
            latest_evaluation_id=None,
            notes=None,
        )

    @classmethod
    def intake(cls, **acquisition_changes) -> CandidateIntake:
        values = {
            "seller_or_store": "Ross",
            "purchase_price": "39.99",
            "currency": "USD",
            "quantity_available": 1,
            "purchase_limit": 1,
            "availability_status": AvailabilityStatus.LIMITED,
            "condition": ProductCondition.NEW,
            "location_description": "Algonquin, IL",
            "observed_at": datetime.now(timezone.utc),
            "price_basis": HumanInputBasis.HUMAN_OBSERVED,
            "quantity_basis": HumanInputBasis.HUMAN_OBSERVED,
            "condition_basis": HumanInputBasis.HUMAN_OBSERVED,
            "availability_basis": HumanInputBasis.HUMAN_OBSERVED,
        }
        values.update(acquisition_changes)
        acquisition = HumanAcquisitionInput(**values)
        return CandidateIntake(
            intake_source=CandidateIntakeSource.HUMAN_PHYSICAL,
            product_identity=cls.identity(),
            identity_confidence=IdentityConfidence.STRONG,
            identity_basis=HumanInputBasis.HUMAN_OBSERVED,
            acquisition=acquisition,
            resale=None,
            notes=None,
        )

    @classmethod
    def evaluation(cls, intake=None) -> CandidateEvaluation:
        return CandidateEvaluation(
            evaluation_id="evaluation-1",
            candidate_id="candidate-1",
            trigger=EvaluationTrigger.HUMAN_REQUEST,
            status=EvaluationStatus.IN_PROGRESS,
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            intake_snapshot=cls.intake() if intake is None else intake,
            sourcing_result=None,
            resale_result=None,
            profitability_result=None,
            assumptions=["Manager-authored assumption must not be imported."],
            uncertainties=[],
            manager_notes=None,
        )

    @staticmethod
    def finding(
        cost_id,
        *,
        cost_type=CostType.FIXED_PER_UNIT,
        value=None,
        basis=None,
        low=None,
        high=None,
        currency="USD",
        modeled=None,
        unresolved=None,
    ) -> EconomicCostFinding:
        return EconomicCostFinding(
            cost_id=cost_id,
            name=cost_id.replace("_", " ").title(),
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
                SourceReference(
                    url=f"https://evidence.example/{cost_id}",
                    description="Economic evidence",
                )
            ],
            limitations=[f"Limitation for {cost_id}"],
            notes=None,
        )

    @classmethod
    def acquisition_findings(cls, *, basis=InputBasis.HUMAN_OBSERVED):
        return EconomicCostFindingsResult(
            findings=[
                cls.finding(PURCHASE_PRICE_COST_ID, value="39.99", basis=basis),
                cls.finding(
                    "acquisition_sales_tax",
                    cost_type=CostType.PERCENT_OF_UNIT_PRICE,
                    currency=None,
                    unresolved=UnknownMateriality.MATERIAL,
                ),
                cls.finding(
                    "packaging",
                    value="1.25",
                    basis=InputBasis.VERIFIED,
                ),
            ]
        )

    @classmethod
    def selling_findings(cls):
        return EconomicCostFindingsResult(
            findings=[
                cls.finding(
                    "marketplace_fee",
                    cost_type=CostType.PERCENT_OF_UNIT_PRICE,
                    currency=None,
                    modeled="13.5",
                    unresolved=UnknownMateriality.MATERIAL,
                ),
                cls.finding(
                    "order_fee",
                    cost_type=CostType.FIXED_PER_BATCH,
                    value="0.30",
                    basis=InputBasis.VERIFIED,
                ),
                cls.finding(
                    "shipping",
                    low="8",
                    high="12",
                    basis=InputBasis.ESTIMATED,
                ),
            ]
        )

    @classmethod
    def resale_evidence(cls, marketplace="eBay", url="https://www.ebay.com/itm/example"):
        return ResaleEvidenceItem(
            marketplace=marketplace,
            listing_title="Nike Pegasus 41",
            evidence_type=EvidenceType.ACTIVE_ASK,
            sale_completion_basis=None,
            realized_price_basis=None,
            quantity_sold=None,
            price=120,
            currency="USD",
            shipping_cost=None,
            condition=ProductCondition.NEW,
            sale_or_listing_date="2026-10-11",
            url=url,
            match_quality=MatchQuality.EXACT,
            limitations=["Active ask is not realized-sale evidence."],
        )

    @classmethod
    def resale(cls, **changes) -> ResaleResult:
        values = {
            "researched_identity": cls.identity(),
            "identity_match": MatchQuality.EXACT,
            "identity_match_notes": "Exact model and variant.",
            "evidence_items": [cls.resale_evidence()],
            "verified_realized_price_low": 80,
            "verified_realized_price_high": 100,
            "estimated_achievable_price_low": 85,
            "estimated_achievable_price_high": 95,
            "currency": "USD",
            "evidence_quality": "MODERATE",
            "evidence_quality_reason": "Public evidence is incomplete.",
            "demand_assessment": "UNKNOWN",
            "market_observations": [],
            "unresolved_issues": ["Size-specific demand is uncertain."],
            "research_timestamp": "2026-10-11T12:00:00Z",
            "notes": None,
        }
        values.update(changes)
        return ResaleResult(**values)

    @staticmethod
    def artifact(artifact_id, sequence, artifact_type, payload):
        return EvaluationArtifact(
            artifact_id=artifact_id,
            evaluation_id="evaluation-1",
            sequence_number=sequence,
            artifact_type=artifact_type,
            payload_json=payload.model_dump_json(),
            context_json=None,
            created_at=datetime.now(timezone.utc),
        )

    @classmethod
    def snapshot(cls, acquisition=None, resale=None, selling=None, sequences=(1, 2, 3, 4)):
        acquisition = acquisition or cls.acquisition_findings()
        resale = resale or cls.resale()
        selling = selling or cls.selling_findings()
        return EconomicEvidenceSnapshot(
            evaluation_id="evaluation-1",
            sourcing_report=cls.artifact(
                11,
                sequences[0],
                EvaluationArtifactType.SOURCING_REPORT,
                SourcingTextReport(report_text="Exact sourcing report."),
            ),
            acquisition_cost_findings=cls.artifact(
                12,
                sequences[1],
                EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
                acquisition,
            ),
            resale=cls.artifact(
                13, sequences[2], EvaluationArtifactType.RESALE, resale
            ),
            selling_cost_findings=cls.artifact(
                14,
                sequences[3],
                EvaluationArtifactType.SELLING_COST_FINDINGS,
                selling,
            ),
        )

    def assemble(self, *, candidate=None, evaluation=None, snapshot=None):
        return assemble_profitability_request(
            candidate or self.candidate(),
            evaluation or self.evaluation(),
            snapshot or self.snapshot(),
        )

    def test_valid_complete_assembly_and_authoritative_mapping(self):
        result = self.assemble()

        self.assertTrue(result.success)
        request = result.request
        self.assertEqual(request.acquisition.product_identity, self.identity())
        self.assertEqual(request.acquisition.source_type, SourceType.PHYSICAL_RETAILER)
        self.assertEqual(request.acquisition.source_name, "Ross")
        self.assertEqual(request.sale.marketplace_name, "eBay")
        self.assertEqual(request.acquisition.quantity, 1)
        self.assertEqual(request.sale.quantity, 1)
        self.assertEqual(request.sale.resale_price_expected, None)
        self.assertNotIn("Manager-authored", " ".join(request.acquisition.assumptions))

    def test_artifact_and_policy_provenance_is_json_serializable(self):
        result = self.assemble()

        provenance = result.provenance
        self.assertEqual(provenance.evaluation_id, "evaluation-1")
        self.assertEqual(
            [(item.artifact_id, item.sequence_number) for item in provenance.artifact_references],
            [(11, 1), (12, 2), (13, 3), (14, 4)],
        )
        self.assertEqual(provenance.projection_policy_version, ASSEMBLY_POLICY_VERSION)
        self.assertIn("one acquired sellable unit", provenance.scenario_policy)
        self.assertEqual(provenance.resale_price_selection.artifact_id, 13)
        self.assertEqual(provenance.resale_price_selection.sequence_number, 3)
        self.assertEqual(
            provenance.resale_price_selection.selected_fields,
            ["estimated_achievable_price_low", "estimated_achievable_price_high"],
        )
        self.assertIn(
            "No evidence-item URL",
            provenance.resale_price_selection.url_attribution,
        )
        self.assertIn(
            "https://www.ebay.com/itm/example",
            [item.url for item in provenance.destination_references],
        )
        self.assertIn("Size-specific demand is uncertain.", provenance.research_limitations)
        json.loads(provenance.model_dump_json())

    def test_stale_acquisition_and_selling_captures_fail_closed(self):
        for sequences in ((2, 1, 3, 4), (1, 2, 4, 3)):
            with self.subTest(sequences=sequences):
                result = self.assemble(snapshot=self.snapshot(sequences=sequences))
                self.assertFalse(result.success)
                self.assertEqual(result.failures[0].code, AssemblyFailureCode.STALE_EVIDENCE)

    def test_missing_and_invalid_artifacts_fail_closed(self):
        missing = replace(self.snapshot(), sourcing_report=None)
        invalid = self.snapshot()
        invalid = replace(
            invalid,
            resale=invalid.resale.model_copy(update={"payload_json": "{}"}),
        )

        missing_result = self.assemble(snapshot=missing)
        invalid_result = self.assemble(snapshot=invalid)

        self.assertEqual(
            missing_result.failures[0].code, AssemblyFailureCode.MISSING_EVIDENCE
        )
        self.assertEqual(
            invalid_result.failures[0].code, AssemblyFailureCode.INVALID_ARTIFACT
        )

    def test_human_observed_and_verified_purchase_prices_are_preserved(self):
        for basis in (InputBasis.HUMAN_OBSERVED, InputBasis.VERIFIED):
            with self.subTest(basis=basis):
                snapshot = self.snapshot(acquisition=self.acquisition_findings(basis=basis))
                result = self.assemble(snapshot=snapshot)
                self.assertTrue(result.success)
                self.assertEqual(result.request.acquisition.purchase_price_basis, basis)

    def test_missing_duplicate_and_unresolved_purchase_prices_fail(self):
        tax = self.finding(
            "tax",
            currency=None,
            cost_type=CostType.PERCENT_OF_UNIT_PRICE,
            unresolved=UnknownMateriality.MATERIAL,
        )
        purchase = self.acquisition_findings().findings[0]
        unresolved = purchase.model_copy(
            update={
                "value": None,
                "basis": None,
                "unresolved_materiality": UnknownMateriality.MATERIAL,
            }
        )
        cases = (
            EconomicCostFindingsResult(findings=[tax]),
            EconomicCostFindingsResult(findings=[purchase, purchase]),
            EconomicCostFindingsResult(findings=[unresolved]),
        )
        expected = (
            AssemblyFailureCode.PURCHASE_PRICE,
            AssemblyFailureCode.PURCHASE_PRICE,
            AssemblyFailureCode.PROJECTION,
        )
        for findings, code in zip(cases, expected, strict=True):
            with self.subTest(code=code):
                result = self.assemble(snapshot=self.snapshot(acquisition=findings))
                self.assertFalse(result.success)
                self.assertEqual(result.failures[0].code, code)

    def test_source_kind_mapping_is_explicit(self):
        expected = {
            CandidateSourceKind.PHYSICAL_STORE: SourceType.PHYSICAL_RETAILER,
            CandidateSourceKind.ONLINE_RETAILER: SourceType.ONLINE_RETAILER,
            CandidateSourceKind.MARKETPLACE_SELLER: SourceType.ONLINE_MARKETPLACE,
            CandidateSourceKind.HUMAN_OWNED: SourceType.OTHER,
            CandidateSourceKind.OTHER: SourceType.OTHER,
        }
        for source_kind, source_type in expected.items():
            with self.subTest(source_kind=source_kind):
                result = self.assemble(candidate=self.candidate(source_kind=source_kind))
                self.assertEqual(result.request.acquisition.source_type, source_type)

    def test_estimated_price_pair_is_preferred_and_verified_pair_is_fallback(self):
        preferred = self.assemble()
        fallback = self.assemble(
            snapshot=self.snapshot(
                resale=self.resale(
                    estimated_achievable_price_low=None,
                    estimated_achievable_price_high=None,
                    verified_realized_price_low=90,
                    verified_realized_price_high=90,
                )
            )
        )

        self.assertEqual(str(preferred.request.sale.resale_price_low), "85.0")
        self.assertEqual(preferred.request.sale.resale_price_basis, InputBasis.ESTIMATED)
        self.assertEqual(fallback.request.sale.resale_price_low, fallback.request.sale.resale_price_high)
        self.assertEqual(fallback.request.sale.resale_price_basis, InputBasis.VERIFIED)

    def test_missing_partial_and_active_ask_only_prices_fail(self):
        cases = (
            self.resale(
                estimated_achievable_price_low=None,
                estimated_achievable_price_high=None,
                verified_realized_price_low=None,
                verified_realized_price_high=None,
            ),
            self.resale(estimated_achievable_price_high=None),
            self.resale(
                estimated_achievable_price_low=None,
                estimated_achievable_price_high=None,
                verified_realized_price_low=80,
                verified_realized_price_high=None,
            ),
        )
        for resale in cases:
            with self.subTest(resale=resale):
                result = self.assemble(snapshot=self.snapshot(resale=resale))
                self.assertFalse(result.success)
                self.assertEqual(result.failures[0].code, AssemblyFailureCode.RESALE_PRICE)

    def test_candidate_marketplace_wins_over_evidence_marketplace(self):
        result = self.assemble(candidate=self.candidate(destination_name="Amazon"))
        self.assertEqual(result.request.sale.marketplace_name, "Amazon")
        self.assertNotIn(
            "https://www.ebay.com/itm/example",
            result.request.sale.marketplace_references,
        )
        self.assertIn(
            "https://www.ebay.com/itm/example",
            [item.url for item in result.provenance.comparative_references],
        )

    def test_marketplace_references_are_classified_without_url_inference(self):
        ebay = self.resale_evidence(" eBaY ", "https://evidence.example/ebay")
        amazon = self.resale_evidence("Amazon", "https://evidence.example/amazon")
        walmart = self.resale_evidence("Walmart", "https://evidence.example/walmart")
        unknown = self.resale_evidence("   ", "https://evidence.example/unknown")
        result = self.assemble(
            snapshot=self.snapshot(
                resale=self.resale(evidence_items=[ebay, amazon, walmart, unknown])
            )
        )

        self.assertEqual(
            result.request.sale.marketplace_references,
            ["https://www.ebay.com", "https://evidence.example/ebay"],
        )
        self.assertEqual(
            [item.url for item in result.provenance.destination_references],
            ["https://www.ebay.com", "https://evidence.example/ebay"],
        )
        self.assertEqual(
            [item.url for item in result.provenance.comparative_references],
            ["https://evidence.example/amazon", "https://evidence.example/walmart"],
        )
        self.assertEqual(
            [item.url for item in result.provenance.unattributed_references],
            ["https://evidence.example/unknown"],
        )

    def test_candidate_destination_url_remains_when_no_evidence_items_exist(self):
        result = self.assemble(
            snapshot=self.snapshot(resale=self.resale(evidence_items=[]))
        )

        self.assertEqual(
            result.request.sale.marketplace_references,
            ["https://www.ebay.com"],
        )
        self.assertEqual(result.provenance.comparative_references, [])
        self.assertEqual(result.provenance.unattributed_references, [])

    def test_selling_cost_urls_remain_only_in_projection_provenance(self):
        result = self.assemble()
        selling_cost_url = "https://evidence.example/marketplace_fee"
        classified_urls = [
            item.url
            for references in (
                result.provenance.destination_references,
                result.provenance.comparative_references,
                result.provenance.unattributed_references,
            )
            for item in references
        ]

        self.assertNotIn(selling_cost_url, result.request.sale.marketplace_references)
        self.assertNotIn(selling_cost_url, classified_urls)
        self.assertTrue(
            any(
                reference.url == selling_cost_url
                for finding in result.provenance.projection.provenance
                for reference in finding.source_references
            )
        )

    def test_duplicate_urls_are_stably_classified_once(self):
        duplicate = "https://www.ebay.com"
        result = self.assemble(
            snapshot=self.snapshot(
                resale=self.resale(
                    evidence_items=[
                        self.resale_evidence("eBay", duplicate),
                        self.resale_evidence("eBay", duplicate),
                        self.resale_evidence("Amazon", duplicate),
                    ]
                )
            )
        )

        self.assertEqual(result.request.sale.marketplace_references, [duplicate])
        all_references = [
            *result.provenance.destination_references,
            *result.provenance.comparative_references,
            *result.provenance.unattributed_references,
        ]
        self.assertEqual([item.url for item in all_references], [duplicate])
        selection_json = result.provenance.resale_price_selection.model_dump()
        self.assertNotIn("source_urls", selection_json)
        self.assertNotIn("direct_source_urls", selection_json)

    def test_product_and_condition_conflicts_fail_closed(self):
        identity_conflict = self.resale(
            researched_identity=self.identity(model_number="DIFFERENT")
        )
        condition_conflict = self.resale(
            researched_identity=self.identity(condition=ProductCondition.USED)
        )
        for resale, code in (
            (identity_conflict, AssemblyFailureCode.IDENTITY_CONFLICT),
            (condition_conflict, AssemblyFailureCode.CONDITION_CONFLICT),
        ):
            with self.subTest(code=code):
                result = self.assemble(snapshot=self.snapshot(resale=resale))
                self.assertFalse(result.success)
                self.assertEqual(result.failures[0].code, code)

    def test_currency_mismatch_and_unsupported_currency_fail(self):
        mismatch = self.assemble(snapshot=self.snapshot(resale=self.resale(currency="CAD")))
        unsupported_purchase = self.acquisition_findings()
        unsupported_purchase.findings[0].currency = "usd"
        unsupported = self.assemble(
            snapshot=self.snapshot(acquisition=unsupported_purchase)
        )

        self.assertEqual(mismatch.failures[0].code, AssemblyFailureCode.CURRENCY_MISMATCH)
        self.assertEqual(unsupported.failures[0].code, AssemblyFailureCode.UNSUPPORTED_CURRENCY)

    def test_quantity_availability_and_limit_policy(self):
        available = self.assemble()
        self.assertEqual(available.request.acquisition.quantity_available, 1)
        self.assertEqual(
            available.request.acquisition.quantity_available_basis,
            InputBasis.HUMAN_OBSERVED,
        )
        self.assertEqual(available.request.acquisition.purchase_limit, 1)

        for changes in (
            {"quantity_available": 0},
            {"availability_status": AvailabilityStatus.OUT_OF_STOCK},
        ):
            with self.subTest(changes=changes):
                result = self.assemble(evaluation=self.evaluation(self.intake(**changes)))
                self.assertEqual(result.failures[0].code, AssemblyFailureCode.QUANTITY)

        valid = self.intake().acquisition.model_dump()
        valid["purchase_limit"] = 0
        invalid_acquisition = HumanAcquisitionInput.model_construct(**valid)
        invalid_intake = self.intake().model_copy(update={"acquisition": invalid_acquisition})
        result = self.assemble(evaluation=self.evaluation(invalid_intake))
        self.assertEqual(result.failures[0].code, AssemblyFailureCode.QUANTITY)

    def test_unknown_and_unsupported_quantity_evidence_is_omitted_not_relabelled(self):
        unknown = self.assemble(
            evaluation=self.evaluation(
                self.intake(quantity_available=None, quantity_basis=None)
            )
        )
        reported = self.assemble(
            evaluation=self.evaluation(
                self.intake(quantity_basis=HumanInputBasis.HUMAN_REPORTED)
            )
        )

        self.assertIsNone(unknown.request.acquisition.quantity_available)
        self.assertIsNone(reported.request.acquisition.quantity_available)
        reasons = [item.reason for item in reported.provenance.omitted_optional_fields]
        self.assertTrue(any("not silently relabeled" in reason for reason in reasons))

    def test_projection_preserves_costs_unknowns_assumptions_and_sensitivity(self):
        result = self.assemble()
        request = result.request

        self.assertEqual(request.acquisition.additional_costs[0].cost_id, "packaging")
        self.assertEqual(request.sale.selling_costs[0].cost_id, "marketplace_fee")
        self.assertEqual(request.sale.selling_costs[0].basis, InputBasis.ASSUMED)
        self.assertEqual(request.sale.selling_costs[1].cost_type, CostType.FIXED_PER_BATCH)
        self.assertEqual(request.sale.selling_costs[2].value, 12)
        self.assertEqual(request.sale.unknowns[0].materiality, UnknownMateriality.MATERIAL)
        self.assertEqual(request.sensitivity_inputs[0].cost_id, "shipping")
        self.assertEqual(
            _calculate_cost(request.sale.selling_costs[1], 1, Decimal("90")),
            Decimal("0.30"),
        )
        self.assertEqual(
            _calculate_cost(request.sale.selling_costs[0], 1, Decimal("100")),
            Decimal("13.5"),
        )
        self.assertEqual(
            _calculate_profitability(request).status,
            ProfitabilityStatus.INSUFFICIENT_INPUTS,
        )

    def test_inputs_are_not_mutated(self):
        candidate = self.candidate()
        evaluation = self.evaluation()
        snapshot = self.snapshot()
        before = (
            candidate.model_dump_json(),
            evaluation.model_dump_json(),
            tuple(
                artifact.payload_json
                for artifact in (
                    snapshot.sourcing_report,
                    snapshot.acquisition_cost_findings,
                    snapshot.resale,
                    snapshot.selling_cost_findings,
                )
            ),
        )

        self.assemble(candidate=candidate, evaluation=evaluation, snapshot=snapshot)

        after = (
            candidate.model_dump_json(),
            evaluation.model_dump_json(),
            tuple(
                artifact.payload_json
                for artifact in (
                    snapshot.sourcing_report,
                    snapshot.acquisition_cost_findings,
                    snapshot.resale,
                    snapshot.selling_cost_findings,
                )
            ),
        )
        self.assertEqual(after, before)

    def test_repository_snapshot_selection_and_pure_assembly_make_no_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = CandidateRepository(Path(directory) / "assembly.db")
            candidate = repository.create_candidate(
                product_identity=self.identity(),
                acquisition_source=self.candidate().acquisition_source,
                resale_destination=self.candidate().resale_destination,
                intake_origin=CandidateIntakeSource.HUMAN_PHYSICAL,
            )
            evaluation = repository.create_evaluation(
                candidate.candidate_id,
                trigger=EvaluationTrigger.HUMAN_REQUEST,
                intake_snapshot=self.intake(),
            )
            payloads = (
                (EvaluationArtifactType.SOURCING_REPORT, SourcingTextReport(report_text="old")),
                (EvaluationArtifactType.ACQUISITION_COST_FINDINGS, self.acquisition_findings()),
                (EvaluationArtifactType.RESALE, self.resale()),
                (EvaluationArtifactType.SELLING_COST_FINDINGS, self.selling_findings()),
                (EvaluationArtifactType.SOURCING_REPORT, SourcingTextReport(report_text="new")),
                (EvaluationArtifactType.ACQUISITION_COST_FINDINGS, self.acquisition_findings()),
                (EvaluationArtifactType.RESALE, self.resale()),
                (EvaluationArtifactType.SELLING_COST_FINDINGS, self.selling_findings()),
            )
            for artifact_type, payload in payloads:
                repository.append_evaluation_artifact(
                    evaluation.evaluation_id,
                    artifact_type=artifact_type,
                    payload_json=payload.model_dump_json(),
                )
            snapshot = repository.economic_evidence_snapshot(evaluation.evaluation_id)
            before = repository.list_evaluation_artifacts(evaluation.evaluation_id)

            result = assemble_profitability_request(candidate, evaluation, snapshot)

            after = repository.list_evaluation_artifacts(evaluation.evaluation_id)
            self.assertTrue(result.success)
            self.assertEqual(
                [item.sequence_number for item in result.provenance.artifact_references],
                [5, 6, 7, 8],
            )
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
