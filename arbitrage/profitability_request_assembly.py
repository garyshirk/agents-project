from enum import Enum

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from arbitrage.contracts import (
    AcquisitionScenario,
    AvailabilityStatus,
    CandidateEvaluation,
    CandidateRecord,
    CandidateSourceKind,
    EconomicCostFindingsResult,
    EvaluationArtifact,
    EvaluationArtifactType,
    HumanInputBasis,
    InputBasis,
    MatchQuality,
    ProductIdentity,
    ProfitabilityRequest,
    ResaleResult,
    SaleScenario,
    ScenarioSide,
    SourceType,
    SourcingTextReport,
)
from arbitrage.persistence import EconomicEvidenceSnapshot
from arbitrage.identity_comparison import IdentityComparisonOutcome, compare_product_identity
from arbitrage.profitability_assembly import (
    EconomicFindingProjection,
    PURCHASE_PRICE_COST_ID,
    project_economic_findings,
)


ASSEMBLY_POLICY_VERSION = "v1c-step5c2-v1"
ONE_UNIT_POLICY = "Model one acquired sellable unit and one resale order."
IDENTITY_POLICY_VERSION = "v1c-identity-acceptance-v1"


class IdentityAcceptanceDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    comparison_outcome: IdentityComparisonOutcome
    matched_fields: list[str]
    conflicting_fields: list[str]
    unresolved_fields: list[str]
    comparator_notes: list[str]
    resale_identity_match: MatchQuality
    policy_version: str = IDENTITY_POLICY_VERSION
    accepted: bool
    allowed_unresolved_fields: list[str]
    blocking_fields: list[str]
    rule_ids: list[str]
    reasons: list[str]


def evaluate_identity_acceptance(
    candidate_identity: ProductIdentity, resale_result: ResaleResult
) -> IdentityAcceptanceDecision:
    """Apply I1–I6 without interpreting prose or changing supplied identities.

    EXACT and STRONG share the same policy. Optional omissions are explicit
    exceptions, not evidence that those attributes are economically immaterial.
    """
    comparison = compare_product_identity(candidate_identity, resale_result.researched_identity)
    researched = resale_result.researched_identity
    blocking = list(comparison.conflicting_fields)
    rules = []
    reasons = []
    if blocking:
        rules.append("I1")
        reasons.append("Established structured identity contradictions block assembly.")
    core_missing = []
    if not any(field in comparison.matched_fields for field in ("model_number", "upc_gtin")):
        core_missing.extend(field for field in ("model_number", "upc_gtin") if field not in blocking)
    for field in ("condition", "package_quantity"):
        if field in comparison.unresolved_fields:
            core_missing.append(field)
    if candidate_identity.material_variant is None or researched.material_variant is None:
        core_missing.append("material_variant")
    if core_missing:
        blocking.extend(core_missing)
        rules.append("I2")
        reasons.append(f"Missing core identity evidence blocks assembly: {core_missing}.")
    left = candidate_identity.material_variant
    right = researched.material_variant
    if left is not None and right is not None:
        missing = []
        for name in ("colorway", "audience", "width", "packaging"):
            field = f"material_variant.{name}"
            if field in comparison.unresolved_fields and (getattr(left, name) or getattr(right, name)):
                missing.append(field)
        if any(getattr(side, name) for side in (left, right) for name in ("size_value", "size_system")):
            for name in ("size_value", "size_system"):
                field = f"material_variant.{name}"
                if field not in comparison.matched_fields and field not in comparison.conflicting_fields:
                    missing.append(field)
        if missing:
            blocking.extend(missing)
            rules.append("I3")
            reasons.append(f"One-sided variant attributes or incomplete sizing evidence block assembly: {missing}.")
    if resale_result.identity_match in (MatchQuality.IMPERFECT, MatchQuality.AMBIGUOUS):
        blocking.append("identity_match")
        rules.append("I5")
        reasons.append(f"Resale assessment {resale_result.identity_match.value} blocks assembly.")
    if left is None or right is None:
        rules.append("I6")
        reasons.append("Absent structured variants are not reconstructed from raw text.")
    blocking = list(dict.fromkeys(blocking))
    accepted = not blocking
    allowed = list(comparison.unresolved_fields) if accepted else []
    if accepted:
        rules.extend(["I2", "I3", "I5"])
        if allowed:
            rules.append("I4")
            reasons.append("Allowed optional omissions remain unknown; economic immateriality is not established.")
        else:
            reasons.append("Core and populated structured identity attributes agree.")
    return IdentityAcceptanceDecision(
        comparison_outcome=comparison.outcome,
        matched_fields=list(comparison.matched_fields),
        conflicting_fields=list(comparison.conflicting_fields),
        unresolved_fields=list(comparison.unresolved_fields),
        comparator_notes=list(comparison.notes),
        resale_identity_match=resale_result.identity_match,
        accepted=accepted, allowed_unresolved_fields=allowed,
        blocking_fields=blocking, rule_ids=list(dict.fromkeys(rules)), reasons=reasons,
    )


