"""Internal persistence contracts, not model-output or commercial-action schemas."""

from enum import Enum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from arbitrage.contracts import CostType, InputBasis, NonNegativeDecimalString, ProfitabilityRequest, ProfitabilityResult


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class SubmissionKind(str, Enum):
    HYPOTHETICAL = "HYPOTHETICAL"
    HUMAN_REPORTED = "HUMAN_REPORTED"
    QUALIFIED = "QUALIFIED"
    UNSPECIFIED = "UNSPECIFIED"


class ExecutionState(str, Enum):
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AssessmentState(str, Enum):
    EVIDENCE_BASED_ESTIMATE = "EVIDENCE_BASED_ESTIMATE"
    CONDITIONAL_ESTIMATE = "CONDITIONAL_ESTIMATE"
    INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"


class ReviewDecision(str, Enum):
    NEEDS_MORE_EVIDENCE = "NEEDS_MORE_EVIDENCE"
    WORTH_INVESTIGATING = "WORTH_INVESTIGATING"
    REJECTED = "REJECTED"
    REVIEWED_INCONCLUSIVE = "REVIEWED_INCONCLUSIVE"


class KnowledgeSource(str, Enum):
    MODEL_GENERAL_KNOWLEDGE = "MODEL_GENERAL_KNOWLEDGE"
    CURRENT_EVALUATION_EVIDENCE = "CURRENT_EVALUATION_EVIDENCE"
    HISTORICAL_EVALUATIONS = "HISTORICAL_EVALUATIONS"
    HUMAN_PROVIDED = "HUMAN_PROVIDED"


class InformationKind(str, Enum):
    AGENT_ASSUMPTION = "AGENT_ASSUMPTION"
    HUMAN_ASSUMPTION = "HUMAN_ASSUMPTION"
    HUMAN_CORRECTION = "HUMAN_CORRECTION"
    REPORTED_INFORMATION = "REPORTED_INFORMATION"
    OBSERVED_COST = "OBSERVED_COST"
    ACTUAL_TRANSACTION_OUTCOME = "ACTUAL_TRANSACTION_OUTCOME"


class Producer(Contract):
    kind: Literal["HUMAN", "AGENT", "APPLICATION"]
    name: Text


class ArtifactRef(Contract):
    opportunity_id: Text
    evaluation_id: Text
    artifact_id: Text
    sequence: int = Field(ge=1)


class SupportingRecord(Contract):
    source: KnowledgeSource
    artifact: ArtifactRef


class Provenance(Contract):
    information_kind: InformationKind
    sources: tuple[KnowledgeSource, ...] = Field(min_length=1)
    rationale: Text
    supporting_records: tuple[SupportingRecord, ...] = ()

    @model_validator(mode="after")
    def validate_sources(self):
        if len(self.sources) != len(set(self.sources)):
            raise ValueError("knowledge sources must be unique")
        if any(record.source not in self.sources for record in self.supporting_records):
            raise ValueError("supporting record source must be declared")
        if any(record.source == KnowledgeSource.MODEL_GENERAL_KNOWLEDGE for record in self.supporting_records):
            raise ValueError("general model knowledge is not a persisted evidence record")
        if (self.information_kind in {InformationKind.HUMAN_ASSUMPTION, InformationKind.HUMAN_CORRECTION}
                and KnowledgeSource.HUMAN_PROVIDED not in self.sources):
            raise ValueError("human assumptions/corrections require human provenance")
        if (self.information_kind in {InformationKind.OBSERVED_COST, InformationKind.ACTUAL_TRANSACTION_OUTCOME}
                and set(self.sources) == {KnowledgeSource.MODEL_GENERAL_KNOWLEDGE}):
            raise ValueError("model general knowledge alone is not an observation or transaction outcome")
        return self


