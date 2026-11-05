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


class ManagerContextLimits(Contract):
    max_artifacts: int = Field(ge=1, le=100, strict=True)
    max_characters: int = Field(ge=1, le=1_000_000, strict=True)


class ManagerContext(Contract):
    schema_version: Literal["v2-manager-context-v1"] = "v2-manager-context-v1"
    opportunity_id: Text
    evaluation_id: Text
    review_subject: ArtifactRef
    publication_target: ArtifactRef | None
    selected_references: tuple[ArtifactRef, ...]
    historical_references: tuple[ArtifactRef, ...]
    limits: ManagerContextLimits
    selection_limitations: tuple[Text, ...]
    # Validated ManagerSnapshot JSON avoids a recursive Artifact -> context -> Artifact schema.
    snapshot_json: str
    snapshot_sha256: Text
    created_at: AwareDatetime


class ResearchRequest(Contract):
    kind: Literal["RESEARCH_REQUEST"]
    objective: Text
    target_subject: Text
    requested_role: Literal["SOURCING", "RESALE", "ECONOMIC_COSTS", "HUMAN_INFORMATION"]
    scope: tuple[Text, ...] = Field(min_length=1)
    max_invocations: int = Field(ge=1, le=20, strict=True)
    priority: Literal["HIGH", "NORMAL", "LOW"] | None = None


class ComparableInterpretation(Contract):
    kind: Literal["COMPARABLE_INTERPRETATION"]
    comparable: ArtifactRef
    selected_for_analysis: bool
    assessment: AssessmentDraft
    predecessor: ArtifactRef | None = None


class AssumptionProposal(Contract):
    kind: Literal["ECONOMIC_ASSUMPTION"]
    assumption: EconomicAssumption

    @model_validator(mode="after")
    def numerical_assumption(self):
        if self.assumption.amount is None or self.assumption.unit is None:
            raise ValueError("Manager economic assumptions require explicit amount and unit")
        return self


class ScenarioProposal(Contract):
    kind: Literal["SCENARIO_PROPOSAL"]
    scenario: ScenarioDraft
    predecessor: ArtifactRef | None = None


class ComparableUse(Contract):
    comparable: ArtifactRef
    rationale: Text


class SummaryExplanation(Contract):
    opportunity_evaluated: Text
    evidence_explanation: Text
    comparables: tuple[ComparableUse, ...]
    product_differences: tuple[Text, ...]
    assumptions_explanation: Text
    assumption_references: tuple[ArtifactRef, ...]
    uncertain_costs: tuple[UnknownEconomicInput, ...]
    confidence_improvements: tuple[Text, ...] = Field(min_length=1)
    conditional_usefulness: Text | None = None


class SummaryProposal(Contract):
    kind: Literal["ECONOMIC_SUMMARY"]
    summary: SummaryDraft
    expected_summary: ArtifactRef | None
    explanation: SummaryExplanation

    @model_validator(mode="after")
    def conditional_disclosure(self):
        if self.summary.assessment_state == AssessmentState.CONDITIONAL_ESTIMATE:
            if not self.explanation.conditional_usefulness or not self.summary.consequential_uncertainties:
                raise ValueError("conditional summaries require usefulness rationale and consequential uncertainties")
        return self


class ManagerProposal(Contract):
    schema_version: Literal["v2-manager-proposal-v1"] = "v2-manager-proposal-v1"
    opportunity_id: Text
    evaluation_id: Text
    context: ArtifactRef
    author_name: Text
    rationale: Text
    evidence_references: tuple[ArtifactRef, ...]
    limitations: tuple[Text, ...]
    uncertainties: tuple[Text, ...]
    intent: Annotated[ResearchRequest | ComparableInterpretation | AssumptionProposal | ScenarioProposal
                      | SummaryProposal, Field(discriminator="kind")]
    supersedes: ArtifactRef | None = None