class AssemblyFailureCode(str, Enum):
    RECORD_MISMATCH = "RECORD_MISMATCH"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    INVALID_ARTIFACT = "INVALID_ARTIFACT"
    PURCHASE_PRICE = "PURCHASE_PRICE"
    UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    CONDITION_CONFLICT = "CONDITION_CONFLICT"
    INSUFFICIENT_IDENTITY_EVIDENCE = "INSUFFICIENT_IDENTITY_EVIDENCE"
    RESALE_IDENTITY_IMPERFECT = "RESALE_IDENTITY_IMPERFECT"
    RESALE_PRICE = "RESALE_PRICE"
    QUANTITY = "QUANTITY"
    PROJECTION = "PROJECTION"
    INVALID_REQUEST = "INVALID_REQUEST"


class AssemblyFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: AssemblyFailureCode
    message: str
    identity_decision: IdentityAcceptanceDecision | None = None


class ArtifactProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_type: EvaluationArtifactType
    artifact_id: int
    sequence_number: int


class OmittedOptionalField(BaseModel):
    model_config = ConfigDict(frozen=True)

    field_path: str
    reason: str


class ResalePriceSelection(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy: str
    selected_source: str
    artifact_id: int
    sequence_number: int
    selected_fields: list[str]
    basis: InputBasis
    url_attribution: str
    limitations: list[str]


class MarketplaceResearchReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    marketplace: str | None
    origin: str


class ProfitabilityRequestProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluation_id: str
    artifact_references: list[ArtifactProvenance]
    projection_policy_version: str
    scenario_policy: str
    resale_price_selection: ResalePriceSelection
    projection: EconomicFindingProjection
    acquisition_source_urls: list[str]
    destination_references: list[MarketplaceResearchReference]
    comparative_references: list[MarketplaceResearchReference]
    unattributed_references: list[MarketplaceResearchReference]
    research_limitations: list[str]
    omitted_optional_fields: list[OmittedOptionalField]
    identity_decision: IdentityAcceptanceDecision | None = None


class ProfitabilityRequestAssemblyResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    success: bool
    request: ProfitabilityRequest | None
    provenance: ProfitabilityRequestProvenance | None
    failures: list[AssemblyFailure]

    @model_validator(mode="after")
    def validate_outcome(self) -> "ProfitabilityRequestAssemblyResult":
        if self.success:
            if self.request is None or self.provenance is None or self.failures:
                raise ValueError("successful assembly requires request and provenance only")
        elif self.request is not None or self.provenance is not None or not self.failures:
            raise ValueError("failed assembly requires failures and no request or provenance")
        return self


SOURCE_TYPE_BY_KIND = {
    CandidateSourceKind.PHYSICAL_STORE: SourceType.PHYSICAL_RETAILER,
    CandidateSourceKind.ONLINE_RETAILER: SourceType.ONLINE_RETAILER,
    CandidateSourceKind.MARKETPLACE_SELLER: SourceType.ONLINE_MARKETPLACE,
    CandidateSourceKind.HUMAN_OWNED: SourceType.OTHER,
    CandidateSourceKind.OTHER: SourceType.OTHER,
}


def _failure(
    code: AssemblyFailureCode, message: str,
    identity_decision: IdentityAcceptanceDecision | None = None,
) -> ProfitabilityRequestAssemblyResult:
    return ProfitabilityRequestAssemblyResult(
        success=False,
        request=None,
        provenance=None,
        failures=[AssemblyFailure(code=code, message=message, identity_decision=identity_decision)],
    )


def _artifact_reference(artifact: EvaluationArtifact) -> ArtifactProvenance:
    return ArtifactProvenance(
        artifact_type=artifact.artifact_type,
        artifact_id=artifact.artifact_id,
        sequence_number=artifact.sequence_number,
    )


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _deduplicate_references(
    references: list[MarketplaceResearchReference],
    seen: set[str],
) -> list[MarketplaceResearchReference]:
    result: list[MarketplaceResearchReference] = []
    for reference in references:
        if reference.url not in seen:
            seen.add(reference.url)
            result.append(reference)
    return result


def _classify_resale_references(candidate: CandidateRecord, resale: ResaleResult):
    destination_name = candidate.resale_destination.name.strip()
    destination_key = destination_name.casefold()
    destination: list[MarketplaceResearchReference] = []
    comparative: list[MarketplaceResearchReference] = []
    unattributed: list[MarketplaceResearchReference] = []
    if candidate.resale_destination.url:
        destination.append(
            MarketplaceResearchReference(
                url=candidate.resale_destination.url,
                marketplace=destination_name,
                origin="Candidate resale destination",
            )
        )
    for item in resale.evidence_items:
        if not item.url:
            continue
        marketplace = item.marketplace.strip()
        reference = MarketplaceResearchReference(
            url=item.url,
            marketplace=marketplace or None,
            origin="Resale evidence item",
        )
        if not marketplace:
            unattributed.append(reference)
        elif marketplace.casefold() == destination_key:
            destination.append(reference)
        else:
            comparative.append(reference)
    seen: set[str] = set()
    return (
        _deduplicate_references(destination, seen),
        _deduplicate_references(comparative, seen),
        _deduplicate_references(unattributed, seen),
    )


def _parse_artifacts(snapshot: EconomicEvidenceSnapshot):
    artifacts = (
        snapshot.sourcing_report,
        snapshot.acquisition_cost_findings,
        snapshot.resale,
        snapshot.selling_cost_findings,
    )
    if any(artifact is None for artifact in artifacts):
        return None, _failure(
            AssemblyFailureCode.MISSING_EVIDENCE,
            "SOURCING_REPORT, ACQUISITION_COST_FINDINGS, RESALE, and "
            "SELLING_COST_FINDINGS are all required",
        )
    sourcing, acquisition, resale, selling = artifacts
    assert sourcing is not None and acquisition is not None
    assert resale is not None and selling is not None
    expected_types = (
        EvaluationArtifactType.SOURCING_REPORT,
        EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
        EvaluationArtifactType.RESALE,
        EvaluationArtifactType.SELLING_COST_FINDINGS,
    )
    if any(
        artifact.evaluation_id != snapshot.evaluation_id
        or artifact.artifact_type != expected_type
        for artifact, expected_type in zip(artifacts, expected_types, strict=True)
    ):
        return None, _failure(
            AssemblyFailureCode.RECORD_MISMATCH,
            "Evidence artifacts must have the expected types and belong to the snapshot Evaluation",
        )
    if not snapshot.acquisition_ready or not snapshot.selling_ready:
        return None, _failure(
            AssemblyFailureCode.STALE_EVIDENCE,
            "Each economic capture must follow its latest substantive research artifact",
        )
    try:
        sourcing_report = SourcingTextReport.model_validate_json(sourcing.payload_json)
        acquisition_findings = EconomicCostFindingsResult.model_validate_json(
            acquisition.payload_json
        )
        resale_result = ResaleResult.model_validate_json(resale.payload_json)
        selling_findings = EconomicCostFindingsResult.model_validate_json(
            selling.payload_json
        )
    except (ValidationError, ValueError, TypeError) as error:
        return None, _failure(
            AssemblyFailureCode.INVALID_ARTIFACT,
            f"Economic evidence artifact payload is invalid: {error}",
        )
    return (
        artifacts,
        sourcing_report,
        acquisition_findings,
        resale_result,
        selling_findings,
    ), None


def _select_resale_prices(resale: ResaleResult):
    estimated = (
        resale.estimated_achievable_price_low,
        resale.estimated_achievable_price_high,
    )
    verified = (
        resale.verified_realized_price_low,
        resale.verified_realized_price_high,
    )
    if (estimated[0] is None) != (estimated[1] is None):
        return None, _failure(
            AssemblyFailureCode.RESALE_PRICE,
            "estimated achievable resale price requires a complete low/high pair",
        )
    if estimated[0] is not None:
        return (
            estimated[0],
            estimated[1],
            InputBasis.ESTIMATED,
            ["estimated_achievable_price_low", "estimated_achievable_price_high"],
        ), None
    if (verified[0] is None) != (verified[1] is None):
        return None, _failure(
            AssemblyFailureCode.RESALE_PRICE,
            "verified realized resale price requires a complete low/high pair",
        )
    if verified[0] is not None:
        return (
            verified[0],
            verified[1],
            InputBasis.VERIFIED,
            ["verified_realized_price_low", "verified_realized_price_high"],
        ), None
    return None, _failure(
        AssemblyFailureCode.RESALE_PRICE,
        "No complete estimated achievable or verified realized resale price pair is available",
    )


def assemble_profitability_request(
    candidate: CandidateRecord,
    evaluation: CandidateEvaluation,
    evidence: EconomicEvidenceSnapshot,
) -> ProfitabilityRequestAssemblyResult:
    """Build one auditable Profitability request from already-loaded evidence."""
    if evaluation.candidate_id != candidate.candidate_id:
        return _failure(
            AssemblyFailureCode.RECORD_MISMATCH,
            "Candidate and Evaluation identifiers do not match",
        )
    if evidence.evaluation_id != evaluation.evaluation_id:
        return _failure(
            AssemblyFailureCode.RECORD_MISMATCH,
            "Economic evidence snapshot does not belong to the supplied Evaluation",
        )

    parsed, parsing_failure = _parse_artifacts(evidence)
    if parsing_failure is not None:
        return parsing_failure
    assert parsed is not None
    artifacts, _, acquisition_findings, resale, selling_findings = parsed

    purchase_matches = [
        finding
        for finding in acquisition_findings.findings
        if finding.cost_id == PURCHASE_PRICE_COST_ID
    ]
    if len(purchase_matches) != 1:
        return _failure(
            AssemblyFailureCode.PURCHASE_PRICE,
            f"Exactly one {PURCHASE_PRICE_COST_ID} finding is required",
        )
    acquisition_currency = purchase_matches[0].currency
    if (
        acquisition_currency is None
        or len(acquisition_currency) != 3
        or not acquisition_currency.isalpha()
        or acquisition_currency.upper() != acquisition_currency
    ):
        return _failure(
            AssemblyFailureCode.UNSUPPORTED_CURRENCY,
            "Purchase price requires an explicit three-letter uppercase currency",
        )
    if (
        resale.currency is None
        or len(resale.currency) != 3
        or not resale.currency.isalpha()
        or resale.currency.upper() != resale.currency
    ):
        return _failure(
            AssemblyFailureCode.UNSUPPORTED_CURRENCY,
            "Resale evidence requires an explicit three-letter uppercase currency",
        )
    if acquisition_currency != resale.currency:
        return _failure(
            AssemblyFailureCode.CURRENCY_MISMATCH,
            f"Acquisition currency {acquisition_currency} does not match resale currency {resale.currency}",
        )

    identity_decision = evaluate_identity_acceptance(candidate.product_identity, resale)
    if "condition" in identity_decision.conflicting_fields:
        return _failure(
            AssemblyFailureCode.CONDITION_CONFLICT,
            "Candidate and Resale researched conditions conflict",
            identity_decision,
        )
    if identity_decision.conflicting_fields:
        return _failure(
            AssemblyFailureCode.IDENTITY_CONFLICT,
            f"Candidate and Resale identities materially conflict: {identity_decision.conflicting_fields}",
            identity_decision,
        )
    if not identity_decision.accepted:
        code = (
            AssemblyFailureCode.RESALE_IDENTITY_IMPERFECT
            if resale.identity_match == MatchQuality.IMPERFECT
            else AssemblyFailureCode.INSUFFICIENT_IDENTITY_EVIDENCE
        )
        return _failure(code, "; ".join(identity_decision.reasons), identity_decision)

    selected_prices, price_failure = _select_resale_prices(resale)
    if price_failure is not None:
        return price_failure
    assert selected_prices is not None
    resale_low, resale_high, resale_basis, selected_fields = selected_prices

    projection = project_economic_findings(
        acquisition_findings,
        selling_findings,
        currency=acquisition_currency,
    )
    if not projection.valid:
        return _failure(
            AssemblyFailureCode.PROJECTION,
            "; ".join(projection.failure_reasons),
        )
    assert projection.unit_purchase_price is not None
    assert projection.purchase_price_basis is not None

    omitted: list[OmittedOptionalField] = [
        OmittedOptionalField(
            field_path="sale.resale_price_expected",
            reason="Policy preserves evidence bounds and does not calculate a midpoint.",
        ),
        OmittedOptionalField(
            field_path="acquisition.purchase_requirements",
            reason="No typed purchase-requirements field is available; Sourcing prose is not parsed.",
        ),
    ]
    quantity_available = None
    quantity_available_basis = None
    purchase_limit = None
    intake_acquisition = (
        evaluation.intake_snapshot.acquisition
        if evaluation.intake_snapshot is not None
        else None
    )
    if intake_acquisition is None:
        omitted.append(
            OmittedOptionalField(
                field_path="acquisition.quantity_available",
                reason="No typed acquisition intake evidence is available.",
            )
        )
        omitted.append(
            OmittedOptionalField(
                field_path="acquisition.purchase_limit",
                reason="No typed acquisition intake evidence is available.",
            )
        )
    else:
        if (
            intake_acquisition.quantity_available == 0
            or intake_acquisition.purchase_limit == 0
            or intake_acquisition.availability_status == AvailabilityStatus.OUT_OF_STOCK
        ):
            return _failure(
                AssemblyFailureCode.QUANTITY,
                "Typed acquisition evidence makes a one-unit scenario impossible",
            )
        if intake_acquisition.quantity_available is None:
            omitted.append(
                OmittedOptionalField(
                    field_path="acquisition.quantity_available",
                    reason="Typed intake does not establish inventory quantity.",
                )
            )
        elif intake_acquisition.quantity_basis == HumanInputBasis.HUMAN_OBSERVED:
            quantity_available = intake_acquisition.quantity_available
            quantity_available_basis = InputBasis.HUMAN_OBSERVED
        else:
            omitted.append(
                OmittedOptionalField(
                    field_path="acquisition.quantity_available",
                    reason=(
                        "Typed quantity evidence uses an unsupported human basis and was not "
                        "silently relabeled."
                    ),
                )
            )
        if intake_acquisition.purchase_limit is None:
            omitted.append(
                OmittedOptionalField(
                    field_path="acquisition.purchase_limit",
                    reason="Typed intake does not establish a purchase limit.",
                )
            )
        else:
            purchase_limit = intake_acquisition.purchase_limit

    resale_artifact = artifacts[2]
    (
        destination_references,
        comparative_references,
        unattributed_references,
    ) = _classify_resale_references(candidate, resale)
    destination_urls = [
        reference.url for reference in destination_references
    ]
    acquisition_urls = _deduplicate(
        [
            candidate.acquisition_source.url or "",
            *(
                reference.url
                for item in projection.provenance
                if item.scenario_side == ScenarioSide.ACQUISITION
                for reference in item.source_references
            ),
        ]
    )
    limitations = _deduplicate(
        [
            *resale.unresolved_issues,
            *(limitation for item in resale.evidence_items for limitation in item.limitations),
            *(limitation for item in projection.provenance for limitation in item.limitations),
        ]
    )
    acquisition_assumptions = [
        assumption
        for assumption in projection.assumptions
        if assumption.startswith("ACQUISITION/")
    ]
    sale_assumptions = [
        assumption for assumption in projection.assumptions if assumption.startswith("SALE/")
    ]
    acquisition_assumptions.append(ONE_UNIT_POLICY)
    sale_assumptions.append(ONE_UNIT_POLICY)

    try:
        request = ProfitabilityRequest(
            acquisition=AcquisitionScenario(
                product_identity=candidate.product_identity.model_copy(deep=True),
                source_type=SOURCE_TYPE_BY_KIND[candidate.acquisition_source.source_kind],
                source_name=candidate.acquisition_source.name,
                source_references=acquisition_urls,
                condition=candidate.product_identity.condition,
                currency=projection.currency,
                unit_purchase_price=projection.unit_purchase_price,
                purchase_price_basis=projection.purchase_price_basis,
                quantity=1,
                additional_costs=[
                    item.model_copy(deep=True)
                    for item in projection.additional_acquisition_costs
                ],
                quantity_available=quantity_available,
                quantity_available_basis=quantity_available_basis,
                purchase_limit=purchase_limit,
                purchase_requirements=[],
                assumptions=acquisition_assumptions,
                unknowns=[
                    item.model_copy(deep=True) for item in projection.acquisition_unknowns
                ],
                notes=None,
            ),
            sale=SaleScenario(
                marketplace_name=candidate.resale_destination.name,
                marketplace_references=destination_urls,
                currency=projection.currency,
                target_condition=candidate.product_identity.condition,
                resale_price_low=resale_low,
                resale_price_expected=None,
                resale_price_high=resale_high,
                resale_price_basis=resale_basis,
                selling_costs=[
                    item.model_copy(deep=True) for item in projection.selling_costs
                ],
                quantity=1,
                assumptions=sale_assumptions,
                unknowns=[item.model_copy(deep=True) for item in projection.selling_unknowns],
                notes=resale.notes,
            ),
            sensitivity_inputs=[
                item.model_copy(deep=True) for item in projection.sensitivity_inputs
            ],
        )
    except (ValidationError, ValueError, TypeError, KeyError) as error:
        return _failure(
            AssemblyFailureCode.INVALID_REQUEST,
            f"Assembled ProfitabilityRequest is invalid: {error}",
        )

    price_selection = ResalePriceSelection(
        policy=(
            "Prefer complete estimated achievable bounds; otherwise use complete verified "
            "realized bounds; never derive prices from active asks."
        ),
        selected_source="Aggregate price fields in the selected RESALE artifact.",
        artifact_id=resale_artifact.artifact_id,
        sequence_number=resale_artifact.sequence_number,
        selected_fields=selected_fields,
        basis=resale_basis,
        url_attribution=(
            "No evidence-item URL is structurally linked to the selected aggregate bounds."
        ),
        limitations=_deduplicate(
            [
                *resale.unresolved_issues,
                resale.evidence_quality_reason,
                "ResaleResult does not link aggregate price bounds to individual evidence items.",
            ]
        ),
    )
    provenance = ProfitabilityRequestProvenance(
        evaluation_id=evaluation.evaluation_id,
        artifact_references=[_artifact_reference(item) for item in artifacts],
        projection_policy_version=ASSEMBLY_POLICY_VERSION,
        scenario_policy=ONE_UNIT_POLICY,
        resale_price_selection=price_selection,
        projection=projection,
        acquisition_source_urls=acquisition_urls,
        destination_references=destination_references,
        comparative_references=comparative_references,
        unattributed_references=unattributed_references,
        research_limitations=limitations,
        omitted_optional_fields=omitted,
        identity_decision=identity_decision,
    )
    return ProfitabilityRequestAssemblyResult(
        success=True,
        request=request,
        provenance=provenance,
        failures=[],
    )
