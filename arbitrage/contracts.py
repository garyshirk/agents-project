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

    @field_validator("name")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


def _validate_unique_cost_ids(costs: list[CostComponent], field_name: str) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for cost in costs:
        if cost.cost_id in seen and cost.cost_id not in duplicates:
            duplicates.append(cost.cost_id)
        seen.add(cost.cost_id)
    if duplicates:
        raise ValueError(f"{field_name} contains duplicate cost_id values: {duplicates}")


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
