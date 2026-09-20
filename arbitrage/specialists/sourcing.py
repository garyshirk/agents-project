from agents import Agent, WebSearchTool


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
        "invent a price, availability status, seller, product match, or source."
    ),
    tools=[WebSearchTool(external_web_access=True)],
)
