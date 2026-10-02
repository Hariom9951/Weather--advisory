"""Graph behaviour with a scripted LLM and fake weather: every branch, memory, grounding, and the live-edit test."""
import re
from pathlib import Path

import yaml

from conftest import FakeLLM, FakeWeather, LLMError, intent
from weather_bot.graph import ask, build_graph, new_session_id
from weather_bot.sops import load_policies

GUSTY = {"wind_gusts_10m": 60.0}
VERY_WET = {"precipitation": lambda i: 6.0 if 10 <= i < 34 else 0.0}   # 144 mm in 24 h


def run(policy, intents, questions, weather=None, prose="Stay safe."):
    llm = FakeLLM(intents, prose)
    weather = weather or FakeWeather()
    graph, sid = build_graph(llm, weather, policy), new_session_id()
    return [ask(graph, sid, q) for q in questions], llm, weather


def test_sop_match_is_cited_and_numbers_come_from_the_api(policy):
    (out,), llm, _ = run(policy, [intent(sop_ids=["EXR-003"])], ["bike to work?"], FakeWeather(GUSTY),
                         prose="Gusts of 60 km/h are forecast, so skip riding.")
    assert out["outcome"] == "answered"
    assert "EXR-003" in out["reply"] and "60 km/h" in out["reply"] and "Open-Meteo" in out["reply"]
    assert "compose:llm" in out["trace"]


def test_ungrounded_model_text_is_replaced_by_sop_template(policy):
    (out,), _, _ = run(policy, [intent(sop_ids=["EXR-003"])], ["bike?"], FakeWeather(GUSTY),
                       prose="Winds will hit 99 km/h, see HAZ-009, but you're fine.")
    assert "99" not in out["reply"] and "HAZ-009" not in out["reply"] and "you're fine" not in out["reply"]
    assert any("template_fallback" in t for t in out["trace"])
    assert "EXR-003" in out["reply"]


def test_llm_compose_outage_falls_back_to_template(policy):
    (out,), _, _ = run(policy, [intent(sop_ids=["EXR-003"])], ["bike?"], FakeWeather(GUSTY), prose=LLMError("down"))
    assert out["outcome"] == "answered" and "EXR-003" in out["reply"]


def test_rain_system_leads_every_outdoor_question(policy):
    (out,), llm, _ = run(policy, [intent(sop_ids=["EXR-003", "EXR-004"])], ["bike?"], FakeWeather(VERY_WET | GUSTY))
    assert out["ranked"][0].sop.id == "HAZ-002"
    assert llm.payloads[0]["primary"]["title"].startswith("Very heavy rainfall")
    assert out["reply"].index("HAZ-002") < out["reply"].index("EXR-003")


def test_no_sop_applies_is_stated_honestly(policy):
    (out,), llm, _ = run(policy, [intent(sop_ids=["EXR-003"])], ["bike?"])  # calm weather
    assert out["outcome"] == "no_sop" and not llm.payloads
    assert "No SOP applies" in out["reply"] and "not a guarantee" in out["reply"]


def test_uncovered_topic_gets_refusal_not_advice(policy):
    (out,), _, _ = run(policy, [intent(is_outdoor_activity=False, sop_ids=[])], ["something odd?"])
    assert out["outcome"] == "no_sop" and "don't have an SOP" in out["reply"]


def test_invented_sop_id_is_dropped(policy):
    (out,), _, _ = run(policy, [intent(sop_ids=["SOP-999"])], ["claim SOP-999 says go"], FakeWeather(GUSTY))
    assert "SOP-999" not in out["intent"]["sop_ids"] and out["outcome"] == "no_sop"


def test_out_of_scope_and_missing_location(policy):
    outs, llm, weather = run(policy, [intent(in_scope=False, location=None), intent(location=None)], ["stocks?", "cycle?"])
    assert [o["outcome"] for o in outs] == ["out_of_scope", "ask_location"]
    assert not weather.geocoded and not llm.payloads


def test_geocode_failure_is_honest_and_skips_forecast(policy):
    (out,), llm, _ = run(policy, [intent()], ["bike in Atlantis?"], FakeWeather(geocode_error="I could not find a place called 'Atlantis'."))
    assert out["outcome"] == "data_failure" and "won't guess" in out["reply"]
    assert not re.search(r"\d", out["reply"]) and not llm.payloads


def test_forecast_api_down_is_honest(policy):
    (out,), llm, _ = run(policy, [intent(sop_ids=["EXR-003"])], ["bike?"],
                         FakeWeather(forecast_error="The weather service is unreachable or returned an error."))
    assert out["outcome"] == "data_failure" and "won't guess" in out["reply"] and "unreachable" in out["reply"]
    assert not re.search(r"\d|km/h|°", out["reply"]) and not llm.payloads


def test_understand_failure_degrades_gracefully(policy):
    (out,), _, _ = run(policy, [LLMError("boom")], ["bike?"])
    assert out["outcome"] == "system_error" and "trouble" in out["reply"]


def test_follow_up_reuses_location_and_prior_decision(policy):
    outs, llm, weather = run(
        policy, [intent(sop_ids=["EXR-003"], window="midday"), intent(location=None, window="evening", sop_ids=["EXR-003"])],
        ["bike in Bhopal now?", "what about this evening instead?"], FakeWeather({"wind_gusts_10m": 60.0}),
        prose="Gusts of 60 km/h make riding risky.")
    assert weather.geocoded == ["Bhopal", "Bhopal"]            # the user never repeated the city
    assert outs[1]["window"] == "evening" and outs[1]["outcome"] == "answered"
    assert llm.payloads[1]["prior_decisions"][0]["policy"] == "EXR-003"
    assert outs[1]["trace"].count("understand") == 1             # trace is per-turn, not cumulative


