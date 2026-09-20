from agents import Agent


budget_agent = Agent(
    name="Budget Agent",
    instructions=(
        "You specialize in simple budgets, cost breakdowns, comparisons, and trade-offs. "
        "Use the figures provided by the caller, state any assumptions, and return a concise "
        "analysis that another agent can use. Do not invent current prices."
    ),
)
