from decimal import Decimal
from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field, WithJsonSchema, field_validator, model_validator


DecimalString = Annotated[
    Decimal,
    WithJsonSchema({"type": "string"}, mode="validation"),
    WithJsonSchema({"type": "string"}, mode="serialization"),
]

NonNegativeDecimalString = Annotated[
    Decimal,
    Field(ge=0),
    WithJsonSchema({"type": "string"}, mode="validation"),
    WithJsonSchema({"type": "string"}, mode="serialization"),
]


class ProductCondition(str, Enum):
    NEW = "NEW"
    OPEN_BOX = "OPEN_BOX"
    USED = "USED"
    REFURBISHED = "REFURBISHED"
    UNKNOWN = "UNKNOWN"


class ProductIdentity(BaseModel):
    brand: str | None
    product_name: str
    model_number: str | None
    upc_gtin: str | None
    variant: str | None
    condition: ProductCondition
    package_quantity: int | None


class IdentityConfidence(str, Enum):
    VERIFIED = "VERIFIED"
    STRONG = "STRONG"
    PROBABLE = "PROBABLE"
    AMBIGUOUS = "AMBIGUOUS"


class SourceReference(BaseModel):
    url: str
    description: str


class SourcingRequest(BaseModel):
    candidate_description: str
    source_hint: str | None
    seller_hint: str | None
    known_identity: ProductIdentity | None
    user_provided_facts: list[str]
    research_objectives: list[str]
    unresolved_questions: list[str]
    additional_context: str | None


class AvailabilityStatus(str, Enum):
    IN_STOCK = "IN_STOCK"
    LIMITED = "LIMITED"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    PREORDER = "PREORDER"
    UNKNOWN = "UNKNOWN"


class CandidateIntakeSource(str, Enum):
    HUMAN_PHYSICAL = "HUMAN_PHYSICAL"
    HUMAN_ONLINE = "HUMAN_ONLINE"
    AUTONOMOUS_DISCOVERY = "AUTONOMOUS_DISCOVERY"


class HumanInputBasis(str, Enum):
    HUMAN_OBSERVED = "HUMAN_OBSERVED"
    HUMAN_REPORTED = "HUMAN_REPORTED"
    HUMAN_ESTIMATED = "HUMAN_ESTIMATED"
    HUMAN_ASSUMED = "HUMAN_ASSUMED"


class HumanAcquisitionInput(BaseModel):
    seller_or_store: str | None
    purchase_price: NonNegativeDecimalString | None
    currency: str | None
    quantity_available: int | None = Field(ge=0)
    purchase_limit: int | None = Field(ge=1)
    availability_status: AvailabilityStatus | None
    condition: ProductCondition | None
    location_description: str | None
    observed_at: datetime | None
    price_basis: HumanInputBasis | None
    quantity_basis: HumanInputBasis | None
    condition_basis: HumanInputBasis | None
    availability_basis: HumanInputBasis | None

    @model_validator(mode="after")
    def validate_provenance(self) -> "HumanAcquisitionInput":
        pairs = (
            ("purchase_price", self.purchase_price, "price_basis", self.price_basis),
            ("quantity_available", self.quantity_available, "quantity_basis", self.quantity_basis),
            ("condition", self.condition, "condition_basis", self.condition_basis),
            (
                "availability_status",
                self.availability_status,
                "availability_basis",
                self.availability_basis,
            ),
        )
        for value_name, value, basis_name, basis in pairs:
            if value is None and basis is not None:
                raise ValueError(f"{basis_name} must be None when {value_name} is None")
            if value is not None and basis is None:
                raise ValueError(f"{basis_name} is required when {value_name} is supplied")
        return self