class ManagerDecision(Contract):
    schema_version: Literal["v2-manager-decision-v1"] = "v2-manager-decision-v1"
    decision_id: Text
    revision: int = Field(ge=1)
    supersedes: ArtifactRef | None = None
    proposal: ManagerProposal
    created_at: AwareDatetime

    @model_validator(mode="after")
    def consistent_revision(self):
        if (self.revision == 1) != (self.supersedes is None) or self.supersedes != self.proposal.supersedes:
            raise ValueError("Manager revision must preserve its exact authored predecessor")
        return self


class ManagerApplication(Contract):
    schema_version: Literal["v2-manager-application-v1"] = "v2-manager-application-v1"
    decision: ArtifactRef
    outcome: ArtifactRef | None
    created_at: AwareDatetime


DecisionKind = Literal["RESEARCH_REQUEST", "COMPARABLE_INTERPRETATION", "ECONOMIC_ASSUMPTION", "SCENARIO_PROPOSAL", "ECONOMIC_SUMMARY"]


class ManagerModelConfig(Contract):
    model: Text
    decision_type: DecisionKind
    max_output_tokens: int = Field(ge=16, strict=True)
    timeout_seconds: float = Field(gt=0, le=300, allow_inf_nan=False)
    max_invocations: int = Field(ge=1, strict=True)
    max_attempts: int = Field(ge=1, strict=True)
    max_total_tokens: int = Field(ge=1, strict=True)
    max_request_bytes: int = Field(ge=1, le=2_000_000, strict=True)
    max_response_bytes: int = Field(ge=1, le=2_000_000, strict=True)
    max_estimated_cost_usd: str | None

    @model_validator(mode="after")
    def no_invented_prices(self):
        if self.max_estimated_cost_usd is not None:
            raise ValueError("No authoritative pricing/billable-total bound is configured; use explicit token/invocation budgets")
        return self


class LiveManagerRequest(Contract):
    context: ArtifactRef
    objective: Text
    config: ManagerModelConfig


class ModelUsage(Contract):
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)


class ModelCallRecord(Contract):
    schema_version: Literal["v2-manager-model-call-v1"] = "v2-manager-model-call-v1"
    stage: Literal["STARTED", "RECEIVED", "FINISHED"]
    call_id: Text
    attempt: ArtifactRef
    start: ArtifactRef | None
    request: LiveManagerRequest
    context_sha256: Text
    request_sha256: Text
    instruction_version: Text
    instruction_sha256: Text
    reserved_tokens: int = Field(ge=1)
    live_authorized: bool
    offline_client: bool
    ambiguous_retry_acknowledged: bool
    started_at: AwareDatetime
    recorded_at: AwareDatetime
    status: Text
    ambiguous: bool = False
    provider_response_id: Text | None = None
    provider_request_id: Text | None = None
    provider_model: Text | None = None
    usage: ModelUsage | None = None
    response_sha256: Text | None = None
    response_text: str | None = None
    application: ArtifactRef | None = None
    failure: FailureRecord | None = None
    limitations: tuple[Text, ...] = ()


class WorkflowLimits(Contract):
    max_specialist_invocations: int = Field(ge=1, strict=True)
    max_attempts_per_step: int = Field(ge=1, strict=True)
    max_workflow_steps: int = Field(ge=1, strict=True)
    max_calculation_attempts: int = Field(ge=1, strict=True)


class ResaleDelegation(Contract):
    schema_version: Literal["v2-resale-delegation-v1"] = "v2-resale-delegation-v1"
    delegation_id: Text
    opportunity_id: Text
    evaluation_id: Text
    manager_decision: ArtifactRef
    manager_context: ArtifactRef
    context_sha256: Text
    original_intake: OpportunitySubmission
    # Original descriptive intake, not a Manager-authored canonical product identity.
    target_product_description: Text
    research_request: ResearchRequest
    intended_marketplace: Text
    evidence_limitations: tuple[Text, ...]


