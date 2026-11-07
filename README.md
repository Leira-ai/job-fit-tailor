# Job Fit & Tailor

An Anna App that scores a job seeker's CV against ONE job ad and returns an
honest, evidence-based application package in a single run: a fit grade and
score, matched evidence quoted from the CV, an honest gap list, a tailored
CV with an auditable change log, a cover note, five likely interview
questions, and ATS keywords split into present and missing.

Slug: `job-fit-tailor`. Status: **local build only. Not submitted, not
published.** Built with the `anna-app` CLI (minimal template), following the
official Anna developer docs (concepts, Python Executa quickstart, protocol
lifecycle and v2 negotiation, sampling, display blocks, app quickstart, app
manifest, UI manifest, listing fields, local dev).

## How it works

- `executas/job-fit-tailor/` is a Python Executa plugin speaking JSON-RPC
  2.0 over stdio (`describe`, `initialize`, `health`, `invoke`). It exposes
  one tool, `analyze_job_fit`, with parameters `cv_text` (required),
  `job_text` (required), `job_url` (optional, recorded and echoed only, not
  fetched in v1), `scenario` (`corporate`, `startup`, `internship`), and
  `language` (`en`, `id`).
- When the host negotiates protocol v2 and injects a `sampling_token` in
  the invoke context, the plugin asks the host LLM (reverse RPC
  `sampling/createMessage`, `responseFormat: json_object`) to produce the
  analysis under the honesty contract below. The draft is post-checked:
  the grade is recomputed from the score, and any number in the tailored
  CV that does not appear in the user's CV invalidates the draft, which
  is then replaced by the safe scaffold.
- Without a host (local pipe tests, offline harness), a deterministic
  fallback runs: it extracts requirement lines and known skill phrases
  from the ad, matches them against the CV, computes a coverage score,
  quotes the exact CV lines as evidence, lists unsupported requirements
  as gaps, and returns the tailored CV as a clearly labelled scaffold
  made only of the user's own CV lines, reordered. The engine used is
  logged to stderr and reported in the result field `engine`.
- The result also carries `_display` blocks (grade card, gap summary) so
  the chat UI renders them verbatim.
- `bundle/` is a one-page, mobile-first UI: CV and job ad textareas, an
  optional job URL field, scenario and language selects, a sample-data
  button (fictional CV and ad), and a result panel with the grade card,
  evidence and gaps, the tailored CV with a copy button, the change log,
  the cover note, and the interview questions. It calls the tool through
  `anna.tools.invoke` with the bundled `tool_id`.
- `manifest.json` adds the `system_prompt_addendum` (ask for CV plus one
  ad, suggest the three scenario presets, call the tool, present grade
  card first, offer one revision pass) and declares desktop and mobile
  form factors. `app.json` holds the store listing: name, tagline,
  category `productivity` (the listing doc has no Business category),
  and a long description in English with an Indonesian summary paragraph.

## Honesty rules (baked into prompts, fallback, and listing)

- Never invent or embellish experience, skills, employers, dates,
  metrics, or qualifications that are not in the user's CV text.
- Anything the ad requires that the CV does not support goes to gaps,
  never into the tailored CV.
- Every substantive change to the CV is listed in the change log.
- No hidden keyword stuffing, no ATS trickery advice.
- No em dash character in any output or project file.
- CV text stays inside the run; the only external call is host sampling.

## Run locally

```bash
export PATH="$HOME/.local/bin:$PATH"   # uv lives here on this VM
cd ~/workspace/anna-ai/job-fit-tailor
anna-app validate
anna-app validate --strict
anna-app dev            # harness at http://localhost:5180 (add --no-llm to work offline)
```

Protocol smoke test without the harness (line-delimited JSON-RPC on stdio):

```bash
echo '{"jsonrpc":"2.0","method":"describe","id":1}' | python3 executas/job-fit-tailor/job_fit_tailor_plugin.py
echo '{"jsonrpc":"2.0","method":"health","id":2}' | python3 executas/job-fit-tailor/job_fit_tailor_plugin.py
```

