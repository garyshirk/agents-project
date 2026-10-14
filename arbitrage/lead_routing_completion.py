"""State-authoritative Lead completion and bounded terminal-only diagnostics."""

import json

from agents import RunContextWrapper, RunHooks, ToolsToFinalOutputResult

from arbitrage.candidate_workflow import ApplicationContext
from arbitrage.lead_routing import LEAD_ROUTING_TOOLS, ROUTING_RECORDED


_ROUTING_NAMES = frozenset(tool.name for tool in LEAD_ROUTING_TOOLS)
_KNOWN_TOOLS = _ROUTING_NAMES | {"consult_sourcing_agent"}
_MAX_EVENTS = 20


def finish_lead_on_routing(
    context: RunContextWrapper[ApplicationContext], tool_results
) -> ToolsToFinalOutputResult:
    """Tool names/errors alone never establish a successful terminal decision."""
    state = context.context.lead_routing
    if not state.decisions:
        return ToolsToFinalOutputResult(is_final_output=False, final_output=None)
    # Retain the same zero/multiple-decision invariant used by the application.
    state.complete()
    return ToolsToFinalOutputResult(is_final_output=True, final_output=ROUTING_RECORDED)


class LeadRoutingDiagnostics(RunHooks[ApplicationContext]):
    """Record only allowlisted names and outcome categories, never tool payloads."""

    def __init__(self):
        self.events: list[dict[str, str]] = []
        self.events_truncated = False

    async def on_tool_start(self, context, agent, tool):
        if len(self.events) >= _MAX_EVENTS:
            self.events_truncated = True
            return
        name = tool.name if tool.name in _KNOWN_TOOLS else "unrecognized_tool"
        self.events.append({"tool": name, "outcome": "started"})

    async def on_tool_end(self, context, agent, tool, result):
        name = tool.name if tool.name in _KNOWN_TOOLS else "unrecognized_tool"
        event = next((item for item in reversed(self.events)
                      if item["tool"] == name and item["outcome"] == "started"), None)
        if event is None:
            return
        # The default SDK formatter merges JSON/schema errors under this message.
        # Do not retain or print that message or its potentially sensitive details.
        prefix = result[:256] if isinstance(result, str) else ""
        if prefix.startswith("An error occurred"):
            event["outcome"] = (
                "argument_error_reported" if "Invalid JSON input for tool" in prefix
                or "parsing tool arguments" in prefix else "tool_error_reported"
            )
        elif name in _ROUTING_NAMES:
            event["outcome"] = (
                "routing_recorded" if len(context.context.lead_routing.decisions) == 1
                else "routing_not_recorded"
            )
        else:
            event["outcome"] = "result_returned"

    def report_failure(self, context, *, runner_returned, result, error):
        output = getattr(result, "final_output", None)
        output_type = type(output).__name__ if type(output) in (str, dict, list, int, bool, type(None)) else "other"
        description = f"text omitted ({len(output)} characters)" if isinstance(output, str) else "content omitted"
        error_type = type(error).__name__
        if error_type not in {"LeadRoutingError", "MaxTurnsExceeded", "ModelBehaviorError", "UserError"}:
            error_type = "other_exception"
        print("[debug] Lead routing failure: " + json.dumps({
            "runner_status": "returned" if runner_returned else "raised",
            "terminal_decision_count": len(context.lead_routing.decisions),
            "tool_events": self.events,
            "events_truncated": self.events_truncated,
            "argument_error_reported": any(event["outcome"] == "argument_error_reported" for event in self.events),
            "argument_error_detail": "JSON parsing versus schema/semantic validation is not distinguished",
            "final_output_available": result is not None,
            "final_output_type": output_type if result is not None else "unavailable",
            "final_output_description": description if result is not None else "unavailable",
            "sdk_error_class": error_type if not runner_returned else None,
            "application_error_class": error_type if runner_returned else None,
        }, sort_keys=True))
