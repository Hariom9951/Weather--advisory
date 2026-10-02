"""Open-Meteo client against a mocked transport: success shape and every failure that must become WeatherError."""
from datetime import datetime

import httpx
import pytest

from weather_bot.weather import OpenMeteo, Place, WeatherError

PLACE = Place("Bhopal", "Madhya Pradesh", "India", 23.26, 77.41)
FORECAST_OK = {
    "utc_offset_seconds": 19800,
    "hourly_units": {"time": "iso8601", "uv_index": "", "wind_gusts_10m": "km/h"},
    "hourly": {"time": ["2026-10-01T00:00", "2026-10-01T01:00"], "uv_index": [0.0, 0.1], "wind_gusts_10m": [6.1, None]},
}


def client(handler):
    return OpenMeteo(httpx.Client(transport=httpx.MockTransport(handler)))


def test_forecast_requests_explicit_fields_and_parses():
    seen = {}

    def handler(req):
        seen.update(req.url.params)
        return httpx.Response(200, json=FORECAST_OK)

    fc = client(handler).forecast(PLACE, ["wind_gusts_10m", "uv_index"])
    assert seen["hourly"] == "uv_index,wind_gusts_10m" and seen["latitude"] == "23.26" and seen["timezone"] == "auto"
    assert fc.series["wind_gusts_10m"] == [6.1, None] and fc.units["wind_gusts_10m"] == "km/h"
    assert fc.times[0] == datetime(2026, 10, 1, 0, 0) and fc.now.tzinfo is None


def test_geocode_picks_first_result_or_matches_hint():
    results = {"results": [{"name": "Bhopal", "admin1": "Uttar Pradesh", "country": "India", "latitude": 1, "longitude": 2},
                           {"name": "Bhopal", "admin1": "Madhya Pradesh", "country": "India", "latitude": 23.26, "longitude": 77.41}]}
    c = client(lambda req: httpx.Response(200, json=results))
    assert c.geocode("Bhopal").admin1 == "Uttar Pradesh"                      # documented default: first result
    assert c.geocode("Bhopal, Madhya Pradesh").latitude == 23.26              # hint disambiguates


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={}),                      # no "results" key: Open-Meteo's real shape for no match
    httpx.Response(200, json={"results": []}),
    httpx.Response(500, text="oops"),
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(200, json=[1, 2]),
    httpx.Response(200, json={"results": [{"name": "X"}]}),   # no coordinates
])
def test_geocode_failures_raise_weather_error(response):
    with pytest.raises(WeatherError):
        client(lambda req: response).geocode("Nowhere")


def test_network_failure_raises_weather_error():
    def boom(req):
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(WeatherError, match="unreachable"):
        client(boom).forecast(PLACE, ["uv_index"])
    with pytest.raises(WeatherError):
        client(boom).geocode("Bhopal")


@pytest.mark.parametrize("payload", [
    {"hourly": {"time": ["2026-10-01T00:00"]}, "utc_offset_seconds": 0},                                   # metric missing
    {"hourly": {"time": ["2026-10-01T00:00"], "uv_index": [1.0, 2.0]}, "utc_offset_seconds": 0},          # length mismatch
    {"hourly": {"time": [], "uv_index": []}, "utc_offset_seconds": 0},                                      # empty
    {"latitude": 1},                                                                                        # no hourly (the "forgot fields" trap)
    {"hourly": {"time": ["not-a-date"], "uv_index": [1.0]}, "utc_offset_seconds": 0},
])
def test_incomplete_forecast_raises_weather_error(payload):
    with pytest.raises(WeatherError):
        client(lambda req: httpx.Response(200, json=payload)).forecast(PLACE, ["uv_index"])
