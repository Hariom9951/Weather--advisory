"""SOP policy model, loader, and deterministic evaluator.

Policies are data (policies/sops.yaml). This module is the only code that interprets them, and it knows
nothing about HTTP or LLMs: adding/changing an SOP never requires editing it.
"""
from __future__ import annotations

import operator
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from weather_bot.weather import ALLOWED_METRICS, Forecast

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "sops.yaml"
SEVERITY_LABELS = {0: "Favourable", 1: "Note", 2: "Caution", 3: "Warning", 4: "Danger"}
_OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq}
_AGGS = {
    "max": max, "min": min, "sum": sum,
    "mean": lambda v: sum(v) / len(v),
    "change": lambda v: v[-1] - v[0],  # last minus first hour in the window (e.g. pressure trend)
}
SOP_ID_RE = re.compile(r"\b[A-Z]{3}-\d{3}\b")


class PolicyError(ValueError):
    """The policy file is invalid. Raised at load time so a bad SOP can never reach a user."""


class WindowSpec(BaseModel):
    """A named time window. Exactly one of: rolling_hours, rest_of_day, or start_hour+end_hour."""
    model_config = ConfigDict(extra="forbid")
    label: str
    user_selectable: bool = False  # may the question-understanding step choose this window?
    rolling_hours: int | None = Field(None, gt=0)
    rest_of_day: bool = False
    min_hours: int = 3        # rest_of_day only: if fewer hours than this remain ...
    fallback_hours: int = 12  # ... use this many rolling hours instead
    start_hour: int | None = Field(None, ge=0, le=23)
    end_hour: int | None = Field(None, ge=1, le=24)
    day_offset: int = 0
    roll_if_past: bool = False  # clock window already over today -> use tomorrow's

    @model_validator(mode="after")
    def _one_kind(self):
        kinds = [self.rolling_hours is not None, self.rest_of_day, self.start_hour is not None]
        if sum(kinds) != 1:
            raise ValueError("window needs exactly one of rolling_hours / rest_of_day / start_hour+end_hour")
        if self.start_hour is not None and (self.end_hour is None or self.end_hour <= self.start_hour):
            raise ValueError("clock window needs end_hour > start_hour")
        return self

    def bounds(self, now: datetime) -> tuple[datetime, datetime]:
        """Half-open [start, end) in the forecast's local time."""
        hour = now.replace(minute=0, second=0, microsecond=0)
        if self.rolling_hours is not None:
            return hour, hour + timedelta(hours=self.rolling_hours)
        if self.rest_of_day:
            midnight = hour.replace(hour=0) + timedelta(days=1)
            if midnight - hour >= timedelta(hours=self.min_hours):
                return hour, midnight
            return hour, hour + timedelta(hours=self.fallback_hours)
        day = hour.replace(hour=0) + timedelta(days=self.day_offset)
        start, end = day + timedelta(hours=self.start_hour), day + timedelta(hours=self.end_hour)
        if self.roll_if_past and end <= now:
            start, end = start + timedelta(days=1), end + timedelta(days=1)
        if start <= hour < end:  # window already under way: only the remaining part is still ahead
            start = hour
        return start, end


def describe_window(label: str, start: datetime, end: datetime, now: datetime) -> str:
    """e.g. 'midday/afternoon, tomorrow 11:00-16:00 local'. Names the day so a window that rolled forward is never mistaken for today."""
    def day(dt: datetime) -> str:
        delta = (dt.date() - now.date()).days
        return "today" if delta == 0 else "tomorrow" if delta == 1 else f"{dt:%d %b}"
    last = end - timedelta(seconds=1)  # end is exclusive: 24:00 belongs to the previous day
    if last.date() == start.date():
        tail = "24:00" if end.time() == datetime.min.time() else f"{end:%H:%M}"
    else:
        tail = f"{day(last)} {end:%H:%M}"
    return f"{label}, {day(start)} {start:%H:%M}-{tail} local"


