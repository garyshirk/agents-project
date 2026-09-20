from agents import Agent

from arbitrage.tools.general import get_current_datetime
from arbitrage.tools.weather import get_current_weather


travel_agent = Agent(
    name="Travel Agent",
    handoff_description="Handles travel planning, destination advice, weather, and logistics.",
    instructions=(
        "You specialize in travel planning, destination advice, "
        "weather-related travel questions, and travel logistics. "
        "For any answer that depends on current or future weather, always call "
        "get_current_weather for the requested location and date before answering. "
        "If needed, use get_current_datetime first to resolve a relative date such as tomorrow."
    ),
    tools=[get_current_datetime, get_current_weather],
)