class HumanResaleInput(BaseModel):
    destination_market: str | None
    resale_price_low: NonNegativeDecimalString | None
    resale_price_expected: NonNegativeDecimalString | None
    resale_price_high: NonNegativeDecimalString | None
    basis: HumanInputBasis | None
    notes: str | None

    @model_validator(mode="after")
    def validate_prices(self) -> "HumanResaleInput":
        prices = (
            self.resale_price_low,
            self.resale_price_expected,
            self.resale_price_high,
        )
        if any(price is not None for price in prices) and self.basis is None:
            raise ValueError("basis is required when a resale price is supplied")
        if all(price is None for price in prices) and self.basis is not None:
            raise ValueError("basis must be None when no resale price is supplied")
        if (
            self.resale_price_low is not None
            and self.resale_price_high is not None
            and self.resale_price_low > self.resale_price_high
        ):
            raise ValueError("resale_price_low must not exceed resale_price_high")
        if self.resale_price_expected is not None:
            if (
                self.resale_price_low is not None
                and self.resale_price_expected < self.resale_price_low
            ):
                raise ValueError("resale_price_expected must not be below resale_price_low")
            if (
                self.resale_price_high is not None
                and self.resale_price_expected > self.resale_price_high
            ):
                raise ValueError("resale_price_expected must not exceed resale_price_high")
        return self


class CandidateIntake(BaseModel):
    intake_source: CandidateIntakeSource
    product_identity: ProductIdentity | None
    identity_confidence: IdentityConfidence
    identity_basis: HumanInputBasis | None
    acquisition: HumanAcquisitionInput | None
    resale: HumanResaleInput | None
    notes: str | None

    @model_validator(mode="after")
    def validate_identity_provenance(self) -> "CandidateIntake":
        if self.product_identity is None and self.identity_basis is not None:
            raise ValueError("identity_basis must be None when product_identity is None")
        if self.product_identity is not None and self.identity_basis is None:
            raise ValueError("identity_basis is required when product_identity is supplied")
        return self


class AdditionalCost(BaseModel):
    description: str
    amount: float | None
    currency: str | None


class SourcingResult(BaseModel):
    product_identity: ProductIdentity
    identity_confidence: IdentityConfidence
    identity_confidence_reason: str
    source_name: str | None
    seller_name: str | None
    source_url: str | None
    source_references: list[SourceReference]
    item_price: float | None
    currency: str | None
    shipping_cost: float | None
    shipping_cost_known: bool
    other_known_costs: list[AdditionalCost]
    availability_status: AvailabilityStatus
    inventory_quantity: int | None
    purchase_limit: int | None
    acquisition_requirements: list[str]
    unresolved_issues: list[str]
    research_timestamp: str
    notes: str | None


class ResaleRequest(BaseModel):
    product_identity: ProductIdentity
    identity_confidence: IdentityConfidence
    identity_uncertainty: str | None
    research_constraints: list[str]
    research_objectives: list[str]
    sourcing_context: str | None
    additional_context: str | None


class EvidenceType(str, Enum):
    VERIFIED_REALIZED_SALE = "VERIFIED_REALIZED_SALE"
    SOLD_STATUS_PRICE_UNVERIFIED = "SOLD_STATUS_PRICE_UNVERIFIED"
    ACTIVE_ASK = "ACTIVE_ASK"
    DEMAND_SIGNAL = "DEMAND_SIGNAL"
    RETAIL_REFERENCE = "RETAIL_REFERENCE"
    OTHER = "OTHER"


class MatchQuality(str, Enum):
    EXACT = "EXACT"
    STRONG = "STRONG"
    IMPERFECT = "IMPERFECT"
    AMBIGUOUS = "AMBIGUOUS"


class ResaleEvidenceItem(BaseModel):
    marketplace: str
    listing_title: str | None
    evidence_type: EvidenceType
    sale_completion_basis: str | None
    realized_price_basis: str | None
    quantity_sold: int | None
    price: float | None
    currency: str | None
    shipping_cost: float | None
    condition: ProductCondition
    sale_or_listing_date: str | None
    url: str | None
    match_quality: MatchQuality
    limitations: list[str]

    @model_validator(mode="after")
    def validate_verified_sale_bases(self) -> "ResaleEvidenceItem":
        if self.evidence_type == EvidenceType.VERIFIED_REALIZED_SALE:
            missing = [
                name
                for name in ("sale_completion_basis", "realized_price_basis")
                if not (getattr(self, name) or "").strip()
            ]
            if missing:
                raise ValueError(
                    "VERIFIED_REALIZED_SALE requires nonblank " + " and ".join(missing)
                )
        return self


