"""Open-Meteo client: geocoding + hourly forecast. Pure data access; no policy, no LLM."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Protocol

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 3  # covers "tomorrow" from any hour of today

# Open-Meteo hourly variables an SOP may reference. Allowlist so a typo in a policy fails at load
# time instead of turning every forecast request into an HTTP 400 at runtime.
ALLOWED_METRICS = frozenset({
    "temperature_2m", "apparent_temperature", "relative_humidity_2m", "dew_point_2m",
    "precipitation", "precipitation_probability", "rain", "showers", "snowfall", "snow_depth",
    "weather_code", "pressure_msl", "surface_pressure", "cloud_cover", "visibility",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "uv_index", "cape",
    "shortwave_radiation", "is_day",
})


class WeatherError(Exception):
    """Location or forecast could not be obtained. The message is safe to show the user."""


@dataclass(frozen=True)
class Place:
    name: str
    admin1: str | None
    country: str | None
    latitude: float
    longitude: float

    @property
    def label(self) -> str:
        return ", ".join(p for p in (self.name, self.admin1, self.country) if p)


@dataclass(frozen=True)
class Forecast:
    """Hourly series in the place's local time. `now` is local wall-clock time at fetch."""
    place: Place
    now: datetime
    times: list[datetime]
    series: dict[str, list[float | None]]
    units: dict[str, str]


class WeatherSource(Protocol):
    def geocode(self, name: str) -> Place: ...
    def forecast(self, place: Place, metrics: Iterable[str]) -> Forecast: ...


class OpenMeteo:
    def __init__(self, client: httpx.Client | None = None, timeout: float = 10.0):
        self._client = client or httpx.Client(timeout=timeout)

    def _get_json(self, url: str, params: dict, what: str) -> dict:
        try:
            resp = self._client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WeatherError(f"The {what} service is unreachable or returned an error.") from exc
        if not isinstance(data, dict):
            raise WeatherError(f"The {what} service returned an unexpected response.")
        return data

    def geocode(self, name: str) -> Place:
        # "Bhopal, India" -> search "Bhopal", prefer a candidate whose admin/country matches the rest.
        head, _, hint = (p.strip() for p in name.partition(","))
        data = self._get_json(GEOCODE_URL, {"name": head, "count": 10, "language": "en"}, "location")
        results = data.get("results") or []
        if not results:
            raise WeatherError(f"I could not find a place called '{name}'.")
        hint_l = hint.lower()
        best = next(
            (r for r in results if hint_l and hint_l in f"{r.get('admin1', '')} {r.get('country', '')}".lower()),
            results[0],
        )
        try:
            return Place(best["name"], best.get("admin1"), best.get("country"),
                         float(best["latitude"]), float(best["longitude"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherError("The location service returned an unusable result.") from exc

    def forecast(self, place: Place, metrics: Iterable[str]) -> Forecast:
        metrics = sorted(set(metrics))
        params = {
            "latitude": place.latitude, "longitude": place.longitude,
            "hourly": ",".join(metrics), "forecast_days": FORECAST_DAYS, "timezone": "auto",
        }
        data = self._get_json(FORECAST_URL, params, "weather")
        try:
            hourly = data["hourly"]
            times = [datetime.fromisoformat(t) for t in hourly["time"]]
            series = {m: list(hourly[m]) for m in metrics}
            offset = int(data["utc_offset_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherError("The weather service returned incomplete data.") from exc
        if not times or any(len(s) != len(times) for s in series.values()):
            raise WeatherError("The weather service returned incomplete data.")
        now = (datetime.now(timezone.utc) + timedelta(seconds=offset)).replace(tzinfo=None)
        return Forecast(place, now, times, series, dict(data.get("hourly_units") or {}))
