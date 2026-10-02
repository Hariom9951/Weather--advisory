"""HTTP API + static frontend. Run: uvicorn server:app --port 8000   (then open http://localhost:8000)

POST /api/chat streams NDJSON: one {"type":"step","node":...} per real graph node as it finishes, then a single
{"type":"result",...} (or {"type":"error"}). The policy file is re-read whenever it changes on disk.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from weather_bot import reply as R
from weather_bot.graph import build_graph, new_checkpointer
from weather_bot.llm import ChatLLM, LLMError
from weather_bot.sops import DEFAULT_POLICY_PATH, SEVERITY_LABELS, PolicyError, describe_rule, load_policies
from weather_bot.weather import OpenMeteo

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("weather_bot.server")

# Background mood the frontend shows for each kind of answer.
OUTCOME_MOOD = {"no_sop": ("No SOP applies", "neutral"), "out_of_scope": ("Outside my scope", "neutral"),
                "ask_location": ("Need a location", "neutral"), "data_failure": ("Weather data unavailable", "fault"),
                "system_error": ("Service issue", "fault")}


class ChatRequest(BaseModel):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    question: str = Field(min_length=1, max_length=1000)


def describe(state: dict) -> dict:
    """Turn a finished graph state into the structured payload the UI renders."""
    ranked, fc = state.get("ranked") or [], state.get("forecast")
    prose = state["reply"].partition("\n---\n")[0].strip()  # the citation footer is rebuilt in the UI from structure
    if ranked:
        label = SEVERITY_LABELS[ranked[0].sop.severity]
        mood = label.lower()
    else:
        label, mood = OUTCOME_MOOD.get(state["outcome"], ("Info", "neutral"))
    return {
        "type": "result", "outcome": state["outcome"], "label": label, "mood": mood, "prose": prose,
        "policies": [{"id": m.sop.id, "title": m.sop.title, "severity": SEVERITY_LABELS[m.sop.severity]} for m in ranked],
        "checked": state.get("checked") or [],
        "facts": R.fact_items(ranked),
        "source": ({"place": fc.place.label, "lat": round(fc.place.latitude, 2), "lon": round(fc.place.longitude, 2),
                    "fetched": f"{fc.now:%d %b %H:%M}"} if fc and ranked else None),
        "trace": state.get("trace", [])[1:],
    }


def create_app(llm_factory=ChatLLM, weather=None, policy_path: Path = DEFAULT_POLICY_PATH) -> FastAPI:
    load_dotenv(ROOT / ".env")
    app, lock = FastAPI(title="Weather Advisory", docs_url=None, redoc_url=None), threading.Lock()
    weather = weather or OpenMeteo()
    saver, cache = new_checkpointer(), {}  # saver is shared; sessions are separated by thread_id

    def current():
        """(policy, graph), rebuilt when the policy file changes. LLM is created lazily so the UI loads without a key."""
        with lock:
            mtime = policy_path.stat().st_mtime
            if cache.get("mtime") != mtime:
                try:
                    policy = load_policies(policy_path)
                except PolicyError as exc:
                    raise HTTPException(503, str(exc)) from exc
                cache.update(mtime=mtime, policy=policy, graph=None)
            if cache.get("graph") is None:
                try:
                    cache["llm"] = cache.get("llm") or llm_factory()
                except LLMError as exc:
                    raise HTTPException(503, str(exc)) from exc
                cache["graph"] = build_graph(cache["llm"], weather, cache["policy"], checkpointer=saver)
            return cache["policy"], cache["graph"]

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/policies")
    def policies():
        try:
            with lock:
                policy = load_policies(policy_path)
        except PolicyError as exc:
            raise HTTPException(503, str(exc)) from exc
        return [{"id": s.id, "category": s.category, "title": s.title, "severity": SEVERITY_LABELS[s.severity],
                 "applies_to": " ".join(s.applies_to.split()), "advice": " ".join(s.advice.split()),
                 "rule": describe_rule(s.when, policy), "rationale": " ".join(s.rationale.split()), "lead": s.lead} for s in policy.sops]

    @app.post("/api/chat")
    def chat(req: ChatRequest):
        _, graph = current()  # raises 503 with a readable message before streaming starts
        config = {"configurable": {"thread_id": req.session_id}}

        def events():
            try:
                for update in graph.stream({"question": req.question}, config, stream_mode="updates"):
                    for node in update:
                        yield json.dumps({"type": "step", "node": node}) + "\n"
                yield json.dumps(describe(graph.get_state(config).values)) + "\n"
            except Exception:  # never leak internals to the browser; the log has the traceback
                log.exception("chat turn failed")
                yield json.dumps({"type": "error", "message": "Something went wrong on my side. Please try again."}) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
    return app


app = create_app()