class EvidenceQuality(str, Enum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    INSUFFICIENT = "INSUFFICIENT"


class DemandAssessment(str, Enum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    UNKNOWN = "UNKNOWN"


class ResaleResult(BaseModel):
    researched_identity: ProductIdentity
    identity_match: MatchQuality
    identity_match_notes: str | None
    evidence_items: list[ResaleEvidenceItem]
    verified_realized_price_low: float | None
    verified_realized_price_high: float | None
    estimated_achievable_price_low: float | None
    estimated_achievable_price_high: float | None
    currency: str | None
    evidence_quality: EvidenceQuality
    evidence_quality_reason: str
    demand_assessment: DemandAssessment
    market_observations: list[str]
    unresolved_issues: list[str]
    research_timestamp: str
    notes: str | None


class InputBasis(str, Enum):
    HUMAN_OBSERVED = "HUMAN_OBSERVED"
    VERIFIED = "VERIFIED"
    CALCULATED = "CALCULATED"
    ESTIMATED = "ESTIMATED"
    ASSUMED = "ASSUMED"


class SourceType(str, Enum):
    ONLINE_RETAILER = "ONLINE_RETAILER"
    PHYSICAL_RETAILER = "PHYSICAL_RETAILER"
    ONLINE_MARKETPLACE = "ONLINE_MARKETPLACE"
    PHYSICAL_MARKETPLACE = "PHYSICAL_MARKETPLACE"
    WHOLESALER = "WHOLESALER"
    OTHER = "OTHER"


class CostType(str, Enum):
    FIXED_PER_UNIT = "FIXED_PER_UNIT"
    FIXED_PER_BATCH = "FIXED_PER_BATCH"
    PERCENT_OF_UNIT_PRICE = "PERCENT_OF_UNIT_PRICE"


class ProfitabilityStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_INPUTS = "INSUFFICIENT_INPUTS"


class ScenarioSide(str, Enum):
    ACQUISITION = "ACQUISITION"
    SALE = "SALE"


class UnknownMateriality(str, Enum):
    NON_MATERIAL = "NON_MATERIAL"
    MATERIAL = "MATERIAL"


class CostComponent(BaseModel):
    cost_id: str
    name: str
    cost_type: CostType
    value: NonNegativeDecimalString
    basis: InputBasis
    notes: str | None

    @field_validator("cost_id", "name")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class UnknownInput(BaseModel):
    name: str
    materiality: UnknownMateriality
    notes: str | None
    related_cost_ids: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("related_cost_ids")
    @classmethod
    def validate_related_cost_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("related_cost_ids must not contain blank values")
        if len(values) != len(set(values)):
            raise ValueError("related_cost_ids must not contain duplicates")
        return values


def _validate_unique_cost_ids(costs: list[CostComponent], field_name: str) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for cost in costs:
        if cost.cost_id in seen and cost.cost_id not in duplicates:
            duplicates.append(cost.cost_id)
        seen.add(cost.cost_id)
    if duplicates:
        raise ValueError(f"{field_name} contains duplicate cost_id values: {duplicates}")


def _validate_no_unknown_zero_placeholders(
    costs: list[CostComponent], unknowns: list[UnknownInput]
) -> None:
    unresolved_cost_ids = {
        cost_id for unknown in unknowns for cost_id in unknown.related_cost_ids
    }
    contradictions = [
        cost.cost_id
        for cost in costs
        if cost.cost_id in unresolved_cost_ids
        and cost.basis == InputBasis.ASSUMED
        and cost.value == 0
    ]
    if contradictions:
        raise ValueError(
            "an economic input cannot be both unresolved and an assumed zero-valued "
            f"cost in the same scenario: {contradictions}"
        )


class AcquisitionScenario(BaseModel):
    product_identity: ProductIdentity
    source_type: SourceType
    source_name: str
    source_references: list[str]
    condition: ProductCondition
    currency: str
    unit_purchase_price: NonNegativeDecimalString
    purchase_price_basis: InputBasis
    quantity: int = Field(ge=1)
    additional_costs: list[CostComponent]
    quantity_available: int | None = Field(ge=0)
    quantity_available_basis: InputBasis | None
    purchase_limit: int | None = Field(ge=1)
    purchase_requirements: list[str]
    assumptions: list[str]
    unknowns: list[UnknownInput]
    notes: str | None

    @field_validator("source_name", "currency")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def validate_scenario(self) -> "AcquisitionScenario":
        if self.quantity_available is None and self.quantity_available_basis is not None:
            raise ValueError(
                "quantity_available_basis must be None when quantity_available is None"
            )
        if self.quantity_available is not None:
            if self.quantity_available_basis is None:
                raise ValueError(
                    "quantity_available_basis is required when quantity_available is supplied"
                )
            if self.quantity > self.quantity_available:
                raise ValueError("quantity must not exceed quantity_available")
        if self.purchase_limit is not None and self.quantity > self.purchase_limit:
            raise ValueError("quantity must not exceed purchase_limit")
        if self.condition != self.product_identity.condition:
            raise ValueError("acquisition condition must match product identity condition")
        _validate_unique_cost_ids(self.additional_costs, "additional_costs")
        _validate_no_unknown_zero_placeholders(self.additional_costs, self.unknowns)
        return self


class SaleScenario(BaseModel):
    marketplace_name: str
    marketplace_references: list[str]
    currency: str
    target_condition: ProductCondition
    resale_price_low: NonNegativeDecimalString
    resale_price_expected: NonNegativeDecimalString | None
    resale_price_high: NonNegativeDecimalString
    resale_price_basis: InputBasis
    selling_costs: list[CostComponent]
    quantity: int = Field(ge=1)
    assumptions: list[str]
    unknowns: list[UnknownInput]
    notes: str | None

    @field_validator("marketplace_name", "currency")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def validate_scenario(self) -> "SaleScenario":
        if self.resale_price_low > self.resale_price_high:
            raise ValueError("resale_price_low must not exceed resale_price_high")
        if self.resale_price_expected is not None and not (
            self.resale_price_low
            <= self.resale_price_expected
            <= self.resale_price_high
        ):
            raise ValueError("resale_price_expected must be within the low/high range")
        _validate_unique_cost_ids(self.selling_costs, "selling_costs")
        _validate_no_unknown_zero_placeholders(self.selling_costs, self.unknowns)
        return self


class SensitivityInput(BaseModel):
    cost_id: str
    scenario_side: ScenarioSide
    low_value: NonNegativeDecimalString
    high_value: NonNegativeDecimalString

    @field_validator("cost_id")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "SensitivityInput":
        if self.high_value < self.low_value:
            raise ValueError("high_value must be greater than or equal to low_value")
        return self


class ProfitabilityRequest(BaseModel):
    acquisition: AcquisitionScenario
    sale: SaleScenario
    sensitivity_inputs: list[SensitivityInput]

    @model_validator(mode="after")
    def validate_request(self) -> "ProfitabilityRequest":
        if self.acquisition.currency != self.sale.currency:
            raise ValueError("acquisition and sale currencies must match")
        if self.acquisition.quantity != self.sale.quantity:
            raise ValueError("acquisition and sale quantities must match")
        if not (
            self.acquisition.product_identity.condition
            == self.acquisition.condition
            == self.sale.target_condition
        ):
            raise ValueError("product, acquisition, and sale conditions must match")

        acquisition_ids = {cost.cost_id for cost in self.acquisition.additional_costs}
        sale_ids = {cost.cost_id for cost in self.sale.selling_costs}
        targets: set[tuple[ScenarioSide, str]] = set()
        for sensitivity in self.sensitivity_inputs:
            available_ids = (
                acquisition_ids
                if sensitivity.scenario_side == ScenarioSide.ACQUISITION
                else sale_ids
            )
            if sensitivity.cost_id not in available_ids:
                raise ValueError(
                    "sensitivity cost_id does not exist on the selected scenario side: "
                    f"{sensitivity.scenario_side.value}/{sensitivity.cost_id}"
                )
            target = (sensitivity.scenario_side, sensitivity.cost_id)
            if target in targets:
                raise ValueError(
                    "duplicate sensitivity target: "
                    f"{sensitivity.scenario_side.value}/{sensitivity.cost_id}"
                )
            targets.add(target)
        return self


class CalculatedCost(BaseModel):
    cost_id: str
    name: str
    cost_type: CostType
    input_value: DecimalString
    calculated_amount: DecimalString


class ProfitScenarioResult(BaseModel):
    resale_price_per_unit: DecimalString
    gross_revenue: DecimalString
    gross_purchase_cost: DecimalString
    acquisition_cost_breakdown: list[CalculatedCost]
    additional_acquisition_cost: DecimalString
    total_acquisition_cost: DecimalString
    effective_acquisition_cost_per_unit: DecimalString
    selling_cost_breakdown: list[CalculatedCost]
    total_selling_cost: DecimalString
    net_sale_proceeds: DecimalString
    net_profit: DecimalString
    profit_per_unit: DecimalString
    roi_percent: DecimalString | None
    profit_margin_percent: DecimalString | None


class InputQualitySummary(BaseModel):
    human_observed_inputs: list[str] = Field(default_factory=list)
    verified_inputs: list[str]
    calculated_inputs: list[str]
    estimated_inputs: list[str]
    assumed_inputs: list[str]
    unknown_inputs: list[str]


class SensitivityResult(BaseModel):
    cost_id: str
    cost_name: str
    scenario_side: ScenarioSide
    original_value: DecimalString
    low_value: DecimalString
    high_value: DecimalString
    low_value_case: ProfitScenarioResult
    high_value_case: ProfitScenarioResult
    net_profit_change_low: DecimalString
    net_profit_change_high: DecimalString


class ProfitabilityResult(BaseModel):
    status: ProfitabilityStatus
    currency: str
    quantity: int
    low_case: ProfitScenarioResult | None
    expected_case: ProfitScenarioResult | None
    high_case: ProfitScenarioResult | None
    input_quality: InputQualitySummary
    sensitivity_results: list[SensitivityResult]
    warnings: list[str]
    notes: str | None

    @model_validator(mode="after")
    def validate_status_cases(self) -> "ProfitabilityResult":
        if self.status == ProfitabilityStatus.INSUFFICIENT_INPUTS:
            if any(case is not None for case in (self.low_case, self.expected_case, self.high_case)):
                raise ValueError("INSUFFICIENT_INPUTS must not contain profitability cases")
            if self.sensitivity_results:
                raise ValueError("INSUFFICIENT_INPUTS must not contain sensitivity results")
            if not self.warnings:
                raise ValueError("INSUFFICIENT_INPUTS must identify its limitations in warnings")
        elif self.low_case is None or self.high_case is None:
            raise ValueError("COMPLETE and PARTIAL results require low and high cases")
        elif self.status == ProfitabilityStatus.PARTIAL and not self.warnings:
            raise ValueError("PARTIAL results must identify omitted unknowns in warnings")
        return self


class ProfitabilityToolResult(BaseModel):
    success: bool
    result: ProfitabilityResult | None
    error: str | None

    @model_validator(mode="after")
    def validate_outcome(self) -> "ProfitabilityToolResult":
        if self.success and (self.result is None or self.error is not None):
            raise ValueError("successful tool output requires result and no error")
        if not self.success and (self.result is not None or not (self.error or "").strip()):
            raise ValueError("failed tool output requires error and no result")
        return self


class CandidateSourceKind(str, Enum):
    PHYSICAL_STORE = "PHYSICAL_STORE"
    ONLINE_RETAILER = "ONLINE_RETAILER"
    MARKETPLACE_SELLER = "MARKETPLACE_SELLER"
    HUMAN_OWNED = "HUMAN_OWNED"
    OTHER = "OTHER"


class CandidateSource(BaseModel):
    source_kind: CandidateSourceKind
    name: str
    physical_location: str | None
    url: str | None
    seller_identity: str | None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value

    @model_validator(mode="after")
    def validate_physical_location(self) -> "CandidateSource":
        if self.source_kind == CandidateSourceKind.PHYSICAL_STORE and not (
            self.physical_location or ""
        ).strip():
            raise ValueError("physical_location is required for PHYSICAL_STORE")
        return self


class CandidateDestinationKind(str, Enum):
    MARKETPLACE = "MARKETPLACE"
    LOCAL_MARKETPLACE = "LOCAL_MARKETPLACE"
    STOREFRONT = "STOREFRONT"
    OTHER = "OTHER"


class CandidateDestination(BaseModel):
    destination_kind: CandidateDestinationKind
    name: str
    url: str | None
    seller_account: str | None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


class CandidateLifecycleStatus(str, Enum):
    INVESTIGATING = "INVESTIGATING"
    AWAITING_HUMAN_INPUT = "AWAITING_HUMAN_INPUT"
    EVALUATED = "EVALUATED"
    CLOSED = "CLOSED"


class CandidateRecord(BaseModel):
    candidate_id: str
    product_identity: ProductIdentity
    acquisition_source: CandidateSource
    resale_destination: CandidateDestination
    intake_origin: CandidateIntakeSource
    lifecycle_status: CandidateLifecycleStatus
    created_at: datetime
    updated_at: datetime
    latest_evaluation_id: str | None
    notes: str | None

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_timestamps(self) -> "CandidateRecord":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class EvaluationTrigger(str, Enum):
    HUMAN_REQUEST = "HUMAN_REQUEST"
    MANUAL_REFRESH = "MANUAL_REFRESH"
    AUTONOMOUS_DISCOVERY = "AUTONOMOUS_DISCOVERY"
    AUTONOMOUS_REFRESH = "AUTONOMOUS_REFRESH"


class EvaluationStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    AWAITING_HUMAN_INPUT = "AWAITING_HUMAN_INPUT"
    COMPLETED = "COMPLETED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    FAILED = "FAILED"


class CandidateEvaluation(BaseModel):
    evaluation_id: str
    candidate_id: str
    trigger: EvaluationTrigger
    status: EvaluationStatus
    started_at: datetime
    completed_at: datetime | None
    intake_snapshot: CandidateIntake | None
    sourcing_result: SourcingResult | None
    resale_result: ResaleResult | None
    profitability_result: ProfitabilityResult | None
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    manager_notes: str | None

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_status_and_timestamps(self) -> "CandidateEvaluation":
        terminal = {
            EvaluationStatus.COMPLETED,
            EvaluationStatus.INSUFFICIENT_EVIDENCE,
            EvaluationStatus.FAILED,
        }
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.status in terminal and self.completed_at is None:
            raise ValueError("terminal evaluations require completed_at")
        if self.status not in terminal and self.completed_at is not None:
            raise ValueError("nonterminal evaluations must not have completed_at")
        return self


class WorkflowEvaluationAction(str, Enum):
    AWAIT_HUMAN_INPUT = "AWAIT_HUMAN_INPUT"
    RESUME = "RESUME"


class StartCandidateEvaluationRequest(BaseModel):
    product_identity: ProductIdentity
    acquisition_source: CandidateSource
    resale_destination: CandidateDestination
    intake_origin: CandidateIntakeSource
    intake_snapshot: CandidateIntake | None
    trigger: EvaluationTrigger
    assumptions: list[str]
    uncertainties: list[str]
    candidate_notes: str | None
    manager_notes: str | None


class UpdateCandidateEvaluationRequest(BaseModel):
    action: WorkflowEvaluationAction


class FinishCandidateEvaluationRequest(BaseModel):
    status: EvaluationStatus
    intake_snapshot: CandidateIntake | None
    sourcing_result: SourcingResult | None
    resale_result: ResaleResult | None
    profitability_result: ProfitabilityResult | None
    assumptions: list[str]
    uncertainties: list[str]
    manager_notes: str | None

    @model_validator(mode="after")
    def validate_terminal_status(self) -> "FinishCandidateEvaluationRequest":
        if self.status not in {
            EvaluationStatus.COMPLETED,
            EvaluationStatus.INSUFFICIENT_EVIDENCE,
            EvaluationStatus.FAILED,
        }:
            raise ValueError("finish requires a terminal EvaluationStatus")
        return self


class CandidateWorkflowResult(BaseModel):
    success: bool
    candidate_id: str | None
    evaluation_id: str | None
    candidate_lifecycle: CandidateLifecycleStatus | None
    evaluation_status: EvaluationStatus | None
    active: bool
    error: str | None


class LeadDisposition(str, Enum):
    NEEDS_MORE_INFO = "NEEDS_MORE_INFO"
    CANDIDATE_READY = "CANDIDATE_READY"
    EXISTING_CANDIDATE = "EXISTING_CANDIDATE"
    AMBIGUOUS_BOUNDARY = "AMBIGUOUS_BOUNDARY"
    STOP = "STOP"


class CandidateRelation(str, Enum):
    NO_ACTIVE_CANDIDATE = "NO_ACTIVE_CANDIDATE"
    SAME_AS_ACTIVE = "SAME_AS_ACTIVE"
    CLEARLY_NEW = "CLEARLY_NEW"
    AMBIGUOUS = "AMBIGUOUS"


class LeadDecision(BaseModel):
    disposition: LeadDisposition
    relation_to_active: CandidateRelation
    reasoning: str
    continue_substantive_evaluation: bool
    candidate_request: StartCandidateEvaluationRequest | None
    clarification_question: str | None
    user_message: str | None
    unresolved_uncertainties: list[str]

    @model_validator(mode="after")
    def validate_decision(self) -> "LeadDecision":
        has_question = bool((self.clarification_question or "").strip())
        has_message = bool((self.user_message or "").strip())
        if not self.reasoning.strip():
            raise ValueError("reasoning must not be blank")
        if self.disposition == LeadDisposition.CANDIDATE_READY:
            if self.candidate_request is None:
                raise ValueError("CANDIDATE_READY requires candidate_request")
            if not self.continue_substantive_evaluation or has_question:
                raise ValueError(
                    "CANDIDATE_READY requires substantive evaluation and no clarification"
                )
            if self.relation_to_active == CandidateRelation.AMBIGUOUS:
                raise ValueError("CANDIDATE_READY cannot have an ambiguous relation")
        elif self.disposition == LeadDisposition.EXISTING_CANDIDATE:
            if self.candidate_request is not None:
                raise ValueError("EXISTING_CANDIDATE must not include candidate_request")
            if not self.continue_substantive_evaluation or has_question:
                raise ValueError(
                    "EXISTING_CANDIDATE requires substantive evaluation and no clarification"
                )
            if self.relation_to_active != CandidateRelation.SAME_AS_ACTIVE:
                raise ValueError("EXISTING_CANDIDATE requires SAME_AS_ACTIVE")
        elif self.disposition == LeadDisposition.NEEDS_MORE_INFO:
            if self.candidate_request is not None or not has_question:
                raise ValueError(
                    "NEEDS_MORE_INFO requires a clarification and no candidate_request"
                )
            if self.continue_substantive_evaluation:
                raise ValueError("NEEDS_MORE_INFO must not continue substantive evaluation")
        elif self.disposition == LeadDisposition.AMBIGUOUS_BOUNDARY:
            if self.candidate_request is not None or not has_question:
                raise ValueError(
                    "AMBIGUOUS_BOUNDARY requires a clarification and no candidate_request"
                )
            if self.continue_substantive_evaluation:
                raise ValueError("AMBIGUOUS_BOUNDARY must not continue substantive evaluation")
            if self.relation_to_active != CandidateRelation.AMBIGUOUS:
                raise ValueError("AMBIGUOUS_BOUNDARY requires an AMBIGUOUS relation")
        elif self.disposition == LeadDisposition.STOP:
            if self.candidate_request is not None or self.continue_substantive_evaluation:
                raise ValueError("STOP must not include a request or continue evaluation")
            if has_question:
                raise ValueError("STOP must not request clarification")
            if not has_message:
                raise ValueError("STOP requires user_message")
        return self
