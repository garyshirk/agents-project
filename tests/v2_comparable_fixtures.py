"""Fictional evidence only. No values, URLs, or observations came from research."""

from datetime import datetime, timezone

from arbitrage.v2.contracts import (
    AttributeComparison, ComparableDraft, ComparableProduct, ComparableSource,
    PriceEvidence, ProductAttribute,
)


def synthetic_comparables(raw_reference=None):
    refs = (raw_reference,) if raw_reference else ()
    product = ComparableProduct(description="Synthetic Nike Air Max 90", brand="Nike",
        model_identifiers=(ProductAttribute(name="style", value="CN8490-002"),),
        variant_attributes=(ProductAttribute(name="size", value="US men's 10"),
                            ProductAttribute(name="colorway", value="fictional red/white")),
        condition="NEW", packaging="complete original box", package_quantity=1)
    source = ComparableSource(source_category="MARKETPLACE", source_name="Fictional Marketplace",
        seller_name="Synthetic seller", url="https://market.example.invalid/listing-a",
        capture_method="SYNTHETIC_FIXTURE", observed_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        raw_artifact_references=refs, limitations=("Entirely synthetic; no marketplace research or verification.",))
    difference = AttributeComparison(attribute="colorway", candidate_value="black/white", comparable_value="fictional red/white",
        relation="KNOWN_DIFFERENCE", explanation="Different colors in this fictional fixture; no price adjustment implied.")
    a = ComparableDraft(product=product, source=source, product_comparisons=(difference,), price_evidence=PriceEvidence(
        evidence_type="COMPLETED_SALE", amount="101.23", currency="USD", quantity_or_unit_basis="one pair",
        shipping_treatment="EXCLUDED", completion_basis="Synthetic completion statement, not real evidence.",
        realized_price_basis="Synthetic transaction amount, not real evidence."))
    b = ComparableDraft(product=product.model_copy(update={"variant_attributes":(
        ProductAttribute(name="size", value="US men's 10"), ProductAttribute(name="width", value=None))}),
        source=source.model_copy(update={"url":"https://market.example.invalid/listing-b"}),
        product_comparisons=(AttributeComparison(attribute="width", relation="UNRESOLVED", explanation="Width is unknown."),),
        price_evidence=PriceEvidence(evidence_type="ACTIVE_ASK", amount="112.34", currency="USD", quantity_or_unit_basis="one pair"))
    c = ComparableDraft(product=product.model_copy(update={"condition":"USED", "packaging":None,
        "package_quantity":2, "bundle_contents":("two pairs", "accessories unknown")}),
        source=source.model_copy(update={"url":None, "other_reference":"Fictional offline catalog note C"}),
        product_comparisons=(AttributeComparison(attribute="condition", candidate_value="NEW", comparable_value="USED",
            relation="KNOWN_DIFFERENCE", explanation="Different condition; price effect not established."),),
        price_evidence=PriceEvidence(evidence_type="OTHER_QUALIFIED", context=("No numerical price supplied.",)))
    return a, b, c
