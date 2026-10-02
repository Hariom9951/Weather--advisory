# Review guide: the 10-minute walkthrough

Everything below is backed by a file you can open, a test, or an eval row. Nothing here is aspirational.

## 1. The one idea (30 seconds)
**The model understands and phrases; code decides.** A language model is good at "what is this person asking?" and at wording.
It must not decide what is safe, which numbers are true, or which policy exists. So:

| Question | Decided by | Why that side of the line |
|---|---|---|
| What is the user asking, where, and when? | **Model** (structured output, may only pick topic ids from the catalog it is shown) | Paraphrase, Hinglish, follow-ups: keyword lookup cannot do this |
| Which policies are *candidates*? | Code (`sops.candidates`) expands the chosen topics; unknown ids are dropped | The model can never conjure a policy |
| What did the forecast say? | Code (`weather.py`) | Facts must come from the API |
| Does the policy's condition hold? | Code (`sops._eval`), declarative YAML conditions | Safety verdicts must be deterministic and auditable |
| Which policy wins when several match? | Code (`sops.rank`): drop superseded, `lead` first, then severity | Decided on purpose, explained in the README |
| How is it worded? | **Model** (`compose`), given the winning advice and verified facts, never the raw question | Language only, and no instruction channel for an attacker |
| Is the wording acceptable? | Code (`reply.check_prose`): any number not from the API/SOP text, or any non-matching policy id, discards the model's text | "The bot composes language; it does not decide facts" is enforced, not hoped for |
| Citations, numbers, source line | Code (`reply.footer`), always appended | "Why did it say that?" never depends on the model |

## 2. Architecture (the failure path is real)
The diagram in the [README](../README.md) is generated from the compiled graph (`python -m weather_bot.graph`) and a test fails if it
drifts. Failure, refusal and no-match are separate routes that never reach the writer: `tests/test_graph.py::test_graph_has_real_branching_on_every_failure_path`.
Seven distinct end states: answered, no policy, out of scope, missing location, geocode failure, forecast failure, model unavailable.

## 3. Policy judgment
- **Checkable, not "be careful":** every SOP is a condition tree over named Open-Meteo variables, an aggregation, a time window, an operator and a number.
- **Every threshold says why:** the `rationale` field of each SOP (also shown in the UI's policy drawer) names the standard it follows or says
  "policy-owner judgment". Two worked examples of honesty: HAZ-002 follows IMD's operational classification (very heavy from 115.6 mm) and states that an
  older IMD glossary uses 124.5 mm, so changing edition is one number in the YAML; EXR-002 states that Open-Meteo's feels-like is not the NWS heat index.
- **A bigger-than-any-threshold case:** HAZ-001/002 use the 24 h rainfall *total* and are `lead` + `any_outdoor`, so a system that is only 3 mm/h at every hour
  still leads every outdoor answer (test `test_rain_system_uses_24h_total_not_user_window`, eval E06b).
- **Fuzzy case:** "nice day for a picnic" is matched by meaning; the verdict is an explicit five-criterion rubric (LEI-001), its exact complement (LEI-003) and a
  clearly-poor rule (LEI-002). A property test proves a daytime window never falls into a gap.

## 4. Live review: adding an SOP without touching code
Rehearsed against live weather with the real model (results in `evals/RESULTS.md` E12 and below). With the app running (`uvicorn server:app --port 8000`):

1. Open `policies/sops.yaml`, paste one of the ready-made entries at the **end of the file** (indent matters; they are two-space-indented list items).
2. Optionally validate: `python -m weather_bot.sops` (names any mistake).
3. Ask the matching question in the UI. **No restart, no Python edit.** The policy list in the sidebar updates on reload.

| Ready-made SOP | Paste from | Ask (pick a place where it is true today) | Proven live |
|---|---|---|---|
| EXR-005 cold-weather exercise (new rule in an existing category) | `docs/live_sops/cold-run.yaml` | "Is it too cold for a morning run in Reykjavik tomorrow?" | cited EXR-005 with the real 3.8 C |
| LEI-004 muggy hike (new rule, `relative_humidity_2m`, a variable no policy used before) | `docs/live_sops/humid-hike.yaml` | "Is it a good day for a long hike near Kolkata today?" | cited LEI-004 with the real 93% |
| AST-001 stargazing (new category, new variable `cloud_cover`) | the `NEW_SOP_YAML` in `evals/run_evals.py` (E12) | "Is tonight a good night for stargazing in Bhopal?" | cited AST-001, number recomputed from raw data |

Tips that come from rehearsal: the rule only fires where the weather satisfies it (Mumbai's humidity is 73% today, so LEI-004 correctly stays silent there;
Kolkata at 93% fires). If a demo city does not trigger, lowering the threshold in the YAML is itself a no-code change worth showing. A brand-new
Open-Meteo *variable* must appear in `weather.ALLOWED_METRICS` (a deliberate guard so a typo cannot break every request); new rules, thresholds, categories and windows are YAML only.

## 5. Evals (where the weight is)
- `python -m evals.run_evals`: 22 cases, last run 21 pass / 0 fail / 1 skip (the skip needs a heavy-rain event and says so). Every row states what is checked and what a pass looks like.
- `python -m evals.heldout`: 17 cases written *after* the main suite was frozen (Hinglish, named pets, prompt extraction, authority role-play, a fake `SYSTEM:` block, multi-turn pressure,
  noisy unicode, an ambiguous place). Run once, never tuned on: 17 of 17 passed on first contact.
- Grounding is checked against an **independent recomputation** from the raw API series (E07c/E07d/E12), not against the engine's own output.
- Honest limits: the main suite was tuned after its failures were seen (so its passes are weaker evidence); the main suite ran on Gemini 3.5 Flash-Lite and on Groq gpt-oss-120b,
  the held-out set on Gemini only; model output is not perfectly deterministic.

## 6. Questions you should expect, and where the answer is
| Question | Answer in one line | Evidence |
|---|---|---|
| Why not let the model read the SOPs and answer? | Not auditable or reproducible, and it is exactly the failure the brief forbids | D1 in `docs/DECISIONS.md` |
| Why YAML rather than code or a database? | Owners can read and diff it; validated at load; no code change | top of `policies/sops.yaml` |
| How do you stop a made-up policy id? | Unknown ids are dropped; prose citing a non-matching id is rejected | E10b, H12, `test_invented_sop_id_is_dropped` |
| How do you stop wrong numbers? | Numbers are formatted by code; model text with any other number is discarded | `reply.check_prose`, E10c, H13 |
| What if the weather API is down? | Honest failure message; the model is never called | E09a/b/c, `test_forecast_api_down_is_honest` |
| Several policies apply: what happens? | Superseded dropped, `lead` first, then severity; all cited | `sops.rank`, E06a |
| What would you do with more time? | Held-out eval on a second provider; recorded real severe events; ingest IMD bulletins; auth and rate limiting | README "Known gaps" |
| What is the weakest part? | The rain-system rule is a forecast-rainfall proxy, not an IMD bulletin; "now" is the current hour's forecast slot | README "Known gaps" |