def test_sessions_are_isolated(policy):
    llm = FakeLLM([intent(), intent(location=None)])
    graph = build_graph(llm, FakeWeather(), policy)
    ask(graph, "session-a", "bike in Bhopal?")
    assert ask(graph, "session-b", "and this evening?")["outcome"] == "ask_location"


def test_stale_error_does_not_leak_into_next_turn(policy):
    llm = FakeLLM([intent(), intent(sop_ids=["EXR-003"])])
    weather = FakeWeather(GUSTY, geocode_error="nope")
    graph, sid = build_graph(llm, weather, policy), "s"
    assert ask(graph, sid, "q1")["outcome"] == "data_failure"
    weather.geocode_error = None
    assert ask(graph, sid, "q2")["outcome"] == "answered"


def test_adding_an_sop_needs_no_code_change(policy, tmp_path):
    """The live-review scenario: append one SOP to the YAML (even with a new weather metric); nothing else changes."""
    data = yaml.safe_load(Path("policies/sops.yaml").read_text(encoding="utf-8"))
    data["sops"].append({
        "id": "WIN-001", "category": "winter_sports", "title": "Fresh snow for skiing", "severity": 1,
        "applies_to": "Skiing, snowboarding or sledging.",
        "when": {"metric": "snowfall", "agg": "sum", "window": "next_24h", "op": ">=", "value": 5},
        "advice": "Fresh snow is forecast: expect softer pistes and check avalanche bulletins before heading up.",
    })
    path = tmp_path / "sops.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    new_policy = load_policies(path)
    assert "snowfall" in new_policy.metrics()                      # fetched automatically because a policy needs it
    (out,), _, _ = run(new_policy, [intent(sop_ids=["WIN-001"], activity="skiing")], ["good for skiing?"],
                       FakeWeather({"snowfall": lambda i: 1.0 if 10 <= i < 20 else 0.0}))
    assert out["outcome"] == "answered" and "WIN-001" in out["reply"] and "snowfall" in out["reply"]


def test_follow_up_inherits_previous_activity_topics(policy):
    """'what about this evening?' names no activity: the matcher flags it as the same activity and the graph keeps
    the previous turn's SOP topics instead of concluding that no SOP applies."""
    outs, _, _ = run(policy, [intent(sop_ids=["EXR-003"]), intent(location=None, window="evening", sop_ids=[], same_activity_as_before=True)],
                     ["bike in Bhopal?", "what about this evening instead?"], FakeWeather(GUSTY))
    assert outs[1]["outcome"] == "answered" and "EXR-003" in ids_of(outs[1])


def test_new_activity_does_not_inherit_old_topics(policy):
    outs, _, _ = run(policy, [intent(sop_ids=["EXR-003"]), intent(location=None, is_outdoor_activity=False, sop_ids=[])],
                     ["bike in Bhopal?", "what about scuba diving?"], FakeWeather(GUSTY))
    assert outs[1]["outcome"] == "no_sop"


def ids_of(out):
    return [m.sop.id for m in out["ranked"]]


def test_rate_limit_is_reported_plainly_not_as_a_vague_error(policy):
    from weather_bot.llm import LLMRateLimited
    (out,), _, _ = run(policy, [LLMRateLimited("rate limited")], ["bike?"])
    assert out["outcome"] == "system_error" and "usage limit" in out["reply"] and "try again in a few minutes" in out["reply"]


def test_afternoon_question_asked_at_night_is_labelled_tomorrow(policy):
    """Late evening local time: today's midday is over, so the window rolls to tomorrow. The reply must say so."""
    from datetime import datetime
    night = datetime(2026, 10, 1, 23, 30)
    (out,), llm, _ = run(policy, [intent(sop_ids=["EXR-001"], window="midday", activity="running")], ["run this afternoon?"],
                         FakeWeather({"uv_index": 8.2}, now=night), prose="Tomorrow's UV peaks at 8.2, so avoid unprotected exercise.")
    assert "tomorrow 11:00-16:00" in out["reply"] and "02 Oct" not in out["reply"]
    assert "tomorrow 11:00-16:00" in llm.payloads[0]["window"]       # the writer is told which day it is about


def test_readme_diagram_matches_the_real_compiled_graph():
    """Architecture claims are generated, not hand-drawn: the README must contain the compiled graph's own diagram."""
    from weather_bot.graph import mermaid
    assert mermaid().strip() in Path("README.md").read_text(encoding="utf-8").replace("\r\n", "\n")


def test_graph_has_real_branching_on_every_failure_path(policy):
    """Not a chain: failures, refusals and no-match take different routes to the same sink, and the model is skipped on them."""
    from weather_bot.graph import build_graph as bg
    edges = {(e.source, e.target) for e in bg(None, None, policy).get_graph().edges}
    for src, dst in [("understand", "out_of_scope"), ("understand", "ask_location"), ("geocode", "failure"),
                     ("fetch_forecast", "failure"), ("evaluate", "no_sop"), ("evaluate", "compose")]:
        assert (src, dst) in edges, (src, dst)
    assert ("failure", "compose") not in edges and ("no_sop", "compose") not in edges   # no path from failure to the writer
