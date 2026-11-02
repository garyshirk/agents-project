"""Entirely fictional economic inputs; never live or historical business evidence."""

from arbitrage.v2.contracts import (
    AssumptionRecord, EconomicAssumption, EconomicInput, Provenance, ScenarioDraft,
)


def provenance(assumed=False, **changes):
    return Provenance(**(dict(information_kind="AGENT_ASSUMPTION" if assumed else "REPORTED_INFORMATION",
        sources=("MODEL_GENERAL_KNOWLEDGE",) if assumed else ("HUMAN_PROVIDED",),
        rationale="Synthetic offline fixture, not real economic evidence.") | changes))


def scenario(raw, comparable, **changes):
    """Explicit one-unit fixture; no inferred, averaged, or generated scenario values."""
    def value(key, category, side, amount, cost_type="FIXED_PER_UNIT", **options):
        return EconomicInput(input_id=key, category=category, side=side, disposition="EVIDENCE",
            amount=amount, currency="USD", cost_type=cost_type, basis="ESTIMATED",
            provenance=provenance(), evidence_references=(comparable if category == "RESALE_PRICE" else raw,),
            rationale="Fictional explicit amount supplied by the fixture author.", **options)

    def na(key, category, side):
        return EconomicInput(input_id=key, category=category, side=side, disposition="NOT_APPLICABLE",
            cost_type="FIXED_PER_UNIT", provenance=provenance(),
            rationale="Synthetic scenario explicitly excludes this charge; no zero invented.")

    return ScenarioDraft(**(dict(name="Synthetic explicit scenario", currency="USD", quantity=1,
        unit_description="One fictional item sold in one order.", product_description="Synthetic product, unspecified variant",
        destination="Fictional resale channel", policy_version="synthetic-scenario-policy-v1",
        comparable_references=(comparable,), evidence_references=(raw,),
        inputs=(value("purchase", "PURCHASE_PRICE", "ACQUISITION", "40"),
                value("resale", "RESALE_PRICE", "SALE", "100"),
                value("marketplace", "MARKETPLACE_FEE", "SALE", "10", "PERCENT_OF_UNIT_PRICE"),
                na("processing", "PAYMENT_FEE", "SALE"),
                value("shipping", "SELLER_SHIPPING", "SALE", "8", "FIXED_PER_BATCH"),
                value("tax", "ACQUISITION_TAX", "ACQUISITION", "5", "PERCENT_OF_UNIT_PRICE"),
                na("travel", "ACQUISITION_TRAVEL", "ACQUISITION"))) | changes))


def replace_input(draft, key, **changes):
    return draft.model_copy(update={"inputs":tuple(
        EconomicInput.model_validate(i.model_dump() | changes) if i.input_id == key else i for i in draft.inputs)})


def assumed_input(repo, eid, producer, draft, key, amount, operation_key, *, support=None):
    original = next(i for i in draft.inputs if i.input_id == key)
    support = support or provenance(True)
    assumption = repo.record_artifact(eid, AssumptionRecord(assumption=EconomicAssumption(
        name=key, explanation="Explicit synthetic scenario-only assumption.", amount=amount,
        unit=f"USD/{original.cost_type.value}", provenance=support)), producer=producer, operation_key=operation_key)
    return replace_input(draft, key, disposition="ASSUMPTION", amount=amount, basis="ASSUMED",
                         provenance=support, assumption=assumption.reference)
