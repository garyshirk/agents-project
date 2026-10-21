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

from arbitrage.acquisition_capture_state import AcquisitionCaptureState
from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    CostType,
    EconomicCostFinding,
    EconomicCostFindingsResult,
    EvaluationArtifactType,
    SourcingTextReport,
    UnknownMateriality,
)
from arbitrage.profitability_assembly import PURCHASE_PRICE_COST_ID


@dataclass
class AcquisitionCaptureContext:
    candidate_workflow: CandidateWorkflow
    capture_state: AcquisitionCaptureState


def acquisition_capture_tool_error(
    _context: RunContextWrapper[AcquisitionCaptureContext], error: Exception
) -> str:
    return f"Acquisition cost findings were not recorded: {error}"


def validate_acquisition_purchase_price(
    findings: list[EconomicCostFinding],
) -> None:
    purchase_prices = [
        finding for finding in findings if finding.cost_id == PURCHASE_PRICE_COST_ID
    ]
    if len(purchase_prices) != 1:
        raise ValueError(
            f"acquisition findings require exactly one {PURCHASE_PRICE_COST_ID} finding"
        )
    if purchase_prices[0].cost_type != CostType.FIXED_PER_UNIT:
        raise ValueError(
            f"{PURCHASE_PRICE_COST_ID} must use FIXED_PER_UNIT cost_type"
        )
    purchase_price = purchase_prices[0]
    if purchase_price.value is None:
        if (
            purchase_price.estimated_low is not None
            or purchase_price.estimated_high is not None
            or purchase_price.modeled_value is not None
            or purchase_price.unresolved_materiality != UnknownMateriality.MATERIAL
        ):
            raise ValueError(
                f"unresolved {PURCHASE_PRICE_COST_ID} must have no numeric or modeled "
                "value and must use MATERIAL unresolved_materiality"
            )
    elif (
        purchase_price.estimated_low is not None
        or purchase_price.estimated_high is not None
        or purchase_price.modeled_value is not None
        or purchase_price.unresolved_materiality is not None
    ):
        raise ValueError(
            f"resolved {PURCHASE_PRICE_COST_ID} must use one exact supported value"
        )


@function_tool(
    name_override="record_acquisition_cost_findings",
    failure_error_function=acquisition_capture_tool_error,
)
def record_captured_acquisition_cost_findings(
    context: RunContextWrapper[AcquisitionCaptureContext],
    findings: list[EconomicCostFinding],
) -> str:
    """Record all defensible acquisition-side cost findings for this Sourcing report."""
    state = context.context.capture_state
    result = EconomicCostFindingsResult(findings=findings)
    validate_acquisition_purchase_price(result.findings)
    state.begin_action()
    context_json = json.dumps(
        {
            "tool_call_id": getattr(context, "tool_call_id", None),
            "tool_name": getattr(context, "tool_name", None),
        }
    )
    context.context.candidate_workflow.record_economic_cost_findings(
        EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
        result,
        context_json=context_json,
    )
    state.select(result)
    return f"Recorded {len(findings)} acquisition-side economic cost finding(s)."


acquisition_capture_agent = Agent(
    name="Acquisition Capture Agent",
    instructions=(
        "Intake channel HUMAN_SUBMISSION is not HUMAN_OBSERVED evidence. Opportunity status and "
        "supporting excerpts are attributed context, not verification of individual costs or "
        "availability. Never turn hypothetical availability into HUMAN_OBSERVED inventory. "
        "Normalize acquisition-side economic evidence from the supplied active Candidate context, "
        "raw human inputs, and exact Sourcing report. Call record_acquisition_cost_findings exactly "
        "once with every defensible, relevant acquisition cost finding, then stop. This action is "
        "required. Preserve HUMAN_OBSERVED evidence as HUMAN_OBSERVED; web corroboration of product "
        "identity does not promote a human-observed price to VERIFIED. Preserve VERIFIED, ESTIMATED, "
        "and ASSUMED evidence according to its actual basis, including source references and "
        "limitations. For a known cost, put the amount in value and its evidence classification "
        "in basis; unresolved_materiality must be null, not NON_MATERIAL. "
        "Always include exactly one finding with cost_id acquisition_purchase_price for the "
        "per-unit price required to acquire one sellable inventory unit from the identified "
        "source, and give it FIXED_PER_UNIT cost_type. Preserve its supported HUMAN_OBSERVED, "
        "VERIFIED, ESTIMATED, or other accurate basis. If the purchase price is unknown, still "
        "include acquisition_purchase_price with value null and unresolved_materiality MATERIAL; "
        "do not invent a modeled price or zero. Record sales tax and every other additional cost "
        "as separate findings, and never emit a duplicate purchase-price finding. "
        "Do not duplicate an observed amount in modeled_value: without a distinct modeling "
        "assumption, modeled_value "
        "and modeled_value_basis must be null and modeled_value_is_conservative must be false. "
        "Reserve modeled_value for a distinct assumed modeling amount, always use ASSUMED as its "
        "basis, and explain the assumption and limitations. For a materially unresolved cost "
        "without a supported amount, value, both estimate fields, basis, modeled_value, and "
        "modeled_value_basis must be null; modeled_value_is_conservative must be false and "
        "unresolved_materiality must be MATERIAL. An explicitly hypothetical purchase-price amount "
        "uses acquisition_purchase_price, FIXED_PER_UNIT, the supplied amount in value, ASSUMED basis, "
        "and null unresolved_materiality for that resolved scenario assumption. State in limitations "
        "that it is hypothetical and not a verified retailer offer; never label it HUMAN_OBSERVED or "
        "VERIFIED. Do not duplicate the exact assumed amount in modeled_value. Do not infer confirmed "
        "inventory from requested quantity, a hypothetical price, or a named retailer. Unknown material "
        "taxes, shipping, and other acquisition costs remain unresolved, never zero. "
        "For example, a human-observed Ross price of "
        "$39.99 uses value 39.99 and HUMAN_OBSERVED basis with null unresolved and modeled fields "
        "and false modeled_value_is_conservative; unknown sales tax remains unresolved MATERIAL "
        "when appropriate. Never invent a value or silently convert an unknown to zero. Do not "
        "create findings or zero placeholders for irrelevant or not-applicable categories. "
        "Normalize acquisition evidence only; do not perform research, resale analysis, "
        "Profitability, or a final arbitrage judgment. Your final prose is not authoritative and "
        "will not be parsed."
    ),
    tools=[record_captured_acquisition_cost_findings],
    output_type=None,
    model_settings=ModelSettings(
        tool_choice="required",
        parallel_tool_calls=False,
    ),
    tool_use_behavior="stop_on_first_tool",
)


async def run_required_acquisition_capture(
    candidate_workflow: CandidateWorkflow,
    sourcing_report: SourcingTextReport,
    *,
    run_config: RunConfig | None = None,
) -> EconomicCostFindingsResult:
    state = AcquisitionCaptureState()
    state.begin()
    context = AcquisitionCaptureContext(
        candidate_workflow=candidate_workflow,
        capture_state=state,
    )
    evidence = candidate_workflow.acquisition_capture_evidence(sourcing_report)
    await Runner.run(
        acquisition_capture_agent,
        evidence,
        context=context,
        run_config=run_config,
    )
    return state.complete()
