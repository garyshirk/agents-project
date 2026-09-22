from enum import Enum

from pydantic import BaseModel, model_validator


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
