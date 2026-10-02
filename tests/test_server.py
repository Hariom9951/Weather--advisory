"""HTTP layer: policies endpoint, NDJSON chat stream (real graph, fake LLM, fixture weather), validation, failure modes."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from conftest import FakeLLM, FakeWeather, LLMError, intent
from server import create_app
from weather_bot.sops import DEFAULT_POLICY_PATH

SID = "session-abcdef123456"


def make_client(intents, weather=None, prose="Gusts of 60 km/h make riding risky.", policy_path=DEFAULT_POLICY_PATH):
    llm = FakeLLM(intents, prose)
    return TestClient(create_app(lambda: llm, weather or FakeWeather({"wind_gusts_10m": 60.0}), policy_path)), llm


def stream(client, question, sid=SID):
    resp = client.post("/api/chat", json={"session_id": sid, "question": question})
    assert resp.status_code == 200
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def test_health_and_static_frontend():
    client, _ = make_client([])
    assert client.get("/api/health").json() == {"ok": True}
    page = client.get("/")
    assert page.status_code == 200 and "Weather Advisory" in page.text


def test_policies_endpoint_exposes_text_and_rule_lines():
    client, _ = make_client([])
    data = client.get("/api/policies").json()
    exr3 = next(p for p in data if p["id"] == "EXR-003")
    assert exr3["severity"] == "Warning" and "unbalance" in exr3["advice"] and "wind gusts 10m" in exr3["rule"][0]
    assert len(data) >= 10 and next(p for p in data if p["id"] == "HAZ-001")["lead"] is True


def test_chat_streams_steps_then_structured_result():
    client, _ = make_client([intent(sop_ids=["EXR-003"])])
    events = stream(client, "Is it safe to cycle in Bhopal today?")
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "result" and kinds.count("result") == 1
    assert [e["node"] for e in events if e["type"] == "step"][:3] == ["understand", "geocode", "fetch_forecast"]
    result = events[-1]
    assert result["label"] == "Warning" and result["mood"] == "warning"
    assert result["policies"][0]["id"] == "EXR-003" and "---" not in result["prose"] and "Policy:" not in result["prose"]
    assert result["facts"][0]["value"] == "60" and result["facts"][0]["unit"] == "km/h"
    assert result["source"]["place"].startswith("Bhopal")


def test_failure_and_no_sop_results_have_calm_moods():
    client, _ = make_client([intent()], FakeWeather(geocode_error="I could not find a place called 'X'."))
    failed = stream(client, "cycle in X?")[-1]
    assert failed["outcome"] == "data_failure" and failed["mood"] == "fault" and failed["policies"] == [] and failed["source"] is None
    client2, _ = make_client([intent(sop_ids=["EXR-003"])], FakeWeather({}))
    none = stream(client2, "cycle in Bhopal?")[-1]
    assert none["outcome"] == "no_sop" and none["mood"] == "neutral" and "EXR-003" in none["checked"]


def test_session_memory_works_through_the_api():
    client, _ = make_client([intent(sop_ids=["EXR-003"]), intent(location=None, window="evening", same_activity_as_before=True)])
    stream(client, "cycle in Bhopal today?")
    second = stream(client, "what about this evening?")[-1]
    assert second["outcome"] == "answered" and second["policies"][0]["id"] == "EXR-003"


@pytest.mark.parametrize("body", [
    {"session_id": "short", "question": "hi"},                       # session id too short
    {"session_id": SID, "question": ""},                              # empty question
    {"session_id": SID, "question": "x" * 1001},                      # too long
    {"session_id": "bad id with spaces!!", "question": "hi"},         # illegal characters
    {"question": "hi"},                                                # missing session
])
def test_chat_rejects_invalid_requests(body):
    intents = [intent()]
    client, llm = make_client(intents)
    assert client.post("/api/chat", json=body).status_code == 422
    assert len(llm.intents) == 1  # the model was never consulted


def test_missing_llm_key_gives_readable_503_not_a_crash():
    def no_key():
        raise LLMError("GROQ_API_KEY is not set. Copy .env.example to .env and add your key.")

    client = TestClient(create_app(no_key, FakeWeather({}), DEFAULT_POLICY_PATH))
    assert client.get("/api/policies").status_code == 200              # UI still loads
    resp = client.post("/api/chat", json={"session_id": SID, "question": "hi"})
    assert resp.status_code == 503 and "GROQ_API_KEY" in resp.json()["detail"]


def test_internal_errors_never_leak_to_the_browser():
    client, _ = make_client([RuntimeError("secret internal detail")])
    events = stream(client, "cycle in Bhopal?")
    assert events[-1]["type"] == "error" and "secret" not in json.dumps(events)


def test_policy_edits_are_picked_up_without_restart(tmp_path):
    path = tmp_path / "sops.yaml"
    shutil.copy(DEFAULT_POLICY_PATH, path)
    client, _ = make_client([], policy_path=path)
    before = len(client.get("/api/policies").json())
    path.write_text(path.read_text(encoding="utf-8") + """
  - id: ZZZ-001
    category: leisure
    title: Test rule
    severity: 1
    applies_to: Testing only.
    when: {metric: wind_gusts_10m, agg: max, window: auto, op: ">=", value: 1}
    advice: Test advice.
""", encoding="utf-8")
    assert len(client.get("/api/policies").json()) == before + 1


def test_policies_endpoint_includes_the_rationale():
    client, _ = make_client([])
    haz2 = next(p for p in client.get("/api/policies").json() if p["id"] == "HAZ-002")
    assert "115.6" in haz2["rationale"] and "124.5" in haz2["rationale"]   # states the edition it follows and the caveat
