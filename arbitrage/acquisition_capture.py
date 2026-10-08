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
    EconomicCostFinding,
    EconomicCostFindingsResult,
    EvaluationArtifactType,
    SourcingTextReport,
)


@dataclass
class AcquisitionCaptureContext:
    candidate_workflow: CandidateWorkflow
    capture_state: AcquisitionCaptureState


def acquisition_capture_tool_error(
    _context: RunContextWrapper[AcquisitionCaptureContext], error: Exception
) -> str:
    return f"Acquisition cost findings were not recorded: {error}"


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
    state.begin_action()
    result = EconomicCostFindingsResult(findings=findings)
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
        "Normalize acquisition-side economic evidence from the supplied active Candidate context, "
        "raw human inputs, and exact Sourcing report. Call record_acquisition_cost_findings exactly "
        "once with every defensible, relevant acquisition cost finding, then stop. This action is "
        "required. Preserve HUMAN_OBSERVED evidence as HUMAN_OBSERVED; web corroboration of product "
        "identity does not promote a human-observed price to VERIFIED. Preserve VERIFIED, ESTIMATED, "
        "and ASSUMED evidence according to its actual basis, including source references and "
        "limitations. For a known cost, put the amount in value and its evidence classification "
        "in basis; unresolved_materiality must be null, not NON_MATERIAL. Do not duplicate an "
        "observed amount in modeled_value: without a distinct modeling assumption, modeled_value "
        "and modeled_value_basis must be null and modeled_value_is_conservative must be false. "
        "Reserve modeled_value for a distinct assumed modeling amount, always use ASSUMED as its "
        "basis, and explain the assumption and limitations. For a materially unresolved cost "
        "without a supported amount, value, both estimate fields, basis, modeled_value, and "
        "modeled_value_basis must be null; modeled_value_is_conservative must be false and "
        "unresolved_materiality must be MATERIAL. For example, a human-reported Ross price of "
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
