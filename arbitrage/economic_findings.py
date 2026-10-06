import json

from agents import RunContextWrapper, function_tool

from arbitrage.candidate_workflow import ApplicationContext
from arbitrage.contracts import (
    EconomicCostFinding,
    EconomicCostFindingsResult,
    EvaluationArtifactType,
)


def economic_findings_tool_error(
    _context: RunContextWrapper[ApplicationContext], error: Exception
) -> str:
    return f"Economic cost findings were not recorded: {error}"


def _record_findings(
    context: RunContextWrapper[ApplicationContext],
    findings: list[EconomicCostFinding],
    artifact_type: EvaluationArtifactType,
    side_name: str,
) -> str:
    result = EconomicCostFindingsResult(findings=findings)
    context_json = json.dumps(
        {
            "tool_call_id": getattr(context, "tool_call_id", None),
            "tool_name": getattr(context, "tool_name", None),
        }
    )
    context.context.candidate_workflow.record_economic_cost_findings(
        artifact_type,
        result,
        context_json=context_json,
    )
    return f"Recorded {len(findings)} {side_name} economic cost finding(s)."


@function_tool(failure_error_function=economic_findings_tool_error)
def record_acquisition_cost_findings(
    context: RunContextWrapper[ApplicationContext],
    findings: list[EconomicCostFinding],
) -> str:
    """Record validated acquisition-side cost findings for the active Evaluation."""
    return _record_findings(
        context,
        findings,
        EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
        "acquisition-side",
    )


@function_tool(failure_error_function=economic_findings_tool_error)
def record_selling_cost_findings(
    context: RunContextWrapper[ApplicationContext],
    findings: list[EconomicCostFinding],
) -> str:
    """Record validated selling-side cost findings for the active Evaluation."""
    return _record_findings(
        context,
        findings,
        EvaluationArtifactType.SELLING_COST_FINDINGS,
        "selling-side",
    )
