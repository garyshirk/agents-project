from agents import Agent, handoff

from arbitrage.specialists.budget import budget_agent
from arbitrage.specialists.resale import resale_agent
from arbitrage.specialists.sourcing import sourcing_agent
from arbitrage.specialists.travel import travel_agent
from arbitrage.tools.general import calculate_tip, get_current_datetime
from arbitrage.tools.weather import get_current_weather


def show_travel_handoff(context) -> None:
    print("[debug] handing off to Travel Agent")


async def travel_tool_output(result):
    print("[debug] Travel Agent called as tool")
    return result.final_output


async def budget_tool_output(result):
    print("[debug] Budget Agent called as tool")
    return result.final_output


async def sourcing_tool_output(result):
    print("[debug] Sourcing Agent called as tool")
    return result.final_output


async def resale_tool_output(result):
    print("[debug] Resale Agent called as tool")
    return result.final_output


travel_agent_tool = travel_agent.as_tool(
    tool_name="consult_travel_agent",
    tool_description=(
        "Get focused travel planning, destination, weather, or logistics advice "
        "when the Assistant should remain responsible for the final response."
    ),
    custom_output_extractor=travel_tool_output,
)

budget_agent_tool = budget_agent.as_tool(
    tool_name="consult_budget_agent",
    tool_description=(
        "Get a focused budget, cost breakdown, comparison, or spending analysis "
        "when the Assistant should remain responsible for the final response."
    ),
    custom_output_extractor=budget_tool_output,
)

sourcing_agent_tool = sourcing_agent.as_tool(
    tool_name="consult_sourcing_agent",
    tool_description=(
        "Research current public-web product listings, retailer prices, seller information, "
        "and sourcing options."
    ),
    custom_output_extractor=sourcing_tool_output,
)

resale_agent_tool = resale_agent.as_tool(
    tool_name="consult_resale_agent",
    tool_description=(
        "Research resale-market evidence and realistic resale value for an exact product."
    ),
    custom_output_extractor=resale_tool_output,
)


agent = Agent(
    name="Assistant",
    instructions=(
        "You are a helpful general-purpose assistant. "
        "Answer ordinary requests yourself and use your function tools when appropriate. "
        "For a travel-only request, hand off to the Travel Agent unless the user asks you "
        "to remain in control or the request also requires another specialist. "
        "Use consult_travel_agent for bounded travel advice when you will compose the final answer. "
        "Use consult_budget_agent for budgets, cost breakdowns, comparisons, and spending trade-offs. "
        "Use consult_sourcing_agent for acquisition listings, current purchase prices, seller "
        "information, and sourcing research. Use consult_resale_agent for resale-market evidence "
        "and realistic resale-value research. When resale research depends on exact product identity "
        "discovered by Sourcing, include those identity details in the Resale Agent delegation; it "
        "does not receive the Sourcing Agent's result or conversation history automatically. "
        "When using sourcing or resale results, preserve each source URL alongside the corresponding "
        "finding in your final response, and never claim links are included unless they appear "
        "in the response text. "
        "For requests requiring both travel and budget expertise, call both specialist tools and "
        "combine their results into one final answer. "
        "When calling a specialist tool, include all relevant details because it does not receive "
        "the conversation history automatically."
    ),
    tools=[
        get_current_datetime,
        calculate_tip,
        get_current_weather,
        travel_agent_tool,
        budget_agent_tool,
        sourcing_agent_tool,
        resale_agent_tool,
    ],
    handoffs=[handoff(travel_agent, on_handoff=show_travel_handoff)],
)
