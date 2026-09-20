import json
from urllib.parse import urlencode
from urllib.request import urlopen

from agents import function_tool


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
