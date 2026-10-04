"""Isolated plain-text multi-agent diagnostic for the Ross workflow.

Dry-run is the default. Pass ``--run`` explicitly to make the live SDK call.
"""

from __future__ import annotations

import argparse
import json
import traceback
from collections.abc import Sequence
from typing import Any

from agents import Agent, Runner, WebSearchTool
from dotenv import load_dotenv


ROSS_PROMPT = (
    "I'm at Ross and found a new pair of men's Nike running shoes in the original "
    "box for $39.99. The shoes appear unworn. The box says style FD2722-001, "
    "men's size 10, and the box label matches the shoes. Evaluate this as a possible "
    "arbitrage opportunity for resale on eBay. Ask me for additional information "
    "only if it would materially improve the evaluation and cannot reasonably be "
    "obtained through research. Do not purchase or list anything."
)


SOURCING_INSTRUCTIONS = (
    "Research current public product and acquisition information using web search. "
    "Prefer authoritative retailer and manufacturer pages over secondary sources. "
    "Carefully identify the exact product, model, and variant. Distinguish the source "
    "from a third-party seller. Preserve product identity, identity confidence and "
    "uncertainty, source and seller, displayed item price, shipping or other acquisition "
    "costs when available, availability, direct URLs, provenance, unresolved issues, "
    "and important limitations. Never invent a price, availability status, seller, "
    "product match, or source. Return a concise, clearly labeled natural-language "
    "research report. Do not emit JSON or imitate a data schema."
)


RESALE_INSTRUCTIONS = (
    "Research resale-market evidence for the exact product using web search; do not "
    "calculate profitability. Prefer exact identifiers over fuzzy title matching and "
    "identify mismatched variants. Distinguish verified realized sales, sold-status "
    "records whose realized price is unverified, active asking prices, and demand "
    "signals. Preserve condition, price, shipping, date, source URL, product match "
    "quality, uncertainty, and limitations. Never treat an active listing as proof of "
    "a sale and never invent a sale price, demand, seller, condition, match, or source. "
    "Return a concise, clearly labeled natural-language research report. Do not emit "
    "JSON or imitate a data schema."
)


MANAGER_INSTRUCTIONS = (
    "Evaluate the supplied product as a possible arbitrage research candidate for human "
    "review. Always call the diagnostic Sourcing Agent first. Then call the diagnostic "
    "Resale Agent, passing the relevant exact identity, condition, uncertainty, and "
    "source context from the user's prompt and the Sourcing report in natural language. "
    "Use both plain-text reports, preserve material uncertainties and source URLs, and "
    "distinguish facts from estimates. Do not calculate formal profitability, purchase "
    "anything, or list anything. Return a concise natural-language final evaluation, "
    "not JSON or a schema-shaped response."
)


def _text_input_builder(label: str):
    def build(options: dict[str, Any]) -> str:
        print(f"[debug] {label} diagnostic tool entered")
        params = options.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("input"), str):
            raise TypeError(f"{label} diagnostic tool requires one text input")
        return params["input"]

    return build


async def _text_output(label: str, result: Any) -> str:
    output = result.final_output
    if not isinstance(output, str):
        raise TypeError(f"{label} diagnostic agent did not return plain text")
    print(f"[debug] {label} diagnostic tool completed")
    return output


async def sourcing_text_output(result: Any) -> str:
    return await _text_output("Sourcing", result)


async def resale_text_output(result: Any) -> str:
    return await _text_output("Resale", result)


sourcing_agent = Agent(
    name="Diagnostic Plain-Text Sourcing Agent",
    instructions=SOURCING_INSTRUCTIONS,
    tools=[WebSearchTool(external_web_access=True)],
)

resale_agent = Agent(
    name="Diagnostic Plain-Text Resale Agent",
    instructions=RESALE_INSTRUCTIONS,
    tools=[WebSearchTool(external_web_access=True)],
)

sourcing_tool = sourcing_agent.as_tool(
    tool_name="consult_diagnostic_sourcing_agent",
    tool_description=(
        "Delegate current product identity and acquisition research using one natural-language "
        "input string; returns a natural-language report."
    ),
    input_builder=_text_input_builder("Sourcing"),
    custom_output_extractor=sourcing_text_output,
)