class OpportunitySubmission(Contract):
    schema_version: Literal["v2-submission-v1"] = "v2-submission-v1"
    original_text: str = Field(min_length=1)
    scenario_kind: SubmissionKind
    human_actor: Text
    qualifications: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def preserve_original(self):
        if not self.original_text.strip():
            raise ValueError("original submission must not be blank")
        return self


class Opportunity(Contract):
    schema_version: Literal["v2-opportunity-v1"] = "v2-opportunity-v1"
    opportunity_id: Text
    submission: OpportunitySubmission
    created_at: AwareDatetime


class Evaluation(Contract):
    schema_version: Literal["v2-evaluation-v1"] = "v2-evaluation-v1"
    evaluation_id: Text
    opportunity_id: Text
    execution_state: ExecutionState
    created_at: AwareDatetime
    updated_at: AwareDatetime
    predecessor_evaluation_id: Text | None = None
    current_summary: ArtifactRef | None = None
    current_review_subject: ArtifactRef

    @model_validator(mode="after")
    def validate_ownership(self):
        if self.updated_at < self.created_at:
            raise ValueError("updated timestamp precedes creation")
        for reference in (self.current_summary, self.current_review_subject):
            if reference and (reference.evaluation_id != self.evaluation_id
                              or reference.opportunity_id != self.opportunity_id):
                raise ValueError("Evaluation references must have exact ownership")
        return self


class EconomicFact(Contract):
    name: Text
    value: Text
    provenance: Provenance

    @model_validator(mode="after")
    def not_an_assumption(self):
        if self.provenance.information_kind in {InformationKind.AGENT_ASSUMPTION, InformationKind.HUMAN_ASSUMPTION}:
            raise ValueError("assumptions belong in the explicit assumptions collection")
        return self


class UnknownEconomicInput(Contract):
    name: Text
    reason: Text


class EconomicAssumption(Contract):
    name: Text
    explanation: Text
    provenance: Provenance
    amount: NonNegativeDecimalString | None = None
    unit: Text | None = None

    @model_validator(mode="after")
    def explicitly_assumed(self):
        if self.provenance.information_kind not in {
            InformationKind.AGENT_ASSUMPTION, InformationKind.HUMAN_ASSUMPTION, InformationKind.HUMAN_CORRECTION,
        }:
            raise ValueError("an assumption cannot be promoted to an observed cost or transaction outcome")
        if (self.amount is None) != (self.unit is None):
            raise ValueError("numerical assumptions require both amount and unit")
        return self


class SummaryDraft(Contract):
    assessment_state: AssessmentState
    state_decision_rationale: Text
    consequential_uncertainties: tuple[Text, ...]
    known_economic_facts: tuple[EconomicFact, ...]
    unknown_economic_inputs: tuple[UnknownEconomicInput, ...]
    assumptions: tuple[EconomicAssumption, ...]
    limitations: tuple[Text, ...]
    evidence_references: tuple[ArtifactRef, ...] = ()
    calculation_references: tuple[ArtifactRef, ...] = ()
    policy_version: Text
    # Explicit numerical claims, unlike a qualitative label, require a calculation.
    numerical_estimate: bool = False


class EconomicAssessmentSummary(SummaryDraft):
    schema_version: Literal["v2-economic-summary-v1"] = "v2-economic-summary-v1"
    revision: int = Field(ge=1)
    supersedes: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_revision(self):
        if (self.revision == 1) != (self.supersedes is None):
            raise ValueError("revised summaries require a predecessor; first summaries must not have one")
        return self


class PartialReviewSubject(Contract):
    schema_version: Literal["v2-partial-subject-v1"] = "v2-partial-subject-v1"
    label: Literal["PARTIAL_OR_UNFINISHED"] = "PARTIAL_OR_UNFINISHED"
    reason: Text
    execution_state: ExecutionState
    covered_through_sequence: int = Field(ge=0)
    supersedes_subject: ArtifactRef | None = None


