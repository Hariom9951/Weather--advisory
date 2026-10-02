from datetime import datetime
from pathlib import Path

import pytest
import yaml

from conftest import NOW, make_forecast
from weather_bot.sops import PolicyError, WindowSpec, candidates, evaluate, load_policies, rank


def run(policy, sop_id, overrides, window="today", now=NOW):
    fc = make_forecast(policy.metrics(), overrides, now=now)
    return evaluate(policy, [policy.by_id()[sop_id]], fc, window)


def test_shipped_policy_meets_brief(policy):
    assert len(policy.sops) >= 10
    assert len({s.category for s in policy.sops}) >= 3
    assert len({s.severity for s in policy.sops}) >= 3          # a range of severities, not all "dangerous"
    assert any(s.severity == 0 for s in policy.sops)             # fuzzy/favourable outcome exists
    assert any(s.lead and s.any_outdoor for s in policy.sops)    # the regional rain-system rule


@pytest.mark.parametrize("uv,expected", [(7.9, False), (8.0, True), (11.0, True)])
def test_threshold_boundary(policy, uv, expected):
    assert bool(run(policy, "EXR-001", {"uv_index": {12: uv}})) is expected


def test_auto_window_follows_the_users_window(policy):
    peak_midday = {"uv_index": {12: 9.0}}
    assert run(policy, "EXR-001", peak_midday, window="midday")
    assert not run(policy, "EXR-001", peak_midday, window="evening")


def test_missing_data_never_matches(policy):
    assert not run(policy, "EXR-001", {"uv_index": lambda i: None})
    assert not run(policy, "EXR-003", {"wind_gusts_10m": lambda i: None})


def test_group_all_and_any(policy):
    calm_picnic = run(policy, "LEI-001", {})
    assert calm_picnic and calm_picnic[0].sop.severity == 0
    assert not run(policy, "LEI-001", {"precipitation_probability": {12: 40.0}})       # one failing leaf breaks `all`
    assert run(policy, "LEI-002", {"wind_gusts_10m": {12: 36.0}})                      # one passing leaf satisfies `any`
    assert not run(policy, "LEI-002", {})


def test_in_any_weather_code(policy):
    assert run(policy, "HAZ-003", {"weather_code": {13: 95.0}})
    assert not run(policy, "HAZ-003", {"weather_code": {13: 61.0}})


def test_rain_system_uses_24h_total_not_user_window(policy):
    # 3 mm/h for 24 h = 72 mm: no single hour looks extreme, but the total is IMD "heavy".
    wet = {"precipitation": lambda i: 3.0 if 10 <= i < 34 else 0.0}
    m = run(policy, "HAZ-001", wet, window="evening")
    assert m and m[0].evidence[0].value == pytest.approx(72.0)
    assert not run(policy, "HAZ-001", {"precipitation": lambda i: 2.0 if 10 <= i < 34 else 0.0})  # 48 mm


def test_rank_supersedes_lead_then_severity(policy):
    fc = make_forecast(policy.metrics(), {"precipitation": lambda i: 6.0 if 10 <= i < 34 else 0.0,   # 144 mm: very heavy
                                          "wind_gusts_10m": 60.0, "uv_index": 9.0})
    ids = ["HAZ-001", "HAZ-002", "EXR-001", "EXR-003"]
    ranked = rank(evaluate(policy, [policy.by_id()[i] for i in ids], fc))
    order = [m.sop.id for m in ranked]
    assert "HAZ-001" not in order                     # superseded by HAZ-002
    assert order == ["HAZ-002", "EXR-003", "EXR-001"]  # lead first, then severity 3 before 2


def test_candidates_drop_unknown_ids_and_add_any_outdoor(policy):
    got = {s.id for s in candidates(policy, ["EXR-003", "SOP-999"], is_outdoor=True)}
    assert "EXR-003" in got and "SOP-999" not in got and "HAZ-001" in got
    assert {s.id for s in candidates(policy, [], is_outdoor=False)} == set()


def test_windows_clock_rollover_and_rest_of_day():
    evening = WindowSpec(label="e", start_hour=17, end_hour=21, roll_if_past=True)
    assert evening.bounds(datetime(2026, 10, 1, 10, 0)) == (datetime(2026, 10, 1, 17), datetime(2026, 10, 1, 21))
    assert evening.bounds(datetime(2026, 10, 1, 22, 0)) == (datetime(2026, 10, 2, 17), datetime(2026, 10, 2, 21))
    assert evening.bounds(datetime(2026, 10, 1, 18, 30)) == (datetime(2026, 10, 1, 18), datetime(2026, 10, 1, 21))
    today = WindowSpec(label="t", rest_of_day=True)
    assert today.bounds(datetime(2026, 10, 1, 10, 30)) == (datetime(2026, 10, 1, 10), datetime(2026, 10, 2, 0))
    assert today.bounds(datetime(2026, 10, 1, 22, 30)) == (datetime(2026, 10, 1, 22), datetime(2026, 10, 2, 10))


