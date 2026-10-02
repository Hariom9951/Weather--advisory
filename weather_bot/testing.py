"""Offline weather sources for tests and evals: synthetic fixtures and recorded real forecasts.

Live weather never sits still, so deterministic checks run against a forecast we control; the live-grounding
eval records any genuinely severe real forecast it finds (save_forecast) so it can be replayed after the event passes.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from weather_bot.weather import Forecast, Place, WeatherError

BHOPAL = Place("Bhopal", "Madhya Pradesh", "India", 23.26, 77.41)
UNITS = {"temperature_2m": "°C", "apparent_temperature": "°C", "precipitation": "mm", "precipitation_probability": "%",
         "wind_gusts_10m": "km/h", "uv_index": "", "visibility": "m", "weather_code": "wmo code", "snowfall": "cm",
         "is_day": ""}
CALM = {"temperature_2m": 24.0, "apparent_temperature": 25.0, "precipitation": 0.0, "precipitation_probability": 5.0,
        "wind_gusts_10m": 10.0, "uv_index": 2.0, "visibility": 24000.0, "weather_code": 0.0, "is_day": 1.0}


def make_forecast(metrics, overrides=None, now: datetime | None = None, place: Place = BHOPAL) -> Forecast:
    """72 hourly slots from 00:00 on `now`'s day (default: the real current local time).
    overrides: {metric: constant | {hour_index: value} | callable(hour_index)}; other metrics get calm values."""
    now = now or datetime.now().replace(second=0, microsecond=0)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    times = [start + timedelta(hours=i) for i in range(72)]
    series = {}
    for m in metrics:
        ov, base = (overrides or {}).get(m), CALM.get(m, 0.0)
        if callable(ov):
            series[m] = [ov(i) for i in range(72)]
        elif isinstance(ov, dict):
            series[m] = [ov.get(i, base) for i in range(72)]
        else:
            series[m] = [base if ov is None else ov] * 72
    return Forecast(place, now, times, series, {m: UNITS.get(m, "") for m in metrics})


class FixtureWeather:
    """WeatherSource returning a controlled forecast (or a controlled failure). Records what was geocoded."""

    def __init__(self, overrides=None, geocode_error=None, forecast_error=None, now=None, forecast: Forecast | None = None):
        self.overrides, self.now, self.fixed = overrides, now, forecast
        self.geocode_error, self.forecast_error = geocode_error, forecast_error
        self.geocoded: list[str] = []

    def geocode(self, name):
        self.geocoded.append(name)
        if self.geocode_error:
            raise WeatherError(self.geocode_error)
        return self.fixed.place if self.fixed else BHOPAL

    def forecast(self, place, metrics):
        if self.forecast_error:
            raise WeatherError(self.forecast_error)
        if self.fixed:
            missing = set(metrics) - set(self.fixed.series)
            if missing:  # a recorded forecast predates a newly added SOP metric: refuse rather than guess
                raise WeatherError(f"Recorded forecast lacks {sorted(missing)}; re-record it.")
            return self.fixed
        return make_forecast(metrics, self.overrides, self.now, place)


def save_forecast(fc: Forecast, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "place": vars(fc.place), "now": fc.now.isoformat(), "times": [t.isoformat() for t in fc.times],
        "series": fc.series, "units": fc.units}, indent=1), encoding="utf-8")


def load_forecast(path: Path) -> Forecast:
    d = json.loads(path.read_text(encoding="utf-8"))
    return Forecast(Place(**d["place"]), datetime.fromisoformat(d["now"]), [datetime.fromisoformat(t) for t in d["times"]],
                    d["series"], d["units"])