class FailureRecord(Contract):
    schema_version: Literal["v2-failure-v1"] = "v2-failure-v1"
    stage: Text
    category: Text
    message: Text


class AssumptionRecord(Contract):
    schema_version: Literal["v2-assumption-v1"] = "v2-assumption-v1"
    assumption: EconomicAssumption


class ProductAttribute(Contract):
    name: Text
    value: Text | None = None


class ComparableProduct(Contract):
    description: Text
    brand: Text | None = None
    model_identifiers: tuple[ProductAttribute, ...] = ()
    variant_attributes: tuple[ProductAttribute, ...] = ()
    condition: Text | None = None
    packaging: Text | None = None
    package_quantity: int | None = Field(default=None, ge=1)
    bundle_contents: tuple[Text, ...] = ()
    other_attributes: tuple[ProductAttribute, ...] = ()


class AttributeComparison(Contract):
    attribute: Text
    candidate_value: Text | None = None
    comparable_value: Text | None = None
    relation: Literal["SIMILARITY", "KNOWN_DIFFERENCE", "UNRESOLVED"]
    explanation: Text


class PriceEvidenceType(str, Enum):
    COMPLETED_SALE = "COMPLETED_SALE"
    ACTIVE_ASK = "ACTIVE_ASK"
    PUBLISHED_REFERENCE = "PUBLISHED_REFERENCE"
    HUMAN_REPORTED = "HUMAN_REPORTED"
    OTHER_QUALIFIED = "OTHER_QUALIFIED"


class PriceEvidence(Contract):
    evidence_type: PriceEvidenceType
    amount: NonNegativeDecimalString | None = None
    currency: str | None = None
    quantity_or_unit_basis: Text | None = None
    shipping_treatment: Literal["INCLUDED", "EXCLUDED", "UNKNOWN", "NOT_APPLICABLE"] = "UNKNOWN"
    shipping_amount: NonNegativeDecimalString | None = None
    marketplace_or_channel: Text | None = None
    transaction_at: AwareDatetime | None = None
    completion_basis: Text | None = None
    realized_price_basis: Text | None = None
    context: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def preserve_evidence_type(self):
        if self.currency is not None and (len(self.currency) != 3 or not self.currency.isascii()
                                        or not self.currency.isalpha() or not self.currency.isupper()):
            raise ValueError("currency must be an explicit three-letter uppercase code")
        if (self.amount is not None or self.shipping_amount is not None) and self.currency is None:
            raise ValueError("a numerical price or shipping amount requires currency")
        if self.evidence_type == PriceEvidenceType.COMPLETED_SALE:
            if not self.completion_basis or (self.amount is not None and not self.realized_price_basis):
                raise ValueError("completed-sale claims require completion support and, for a price, realized-price support")
        elif any(value is not None for value in (self.completion_basis, self.realized_price_basis)):
            raise ValueError("non-transaction price evidence must not carry realized-sale assertions")
        if (self.transaction_at is not None and self.evidence_type in {
                PriceEvidenceType.ACTIVE_ASK, PriceEvidenceType.PUBLISHED_REFERENCE}):
            raise ValueError("asking/reference evidence must not assert a transaction date")
        return self


class ComparableSource(Contract):
    source_category: Literal["MARKETPLACE", "RETAILER", "MANUFACTURER", "PRICE_GUIDANCE", "HUMAN", "OTHER", "UNKNOWN"]
    source_name: Text | None = None
    seller_name: Text | None = None
    url: Text | None = None
    other_reference: Text | None = None
    capture_method: Literal["PUBLIC_WEB_REPORT", "HUMAN_REPORT", "SYNTHETIC_FIXTURE", "OTHER"]
    observed_at: AwareDatetime | None = None
    raw_artifact_references: tuple[ArtifactRef, ...] = ()
    limitations: tuple[Text, ...] = ()
    application_verified: Literal[False] = False

    @model_validator(mode="after")
    def validate_url_shape_only(self):
        if self.url is not None:
            try:
                parsed = urlsplit(self.url)
                parsed.port
                valid = (parsed.scheme in {"https", "http"} and parsed.hostname
                         and not parsed.username and not parsed.password
                         and not any(char.isspace() for char in self.url))
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("source URL must be an HTTP(S) URL without credentials; shape is not verification")
        return self