resale_tool = resale_agent.as_tool(
    tool_name="consult_diagnostic_resale_agent",
    tool_description=(
        "Delegate resale-market evidence research using one natural-language input string; "
        "returns a natural-language report."
    ),
    input_builder=_text_input_builder("Resale"),
    custom_output_extractor=resale_text_output,
)

manager_agent = Agent(
    name="Diagnostic Plain-Text Arbitrage Manager",
    instructions=MANAGER_INSTRUCTIONS,
    tools=[sourcing_tool, resale_tool],
)

DIAGNOSTIC_AGENTS = (manager_agent, sourcing_agent, resale_agent)
PRODUCTION_OUTPUT_CONTRACT_NAMES = {
    "LeadDecision",
    "SourcingResult",
    "ResaleResult",
    "ManagerEvaluationJudgment",
}


def assert_plain_text_experiment() -> None:
    for diagnostic_agent in DIAGNOSTIC_AGENTS:
        if diagnostic_agent.output_type is not None:
            raise AssertionError(f"{diagnostic_agent.name} unexpectedly has output_type")
        output_name = getattr(diagnostic_agent.output_type, "__name__", None)
        if output_name in PRODUCTION_OUTPUT_CONTRACT_NAMES:
            raise AssertionError(f"{diagnostic_agent.name} uses {output_name}")


def agent_summary(diagnostic_agent: Agent[Any]) -> dict[str, Any]:
    return {
        "name": diagnostic_agent.name,
        "model": diagnostic_agent.model,
        "model_settings": repr(diagnostic_agent.model_settings),
        "tools": [tool.name for tool in diagnostic_agent.tools],
        "web_search_present": any(
            tool.name == "web_search" for tool in diagnostic_agent.tools
        ),
        "output_type": diagnostic_agent.output_type,
    }


def diagnostic_summary() -> dict[str, Any]:
    assert_plain_text_experiment()
    return {
        "agents": [agent_summary(item) for item in DIAGNOSTIC_AGENTS],
        "specialist_tool_argument_schemas": {
            sourcing_tool.name: sourcing_tool.params_json_schema,
            resale_tool.name: resale_tool.params_json_schema,
        },
        "invocation": "Runner.run_sync(manager_agent, ROSS_PROMPT)",
        "session": None,
        "persistence": None,
        "lead_qualifier_bypassed": True,
        "production_manager_bypassed": True,
        "candidate_workflow_bypassed": True,
        "remaining_json_boundary": (
            "Agent.as_tool uses the SDK's strict one-field function-call argument envelope "
            "for {'input': <natural-language string>}; no domain output schema is used."
        ),
    }


def print_diagnostic_summary() -> None:
    print("PLAIN-TEXT MULTI-AGENT DIAGNOSTIC")
    print(json.dumps(diagnostic_summary(), indent=2, default=str))


def _safe_exception_metadata(error: Exception) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for name in ("request_id", "response_id", "status_code", "code", "type"):
        value = getattr(error, name, None)
        if value is not None and isinstance(value, str | int | float | bool):
            metadata[name] = value
    return metadata


def run_live() -> str:
    assert_plain_text_experiment()
    print("[debug] Manager run started")
    try:
        result = Runner.run_sync(manager_agent, ROSS_PROMPT)
        if not isinstance(result.final_output, str):
            raise TypeError("Diagnostic Manager did not return plain text")
        print("[debug] Manager final text received")
        print(result.final_output)
        return result.final_output
    except Exception as error:
        print("[debug] Plain-text diagnostic exception")
        print(f"Exception type: {type(error).__module__}.{type(error).__qualname__}")
        print(f"str(error): {error}")
        print(f"repr(error): {error!r}")
        print(f"SDK metadata: {_safe_exception_metadata(error)}")
        traceback.print_exception(type(error), error, error.__traceback__)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Explicitly perform the live Manager/model/WebSearch diagnostic.",
    )
    arguments = parser.parse_args(argv)
    print_diagnostic_summary()
    if not arguments.run:
        print("DRY RUN ONLY: no Runner, model, API, WebSearch, session, or database used.")
        return 0
    run_live()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
