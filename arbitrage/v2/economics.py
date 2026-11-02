"""Pure explicit-scenario mapping. No research, identity gate, or economic judgment."""

from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError

from arbitrage.contracts import (
    AcquisitionScenario, CostComponent, CostType, ProductIdentity, ProfitabilityRequest,
    SaleScenario,
)
from arbitrage.tools.profitability import _calculate_profitability
from arbitrage.v2.contracts import CalculationOutcome, ComputationStatus, FailureRecord, ScenarioAssembly, ScenarioDraft


ADAPTER_VERSION = "v2-explicit-scenario-v1"
# Version the actual unchanged engine source, not a guessed package release.
ENGINE_VERSION = "sha256:" + sha256(
    (Path(__file__).resolve().parents[1] / "tools" / "profitability.py").read_bytes()).hexdigest()

REQUIRED_CATEGORIES = {
    "PURCHASE_PRICE", "RESALE_PRICE", "MARKETPLACE_FEE", "PAYMENT_FEE",
    "SELLER_SHIPPING", "ACQUISITION_TAX", "ACQUISITION_TRAVEL",
}
ACQUISITION_CATEGORIES = {"PURCHASE_PRICE", "ACQUISITION_TAX", "ACQUISITION_TRAVEL"}
SALE_CATEGORIES = {"RESALE_PRICE", "MARKETPLACE_FEE", "PAYMENT_FEE", "SELLER_SHIPPING"}