class ResaleSourceObservation(Contract):
    source_id: Text
    source: ComparableSource
    claim_origin: Literal["SYNTHETIC_FIXTURE", "SPECIALIST_SOURCE_REPORT", "SPECIALIST_INFERENCE",
                          "GENERAL_MODEL_KNOWLEDGE", "HUMAN_INTAKE"]
    directly_supports_claim: bool | None
    explanation: Text
    listing_status: Literal["TRANSACTION_COMPLETED", "MARKED_SOLD", "ENDED", "ACCEPTED_OFFER",
                            "ACTIVE", "CUMULATIVE_SOLD_COUNT", "UNKNOWN"] = "UNKNOWN"
    reported_quantity_sold: int | None = Field(default=None, ge=0, strict=True)


class ResaleResearchInput(Contract):
    schema_version: Literal["v2-resale-research-input-v1"] = "v2-resale-research-input-v1"
    delegation: ArtifactRef
    context: ResaleDelegation


class ResaleResearchFinding(Contract):
    source_id: Text
    comparable: ComparableDraft
    comparability_claim: ComparabilityCategory
    explanation: Text
    unresolved_attributes: tuple[Text, ...] = ()


class ResaleResearchResult(Contract):
    schema_version: Literal["v2-resale-research-result-v1"] = "v2-resale-research-result-v1"
    delegation: ArtifactRef
    delegation_id: Text
    opportunity_id: Text
    evaluation_id: Text
    objective_addressed: Text
    status: Literal["COMPLETED", "PARTIAL", "NO_EVIDENCE", "RESEARCH_FAILED"]
    sources: tuple[ResaleSourceObservation, ...] = ()
    findings: tuple[ResaleResearchFinding, ...] = ()
    unresolved_attributes: tuple[Text, ...] = ()
    limitations: tuple[Text, ...]
    missing_evidence: tuple[Text, ...]
    suggested_follow_up: tuple[Text, ...] = ()
    explanation: Text

    @model_validator(mode="after")
    def source_links(self):
        sources={s.source_id:s for s in self.sources}
        if len(sources)!=len(self.sources):
            raise ValueError("Source identifiers must be unique")
        if self.status in {"NO_EVIDENCE", "RESEARCH_FAILED"} and self.findings:
            raise ValueError("No-evidence/failed research cannot assert usable comparables")
        for finding in self.findings:
            source=sources.get(finding.source_id)
            if source is None or finding.comparable.source!=source.source:
                raise ValueError("Comparable must preserve its exact declared source")
            price=finding.comparable.price_evidence
            if price.evidence_type==PriceEvidenceType.COMPLETED_SALE:
                if source.listing_status in {"ACTIVE", "ENDED", "CUMULATIVE_SOLD_COUNT"}:
                    raise ValueError("An active/ended listing or cumulative sold count is not a completed transaction")
                if source.directly_supports_claim is not True or not finding.comparable.limitations:
                    raise ValueError("Completed-sale claims require direct support and explicit limitations")
                if source.claim_origin in {"SPECIALIST_INFERENCE", "GENERAL_MODEL_KNOWLEDGE"}:
                    raise ValueError("Inference/general knowledge cannot establish a completed transaction")
        return self


class ResaleResearchRecord(Contract):
    schema_version: Literal["v2-resale-research-record-v1"] = "v2-resale-research-record-v1"
    result: ResaleResearchResult
    raw_report: ArtifactRef
    comparable_references: tuple[ArtifactRef, ...]
    provider_audits: tuple[ArtifactRef, ...] = ()


class ResaleProviderConfig(Contract):
    model: Text
    max_provider_invocations: int = Field(ge=1, strict=True)
    max_research_passes: int = Field(ge=1, strict=True)
    provider_calls_per_pass: Literal[1] = 1
    max_web_search_calls: int = Field(ge=1, strict=True)
    max_output_tokens: int = Field(ge=16, strict=True)
    timeout_seconds: float = Field(gt=0, le=300, allow_inf_nan=False)
    max_request_bytes: int = Field(ge=1, le=2_000_000, strict=True)
    max_response_bytes: int = Field(ge=1, le=2_000_000, strict=True)
    max_total_tokens: int = Field(ge=1, strict=True)


