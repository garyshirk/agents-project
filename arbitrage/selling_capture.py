import json
from dataclasses import dataclass

from agents import (
    Agent,
    ModelSettings,
    RunConfig,
    RunContextWrapper,
    Runner,
    function_tool,
)

from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    EconomicCostFinding,
    EconomicCostFindingsResult,
    EvaluationArtifactType,
    ResaleResult,
    OutboundShippingNonapplicability,
    MarketplaceFeeScope,
)
from arbitrage.selling_capture_state import SellingCaptureState


@dataclass
class SellingCaptureContext:
    candidate_workflow: CandidateWorkflow
    capture_state: SellingCaptureState


def selling_capture_tool_error(
    _context: RunContextWrapper[SellingCaptureContext], error: Exception
) -> str:
    return f"Selling cost findings were not recorded: {error}"


@function_tool(
    name_override="record_selling_cost_findings",
    failure_error_function=selling_capture_tool_error,
)
def record_captured_selling_cost_findings(
    context: RunContextWrapper[SellingCaptureContext],
    findings: list[EconomicCostFinding],
    outbound_shipping_nonapplicability: OutboundShippingNonapplicability | None = None,
    marketplace_fee_scope: MarketplaceFeeScope | None = None,
) -> str:
    """Record all defensible selling-side cost findings for this Resale result."""
    state = context.context.capture_state
    state.begin_action()
    result = EconomicCostFindingsResult(
        findings=findings, outbound_shipping_nonapplicability=outbound_shipping_nonapplicability,
        marketplace_fee_scope=marketplace_fee_scope,
    )
    context_json = json.dumps(
        {
            "tool_call_id": getattr(context, "tool_call_id", None),
            "tool_name": getattr(context, "tool_name", None),
        }
    )
    context.context.candidate_workflow.record_economic_cost_findings(
        EvaluationArtifactType.SELLING_COST_FINDINGS,
        result,
        context_json=context_json,
    )
    state.select(result)
    return f"Recorded {len(findings)} selling-side economic cost finding(s)."


selling_capture_agent = Agent(
    name="Selling Capture Agent",
    instructions=(
        "Explicitly analyze marketplace/payment-processing fee scope. Use marketplace_transaction_fee "
        "for the marketplace fee and payment_processing_charge only for an additional or MATERIAL-unknown "
        "processing charge; link all percentage and fixed-per-order components in marketplace_fee_scope. "
        "Declare INTEGRATED when processing is included, SEPARATELY_APPLICABLE only when evidence "
        "establishes an additional charge, or UNRESOLVED when the relationship is unknown. Give a "
        "rationale, marketplace_fee_cost_ids, processing_fee_cost_ids, and structured evidence with "
        "source_kind FEE_SCHEDULE, ACCOUNT_TERMS, or TRANSACTION_FEE_STATEMENT, source reference, and "
        "applicability_explanation. Product listings do not establish seller fee terms. Never assume "
        "independence from distinct IDs, or apply one marketplace's fee model to another. An integrated "
        "fee must not have a second additive processing amount. Unresolved processing applicability "
        "must have an explicit MATERIAL unknown finding, no numeric processing amount or model, and "
        "no invented zero. Preserve legitimately separate charges and unknown amounts; application "
        "validation enforces overlap safety. "
        "Explicitly address seller-paid outbound shipping for one resale order. Use the cost_id "
        "seller_paid_outbound_shipping with a supported fixed per-unit/per-order cost, supported "
        "estimate, or documented ASSUMED model. If amount or responsibility is unknown, record that "
        "finding with no amount/model and MATERIAL unresolved materiality, in the scenario currency. "
        "Do not use generic shipping, acquisition freight, buyer-facing charges, packaging, fees, or "
        "returns shipping as a substitute. Never infer seller expense or an offset from buyer-paid "
        "shipping or unrelated comparables. Missing evidence does not imply zero. For supported "
        "local in-person fulfillment without shipment, use outbound_shipping_nonapplicability with "
        "fulfillment_mode LOCAL_IN_PERSON_NO_SHIPMENT, a rationale, observed/verified basis, and source "
        "references; do not also emit an outbound-shipping cost. Otherwise leave that disposition null. "
        "Normalize selling-side economic evidence from the supplied active Candidate context, "
        "raw human inputs, and exact structured Resale result. Call record_selling_cost_findings "
        "exactly once with every defensible, relevant selling cost finding, then stop. This "
        "action is required. Capture supported marketplace transaction or final-value fees, "
        "separate payment-processing charges, fixed per-order charges, applicable promoted-listing "
        "fees, seller-paid outbound shipping, buyer-paid shipping effects on seller proceeds, "
        "packaging, materially supported returns costs, and other documented selling charges. "
        "Avoid duplicate fee components and do not double-count an inclusive marketplace fee as "
        "separate component fees. Distinguish percentage from fixed costs, per-unit from per-order "
        "costs, seller-paid from buyer-paid shipping, and optional from mandatory charges. Represent "
        "a fixed per-order charge as FIXED_PER_BATCH for the modeled sale order and document that "
        "scope in notes and limitations; do not change its economic meaning. "
        "Preserve VERIFIED, ESTIMATED, and ASSUMED evidence according to its actual basis, including "
        "source references and limitations. For a known cost, put the amount in value and its "
        "evidence classification in basis; unresolved_materiality must be null, not NON_MATERIAL. "
        "Do not duplicate an observed or supported amount in modeled_value: without a distinct "
        "modeling assumption, modeled_value and modeled_value_basis must be null and "
        "modeled_value_is_conservative must be false. Reserve modeled_value for a distinct assumed "
        "modeling amount, always use ASSUMED as its basis, and explain the assumption and "
        "limitations. For a materially unresolved selling cost without a supported amount, value, "
        "both estimate fields, basis, modeled_value, and modeled_value_basis must be null; "
        "modeled_value_is_conservative must be false and unresolved_materiality must be MATERIAL. "
        "Never invent a fee percentage, shipping amount, payment charge, or other value; never "
        "silently convert an unknown to zero. Do not create findings or zero placeholders for "
        "irrelevant, optional-but-unused, or inapplicable charges. Preserve buyer-paid shipping as "
        "a proceeds limitation or unresolved economic effect when appropriate; do not misclassify "
        "it as a known seller-paid cost without evidence. Normalize selling evidence only; do not "
        "perform research, acquisition analysis, Profitability, or a final arbitrage judgment. "
        "Your final prose is not authoritative and will not be parsed."
    ),
    tools=[record_captured_selling_cost_findings],
    output_type=None,
    model_settings=ModelSettings(
        tool_choice="required",
        parallel_tool_calls=False,
    ),
    tool_use_behavior="stop_on_first_tool",
)


async def run_required_selling_capture(
    candidate_workflow: CandidateWorkflow,
    resale_result: ResaleResult,
    *,
    run_config: RunConfig | None = None,
) -> EconomicCostFindingsResult:
    state = SellingCaptureState()
    state.begin()
    context = SellingCaptureContext(
        candidate_workflow=candidate_workflow,
        capture_state=state,
    )
    evidence = candidate_workflow.selling_capture_evidence(resale_result)
    await Runner.run(
        selling_capture_agent,
        evidence,
        context=context,
        run_config=run_config,
    )
    return state.complete()
