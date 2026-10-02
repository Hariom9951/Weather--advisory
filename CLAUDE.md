# CLAUDE.md: Weather-Advisory Support Bot

Take-home assignment (the brief is not in this public repo; a local copy may sit at the git-ignored `docs/ASSIGNMENT_BRIEF.txt`). A LangGraph chatbot that answers outdoor-activity safety
questions using live Open-Meteo data. **All advice must come from written SOPs; the LLM never decides facts or advice.**

## Commands (Windows/PowerShell or Git Bash; Python 3.10)
```bash
python -m venv .venv && . .venv/Scripts/activate && pip install -r requirements.txt
python -m pytest -q                       # offline suite: fake LLM + fixture weather, no key, no network needed
python -m weather_bot.sops                # validate policies/sops.yaml (run after every policy edit)
uvicorn server:app --port 8000            # API + web UI at http://localhost:8000 (needs GEMINI_API_KEY in .env)
python -m evals.run_evals [E01 E10a ...]  # real-LLM eval suite -> writes evals/RESULTS.md (needs key + network)
```

## Architecture (keep these boundaries)
| File | Responsibility | Must NOT |
|---|---|---|
| `policies/sops.yaml` | All advice + thresholds + time windows (data) | contain code |
| `weather_bot/sops.py` | Load/validate policy, evaluate conditions, rank matches | do I/O, call an LLM |
| `weather_bot/weather.py` | Geocode + hourly forecast (Open-Meteo) | know about SOPs |
| `weather_bot/llm.py` | `understand` (question -> Intent) and `compose` (wording only) | decide safety, see raw text in `compose` |
| `weather_bot/reply.py` | Facts, citation footer, fixed messages, grounding check | call an LLM |
| `weather_bot/graph.py` | LangGraph wiring, branching, session memory | contain policy knowledge |
| `server.py` | HTTP API (`/api/policies`, streaming `/api/chat`), serves `web/`, reloads policies on change | contain policy/LLM logic |
| `web/` | Static frontend (HTML/CSS/JS). Insert server text with `textContent` only, never `innerHTML` | call anything but `/api/*` |
| `weather_bot/testing.py` | Fixture/recorded `WeatherSource` for tests + evals | be used in production flow |

## Invariants (a change that breaks one is a bug)
1. Every answer cites an SOP id or says no SOP applies. Citation + data footer are built by code (`reply.footer`).
2. Numbers in replies come from the API/SOP text. `reply.check_prose` rejects ungrounded numbers/ids -> template fallback.
3. Unknown SOP ids from the model are dropped (`sops.candidates`, `graph.understand`). Model never invents a policy.
4. Weather/geocode/LLM failures route to an honest failure message; never a guessed forecast.
5. Adding/changing an SOP = edit YAML only. Fetched metrics derive from the policies (`PolicySet.metrics()`).
6. `compose` receives structured payload only, never the raw user question (injection surface).
7. Per-turn state is reset in `understand`; only `memory` persists per session (LangGraph `MemorySaver`, thread_id).

## Conventions
- Small modules, type hints, `from __future__ import annotations`, no dead code, comments explain *why*.
- Validate at boundaries (policy load, API responses, LLM output); trust internal code.
- Tests: add/adjust in `tests/` for any logic change; every new graph branch needs a test. Eval cases must state what
  is checked and what a pass looks like; never weaken a case to make it pass; report failures honestly.
- Secrets: `.env` only (git-ignored). Never print or commit keys.
- Git: commit only when the user asks.

## Records (maintain these)
- `docs/DECISIONS.md`: why each design choice was made + known gaps (keep honest).
- `docs/PROGRESS.md`: what is done / verified / pending, with dates.
- Claude memory dir holds the same facts for future sessions; update after significant steps.