def assemble_scenario(draft: ScenarioDraft) -> ScenarioAssembly:
    """Construct only an explicit engine request, without executing arithmetic."""
    draft = ScenarioDraft.model_validate(draft.model_dump())
    errors, unsupported, unresolved = [], [], []
    ids = [item.input_id for item in draft.inputs]
    if len(ids) != len(set(ids)):
        errors.append("Duplicate economic input identifiers")
    by_id = {item.input_id: item for item in draft.inputs}
    fee_items = {i.category: i for i in draft.inputs if i.category in {"MARKETPLACE_FEE", "PAYMENT_FEE"}}
    market, payment = fee_items.get("MARKETPLACE_FEE"), fee_items.get("PAYMENT_FEE")
    if market and payment and payment.disposition in {"EVIDENCE", "ASSUMPTION", "INCLUDED"}:
        if not draft.fee_relationship_rationale:
            errors.append("Fee relationship requires explicit rationale")
        if draft.fee_relationship == "INTEGRATED":
            if payment.disposition != "INCLUDED" or payment.covered_by != market.input_id:
                errors.append("Integrated payment fee cannot be added separately")
        elif draft.fee_relationship == "SEPARATE":
            if payment.disposition == "INCLUDED":
                errors.append("Separate fee declaration conflicts with integrated coverage")
        else:
            errors.append("Additive/included payment fee requires explicit integrated/separate scope")
    if len(draft.currency) != 3 or not draft.currency.isascii() or not draft.currency.isalpha() or not draft.currency.isupper():
        errors.append("Currency must be an explicit three-letter uppercase code")
    if draft.order_count != 1:
        unsupported.append("Engine supports one modeled order/batch; multiple orders cannot be silently combined")
    for category in sorted(REQUIRED_CATEGORIES):
        matches = [item for item in draft.inputs if item.category == category]
        if not matches:
            unresolved.append(f"Missing explicit disposition for {category}")
        elif len(matches) != 1:
            errors.append(f"Exactly one declaration required for {category}")
    for item in draft.inputs:
        if len(item.covers) != len(set(item.covers)):
            errors.append(f"Duplicate coverage: {item.input_id}")
        for key in item.covers:
            covered = by_id.get(key)
            if covered is None or covered.disposition != "INCLUDED" or covered.covered_by != item.input_id:
                errors.append(f"Covered component must be declared INCLUDED, never additive: {key}")
        if ((item.category in ACQUISITION_CATEGORIES and item.side != "ACQUISITION")
                or (item.category in SALE_CATEGORIES and item.side != "SALE")):
            errors.append(f"Wrong economic side: {item.input_id}")
        if item.currency is not None and item.currency != draft.currency:
            errors.append(f"Currency mismatch: {item.input_id}; no currency conversion")
        if item.amount is not None and item.currency is None:
            errors.append(f"Numeric input requires explicit currency (including percentage base): {item.input_id}")
        if item.disposition == "UNKNOWN" and item.material:
            unresolved.append(item.input_id + ": " + item.rationale)
        if item.category in {"PURCHASE_PRICE", "RESALE_PRICE"}:
            if item.cost_type != CostType.FIXED_PER_UNIT:
                unsupported.append(f"Engine requires explicit per-unit price: {item.input_id}")
            if item.disposition not in {"EVIDENCE", "ASSUMPTION", "UNKNOWN"}:
                errors.append(f"Price cannot be included/not-applicable: {item.input_id}")
            if item.amount is None:
                unresolved.append(f"Price unresolved: {item.input_id}")
        if item.disposition == "INCLUDED":
            covering = by_id.get(item.covered_by)
            if (covering is None or covering.input_id == item.input_id or covering.side != item.side
                    or covering.disposition not in {"EVIDENCE", "ASSUMPTION"} or item.input_id not in covering.covers):
                errors.append(f"Coverage must name a numeric component on the same side: {item.input_id}")
            if covering and covering.category == "RESALE_PRICE":
                errors.append(f"Resale revenue cannot cover/erase seller expense: {item.input_id}")
            if item.category in {"MARKETPLACE_FEE", "PAYMENT_FEE"} and covering and covering.category not in {"MARKETPLACE_FEE", "PAYMENT_FEE"}:
                errors.append(f"Fee coverage must reference a fee: {item.input_id}")
    # A known unknown can only be resolved here by an explicitly named assumption.
    for unknown in draft.known_unknowns:
        replacement = by_id.get(unknown.name)
        if replacement is None or replacement.disposition != "ASSUMPTION":
            unresolved.append(unknown.name + ": " + unknown.reason)
    common = dict(unresolved_inputs=tuple(dict.fromkeys(unresolved)),
                  validation_errors=tuple(errors), unsupported_mappings=tuple(unsupported))
    if errors or unsupported:
        return ScenarioAssembly(status="INVALID_INPUTS", **common)
    if unresolved:
        return ScenarioAssembly(status="INSUFFICIENT_INPUTS", **common)
    purchase = next(i for i in draft.inputs if i.category == "PURCHASE_PRICE")
    resale = next(i for i in draft.inputs if i.category == "RESALE_PRICE")
    costs = {side: [] for side in ("ACQUISITION", "SALE")}
    assumptions = {side: [] for side in costs}
    unknowns = {side: [] for side in costs}
    for item in draft.inputs:
        if item.disposition == "ASSUMPTION":
            assumptions[item.side].append(f"{item.input_id}: {item.rationale}")
        if item.disposition == "UNKNOWN":
            unknowns[item.side].append(dict(name=item.input_id, materiality="NON_MATERIAL", notes=item.rationale))
        if item.category in {"PURCHASE_PRICE", "RESALE_PRICE"} or item.amount is None:
            continue
        costs[item.side].append(CostComponent(cost_id=item.input_id, name=item.category,
            cost_type=item.cost_type, value=item.amount, basis=item.basis, notes=item.rationale))
    try:
        request = ProfitabilityRequest(
            acquisition=AcquisitionScenario(
                product_identity=ProductIdentity(brand=None, product_name=draft.product_description,
                    model_number=None, upc_gtin=None, variant=None, condition="UNKNOWN", package_quantity=None),
                source_type="OTHER", source_name="Explicit V2 scenario", source_references=[], condition="UNKNOWN",
                currency=draft.currency, unit_purchase_price=purchase.amount, purchase_price_basis=purchase.basis,
                quantity=draft.quantity, additional_costs=costs["ACQUISITION"], quantity_available=None,
                quantity_available_basis=None, purchase_limit=None, purchase_requirements=[],
                assumptions=assumptions["ACQUISITION"], unknowns=unknowns["ACQUISITION"], notes=draft.unit_description),
            sale=SaleScenario(marketplace_name=draft.destination, marketplace_references=[], currency=draft.currency,
                target_condition="UNKNOWN", resale_price_low=resale.amount, resale_price_high=resale.amount,
                resale_price_expected=None, resale_price_basis=resale.basis, selling_costs=costs["SALE"],
                quantity=draft.quantity, assumptions=assumptions["SALE"], unknowns=unknowns["SALE"], notes=draft.unit_description),
            sensitivity_inputs=[])
    except ValidationError as error:
        return ScenarioAssembly(status="INVALID_INPUTS", validation_errors=(str(error),))
    return ScenarioAssembly(status="READY", request_json=request.model_dump_json())


def calculate_scenario(draft: ScenarioDraft) -> CalculationOutcome:
    assembly = assemble_scenario(draft)
    if assembly.request_json is None:
        return CalculationOutcome.model_validate(assembly.model_dump())
    try:
        request = ProfitabilityRequest.model_validate_json(assembly.request_json)
        result = _calculate_profitability(request)
        if result.status.value == "INSUFFICIENT_INPUTS":
            return CalculationOutcome(status="INSUFFICIENT_INPUTS", request_json=assembly.request_json,
                                      unresolved_inputs=tuple(result.warnings))
        return CalculationOutcome(status=ComputationStatus.CALCULATED, request_json=assembly.request_json,
                                  engine_output_json=result.model_dump_json())
    except Exception as error:
        return CalculationOutcome(status="ERROR", request_json=assembly.request_json,
            failure=FailureRecord(stage="profitability_engine", category=type(error).__name__, message=str(error) or repr(error)))
