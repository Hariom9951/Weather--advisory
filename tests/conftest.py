"""Shared fixtures: Open-Meteo-shaped forecasts and scripted fakes for the LLM and weather source."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather_bot import testing  # noqa: E402
from weather_bot.llm import Intent, LLMError  # noqa: E402
from weather_bot.sops import load_policies  # noqa: E402

NOW = datetime(2026, 10, 1, 10, 30)  # fixed clock so hour-index overrides in tests are deterministic
BHOPAL = testing.BHOPAL


def make_forecast(metrics, overrides=None, now=NOW, place=BHOPAL):
    return testing.make_forecast(metrics, overrides, now, place)


class FakeWeather(testing.FixtureWeather):
    def __init__(self, overrides=None, now=NOW, **kw):
        super().__init__(overrides, now=now, **kw)


class FakeLLM:
    """understand() returns the next scripted Intent; compose() returns `prose` (or raises if prose is an Exception)."""

    def __init__(self, intents, prose="Take care out there."):
        self.intents, self.prose, self.payloads = list(intents), prose, []

    def understand(self, question, policy, memory):
        item = self.intents.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def compose(self, payload):
        self.payloads.append(payload)
        if isinstance(self.prose, Exception):
            raise self.prose
        return self.prose


def intent(**kw) -> Intent:
    base = dict(in_scope=True, is_outdoor_activity=True, location="Bhopal", window=None, activity="cycling", sop_ids=[])
    return Intent(**(base | kw))


@pytest.fixture(scope="session")
def policy():
    return load_policies()


__all__ = ["FakeLLM", "FakeWeather", "intent", "make_forecast", "LLMError", "NOW"]
