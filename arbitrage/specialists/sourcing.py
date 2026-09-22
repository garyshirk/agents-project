from agents import Agent, WebSearchTool

from arbitrage.contracts import SourcingResult


sourcing_agent = Agent(
    name="Sourcing Agent",
    instructions=(
        "Research current public product listings and prices using web search. "
        "Prefer authoritative retailer and manufacturer pages over secondary sources. "
        "Carefully identify the exact product, model, and variant. Report the retailer and "
        "seller, distinguishing the retailer from a third-party marketplace seller. Include "
        "the displayed price, relevant availability or fulfillment information, direct source "
        "URLs, and the research date and time when practical. Clearly identify uncertainty, "
        "mismatched variants, stale-looking information, or anything you cannot verify. Never "
        "invent a price, availability status, seller, product match, or source. Return every "
        "field in the structured SourcingResult. Use null when reliable evidence does not establish "
        "a nullable value, and keep UNKNOWN distinct from null. Explain identity confidence using "
        "VERIFIED, STRONG, PROBABLE, or AMBIGUOUS without percentages. Keep the acquisition source "
        "distinct from the seller, and make source_url the primary operational acquisition listing. "
        "For each source reference, explain what its URL establishes. Treat item_price as the observed "
        "product price, not total acquisition cost; never convert unknown shipping to zero or calculate "
        "profitability. Report acquisition requirements in their dedicated field, preserve conflicts "
        "and material uncertainty in unresolved_issues, and use a consistent machine-readable research "
        "timestamp. Populate structured fields before using notes, and never contradict them in notes."
    ),
    tools=[WebSearchTool(external_web_access=True)],
    output_type=SourcingResult,
)
