"""Pure outbound-shipping completeness policy; never synthesizes a cost."""

from dataclasses import dataclass
from enum import Enum

from arbitrage.contracts import CostType, EconomicCostFindingsResult, InputBasis, UnknownMateriality


OUTBOUND_SHIPPING_COST_ID = "seller_paid_outbound_shipping"


class ShippingCoverageStatus(str, Enum):
    COVERED_COST = "COVERED_COST"
    COVERED_MATERIAL_UNRESOLVED = "COVERED_MATERIAL_UNRESOLVED"
    COVERED_NOT_APPLICABLE = "COVERED_NOT_APPLICABLE"
    MISSING_COVERAGE = "MISSING_COVERAGE"
    INVALID_COVERAGE = "INVALID_COVERAGE"


@dataclass(frozen=True)
class ShippingCoverage:
    status: ShippingCoverageStatus
    reason: str

    @property
    def valid(self):
        return self.status not in {ShippingCoverageStatus.MISSING_COVERAGE,
                                   ShippingCoverageStatus.INVALID_COVERAGE}


def validate_outbound_shipping(result: EconomicCostFindingsResult, currency: str) -> ShippingCoverage:
    matches = [finding for finding in result.findings if finding.cost_id == OUTBOUND_SHIPPING_COST_ID]
    disposition = result.outbound_shipping_nonapplicability
    invalid = ShippingCoverageStatus.INVALID_COVERAGE
    if any(finding.cost_id in {"shipping", "outbound_shipping"} for finding in result.findings):
        return ShippingCoverage(invalid, "Ambiguous shipping identifier; revise capture with explicit seller-paid outbound identification.")
    if len(matches) > 1 or (matches and disposition is not None):
        return ShippingCoverage(invalid, "Duplicate or contradictory outbound-shipping coverage.")
    if disposition is not None:
        if (not disposition.rationale.strip()
                or disposition.basis not in {InputBasis.VERIFIED, InputBasis.HUMAN_OBSERVED}
                or not disposition.source_references
                or any(not ref.url.strip() or not ref.description.strip() for ref in disposition.source_references)):
            return ShippingCoverage(invalid, "Nonapplicability requires a rationale, observed/verified basis, and source provenance.")
        return ShippingCoverage(ShippingCoverageStatus.COVERED_NOT_APPLICABLE,
                                "Explicit evidence-supported nonapplicability; no expense synthesized.")
    if not matches:
        return ShippingCoverage(ShippingCoverageStatus.MISSING_COVERAGE,
                                f"Require {OUTBOUND_SHIPPING_COST_ID} or supported nonapplicability; generic/buyer-facing shipping is insufficient.")
    finding = matches[0]
    if finding.cost_type not in {CostType.FIXED_PER_UNIT, CostType.FIXED_PER_BATCH} or finding.currency != currency:
        return ShippingCoverage(invalid, "Outbound shipping requires a fixed cost type and matching scenario currency.")
    if finding.value is not None and (finding.modeled_value is not None or finding.unresolved_materiality is not None):
        return ShippingCoverage(invalid, "Exact shipping value conflicts with modeled or unresolved value.")
    numeric = any(v is not None for v in (finding.value, finding.estimated_low, finding.modeled_value))
    if numeric:
        if not (finding.source_references or finding.limitations or (finding.notes or "").strip()):
            return ShippingCoverage(invalid, "Shipping amount requires explicit support or modeling rationale.")
        if any(v == 0 for v in (finding.value, finding.estimated_low, finding.modeled_value) if v is not None):
            if (finding.basis not in {InputBasis.VERIFIED, InputBasis.HUMAN_OBSERVED}
                    or not finding.source_references):
                return ShippingCoverage(invalid, "Zero shipping assertions require observed/verified basis and source evidence.")
        return ShippingCoverage(ShippingCoverageStatus.COVERED_COST, "Explicit seller-paid outbound-shipping cost.")
    if finding.unresolved_materiality == UnknownMateriality.MATERIAL:
        return ShippingCoverage(ShippingCoverageStatus.COVERED_MATERIAL_UNRESOLVED,
                                "Seller shipping amount or responsibility remains MATERIAL unresolved.")
    return ShippingCoverage(invalid, "Unknown seller shipping must remain MATERIAL unresolved.")