class ProviderSourceCitation(Contract):
    url: Text
    title: str | None = None
    origin: Literal["URL_ANNOTATION", "SEARCH_SOURCE"]
    tool_call_id: str | None = None
    start_index: int | None = None
    end_index: int | None = None
    # The installed source/annotation types expose no actual page excerpt.
    excerpt: str | None = None
    validation_status: Literal["PROVIDER_ATTRIBUTED_NOT_INDEPENDENTLY_VERIFIED"] = "PROVIDER_ATTRIBUTED_NOT_INDEPENDENTLY_VERIFIED"
    application_visited: Literal[False] = False


class ResaleProviderAudit(Contract):
    schema_version: Literal["v2-resale-provider-audit-v1"] = "v2-resale-provider-audit-v1"
    stage: Literal["STARTED", "RECEIVED", "VALIDATED", "FINISHED"]
    call_id: Text
    call_sequence: int = Field(ge=1)
    research_pass: ArtifactRef
    delegation: ArtifactRef
    start: ArtifactRef | None
    config: ResaleProviderConfig
    prompt_version: Text
    schema_version_name: Text
    input_sha256: Text
    context_sha256: Text
    request_sha256: Text
    tool_configuration_json: Text
    reserved_tokens: int = Field(ge=1)
    live_authorized: bool
    offline_client: bool
    started_at: AwareDatetime
    recorded_at: AwareDatetime
    status: Text
    ambiguous: bool = False
    provider_request_id: str | None = None
    provider_response_id: str | None = None
    provider_model: str | None = None
    usage: ModelUsage | None = None
    response_sha256: str | None = None
    raw_response_json: str | None = None
    output_text: str | None = None
    citations: tuple[ProviderSourceCitation, ...] = ()
    tool_metadata_json: str | None = None
    result_sha256: str | None = None
    publication: ArtifactRef | None = None
    failure: FailureRecord | None = None


StepRole = Literal["RESEARCH", "COMPARABLE", "ASSESSMENT", "ASSUMPTION", "SCENARIO", "CALCULATION", "SUMMARY", "MANAGER", "LIVE_MANAGER", "RESALE_RESEARCH"]


class OfflineFixture(Contract):
    attribution: Literal["SYNTHETIC_OFFLINE"] = "SYNTHETIC_OFFLINE"
    fixture_id: Text
    # Untrusted fixture bytes are parsed only after a durable invocation start.
    response_json: str


class WorkflowStep(Contract):
    step_id: Text
    role: StepRole
    context_references: tuple[ArtifactRef, ...]
    context_text: str
    fixture: OfflineFixture | None = None
    scenario: ArtifactRef | None = None
    retry_of: ArtifactRef | None = None
    live_request: LiveManagerRequest | None = None
    resale_delegation: ArtifactRef | None = None
    resale_config: ResaleProviderConfig | None = None

    @model_validator(mode="after")
    def correct_boundary(self):
        if self.role == "RESALE_RESEARCH":
            if (self.resale_delegation is None or self.resale_delegation not in self.context_references
                    or self.fixture is not None or self.scenario is not None or self.live_request is not None):
                raise ValueError("Resale research requires an exact application delegation and no alternate request")
        elif self.resale_delegation is not None:
            raise ValueError("Only resale research can carry a delegation")
        elif self.role == "LIVE_MANAGER":
            if (self.live_request is None or self.fixture is not None or self.scenario is not None
                    or self.live_request.context not in self.context_references):
                raise ValueError("Live Manager requires exact request context and no offline fixture/tools")
        elif self.live_request is not None:
            raise ValueError("Only a live Manager step may contain a model request")
        elif self.role == "CALCULATION":
            if self.fixture is not None or self.scenario is None or self.scenario not in self.context_references:
                raise ValueError("calculation requires an exact context scenario and no specialist fixture")
        elif self.fixture is None or self.scenario is not None:
            raise ValueError("specialist steps require an explicit offline fixture, not calculation authority")
        if len(self.context_references) != len(set(self.context_references)):
            raise ValueError("duplicate input context references")
        if self.resale_config is not None and self.role!='RESALE_RESEARCH':
            raise ValueError('Provider configuration belongs only to a Resale research step')
        return self