Full invoke tests with the fictional samples in `tests/samples.json` were
run with a stdio driver script during the build; see the test log below.

## Test log (8 Nov 2026, this VM)

Environment: Node v24.22.1, Python 3.12.3, `uv` 0.13.7 (installed to
`~/.local/bin` during this build), `anna-app` CLI 0.1.64 (already present).

1. Executa protocol smoke test (initialize v2, describe, health, invoke
   with two fictional samples; sampling not exercisable over a plain
   pipe because there is no host to inject a `sampling_token`, so both
   invokes ran the deterministic fallback, exactly as designed; the
   stderr log line for each invoke was
   `sampling unavailable, using fallback: no sampling_token in invoke context`):
   - `initialize`: `protocolVersion: "2.0"` negotiated, plugin declared
     `capabilities: {"sampling": {}}`.
   - `describe`: manifest name `tool-leira-job-fit-tailor-s56adxsn`, tools
     `["analyze_job_fit"]`.
   - `health`: `{"status": "ready"}`.
   - Sample A (English, corporate; junior data analyst CV vs a corporate
     data analyst ad requiring 3 years, SQL, Tableau): grade D, score
     48, verdict "Partial fit". Evidence 7 items (python, excel,
     dashboards, reporting, plus requirement lines). Gaps 7 items,
     including "At least 3 years of experience as a data analyst",
     "sql", and "tableau". All honesty assertions passed: no em dash in
     any output, every tailored CV line is an original CV line, every
     number in the tailored CV appears in the input CV.
   - Sample B (Indonesian, internship; final-year student CV vs an
     internship ad requiring SQL and active English): grade F, score 7,
     Indonesian verdict. Gaps 9 items, including "Menguasai SQL untuk
     mengambil data", "sql", and "bahasa inggris". Same honesty
     assertions passed.
   - `uv run tool-leira-job-fit-tailor-s56adxsn` in the executa directory answered
     `health` with `{"status": "ready"}`, confirming the harness spawn
     path and the pyproject entry point.
2. `anna-app validate` output:
   `[validate] @anna-ai/app-schema dispatcher_version=0.33.0` then
   `validate passed` (exit 0).
   `anna-app validate --strict` output: identical, `validate passed`
   (exit 0).
3. `anna-app dev --no-llm` (offline mode chosen because login is out of
   scope for this build): first boot attempt failed with
   `bridge failed to start: python bridge did not signal ready in 8s`
   while the `anna-app-runtime-local@0.2.0a33` wheel was still
   downloading; after warming the uvx cache the bridge answered
   `_ready` and the second boot succeeded. Boot log lines observed:
   `bridge ready`, `executas tool-leira-job-fit-tailor-s56adxsn`,
   `llm bridge disabled (--no-llm)`,
   `storage backend legacy (in-memory runtime_state)`,
   `dashboard http://localhost:5180/`. HTTP checks: dashboard `200`,
   bundle `index.html` `200`, `app.js` `200`. The server was then
   stopped. A click-through of the Analyze button inside the harness
   iframe was not performed (no interactive browser in this build
   task); the identical dispatcher and plugin path is covered by the
   stdio smoke tests above, and the UI uses the documented
   `anna.tools.invoke` call with the bundled tool_id.
4. Em dash scan over all project files (Python scan of every file,
   excluding caches): no file contains the em dash character.

## What remains for the publish step (not done here, by design)

- Leira registers an Anna account and activates Verified Developer
  (accepting the Developer Terms of Service is her own action), then
  picks a developer handle.
- Mint the production tool_id for the executa and replace
  `tool-leira-job-fit-tailor-s56adxsn` in the plugin manifest, `pyproject.toml`,
  `executa.json`, `manifest.json`, and `bundle/app.js`.
- Exercise the sampling path end to end inside the real harness with a
  logged-in session (PAT) or on the platform, since sampling cannot be
  exercised over a local pipe.
- Add logo, cover, and screenshots in the Developer Console listing,
  submit for review, publish the version, and only then submit the
  DoraHacks Build with the app access token, on Leira's approval.