class RawResearchReport(Contract):
    schema_version: Literal["v2-raw-report-v1"] = "v2-raw-report-v1"
    report_text: str = Field(min_length=1)
    limitations: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def preserve_raw_text(self):
        if not self.report_text.strip():
            raise ValueError("raw report must not be blank")
        return self


class ComparableDraft(Contract):
    product: ComparableProduct
    price_evidence: PriceEvidence
    source: ComparableSource
    product_comparisons: tuple[AttributeComparison, ...] = ()
    limitations: tuple[Text, ...] = ()


class ResaleComparable(ComparableDraft):
    schema_version: Literal["v2-comparable-v1"] = "v2-comparable-v1"
    comparable_id: Text
    opportunity_id: Text
    evaluation_id: Text
    revision: int = Field(ge=1)
    supersedes: ArtifactRef | None = None
    captured_at: AwareDatetime

    @model_validator(mode="after")
    def revision_requires_predecessor(self):
        if (self.revision == 1) != (self.supersedes is None):
            raise ValueError("comparable revision must have the appropriate predecessor")
        return self


class ComparabilityCategory(str, Enum):
    EXACT_OR_STRONGLY_SUPPORTED = "EXACT_OR_STRONGLY_SUPPORTED"
    CLOSE_WITH_KNOWN_DIFFERENCES = "CLOSE_WITH_KNOWN_DIFFERENCES"
    WEAK = "WEAK"
    NOT_MEANINGFULLY_COMPARABLE = "NOT_MEANINGFULLY_COMPARABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class AssessmentDraft(Contract):
    candidate_context: ArtifactRef
    candidate_identity: ComparableProduct
    category: ComparabilityCategory
    explanation: Text
    product_comparisons: tuple[AttributeComparison, ...] = ()
    material_known_differences: tuple[Text, ...] = ()
    unresolved_questions: tuple[Text, ...] = ()
    evidence_references: tuple[ArtifactRef, ...] = ()
    provenance: Provenance
    policy_version: Text


class ComparableAssessment(AssessmentDraft):
    schema_version: Literal["v2-comparable-assessment-v1"] = "v2-comparable-assessment-v1"
    assessment_id: Text
    comparable: ArtifactRef
    opportunity_id: Text
    evaluation_id: Text
    revision: int = Field(ge=1)
    supersedes: ArtifactRef | None = None
    assessed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_binding(self):
        if (self.revision == 1) != (self.supersedes is None):
            raise ValueError("assessment revision must have the appropriate predecessor")
        for reference in (self.comparable, self.candidate_context, *self.evidence_references):
            if reference.evaluation_id != self.evaluation_id or reference.opportunity_id != self.opportunity_id:
                raise ValueError("assessment references must belong to this exact Evaluation")
        return self