class StepInputManifest(Contract):
    opportunity_id: Text
    evaluation_id: Text
    step_id: Text
    role: StepRole
    submission: OpportunitySubmission
    context_references: tuple[ArtifactRef, ...]
    context_text: str
    fixture: OfflineFixture | None
    scenario: ArtifactRef | None
    live_request: LiveManagerRequest | None = None
    resale_delegation: ArtifactRef | None = None
    resale_config: ResaleProviderConfig | None = None


class WorkflowEvent(Contract):
    schema_version: Literal["v2-workflow-event-v1"] = "v2-workflow-event-v1"
    opportunity_id: Text
    evaluation_id: Text
    phase: Literal["ACCEPTED", "RUNNING", "PARTIAL", "INTERRUPTED", "COMPLETED", "FAILED", "CANCELLED"]
    event: Literal["INITIALIZED", "RUN_STARTED", "RUN_ENDED", "INTERRUPTION_REPORTED", "RECOVERY_STARTED", "RECOVERED", "FINISHED"]
    limits: WorkflowLimits
    run: ArtifactRef | None = None
    owner_token: Text | None = None
    steps: tuple[WorkflowStep, ...] = ()
    attempt_references: tuple[ArtifactRef, ...] = ()
    reason: Text
    failure: FailureRecord | None = None

    @model_validator(mode="after")
    def validate_event(self):
        if self.event == "RUN_STARTED":
            if self.owner_token is None or self.run is not None or not self.steps or self.phase != "RUNNING":
                raise ValueError("run start requires application claim and explicit bounded plan")
        elif self.owner_token is not None or self.steps:
            raise ValueError("only run starts carry an invocation claim/plan")
        if self.event in {"RUN_ENDED","INTERRUPTION_REPORTED","RECOVERY_STARTED","RECOVERED"} and self.run is None:
            raise ValueError("run event requires its exact start reference")
        for ref in (*((self.run,) if self.run else ()), *self.attempt_references):
            if ref.evaluation_id != self.evaluation_id or ref.opportunity_id != self.opportunity_id:
                raise ValueError("workflow event references must have exact ownership")
        return self


AttemptStatus = Literal["STARTED", "SUCCEEDED", "SPECIALIST_FAILURE", "INVALID_OUTPUT", "PERSISTENCE_FAILURE",
                        "COORDINATOR_ERROR", "INTERRUPTED", "CALCULATION_INSUFFICIENT", "CALCULATION_INVALID", "CALCULATION_ERROR"]


class StepAttempt(Contract):
    schema_version: Literal["v2-step-attempt-v1"] = "v2-step-attempt-v1"
    attempt_id: Text
    attempt_number: int = Field(ge=1)
    run: ArtifactRef
    input_manifest: StepInputManifest
    status: AttemptStatus
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    start: ArtifactRef | None = None
    output_references: tuple[ArtifactRef, ...] = ()
    failure: FailureRecord | None = None

    @model_validator(mode="after")
    def complete_attempt(self):
        if self.status == "STARTED":
            if self.ended_at is not None or self.start is not None or self.output_references or self.failure:
                raise ValueError("started attempts cannot claim completion or output")
        else:
            if self.ended_at is None or self.start is None or self.ended_at < self.started_at:
                raise ValueError("terminal attempt requires original start and completion timestamp")
            if self.status in {"SUCCEEDED", "CALCULATION_INSUFFICIENT", "CALCULATION_INVALID", "CALCULATION_ERROR"}:
                if len(self.output_references) != 1 or self.failure is not None:
                    raise ValueError("accepted invocation requires one exact output artifact")
            elif self.failure is None or self.output_references:
                raise ValueError("failed/interrupted invocation requires failure information, not successful outputs")
        return self


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
    WORKFLOW_EVENT = "WORKFLOW_EVENT"
    MANAGER_CONTEXT = "MANAGER_CONTEXT"
    MANAGER_DECISION = "MANAGER_DECISION"
    MANAGER_APPLICATION = "MANAGER_APPLICATION"
    MODEL_CALL = "MODEL_CALL"
    RESALE_DELEGATION = "RESALE_DELEGATION"
    RESALE_RESEARCH_RESULT = "RESALE_RESEARCH_RESULT"
    RESALE_PROVIDER_CALL = "RESALE_PROVIDER_CALL"


