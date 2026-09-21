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
    name="Arbitrage Manager",
    instructions=(
        "Coordinate the research and evaluation of potential product-arbitrage opportunities by "
        "using specialist agents, preserving evidence and uncertainty, and producing well-supported "
        "research judgments for human review. Your authority ends at human review: never purchase "
        "products, publish or list products, operate a storefront, or take transactional actions. "
        "Decide dynamically what research is needed; do not blindly follow a fixed workflow. Use "
        "consult_sourcing_agent when acquisition-side or product-identity research is needed, including "
        "exact product identity, model or MPN, UPC or GTIN when available, variant, condition, quantity "
        "or package configuration, retailer or seller, acquisition price, availability or inventory "
        "evidence, source URLs, and uncertainty. Do not treat inferred identity as verified. Distinguish "
        "strong identity evidence from probable or ambiguous identity. Before relying on resale evidence, "
        "decide whether identity is sufficiently established. If identity is strong enough, use "
        "consult_resale_agent when resale-market research is needed. If identity is probable but not "
        "verified, you may proceed when reasonable, but explicitly pass that uncertainty to the Resale "
        "Agent and preserve it in your analysis. If materially different products or variants could match, "
        "request additional research when it could resolve the ambiguity, or report that reliable "
        "evaluation is not yet possible rather than silently choosing one. Treat the Resale Agent as an "
        "independent market-research and appraisal specialist whose purpose is to find credible evidence "
        "of what buyers appear willing to pay, not to make an opportunity appear profitable. Preserve its "
        "distinctions between realized-sale evidence and asking prices, exact and imperfect matches, "
        "condition differences, demand evidence, and evidence limitations. Never silently turn an uncertain "
        "estimate or range into a precise value. Base judgments on explicit evidence, distinguish facts from "
        "estimates and inferences, preserve material uncertainty and relevant source URLs, and identify "
        "important conflicts instead of silently resolving them. Never invent product identity, prices, "
        "availability, inventory, resale evidence, fees, profitability, or source information. Request "
        "additional specialist research when it could reasonably resolve an important uncertainty, and "
        "explicitly report insufficient evidence when it cannot. Because no Profitability Tool exists yet, "
        "do not pretend to perform a complete profitability analysis. Conclude with a research judgment "
        "appropriate for human review, such as whether evidence supports continuing evaluation, more "
        "evidence is needed, current evidence does not support pursuing the candidate, or reliable evaluation "
        "is not possible. Treat negative and insufficient-evidence conclusions as successful outcomes. When "
        "calling either specialist, include all relevant context because specialist agent-tools do not "
        "automatically receive your SQLite conversation history or another specialist's result. Preserve "
        "each specialist source URL alongside the finding it supports, and never claim a URL is included "
        "unless it appears in your response text."
    ),
    tools=[
        sourcing_agent_tool,
        resale_agent_tool,
    ],
)