class EconomicInput(Contract):
    """Explicit value/disposition for one named economic component; no text parsing."""
    input_id: Text
    category: Literal["PURCHASE_PRICE", "RESALE_PRICE", "MARKETPLACE_FEE", "PAYMENT_FEE",
                      "SELLER_SHIPPING", "ACQUISITION_TAX", "ACQUISITION_TRAVEL", "OTHER"]
    side: Literal["ACQUISITION", "SALE"]
    disposition: Literal["EVIDENCE", "ASSUMPTION", "UNKNOWN", "NOT_APPLICABLE", "INCLUDED"]
    amount: NonNegativeDecimalString | None = None
    currency: Text | None = None
    cost_type: CostType
    basis: InputBasis | None = None
    provenance: Provenance
    assumption: ArtifactRef | None = None
    evidence_references: tuple[ArtifactRef, ...] = ()
    material: bool = True
    rationale: Text
    limitations: tuple[Text, ...] = ()
    covered_by: Text | None = None
    covers: tuple[Text, ...] = ()

    @field_validator("amount", mode="before")
    @classmethod
    def no_float_money(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("monetary input must use Decimal or decimal text, not float/bool")
        return value

    @model_validator(mode="after")
    def explicit_disposition(self):
        numeric = self.disposition in {"EVIDENCE", "ASSUMPTION"}
        if numeric != (self.amount is not None) or numeric != (self.basis is not None):
            raise ValueError("numeric dispositions require amount and basis; other dispositions forbid them")
        assumed = self.provenance.information_kind in {
            InformationKind.AGENT_ASSUMPTION, InformationKind.HUMAN_ASSUMPTION, InformationKind.HUMAN_CORRECTION}
        if self.disposition == "ASSUMPTION":
            if self.assumption is None or self.basis != InputBasis.ASSUMED or not assumed:
                raise ValueError("assumptions require exact assumption artifact and ASSUMED basis/provenance")
        elif self.assumption is not None:
            raise ValueError("only explicit assumptions may cite an assumption as their value")
        if self.disposition == "EVIDENCE" and (assumed or self.basis == InputBasis.ASSUMED):
            raise ValueError("assumptions must not be relabeled as evidence")
        if self.disposition == "EVIDENCE" and not self.evidence_references:
            raise ValueError("evidence amounts require exact persisted evidence")
        if (self.disposition == "INCLUDED") != (self.covered_by is not None):
            raise ValueError("included costs require an explicit covering component only")
        return self


class ScenarioDraft(Contract):
    name: Text
    currency: Text
    quantity: int = Field(ge=1, strict=True)
    order_count: int = Field(default=1, ge=1, strict=True)
    unit_description: Text
    product_description: Text
    destination: Text
    inputs: tuple[EconomicInput, ...]
    comparable_references: tuple[ArtifactRef, ...] = ()
    assessment_references: tuple[ArtifactRef, ...] = ()
    evidence_references: tuple[ArtifactRef, ...] = ()
    known_unknowns: tuple[UnknownEconomicInput, ...] = ()
    limitations: tuple[Text, ...] = ()
    policy_version: Text
    fee_relationship: Literal["INTEGRATED", "SEPARATE", "UNRESOLVED"] | None = None
    fee_relationship_rationale: Text | None = None


class EconomicScenario(ScenarioDraft):
    schema_version: Literal["v2-economic-scenario-v1"] = "v2-economic-scenario-v1"
    scenario_id: Text
    opportunity_id: Text
    evaluation_id: Text
    revision: int = Field(ge=1)
    supersedes: ArtifactRef | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def revision_binding(self):
        if (self.revision == 1) != (self.supersedes is None):
            raise ValueError("scenario revisions require an exact predecessor")
        return self


class ComputationStatus(str, Enum):
    CALCULATED = "CALCULATED"
    INSUFFICIENT_INPUTS = "INSUFFICIENT_INPUTS"
    INVALID_INPUTS = "INVALID_INPUTS"
    ERROR = "ERROR"


class ScenarioAssembly(Contract):
    status: Literal["READY", "INSUFFICIENT_INPUTS", "INVALID_INPUTS"]
    request_json: str | None = None
    unresolved_inputs: tuple[Text, ...] = ()
    validation_errors: tuple[Text, ...] = ()
    unsupported_mappings: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def valid_request(self):
        if (self.status == "READY") != (self.request_json is not None):
            raise ValueError("only ready assembly may contain an engine request")
        if self.request_json is not None:
            ProfitabilityRequest.model_validate_json(self.request_json)
        return self


class CalculationOutcome(Contract):
    status: ComputationStatus
    # Canonical JSON snapshots avoid mutable nested V1C models in frozen artifacts.
    request_json: str | None = None
    engine_output_json: str | None = None
    unresolved_inputs: tuple[Text, ...] = ()
    validation_errors: tuple[Text, ...] = ()
    unsupported_mappings: tuple[Text, ...] = ()
    failure: FailureRecord | None = None

    @model_validator(mode="after")
    def validate_engine_snapshots(self):
        request = None
        if self.request_json is not None:
            request = ProfitabilityRequest.model_validate_json(self.request_json)
        output = ProfitabilityResult.model_validate_json(self.engine_output_json) if self.engine_output_json is not None else None
        if self.status == ComputationStatus.CALCULATED:
            if self.request_json is None or output is None or output.low_case is None or output.high_case is None:
                raise ValueError("calculated outcomes require validated request and numerical engine output")
            if self.unresolved_inputs or self.validation_errors or self.unsupported_mappings or self.failure:
                raise ValueError("calculated outcome cannot also be a failure")
            if (output.status.value not in {"COMPLETE", "PARTIAL"} or output.currency != request.acquisition.currency
                    or output.quantity != request.acquisition.quantity):
                raise ValueError("engine result status/currency/quantity must match calculated request")
        elif output is not None:
            raise ValueError("non-calculated outcomes cannot carry numerical engine output")
        if self.status == ComputationStatus.ERROR and self.failure is None:
            raise ValueError("error outcomes require durable failure details")
        return self


class CalculationRecord(CalculationOutcome):
    schema_version: Literal["v2-calculation-v1"] = "v2-calculation-v1"
    scenario: ArtifactRef
    scenario_manifest: EconomicScenario
    evidence_references: tuple[ArtifactRef, ...]
    assumption_references: tuple[ArtifactRef, ...]
    adapter_version: Text
    engine_version: Text
    created_at: AwareDatetime


class ArtifactType(str, Enum):
    REVIEW_SUBJECT = "REVIEW_SUBJECT"
    FAILURE = "FAILURE"
    EVALUATION_SUMMARY = "EVALUATION_SUMMARY"
    SCENARIO_ASSUMPTION = "SCENARIO_ASSUMPTION"
    # Reserved categories: no generic/unvalidated future payload writer.
    RESEARCH_ATTEMPT = "RESEARCH_ATTEMPT"
    RAW_RESEARCH_REPORT = "RAW_RESEARCH_REPORT"
    RESALE_COMPARABLE = "RESALE_COMPARABLE"
    COMPARABLE_ASSESSMENT = "COMPARABLE_ASSESSMENT"
    ECONOMIC_FINDINGS = "ECONOMIC_FINDINGS"
    CALCULATION = "CALCULATION"
    ECONOMIC_SCENARIO = "ECONOMIC_SCENARIO"


Payload = Annotated[
    PartialReviewSubject | FailureRecord | EconomicAssessmentSummary | AssumptionRecord
    | RawResearchReport | ResaleComparable | ComparableAssessment | EconomicScenario | CalculationRecord,
    Field(discriminator="schema_version"),
]


class Artifact(Contract):
    schema_version: Literal["v2-artifact-v1"] = "v2-artifact-v1"
    reference: ArtifactRef
    artifact_type: ArtifactType
    payload: Payload
    producer: Producer
    parent_references: tuple[ArtifactRef, ...] = ()
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_payload_type(self):
        expected = {
            ArtifactType.REVIEW_SUBJECT: PartialReviewSubject,
            ArtifactType.FAILURE: FailureRecord,
            ArtifactType.EVALUATION_SUMMARY: EconomicAssessmentSummary,
            ArtifactType.SCENARIO_ASSUMPTION: AssumptionRecord,
            ArtifactType.RAW_RESEARCH_REPORT: RawResearchReport,
            ArtifactType.RESALE_COMPARABLE: ResaleComparable,
            ArtifactType.COMPARABLE_ASSESSMENT: ComparableAssessment,
            ArtifactType.ECONOMIC_SCENARIO: EconomicScenario,
            ArtifactType.CALCULATION: CalculationRecord,
        }
        if self.artifact_type not in expected or not isinstance(self.payload, expected[self.artifact_type]):
            raise ValueError("artifact type and validated payload must agree")
        refs = (*self.parent_references,)
        predecessor = getattr(self.payload, "supersedes", None) or getattr(self.payload, "supersedes_subject", None)
        if predecessor:
            refs = (*refs, predecessor)
        if any(ref.evaluation_id != self.reference.evaluation_id
               or ref.opportunity_id != self.reference.opportunity_id
               or ref.sequence >= self.reference.sequence for ref in refs):
            raise ValueError("artifact parents/predecessors must be earlier records of this exact Evaluation")
        if isinstance(self.payload, PartialReviewSubject) and self.payload.covered_through_sequence != self.reference.sequence - 1:
            raise ValueError("partial review subject must cover the actual preceding artifact sequence")
        if isinstance(self.payload, (ResaleComparable, ComparableAssessment)):
            if (self.payload.evaluation_id != self.reference.evaluation_id
                    or self.payload.opportunity_id != self.reference.opportunity_id):
                raise ValueError("comparable/assessment envelope ownership mismatch")
            timestamp = self.payload.captured_at if isinstance(self.payload, ResaleComparable) else self.payload.assessed_at
            if timestamp != self.created_at:
                raise ValueError("capture/assessment timestamp must be application-owned artifact time")
        if isinstance(self.payload, (EconomicScenario, CalculationRecord)):
            manifest = self.payload if isinstance(self.payload, EconomicScenario) else self.payload.scenario_manifest
            if (manifest.evaluation_id != self.reference.evaluation_id
                    or manifest.opportunity_id != self.reference.opportunity_id
                    or self.payload.created_at != self.created_at):
                raise ValueError("economic artifact ownership/time mismatch")
            if isinstance(self.payload, CalculationRecord):
                ref = self.payload.scenario
                if (ref.evaluation_id != self.reference.evaluation_id or ref.opportunity_id != self.reference.opportunity_id
                        or ref.sequence >= self.reference.sequence):
                    raise ValueError("calculation must reference earlier scenario in this Evaluation")
        return self


class HumanActor(Contract):
    actor: Text
    provenance: Literal["DIRECT_HUMAN_INPUT"] = "DIRECT_HUMAN_INPUT"
    detail: Text | None = None


class HumanReviewDecision(Contract):
    schema_version: Literal["v2-review-v1"] = "v2-review-v1"
    review_id: Text
    evaluation_id: Text
    sequence: int = Field(ge=1)
    reviewed_subject: ArtifactRef
    decision: ReviewDecision
    rationale: Text
    human: HumanActor
    recorded_at: AwareDatetime
    concerns: tuple[Text, ...] = ()
    requested_follow_up: tuple[Text, ...] = ()
    supersedes_review_id: Text | None = None
    authority: Literal["ASSESSMENT_ONLY"] = "ASSESSMENT_ONLY"


class ReviewQueueEntry(Contract):
    evaluation: Evaluation
    review_state: Literal["PENDING"] | ReviewDecision
    current_review: HumanReviewDecision | None
    assessment_state: AssessmentState | None
    summary_matches_current_subject: bool


class ReviewQueuePage(Contract):
    entries: tuple[ReviewQueueEntry, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class AcceptedEvaluation(Contract):
    opportunity: Opportunity
    evaluation: Evaluation


class AssessmentRevisionStatus(Contract):
    artifact: Artifact
    latest_assessment_revision: bool
    current_comparable_revision: bool
    is_current: bool


class ReviewEvidenceSnapshot(Contract):
    evaluation: Evaluation
    artifacts: tuple[Artifact, ...]
