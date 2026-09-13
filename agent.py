import json
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import urlopen

from dotenv import load_dotenv
from agents import Agent, Runner, function_tool

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
def get_current_weather(location: str) -> str:
    """Return a short current-weather summary for a city or location."""
    print(f"[debug] get_current_weather called with {location=}")

    try:
        query = urlencode({"name": location, "count": 1, "format": "json"})
        with urlopen(f"https://geocoding-api.open-meteo.com/v1/search?{query}", timeout=10) as response:
            places = json.load(response)

        if not places.get("results"):
            return f"I couldn't find a location matching {location}."

        place = places["results"][0]
        query = urlencode(
            {
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
                "timezone": "auto",
            }
        )
        with urlopen(f"https://api.open-meteo.com/v1/forecast?{query}", timeout=10) as response:
            weather = json.load(response)

        current = weather["current"]
        units = weather["current_units"]
        place_name = ", ".join(
            part for part in (place["name"], place.get("admin1"), place.get("country")) if part
        )
        return (
            f"Current weather in {place_name}: {weather_description(current['weather_code'])}, "
            f"{current['temperature_2m']}{units['temperature_2m']} "
            f"(feels like {current['apparent_temperature']}{units['apparent_temperature']}), "
            f"humidity {current['relative_humidity_2m']}{units['relative_humidity_2m']}, "
            f"precipitation {current['precipitation']}{units['precipitation']}, and wind "
            f"{current['wind_speed_10m']}{units['wind_speed_10m']}."
        )
    except (OSError, ValueError, KeyError, TypeError):
        return f"I couldn't retrieve current weather for {location} right now."


agent = Agent(
    name="Assistant",
    instructions="You are a helpful general-purpose assistant. Use the date and time tool when needed.",
    tools=[get_current_datetime, calculate_tip, get_current_weather],
)

history = []

while True:
    prompt = input("You: ")
    if prompt.strip().lower() in {"exit", "quit"}:
        break

    history.append({"role": "user", "content": prompt})
    result = Runner.run_sync(agent, history)
    print(f"Assistant: {result.final_output}")
    history = result.to_input_list()