Payload = Annotated[
    PartialReviewSubject | FailureRecord | EconomicAssessmentSummary | AssumptionRecord
    | RawResearchReport | ResaleComparable | ComparableAssessment | EconomicScenario | CalculationRecord
    | WorkflowEvent | StepAttempt | ManagerContext | ManagerDecision | ManagerApplication | ModelCallRecord
    | ResaleDelegation | ResaleResearchRecord | ResaleProviderAudit,
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
            ArtifactType.WORKFLOW_EVENT: WorkflowEvent,
            ArtifactType.RESEARCH_ATTEMPT: StepAttempt,
            ArtifactType.MANAGER_CONTEXT: ManagerContext,
            ArtifactType.MANAGER_DECISION: ManagerDecision,
            ArtifactType.MANAGER_APPLICATION: ManagerApplication,
            ArtifactType.MODEL_CALL: ModelCallRecord,
            ArtifactType.RESALE_DELEGATION: ResaleDelegation,
            ArtifactType.RESALE_RESEARCH_RESULT: ResaleResearchRecord,
            ArtifactType.RESALE_PROVIDER_CALL: ResaleProviderAudit,
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
        if isinstance(self.payload, (WorkflowEvent, StepAttempt)):
            owner = self.payload.input_manifest if isinstance(self.payload, StepAttempt) else self.payload
            if (owner.evaluation_id != self.reference.evaluation_id or owner.opportunity_id != self.reference.opportunity_id):
                raise ValueError("workflow artifact ownership mismatch")
        if isinstance(self.payload,(ManagerContext,ManagerDecision,ManagerApplication)):
            if self.payload.created_at != self.created_at:
                raise ValueError("Manager timestamps must be application-owned")
            owner = (self.payload.proposal if isinstance(self.payload,ManagerDecision) else
                     self.payload.decision if isinstance(self.payload,ManagerApplication) else self.payload)
            if owner.evaluation_id != self.reference.evaluation_id or owner.opportunity_id != self.reference.opportunity_id:
                raise ValueError("Manager artifact ownership mismatch")
        if isinstance(self.payload,ModelCallRecord):
            if (self.payload.attempt.evaluation_id != self.reference.evaluation_id
                    or self.payload.attempt.opportunity_id != self.reference.opportunity_id
                    or self.payload.recorded_at != self.created_at):
                raise ValueError("Model audit ownership/timestamp mismatch")
        if isinstance(self.payload,ResaleProviderAudit):
            if self.payload.recorded_at!=self.created_at or any(
                    r.evaluation_id!=self.reference.evaluation_id or r.opportunity_id!=self.reference.opportunity_id
                    for r in (self.payload.research_pass,self.payload.delegation)):
                raise ValueError('Resale provider audit ownership/timestamp mismatch')
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


class WorkflowSnapshot(Contract):
    evaluation: Evaluation
    events: tuple[Artifact, ...]
    attempts: tuple[Artifact, ...]

    @property
    def unresolved_runs(self):
        closed = {e.payload.run for e in self.events if e.payload.event in {"RUN_ENDED","RECOVERED"}}
        return tuple(e.reference for e in self.events if e.payload.event == "RUN_STARTED" and e.reference not in closed)

    @property
    def unfinished_attempts(self):
        ended = {a.payload.start for a in self.attempts if a.payload.start}
        return tuple(a.reference for a in self.attempts if a.payload.status == "STARTED" and a.reference not in ended)


class ManagerSnapshot(Contract):
    opportunity: Opportunity
    evaluation: Evaluation
    artifacts: tuple[Artifact, ...]
    historical_artifacts: tuple[Artifact, ...]
    workflow_event: Artifact | None
    unresolved_runs: tuple[ArtifactRef, ...]
    unfinished_attempts: tuple[ArtifactRef, ...]
    available_artifact_count: int = Field(ge=0)
