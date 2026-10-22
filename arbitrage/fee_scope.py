"""Pure marketplace/payment-fee overlap guard; amounts are never edited."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from arbitrage.contracts import EconomicCostFindingsResult, FeeRelationship, UnknownMateriality


# Existing IDs recognized for fail-closed historical handling. New/custom components
# are explicitly linked by the declaration, not classified from free-text names.
MARKETPLACE_IDS = {"marketplace_fee", "marketplace_transaction_fee", "marketplace_final_value_fee"}
PROCESSING_IDS = {"payment_processing_charge", "payment_processing_fee", "processing_fee"}


@dataclass(frozen=True)
class FeeScopeValidation:
    valid: bool
    category: str
    reason: str
    cost_ids: tuple[str, ...]


def _numeric(finding):
    return any(value is not None for value in (
        finding.value, finding.estimated_low, finding.estimated_high, finding.modeled_value))


def validate_marketplace_fee_scope(result: EconomicCostFindingsResult) -> FeeScopeValidation:
    scope = result.marketplace_fee_scope
    present = {finding.cost_id for finding in result.findings}
    recognized = present & (MARKETPLACE_IDS | PROCESSING_IDS)

    def fail(category, reason, ids=None):
        return FeeScopeValidation(False, category, reason, tuple(sorted(ids or recognized)))

    if scope is None:
        if present & PROCESSING_IDS:
            return fail("FEE_SCOPE_UNRESOLVED", "Processing applicability requires explicit relationship and supporting evidence; distinct IDs do not establish additive charges.")
        return FeeScopeValidation(True, "NO_OVERLAP_DECLARED", "No recognized separate processing finding; no integrated/separate relationship inferred.", tuple(sorted(recognized)))
    market = scope.marketplace_fee_cost_ids
    processing = scope.processing_fee_cost_ids
    linked = market + processing
    if (not scope.rationale.strip() or not market or len(linked) != len(set(linked))
            or any(not key.strip() or key not in present for key in linked)
            or not recognized.issubset(linked)
            or set(market) & PROCESSING_IDS or set(processing) & MARKETPLACE_IDS):
        return fail("FEE_SCOPE_CONFLICT", "Fee scope has missing, duplicate, contradictory, or unlinked cost identifiers/rationale.", linked)
    for key in linked:
        if sum(finding.cost_id == key for finding in result.findings) != 1:
            return fail("FEE_SCOPE_CONFLICT", "Fee scope must reference each finding exactly once.", linked)
    for evidence in scope.evidence:
        try:
            url = urlsplit(evidence.reference.url)
            url.port  # Reject malformed explicit ports as well as malformed host syntax.
            valid = (url.scheme in {"https", "http"} and bool(url.hostname) and not url.username
                     and not url.password and not any(char.isspace() for char in evidence.reference.url)
                     and bool(evidence.reference.description.strip())
                     and bool(evidence.applicability_explanation.strip()))
            # A known product-listing URL is not seller fee documentation, even if mislabeled.
            ebay_listing = url.hostname and (url.hostname == "ebay.com" or url.hostname.endswith(".ebay.com")) and url.path.startswith(("/itm/", "/p/"))
        except ValueError:
            valid, ebay_listing = False, False
        if not valid or evidence.source_kind == "PRODUCT_LISTING" or ebay_listing:
            return fail("FEE_SCOPE_CONFLICT", "Fee evidence must be valid fee terms or transaction-fee evidence, not a product listing.", linked)
    related = [finding for finding in result.findings if finding.cost_id in processing]
    if scope.relationship in {FeeRelationship.INTEGRATED, FeeRelationship.SEPARATELY_APPLICABLE} and not scope.evidence:
        return fail("FEE_SCOPE_UNRESOLVED", "Integrated/separate applicability requires structured fee evidence and rationale.", linked)
    if scope.relationship == FeeRelationship.SEPARATELY_APPLICABLE:
        if not processing:
            return fail("FEE_SCOPE_CONFLICT", "Separate applicability requires identified processing finding(s).", linked)
        return FeeScopeValidation(True, scope.relationship.value, "Separate applicability is explicitly supported; evidence truth is not independently verified by Python.", tuple(linked))
    if any(_numeric(finding) for finding in related):
        return fail("FEE_SCOPE_CONFLICT", "Integrated or unresolved processing must not contain an additive exact, ranged, or modeled amount; revise capture rather than deleting/zeroing it.", linked)
    if any(finding.unresolved_materiality != UnknownMateriality.MATERIAL for finding in related):
        return fail("FEE_SCOPE_UNRESOLVED", "Unknown processing applicability/amount must remain MATERIAL unresolved.", linked)
    if scope.relationship == FeeRelationship.UNRESOLVED and not related:
        return fail("FEE_SCOPE_UNRESOLVED", "Unresolved processing applicability requires an explicit MATERIAL unknown finding.", linked)
    return FeeScopeValidation(True, scope.relationship.value, "No additive processing amount; represented MATERIAL uncertainty is preserved.", tuple(linked))
