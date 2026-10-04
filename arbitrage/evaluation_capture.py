import json

from agents import Agent, RunContextWrapper, RunHooks
from pydantic import BaseModel

from arbitrage.candidate_workflow import ApplicationContext
from arbitrage.contracts import (
    EvaluationArtifactType,
    ProfitabilityToolResult,
    ResaleResult,
    SourcingTextReport,
)


class EvaluationCaptureHooks(RunHooks[ApplicationContext]):
    """Persist actual specialist/tool results without changing what the Manager sees."""

    _captured_tools = {
        "consult_resale_agent": (
            EvaluationArtifactType.RESALE,
            ResaleResult,
        ),
        "calculate_profitability": (
            EvaluationArtifactType.PROFITABILITY,
            ProfitabilityToolResult,
        ),
    }

    async def on_tool_end(
        self,
        context: RunContextWrapper[ApplicationContext],
        agent: Agent[ApplicationContext],
        tool,
        result: object,
    ) -> None:
        if tool.name == "consult_sourcing_agent":
            if not isinstance(result, str):
                raise TypeError("Sourcing Agent result must be plain text")
            validated = SourcingTextReport(report_text=result)
            artifact_type = EvaluationArtifactType.SOURCING_REPORT
        else:
            capture = self._captured_tools.get(tool.name)
            if capture is None:
                return
            artifact_type, result_type = capture
            if isinstance(result, result_type):
                validated = result
            elif isinstance(result, BaseModel):
                validated = result_type.model_validate(result.model_dump())
            elif isinstance(result, str):
                validated = result_type.model_validate_json(result)
            else:
                validated = result_type.model_validate(result)

        tool_arguments = getattr(context, "tool_arguments", None)
        try:
            arguments = json.loads(tool_arguments) if tool_arguments else None
        except json.JSONDecodeError:
            arguments = tool_arguments
        context_json = json.dumps(
            {
                "tool_call_id": getattr(context, "tool_call_id", None),
                "tool_name": tool.name,
                "arguments": arguments,
            }
        )
        context.context.candidate_workflow.capture_specialist_result(
            artifact_type,
            validated.model_dump_json(),
            context_json=context_json,
        )
