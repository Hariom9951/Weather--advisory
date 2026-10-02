# Design decisions and known gaps

Each entry: decision, why, what was rejected. Findings from testing are listed separately below.

## D1. The model classifies and phrases; code decides
Safety verdicts must be explainable and reproducible, so condition checking, ranking, citations and all numbers are code.
The LLM is used only where meaning is needed (paraphrase-robust topic matching) and where language is the product (wording).
*Rejected:* letting the model read all SOPs and decide (non-deterministic, un-auditable); keyword/embedding lookup alone
(the brief explicitly wants paraphrase robustness; embeddings add a dependency and a threshold nobody can defend).

## D2. SOPs as declarative YAML, one file
Readable, diffable, load-time validated; thresholds and advice text live in one place. The condition language is small on
purpose: leaf = (metric, agg, window, op, value), groups = all/any. *Rejected:* Python rule functions (violates "no code
change"); one file per SOP (more files for 16 short rules, no benefit yet; revisit at ~50 rules); a rules engine library.

## D3. Fetched fields derive from the policies
`PolicySet.metrics()` is the union of metrics referenced by SOPs, so a new SOP can use a new weather variable with no
fetch-code edit. Guard: `weather.ALLOWED_METRICS` allowlist, so a typo fails at load, not as an HTTP 400 on every request.
Cost: a variable Open-Meteo adds tomorrow needs one line there. Said honestly in the README.

## D4. Time windows are data
`auto` = the window the user asked about (now, today, morning, midday, evening, tomorrow); fixed windows (e.g. `next_24h`
for rainfall totals) are named in the YAML. Windows resolve against the forecast's own local time (`timezone=auto`). Clock
windows already past roll to tomorrow; "today" with <3 h left uses the next 12 h. Evidence labels carry the real date range
so a rolled window is never misread.

## D5. Resolution of multiple matching SOPs
Drop superseded -> `lead` first -> higher severity -> id. Primary + up to two "also applies", all cited. Rejected "answer
with only the top SOP" (can hide a second real hazard) and "just list everything" (no ranking = unusable).

## D6. The regional rain system
Modelled as a **24 h rainfall total** against IMD categories, `any_outdoor` + `lead`. Reason: the brief's own point is that
such a system is bigger than any single threshold. Gap: this is a forecast-derived proxy; no IMD bulletin ingestion.

## D7. Grounding enforced in code, not by prompt alone
Facts and footer are built from API values by code. Model prose passes `check_prose` (numbers must appear in facts/SOP
text; ids must be matched SOPs) or is replaced by SOP-template wording. `compose` never sees the raw question, so there is
no instruction channel for an attacker. Gap: digit-based check; spelled-out numbers would pass it.

## D8. Session memory = small structured dict in a LangGraph checkpointer
Not raw chat history: location, window, activity, SOP topics, last 3 decisions, last 3 questions. Cheap, inspectable, and
enough for follow-ups and consistency. Per-turn state is reset in `understand` so an old error/forecast can't leak.

## D9. Honest failure everywhere
Geocode empty/error, forecast error/malformed, LLM `understand` error each have an explicit path and message. LLM `compose`
failure falls back to SOP-template wording (still grounded), so the weather answer survives a model outage.

## Found by testing (not by thinking)
1. **Fuzzy gap (live data).** Bhopal feels-like 33.1 C sat between LEI-001's "pleasant" (<=32) and LEI-002's "poor" (>=36),
   so the picnic question got "no SOP applies" on an ordinary warm day. Fix: LEI-003 "mixed" as the exact complement of
   LEI-001, LEI-002 supersedes it; a property test over a parameter grid proves no daytime gap.
2. **Night picnics.** The same live run called 23:00 "pleasant". Fix: LEI-001/003 require some daylight (`is_day`) in the
   window (`show: false` keeps this gating check out of the user-facing data line).
3. **Follow-ups lost the activity.** "what about this evening?" names no activity, so the matcher had no SOP topics and the
   bot would say "no SOP applies" after a cycling answer. Fix: `same_activity_as_before` flag + deterministic inheritance of the
   previous SOP topics; negative test that a new activity does not inherit.
4. **Cumulative trace.** A reducer on `trace` would have accumulated across turns because state persists per session. Now a plain per-turn list.
5. **Checkpoint serialization.** LangGraph warned that unregistered state types will be blocked in a future version; ours are
   registered explicitly and the suite passes under `LANGGRAPH_STRICT_MSGPACK=true`.
6. **Template wording** didn't name its primary SOP, which would break "primary cited first"; fixed.

7. **Model-facing defects found by the first real-model eval run (6/19 failed):** provider tool-call flakiness (JSON-schema mode + retry);
   sibling-SOP selection (topic grouping in `sops.topic_groups`); fuzzy-leisure over-matching scuba diving (scope text in YAML);
   `in_scope` too narrow, hiding the rain-system rule for unnamed activities (prompt). Fixes were made after seeing the eval set, so held-out cases are still owed.

8. **Provider portability found real issues (Gemini run):** free-tier per-minute limits (explicit `LLMRateLimited`, fail-fast in the app,
   wait-and-retry in evals); a listed Gemini model that was closed to new users (probe before choosing); a fake-policy+real-question message
   refused wholesale (prompt now classifies the genuine question); a window label that read "00:00-00:00" (now 24:00) and a rolled-over
   "this afternoon" that did not say "tomorrow" (labels now name the day).

9. **Examiner audit found the live severe case was skipping.** E07 only looked for heavy rain and skipped on a rainless day. Added E07c (most
   severe real hazard of any type, grounded by independent recomputation), E07d (brief's exact question, oracle derived from the data) and E12 (real-model
   11th-SOP test). Evidence now records the window it used so a check can recompute it without trusting the engine.

10. **Thresholds now carry their reasons.** A reviewer should be able to ask "where does 45 km/h come from?" and get an answer. Each SOP has a `rationale`; checking
   the cited standards turned up a real discrepancy (IMD publishes two rainfall tables: heavy 64.5-115.5 / very heavy 115.6-204.4, and an older glossary with very heavy from 124.5),
   so HAZ-002 states the edition it follows and that changing it is one number.
11. **A held-out eval set** was added because the main suite was tuned on its own failures. It was run once and reported as it came out.

## Known gaps (unfixed, by choice)
- Rain-system proxy, hourly-slot "now", first-geocode-result default, digit-only grounding check, no auth on the UI (details in README).
- Weather API is called per turn with no retry/backoff or caching (one 10 s attempt, then an honest failure).
