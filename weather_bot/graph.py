"""The LangGraph agent. Dependencies (LLM, weather source, policy) are injected so every branch is testable.

                 +-> out_of_scope ---------------------------------+
  understand ----+-> ask_location ----------------------------------+
                 +-> geocode --+-> failure ------------------------+
                               +-> fetch_forecast -+-> failure ----+
                                                   +-> evaluate --+-> no_sop -------------+
                                                                  +-> compose -----------+
  every terminal node -> remember -> END   (compose = LLM wording, checked, else SOP template)
"""
from __future__ import annotations

import uuid
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from weather_bot import reply as R
from weather_bot.llm import LLM, LLMError, LLMRateLimited, clean_activity
from weather_bot.sops import SEVERITY_LABELS, Match, PolicySet, candidates, describe_window, evaluate, rank
from weather_bot.weather import Forecast, Place, WeatherError, WeatherSource

# Our own state types, allowed explicitly so checkpoint (de)serialization stays valid under strict msgpack mode.
_STATE_TYPES = [("weather_bot.weather", "Place"), ("weather_bot.weather", "Forecast"), ("weather_bot.sops", "SOP"),
                ("weather_bot.sops", "Match"), ("weather_bot.sops", "Evidence"), ("weather_bot.sops", "Cond")]
RECENT = 3  # how many earlier turns of memory are carried (questions + decisions)


class State(TypedDict, total=False):
    question: str
    memory: dict                       # persists across turns of one session (checkpointer thread)
    # --- per-turn, reset by `understand` ---
    intent: dict | None
    location: str | None
    window: str
    place: Place | None
    forecast: Forecast | None
    ranked: list[Match]
    checked: list[str]
    outcome: str                       # answered | no_sop | out_of_scope | ask_location | data_failure | system_error
    error: str | None
    reply: str
    trace: list[str]                   # nodes visited this turn, for the UI and evals


_RESET = {"intent": None, "location": None, "place": None, "forecast": None, "ranked": [], "checked": [],
          "outcome": "", "error": None, "reply": ""}


def _sop_payload(m: Match) -> dict:
    s = m.sop
    return {"title": s.title, "severity": SEVERITY_LABELS[s.severity], "category": s.category,
            "advice": " ".join(s.advice.split())}


