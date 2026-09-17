import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from dotenv import load_dotenv
from agents import Agent, Runner, SQLiteSession, function_tool, handoff

load_dotenv()


@function_tool
def get_current_datetime() -> str:
    """Return the current local date and time."""
    print("[debug] get_current_datetime tool called")
    return datetime.now().astimezone().strftime("%A, %B %d, %Y at %I:%M:%S %p %Z")


@function_tool
def calculate_tip(bill_amount: float, tip_percentage: float) -> float:
    """Calculate the tip for a bill amount and tip percentage."""
    print(f"[debug] calculate_tip called with {bill_amount=}, {tip_percentage=}")
    return bill_amount * tip_percentage / 100


def weather_description(code: int) -> str:
    if code == 0:
        return "clear"
    if code <= 3:
        return "partly cloudy"
    if code <= 48:
        return "foggy"
    if code <= 57:
        return "drizzly"
    if code <= 67:
        return "rainy"
    if code <= 77:
        return "snowy"
    if code <= 82:
        return "rain showers"
    if code <= 86:
        return "snow showers"
    return "thunderstorms"


@function_tool
def get_current_weather(location: str, forecast_date: str | None = None) -> str:
    """Return current weather, or a daily forecast for an ISO date, for a location."""
    print(f"[debug] get_current_weather called with {location=}, {forecast_date=}")

    try:
        query = urlencode({"name": location, "count": 1, "format": "json"})
        with urlopen(f"https://geocoding-api.open-meteo.com/v1/search?{query}", timeout=10) as response:
            places = json.load(response)

        if not places.get("results"):
            return f"I couldn't find a location matching {location}."

        place = places["results"][0]
        weather_query = {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "timezone": "auto",
        }
        if forecast_date:
            weather_query.update(
                {
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
                    "start_date": forecast_date,
                    "end_date": forecast_date,
                }
            )
        else:
            weather_query["current"] = "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m"

        query = urlencode(weather_query)
        with urlopen(f"https://api.open-meteo.com/v1/forecast?{query}", timeout=10) as response:
            weather = json.load(response)

        place_name = ", ".join(
            part for part in (place["name"], place.get("admin1"), place.get("country")) if part
        )

        if forecast_date:
            daily = weather["daily"]
            units = weather["daily_units"]
            return (
                f"Forecast for {place_name} on {daily['time'][0]}: "
                f"{weather_description(daily['weather_code'][0])}, "
                f"high {daily['temperature_2m_max'][0]}{units['temperature_2m_max']}, "
                f"low {daily['temperature_2m_min'][0]}{units['temperature_2m_min']}, "
                f"precipitation chance {daily['precipitation_probability_max'][0]}"
                f"{units['precipitation_probability_max']}, precipitation "
                f"{daily['precipitation_sum'][0]}{units['precipitation_sum']}, and maximum wind "
                f"{daily['wind_speed_10m_max'][0]}{units['wind_speed_10m_max']}."
            )

        current = weather["current"]
        units = weather["current_units"]
        return (
            f"Current weather in {place_name}: {weather_description(current['weather_code'])}, "
            f"{current['temperature_2m']}{units['temperature_2m']} "
            f"(feels like {current['apparent_temperature']}{units['apparent_temperature']}), "
            f"humidity {current['relative_humidity_2m']}{units['relative_humidity_2m']}, "
            f"precipitation {current['precipitation']}{units['precipitation']}, and wind "
            f"{current['wind_speed_10m']}{units['wind_speed_10m']}."
        )
    except (OSError, ValueError, KeyError, TypeError):
        if forecast_date:
            return f"I couldn't retrieve a forecast for {location} on {forecast_date}."
        return f"I couldn't retrieve current weather for {location} right now."


def show_travel_handoff(context) -> None:
    print("[debug] handing off to Travel Agent")


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


budget_agent = Agent(
    name="Budget Agent",
    instructions=(
        "You specialize in simple budgets, cost breakdowns, comparisons, and trade-offs. "
        "Use the figures provided by the caller, state any assumptions, and return a concise "
        "analysis that another agent can use. Do not invent current prices."
    ),
)


async def travel_tool_output(result):
    print("[debug] Travel Agent called as tool")
    return result.final_output


async def budget_tool_output(result):
    print("[debug] Budget Agent called as tool")
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


agent = Agent(
    name="Assistant",
    instructions=(
        "You are a helpful general-purpose assistant. "
        "Answer ordinary requests yourself and use your function tools when appropriate. "
        "For a travel-only request, hand off to the Travel Agent unless the user asks you "
        "to remain in control or the request also requires another specialist. "
        "Use consult_travel_agent for bounded travel advice when you will compose the final answer. "
        "Use consult_budget_agent for budgets, cost breakdowns, comparisons, and spending trade-offs. "
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
    ],
    handoffs=[handoff(travel_agent, on_handoff=show_travel_handoff)],
)

session = SQLiteSession(
    "default_conversation",
    Path(__file__).with_name("sessions.db"),
)

try:
    while True:
        prompt = input("You: ")
        if prompt.strip().lower() in {"exit", "quit"}:
            break

        result = Runner.run_sync(agent, prompt, session=session)
        print(f"Assistant: {result.final_output}")
finally:
    session.close()
