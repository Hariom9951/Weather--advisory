# Progress log

## 2026-10-01: build and verification

Done and verified (commands in CLAUDE.md):
- Brief read in full from the shared Google Doc (local git-ignored copy at `docs/ASSIGNMENT_BRIEF.txt`; deliberately not published, since the repo is public).
- Open-Meteo field behaviour checked against the live API before design (hourly fields, `timezone=auto`, units).
- `weather_bot/` implemented: weather client, SOP engine, LLM wrapper (Groq default, Anthropic supported), reply/grounding layer, LangGraph.
- 16 SOPs in `policies/sops.yaml`, 5 categories, severities 0-4, fuzzy leisure trio, regional rain-system pair.
- Frontend: first a Streamlit UI, replaced on request by a custom FastAPI + hand-built `web/` app (bulletin layout, severity-mood sky, streamed real graph steps, policy drawer). Verified in a real browser at desktop and 375px widths (question -> live answer, drawer), plus 13 API tests.
- Offline tests: **85 passing** (`python -m pytest -q`), also under `LANGGRAPH_STRICT_MSGPACK=true`.
- Real-model evals: Groq `openai/gpt-oss-120b` two full runs, and Gemini `gemini-3.5-flash-lite` one full run: Groq: 18 pass / 0 fail / 1 skip. Gemini final run on the 22-case suite (2026-10-02): **21 pass, 0 fail, 1 skip** (E07, heavy-rain-only live case, skipped: no city >= IMD heavy that day).
  First run had 6/19 failures; each was root-caused and fixed (see DECISIONS.md "Found by testing" #7). Fixes were made after
  seeing the eval set, so held-out cases remain a to-do.
- Secrets: key is only in git-ignored `.env`; grep confirms it appears nowhere else in the project.

Examiner audit against the brief (2026-10-02), all done and evidenced:
- Added live severe case E07c (any hazard type, numbers recomputed independently from raw API data), E07d (brief's own Bhopal question with a data-driven oracle), E12 (11th SOP added live with the real model, new category and new weather variable).
- Clean-room check: copied the repo to an empty folder, fresh venv, `pip install -r requirements.txt`, validator and 85 tests pass with no `.env`.
- Secret scan of all git history and tree: 0 hits. Lint (ruff F,E9,B): clean (fixed `zip(strict=)`, an unused variable).
- README gained a brief compliance matrix; graph claims (10 nodes, 4 routers) verified from the compiled graph.

Reviewer-criteria pass (2026-10-02, second round):
- Policy judgment: added a `rationale` to all 16 SOPs, verified against sources (WHO UV bands, IMD rainfall categories, Beaufort, NWS heat-index bands; where sources disagree the rationale says so), shown in the UI drawer, required by a test.
- Architecture: README diagram now generated from the compiled graph, with a drift test and a test that no failure route reaches the writer.
- Eval quality: held-out set `evals/heldout.py` (17 cases, written after the main suite was frozen), 17/17 on first and only run.
- Explanation: `docs/REVIEW_GUIDE.md` plus two extra ready-made live SOPs in `docs/live_sops/`, each proven live with the real model.

Pending / for the user:
- The 2026-10-02 audit changes (E07c/E07d/E12, `Evidence.window_name`, strict `zip`, README compliance matrix, policy-file header note, regenerated `evals/RESULTS.md`) are **uncommitted**; GitHub still has the earlier commit `c650331`. Commit/push only on request. Review `git status` before the first commit; `.env`, `.venv` are ignored.
- E07 will run live when a severe system exists; when it does it records the forecast to `evals/fixtures/` (commit that file to keep a real replay).
- Rotate the Groq key after the review (it was shared in chat).
- Optional: held-out eval cases; run evals on the Anthropic provider.