def build_graph(llm: LLM, weather: WeatherSource, policy: PolicySet, checkpointer=None):
    metrics = policy.metrics()

    def understand(state: State):
        question, memory = state["question"], state.get("memory") or {}
        out = {**_RESET, "trace": [f"--- {question[:40]!r}", "understand"], "window": "today"}
        try:
            intent = llm.understand(question, policy, memory)
        except LLMError as exc:
            reply = ("I've reached my language-model usage limit for now, so I can't read new questions. Please try again in a few minutes."
                     if isinstance(exc, LLMRateLimited) else
                     "I'm having trouble understanding requests right now. Please try again shortly.")
            return {**out, "outcome": "system_error", "error": str(exc), "reply": reply}
        location = (intent.location or "").strip()[:100] or None
        from_memory = False
        if intent.in_scope and not location and memory.get("location"):
            location, from_memory = memory["location"], True  # follow-up: don't make the user repeat themselves
        window = intent.window if intent.window in policy.user_windows() else None
        if window is None:
            window = memory.get("window", "today") if from_memory else "today"
        # Unknown ids (hallucinated or injected, e.g. "SOP-999") are dropped here, before anything uses them.
        intent_d = intent.model_dump() | {"sop_ids": [i for i in intent.sop_ids if i in policy.by_id()]}
        if intent.same_activity_as_before and not intent_d["sop_ids"]:  # follow-up: keep the previous activity's SOP topics
            intent_d.update(sop_ids=memory.get("sop_ids", []), is_outdoor_activity=memory.get("is_outdoor", intent.is_outdoor_activity),
                            activity=intent.activity or memory.get("activity", ""))
        outcome = "out_of_scope" if not intent.in_scope else "" if location else "ask_location"
        return {**out, "intent": intent_d, "location": location, "window": window, "outcome": outcome}

    def geocode(state: State):
        try:
            return {"place": weather.geocode(state["location"]), "trace": state["trace"] + ["geocode"]}
        except WeatherError as exc:
            return {"outcome": "data_failure", "error": str(exc), "trace": state["trace"] + ["geocode:failed"]}

    def fetch_forecast(state: State):
        try:
            return {"forecast": weather.forecast(state["place"], metrics), "trace": state["trace"] + ["fetch_forecast"]}
        except WeatherError as exc:
            return {"outcome": "data_failure", "error": str(exc), "trace": state["trace"] + ["fetch_forecast:failed"]}

    def evaluate_node(state: State):
        intent = state["intent"]
        cands = candidates(policy, intent["sop_ids"], intent["is_outdoor_activity"])
        ranked = rank(evaluate(policy, cands, state["forecast"], state["window"]))
        return {"ranked": ranked, "checked": [s.id for s in cands], "trace": state["trace"] + ["evaluate"],
                "outcome": "answered" if ranked else "no_sop"}

    def compose(state: State):
        ranked, fc, memory = state["ranked"], state["forecast"], state.get("memory") or {}
        fact_lines = R.facts(ranked)
        payload = {
            "activity": clean_activity(state["intent"]["activity"]), "place": fc.place.label,
            "window": describe_window(policy.windows[state["window"]].label, *policy.windows[state["window"]].bounds(fc.now), fc.now),
            "primary": _sop_payload(ranked[0]), "also_applies": [_sop_payload(m) for m in ranked[1:3]],
            "facts": fact_lines, "prior_decisions": memory.get("decisions", []),
        }
        try:
            prose = llm.compose(payload)
            problems = R.check_prose(prose, ranked, fact_lines)
        except LLMError as exc:
            prose, problems = "", [str(exc)]
        if problems:  # model unavailable or wording not grounded: fall back to fixed wording from the SOPs
            prose, note = R.template_prose(ranked), "compose:template_fallback(" + "; ".join(problems) + ")"
        else:
            note = "compose:llm"
        return {"reply": prose + R.footer(ranked, fc), "trace": state["trace"] + [note]}

    def no_sop(state: State):
        label = state["forecast"].place.label
        return {"reply": R.no_sop_reply(label, state["checked"]), "trace": state["trace"] + ["no_sop"]}

    def out_of_scope(state: State):
        return {"reply": R.OUT_OF_SCOPE, "trace": state["trace"] + ["out_of_scope"]}

    def ask_location(state: State):
        return {"reply": R.ASK_LOCATION, "trace": state["trace"] + ["ask_location"]}

    def failure(state: State):
        return {"reply": R.failure_reply(state["error"]), "trace": state["trace"] + ["failure"]}

    def remember(state: State):
        memory = dict(state.get("memory") or {})
        memory["recent_questions"] = (memory.get("recent_questions", []) + [state["question"][:200]])[-RECENT:]
        if state.get("place"):
            memory.update(location=state["location"], place=state["place"].label, window=state["window"])
        if state.get("intent"):
            memory.update(activity=clean_activity(state["intent"]["activity"]), sop_ids=state["intent"]["sop_ids"],
                          is_outdoor=state["intent"]["is_outdoor_activity"])
        if state.get("ranked"):
            primary = state["ranked"][0].sop
            decision = {"place": state["place"].label, "window": state["window"], "activity": memory.get("activity", ""),
                        "policy": primary.id, "severity": SEVERITY_LABELS[primary.severity],
                        "also": [m.sop.id for m in state["ranked"][1:]]}
            memory["decisions"] = (memory.get("decisions", []) + [decision])[-RECENT:]
        return {"memory": memory, "trace": state["trace"] + ["remember"]}

    g = StateGraph(State)
    for name, fn in [("understand", understand), ("geocode", geocode), ("fetch_forecast", fetch_forecast),
                     ("evaluate", evaluate_node), ("compose", compose), ("no_sop", no_sop),
                     ("out_of_scope", out_of_scope), ("ask_location", ask_location), ("failure", failure),
                     ("remember", remember)]:
        g.add_node(name, fn)
    g.add_edge(START, "understand")
    g.add_conditional_edges("understand", lambda s: s["outcome"] or "geocode",
                            {"geocode": "geocode", "out_of_scope": "out_of_scope", "ask_location": "ask_location",
                             "system_error": "remember"})
    g.add_conditional_edges("geocode", lambda s: "failure" if s["outcome"] == "data_failure" else "fetch_forecast",
                            {"failure": "failure", "fetch_forecast": "fetch_forecast"})
    g.add_conditional_edges("fetch_forecast", lambda s: "failure" if s["outcome"] == "data_failure" else "evaluate",
                            {"failure": "failure", "evaluate": "evaluate"})
    g.add_conditional_edges("evaluate", lambda s: "compose" if s["ranked"] else "no_sop",
                            {"compose": "compose", "no_sop": "no_sop"})
    for terminal in ("compose", "no_sop", "out_of_scope", "ask_location", "failure"):
        g.add_edge(terminal, "remember")
    g.add_edge("remember", END)
    return g.compile(checkpointer=checkpointer or new_checkpointer())


def new_checkpointer() -> MemorySaver:
    """In-memory session store: memory lasts as long as the process/session object, as the brief requires."""
    return MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=_STATE_TYPES))


def new_session_id() -> str:
    return uuid.uuid4().hex


def ask(graph, session_id: str, question: str) -> dict:
    """Run one chat turn in a session. Returns the final state (reply, outcome, ranked, trace, ...)."""
    return graph.invoke({"question": question}, {"configurable": {"thread_id": session_id}})


def mermaid() -> str:
    """Mermaid source of the compiled graph, for the README (a test keeps the README in sync with the real graph)."""
    from weather_bot.sops import load_policies
    return build_graph(None, None, load_policies()).get_graph().draw_mermaid()


if __name__ == "__main__":  # `python -m weather_bot.graph` prints the diagram
    print(mermaid())
