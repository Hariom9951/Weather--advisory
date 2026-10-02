# Plan: Weather-Advisory Support Bot

Source of truth: the assignment brief (kept out of this public repo; local git-ignored copy at `docs/ASSIGNMENT_BRIEF.txt`). Facts below were checked
against it and against the live Open-Meteo API on 2026-10-01.

## Core idea: the LLM never decides facts or advice

| Concern | Owner | Why |
|---|---|---|
| Understand the question (location, time window, which SOP topics are relevant) | LLM (structured output, enum-constrained to SOP ids) | Paraphrase robustness; keyword matching is explicitly not wanted |
| Geocode + fetch weather | Plain code (Open-Meteo) | Facts must come from the API |
| Do the SOP conditions hold for this forecast? | Plain code (declarative condition evaluator) | Safety verdict must be deterministic and auditable |
| Which SOP wins when several apply | Plain code (`lead` flag, then severity, then `supersedes`) | Decided on purpose, documented |
| Phrase the reply | LLM, fed only the winning SOP advice + API facts | Language only |
| Check the phrasing | Plain code: every number must come from API/SOP, every cited id must be a matched SOP | Enforces "bot composes language, doesn't decide facts" |
| Citation + data footer | Plain code, always appended | Traceability never depends on the model |

## Graph (real branching)

```
START -> understand -+-> out_of_scope --------------------------+
                     +-> ask_location --------------------------+
                     +-> geocode -+-> data_failure -------------+
                                  +-> fetch_forecast -+-> data_failure
                                                      +-> evaluate -+-> no_sop ------+
                                                                    +-> compose -> validate -+-> finalize
                                                                                             +-> template_fallback
                              every terminal path -> remember -> END
```
Session memory: LangGraph `MemorySaver` keyed by `thread_id` (one per chat session). Carries last location, window,
activity, and a decision log (SOP ids + severity per turn). Weather is always re-fetched; memory is never a data source.

## Policies (SOPs)
`policies/sops.yaml`: one YAML file (windows registry + SOP list). Declarative conditions (metric, aggregation, window,
operator, threshold, nested all/any). Fetched weather fields are derived from the metrics the SOPs reference, so adding
an SOP (even one using a new Open-Meteo variable from the allowlist) touches no Python. 16 SOPs, 5 categories,
severity 0-4, one fuzzy (picnic) pair, one regional-hazard pair with `lead` + `supersedes`.

## Eval suite
`evals/run_evals.py` writes `evals/RESULTS.md`. Real LLM; weather is either live (grounding case, discovered dynamically,
never hardcoded) or an injected Open-Meteo-shaped fixture (deterministic SOP/paraphrase/adversarial cases). Offline
`pytest` suite (fake LLM) covers the deterministic machinery. Honest labelling of live vs fixture vs skipped.

## Build order
1. Repo scaffold, CLAUDE.md, memory/records. 2. `sops.py` + `weather.py` + tests. 3. `llm.py`, `reply.py`, `graph.py` + tests.
4. FastAPI `server.py` + custom `web/` frontend. 5. Evals (needs API key; ask user then). 6. README + write-up with honest gaps.

## Open items
- LLM: Groq `openai/gpt-oss-120b` (key supplied by user, in git-ignored .env); Anthropic supported but unexercised.
- Git: repo initialised; commits only when the user asks.
