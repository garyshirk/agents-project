import json

from collections.abc import Awaitable, Callable

from agents import Agent, RunConfig, RunContextWrapper, RunHooks
from pydantic import BaseModel

from arbitrage.candidate_workflow import ApplicationContext, CandidateWorkflow
from arbitrage.acquisition_capture import run_required_acquisition_capture
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

    def __init__(
        self,
        *,
        run_config: RunConfig | None = None,
        acquisition_capture: Callable[
            [CandidateWorkflow, SourcingTextReport], Awaitable[object]
        ] | None = None,
    ) -> None:
        self.run_config = run_config
        self.acquisition_capture = acquisition_capture

    async def on_tool_start(
        self,
        context: RunContextWrapper[ApplicationContext],
        agent: Agent[ApplicationContext],
        tool,
    ) -> None:
        if tool.name == "consult_resale_agent":
            print("[debug] Resale Agent tool entered")

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
        if tool.name == "consult_sourcing_agent":
            if self.acquisition_capture is not None:
                await self.acquisition_capture(
                    context.context.candidate_workflow,
                    validated,
                )
            else:
                await run_required_acquisition_capture(
                    context.context.candidate_workflow,
                    validated,
                    run_config=self.run_config,
                )
