"""Internal persistence contracts, not model-output or commercial-action schemas."""

from enum import Enum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from arbitrage.contracts import NonNegativeDecimalString


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


Payload = Annotated[
    PartialReviewSubject | FailureRecord | EconomicAssessmentSummary | AssumptionRecord,
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
