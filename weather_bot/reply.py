"""Deterministic reply pieces: facts, citation footer, fixed messages, and the grounding check.

Everything a user must be able to trust (numbers, policy ids, "no guidance", failures) is produced here by
code from API values and SOP text. The LLM only supplies the conversational wrapper, and `check_prose`
rejects that wrapper if it strays.
"""
from __future__ import annotations

import re
from typing import Iterable

from weather_bot.sops import SEVERITY_LABELS, SOP, SOP_ID_RE, Evidence, Match
from weather_bot.weather import Forecast

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_AGG_WORDS = {"max": "highest", "min": "lowest", "sum": "total", "mean": "average", "change": "change in", "any": "reported"}

OUT_OF_SCOPE = ("I can only give guidance on how the weather affects outdoor activities and travel, and I "
                "don't have an SOP covering that question, so I won't guess. Try asking about cycling, a trip, "
                "exercise, kids, older relatives, pets or a picnic.")
ASK_LOCATION = "Which city or town should I check the weather for?"


def fmt_num(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _fact(e: Evidence) -> str:
    unit = f" {e.unit}" if e.unit and e.unit != "wmo code" else ""
    metric = e.metric.replace("_", " ")
    if e.agg == "any":
        return f"{metric} {fmt_num(e.value)} reported ({e.window_label})"
    return f"{_AGG_WORDS[e.agg]} {metric}: {fmt_num(e.value)}{unit} ({e.window_label})"


def facts(matches: Iterable[Match]) -> list[str]:
    """The API-sourced numbers that triggered the matched SOPs (conditions that held), de-duplicated."""
    seen: dict[str, None] = {}
    for m in matches:
        for e in m.evidence:
            if e.passed and e.show and e.value is not None:
                seen.setdefault(_fact(e))
    return list(seen)


def fact_items(matches: Iterable[Match]) -> list[dict]:
    """Same evidence as `facts`, structured for a UI: label, value, unit, window."""
    seen: dict[tuple, dict] = {}
    for m in matches:
        for e in m.evidence:
            if e.passed and e.show and e.value is not None:
                unit = "" if e.unit == "wmo code" else e.unit
                label = e.metric.replace("_", " ") if e.agg == "any" else f"{_AGG_WORDS[e.agg]} {e.metric.replace('_', ' ')}"
                seen.setdefault((label, e.window_label), {"label": label, "value": fmt_num(e.value), "unit": unit,
                                                          "window": e.window_label})
    return list(seen.values())


def _sop_line(s: SOP) -> str:
    return f"{s.id} ({SEVERITY_LABELS[s.severity]}): {s.title}"


def footer(ranked: list[Match], fc: Forecast) -> str:
    place = fc.place
    return "\n".join([
        "", "---",
        "**Policy:** " + "; ".join(_sop_line(m.sop) for m in ranked),
        "**Data:** " + "; ".join(facts(ranked)),
        f"**Source:** Open-Meteo forecast for {place.label} ({place.latitude:.2f}, {place.longitude:.2f}), "
        f"fetched at {fc.now:%d %b %H:%M} local time.",
    ])


def template_prose(ranked: list[Match]) -> str:
    """Model-free wording of the answer: used when the LLM is down or its text fails the grounding check."""
    primary, rest = ranked[0].sop, ranked[1:3]
    text = f"{_sop_line(primary)}. {' '.join(primary.advice.split())}"
    if rest:
        text += "\n\nAlso applies:\n" + "\n".join(f"- {_sop_line(m.sop)}: {' '.join(m.sop.advice.split())}" for m in rest)
    return text


def no_sop_reply(place_label: str, checked: list[str]) -> str:
    if checked:
        return (f"No SOP applies right now for {place_label}: I checked {', '.join(checked)} against the live forecast "
                "and none of their conditions is met. That means my policies have no specific warning to give, not a "
                "guarantee that conditions are safe. I won't add advice that isn't backed by a policy.")
    return (f"I don't have an SOP that gives guidance for that question, so I won't guess. I'm able to advise on "
            f"outdoor exercise, travel, children, older adults, pets and leisure outings for {place_label}.")


def failure_reply(reason: str) -> str:
    return f"{reason} I won't guess at a forecast without real data. Please try again in a moment."


def _numbers(text: str) -> set[float]:
    return {float(n) for n in _NUM_RE.findall(SOP_ID_RE.sub(" ", text))}


def check_prose(prose: str, matched: list[Match], fact_lines: list[str]) -> list[str]:
    """Return the reasons the model's wording is unacceptable (empty list = fine).
    Numbers: every figure must appear in the API facts or the matched SOP text.
    Policy ids: every cited id must be a matched SOP."""
    allowed_text = fact_lines + [m.sop.advice + " " + m.sop.title for m in matched]
    allowed = set().union(*(_numbers(t) for t in allowed_text)) if allowed_text else set()
    problems = [f"ungrounded number {n:g}" for n in sorted(_numbers(prose) - allowed)]
    cited = set(SOP_ID_RE.findall(prose))
    problems += [f"cites non-matching policy {i}" for i in sorted(cited - {m.sop.id for m in matched})]
    if not prose.strip():
        problems.append("empty reply")
    return problems