def _policy_with(tmp_path, mutate):
    data = yaml.safe_load(Path("policies/sops.yaml").read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["sops"][0]["when"].update(metric="temprature_2m"), "unknown weather metric"),
    (lambda d: d["sops"].append(dict(d["sops"][0])), "duplicate SOP id"),
    (lambda d: d["sops"][0].update(supersedes=["ZZZ-999"]), "supersedes unknown"),
    (lambda d: d["sops"][0]["when"].update(window="fortnight"), "unknown window"),
    (lambda d: d["sops"][0].update(severity=9), "severity"),
    (lambda d: d["sops"][0]["when"].update(op="in_any"), "in_any needs a list"),
    (lambda d: d["sops"][0].update(typo_field=1), "typo_field"),
])
def test_invalid_policies_are_rejected_at_load(tmp_path, mutate, needle):
    with pytest.raises(PolicyError, match=needle):
        load_policies(_policy_with(tmp_path, mutate))


def test_leisure_bands_leave_no_gap_in_daytime(policy):
    """LEI-001 (pleasant) and LEI-003 (mixed) are exact complements while there is daylight, so a fuzzy
    'nice day for...?' question never falls into a no-guidance hole between good and poor."""
    import itertools
    for app_t, prob, gust, uv in itertools.product((15.0, 25.0, 33.0, 38.0), (10.0, 40.0, 60.0), (10.0, 32.0, 40.0), (3.0, 9.0)):
        o = {"apparent_temperature": app_t, "precipitation_probability": prob, "wind_gusts_10m": gust, "uv_index": uv}
        good, mixed = run(policy, "LEI-001", o), run(policy, "LEI-003", o)
        assert bool(good) != bool(mixed), o
        if run(policy, "LEI-002", o):
            assert [m.sop.id for m in rank(run(policy, "LEI-002", o) + mixed)] == ["LEI-002"]   # poor supersedes mixed


def test_leisure_gives_nothing_at_night(policy):
    night = {"is_day": 0.0}
    assert not run(policy, "LEI-001", night) and not run(policy, "LEI-003", night)


def test_choosing_one_sop_evaluates_its_whole_topic(policy):
    """The matcher picks topics, not siblings: LEI-001 alone pulls in LEI-002/003, EXR-003 pulls in EXR-004."""
    assert {"LEI-001", "LEI-002", "LEI-003"} <= {s.id for s in candidates(policy, ["LEI-001"], is_outdoor=False)}
    assert "EXR-004" in {s.id for s in candidates(policy, ["EXR-003"], is_outdoor=False)}
    assert "EXR-001" not in {s.id for s in candidates(policy, ["EXR-003"], is_outdoor=False)}


def test_window_label_names_the_day_so_a_rolled_window_is_not_mistaken_for_today():
    from weather_bot.sops import describe_window
    now = datetime(2026, 10, 1, 23, 30)
    midday = WindowSpec(label="midday", start_hour=11, end_hour=16, roll_if_past=True)
    assert describe_window("midday", *midday.bounds(now), now) == "midday, tomorrow 11:00-16:00 local"
    assert describe_window("midday", *midday.bounds(datetime(2026, 10, 1, 9, 0)), datetime(2026, 10, 1, 9, 0)) == "midday, today 11:00-16:00 local"
    overnight = WindowSpec(label="rest", rest_of_day=True)
    assert describe_window("rest", *overnight.bounds(now), now) == "rest, today 23:00-tomorrow 11:00 local"


def test_window_ending_at_midnight_reads_24_00_not_00_00():
    from weather_bot.sops import describe_window
    now = datetime(2026, 10, 2, 0, 20)
    today = WindowSpec(label="today", rest_of_day=True)
    assert describe_window("today", *today.bounds(now), now) == "today, today 00:00-24:00 local"


def test_every_shipped_sop_explains_its_threshold(policy):
    """Policy judgment must be visible: each shipped SOP carries a rationale naming a standard or an explicit owner judgment."""
    for s in policy.sops:
        assert len(s.rationale.split()) >= 12, f"{s.id} needs a real rationale"
    assert all(("judgment" in s.rationale or "IMD" in s.rationale or "WHO" in s.rationale or "WMO" in s.rationale
                or "NWS" in s.rationale or "meteorological" in s.rationale or "fuzzy" in s.rationale.lower()
                or "complement" in s.rationale) for s in policy.sops)