class Cond(BaseModel):
    """Either a leaf (metric/agg/window/op/value) or a group (all_ / any_) of nested conditions."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    all_: list["Cond"] | None = Field(None, alias="all")
    any_: list["Cond"] | None = Field(None, alias="any")
    metric: str | None = None
    agg: Literal["max", "min", "sum", "mean", "change"] = "max"
    window: str = "auto"  # "auto" = the window the user asked about
    op: Literal[">=", ">", "<=", "<", "==", "in_any"] | None = None
    value: float | list[float] | None = None
    show: bool = True  # false = a gating condition that is not worth quoting to the user (e.g. is_day)

    @model_validator(mode="after")
    def _shape(self):
        is_leaf = self.metric is not None
        if sum([is_leaf, self.all_ is not None, self.any_ is not None]) != 1:
            raise ValueError("condition must be exactly one of: leaf (metric), all, any")
        if is_leaf:
            if self.op is None or self.value is None:
                raise ValueError(f"leaf '{self.metric}' needs op and value")
            if (self.op == "in_any") != isinstance(self.value, list):
                raise ValueError(f"leaf '{self.metric}': in_any needs a list value, other ops a number")
        return self

    def leaves(self) -> Iterable["Cond"]:
        if self.metric is not None:
            yield self
        for child in (self.all_ or []) + (self.any_ or []):
            yield from child.leaves()


class SOP(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Z]{3}-\d{3}$")
    category: str
    title: str
    severity: int = Field(ge=0, le=4)
    applies_to: str          # plain-English scope; the question matcher reads this to pick candidates
    any_outdoor: bool = False  # candidate for every outdoor-activity question, whatever the activity
    lead: bool = False       # always presented before non-lead SOPs, whatever their severity
    supersedes: list[str] = []  # SOP ids this one makes redundant when both match
    when: Cond
    advice: str
    rationale: str = ""  # why this threshold: the standard it follows or an explicit policy-owner judgment


class PolicySet(BaseModel):
    windows: dict[str, WindowSpec]
    sops: list[SOP]

    def by_id(self) -> dict[str, SOP]:
        return {s.id: s for s in self.sops}

    def metrics(self) -> set[str]:
        return {leaf.metric for s in self.sops for leaf in s.when.leaves()}

    def user_windows(self) -> list[str]:
        return [n for n, w in self.windows.items() if w.user_selectable]


def load_policies(path: Path | str = DEFAULT_POLICY_PATH, valid_metrics: Iterable[str] = ALLOWED_METRICS) -> PolicySet:
    """Parse and cross-validate the policy file. Any problem raises PolicyError naming the culprit."""
    try:
        policy = PolicySet.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise PolicyError(f"Invalid policy file {path}: {exc}") from exc
    valid_metrics = set(valid_metrics)
    ids = [s.id for s in policy.sops]
    problems = [f"duplicate SOP id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    for s in policy.sops:
        problems += [f"{s.id}: supersedes unknown SOP {o}" for o in s.supersedes if o not in ids]
        for leaf in s.when.leaves():
            if leaf.metric not in valid_metrics:
                problems.append(f"{s.id}: unknown weather metric '{leaf.metric}'")
            if leaf.window != "auto" and leaf.window not in policy.windows:
                problems.append(f"{s.id}: unknown window '{leaf.window}'")
    if "today" not in policy.windows:
        problems.append("windows must define 'today' (default when the user names no time)")
    if problems:
        raise PolicyError("Invalid policy file: " + "; ".join(problems))
    return policy


@dataclass(frozen=True)
class Evidence:
    """One condition leaf as evaluated against the real forecast. `value` is None if no data in window."""
    metric: str
    agg: str
    window_label: str
    value: float | None
    unit: str
    op: str
    threshold: float | list[float]
    passed: bool
    show: bool = True
    window_name: str = ""  # the policy window this was evaluated over (lets an independent check recompute it)


@dataclass(frozen=True)
class Match:
    sop: SOP
    evidence: list[Evidence] = field(default_factory=list)


def _eval(cond: Cond, fc: Forecast, policy: PolicySet, user_window: str, out: list[Evidence]) -> bool:
    if cond.metric is None:
        # No short-circuit: evaluate every child so the recorded evidence is complete.
        results = [_eval(c, fc, policy, user_window, out) for c in (cond.all_ or cond.any_)]
        return all(results) if cond.all_ is not None else any(results)
    spec = policy.windows[user_window if cond.window == "auto" else cond.window]
    start, end = spec.bounds(fc.now)
    vals = [v for t, v in zip(fc.times, fc.series[cond.metric], strict=True) if start <= t < end and v is not None]
    if not vals:  # missing data never satisfies a condition
        value, passed = None, False
    elif cond.op == "in_any":
        hits = [v for v in vals if v in cond.value]
        value, passed = float(hits[0] if hits else vals[0]), bool(hits)
    else:
        value = float(_AGGS[cond.agg](vals))
        passed = _OPS[cond.op](value, cond.value)
    window_label = describe_window(spec.label, start, end, fc.now)
    out.append(Evidence(cond.metric, "any" if cond.op == "in_any" else cond.agg, window_label, value,
                        fc.units.get(cond.metric, ""), cond.op, cond.value, passed, cond.show,
                        user_window if cond.window == "auto" else cond.window))
    return passed


def describe_rule(cond: Cond, policy: PolicySet) -> list[str]:
    """Human-readable lines for a condition tree (for display; evaluation never uses this)."""
    if cond.metric is None:
        kids = cond.all_ if cond.all_ is not None else cond.any_
        lead = "All of:" if cond.all_ is not None else "Any of:"
        return [lead] + ["  " + line for k in kids for line in describe_rule(k, policy)]
    window = "the time you ask about" if cond.window == "auto" else policy.windows[cond.window].label
    if cond.op == "in_any":
        return [f"{cond.metric.replace('_', ' ')} is one of {[int(v) for v in cond.value]} at any hour of {window}"]
    sym = {">=": "≥", "<=": "≤"}.get(cond.op, cond.op)
    return [f"{cond.agg} {cond.metric.replace('_', ' ')} {sym} {cond.value:g} over {window}"]


def evaluate(policy: PolicySet, cands: Iterable[SOP], fc: Forecast, user_window: str = "today") -> list[Match]:
    """Return a Match for each candidate SOP whose condition holds for this forecast."""
    if user_window not in policy.windows:
        user_window = "today"
    matches = []
    for sop in cands:
        evidence: list[Evidence] = []
        if _eval(sop.when, fc, policy, user_window, evidence):
            matches.append(Match(sop, evidence))
    return matches


def rank(matches: list[Match]) -> list[Match]:
    """Resolution policy when several SOPs match: drop superseded ones, then lead SOPs first,
    then highest severity, then id for a stable order. The first element is the primary SOP."""
    superseded = {o for m in matches for o in m.sop.supersedes}
    kept = [m for m in matches if m.sop.id not in superseded]
    return sorted(kept, key=lambda m: (not m.sop.lead, -m.sop.severity, m.sop.id))


def topic_groups(policy: PolicySet) -> list[list[SOP]]:
    """SOPs that share the same `applies_to` text are variants of one topic (e.g. pleasant/mixed/poor picnic).
    The matcher chooses topics; every SOP in a chosen topic is then evaluated, so the model never has to pick
    between siblings. any_outdoor SOPs are excluded: they apply to every outdoor question automatically."""
    groups: dict[str, list[SOP]] = {}
    for s in policy.sops:
        if not s.any_outdoor:
            groups.setdefault(" ".join(s.applies_to.split()), []).append(s)
    return list(groups.values())


def candidates(policy: PolicySet, sop_ids: Iterable[str], is_outdoor: bool) -> list[SOP]:
    """SOPs worth evaluating for a question: the whole topic of every id the matcher picked (unknown ids are
    dropped, so a model cannot conjure a policy) plus any_outdoor SOPs when the question is about an outdoor activity."""
    chosen = set(sop_ids)
    in_topic = {s.id for group in topic_groups(policy) if chosen & {s.id for s in group} for s in group}
    return [s for s in policy.sops if s.id in in_topic or (is_outdoor and s.any_outdoor)]


if __name__ == "__main__":  # `python -m weather_bot.sops [path]` validates a policy file (run after editing it)
    import sys
    p = load_policies(*sys.argv[1:2])
    print(f"OK: {len(p.sops)} SOPs, {len(p.windows)} windows, metrics fetched: {sorted(p.metrics())}")
