# Job Fit & Tailor

An Anna App that scores a job seeker's CV against ONE job ad and returns an
honest, evidence-based application package in a single run: a fit grade and
score, matched evidence quoted from the CV, an honest gap list, a tailored
CV with an auditable change log, a cover note, five likely interview
questions, and ATS keywords split into present and missing.

Slug: `job-fit-tailor`. Current version: **0.2.0, local build only.** The
Anna server still holds the v0.1.0 DRAFT (app @leira/job-fit-tailor, app_id
837); v0.2.0 has NOT been republished, submitted for review, or released.
Publishing remains the main agent's step, on the owner's approval. Built
with the `anna-app` CLI (minimal template), following the official Anna
developer docs (concepts, Python Executa quickstart, protocol lifecycle
and v2 negotiation, sampling, display blocks, app quickstart, app
manifest, UI manifest, listing fields, local dev).

## What changed in v2: radically simpler inputs

v1 forced the user to paste the full CV text and the full job ad text.
v2 asks for two things only:

- **The CV as a file.** Upload a PDF, DOCX, TXT, or MD file (drag and
  drop or file picker) and the tool extracts the text itself. Pasting
  the CV remains as a fallback. A "Remember my CV in this app" option
  (on by default) stores the extracted CV text in app storage and
  preselects it on the next visit, with a Remove button.
- **The job as a link.** Paste one job ad URL and the tool fetches the
  page and reduces it to the ad text with a small deterministic
  readability pass. Pasting the ad text remains as a fallback.

Because extraction can be imperfect, the app never analyzes text the
user cannot see: after every run it shows two "details extracted" cards
(CV details and job details) built from the exact texts the tool used
(`cv_text_used`, `job_text_used`). Each card has an "Edit the exact text
used" expander and a Re-run button, so the user can correct the text
and re-analyze. A short link extraction (600 to 1500 characters) is
flagged as a partial read with a warning; anything shorter, or a failed
fetch, returns `job_fetch_failed` and nothing is analyzed.

## How it works

- `executas/job-fit-tailor/` is a Python Executa plugin speaking JSON-RPC
  2.0 over stdio (`describe`, `initialize`, `health`, `invoke`). It
  exposes one tool, `analyze_job_fit`. Inputs: `cv_text` or
  `cv_file_base64` plus `cv_filename` (PDF via pypdf, DOCX via
  python-docx, TXT/MD as UTF-8; decoded size capped at 3 MB; other types
  rejected with a clear error), an optional `cv_source` hint (file,
  paste, saved), and `job_text` or `job_url`. Pasted text takes
  precedence over the file and over the link. `scenario` and `language`
  are optional: when omitted, the tool picks the scenario from ad
  keywords (internship or student wording, startup wording, otherwise
  corporate) and detects Indonesian versus English from the texts.
- Job URL fetching: HTTP GET with a browser User-Agent, 15 second
  timeout, redirects followed. The readability pass strips script,
  style, and page chrome (nav, header, footer, aside, form), prefers
  article or main content, collapses whitespace, and caps the result at
  30,000 characters. Fetched text under 600 characters (or any fetch
  failure) returns `success: false` with error code `job_fetch_failed`,
  a message telling the user to paste the ad text, and whatever partial
  text was extracted as `partial_job_text`. Job text of 600 to 1500
  characters proceeds with `job_meta.partial: true` and a warning.
- The result keeps every v1 field with the identical shape, and adds
  `cv_text_used`, `job_text_used`, `cv_meta` (name, title, skills,
  education guesses, character count, source), `job_meta` (title,
  company, location guesses, top requirements, character count, source,
  partial flag, warning), and a `warnings` list. All guesses are
  regex and heuristic extractions from the input text only; nothing is
  invented.
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
- Main loop contract: the plugin loops until stdin EOF and answers
  every request in order. JSON-RPC notifications (frames without an
  id) are never answered; see the test log for the v1 bug this fixes.
- `bundle/` is a one-page, mobile-first UI in the v1 design language:
  a CV block (drag-drop file area, collapsed paste expander, remember
  checkbox, saved-CV banner), a job block (one URL field, collapsed
  paste expander), a small Adjust row (scenario and language, both
  defaulting to Auto), rotating step text while a run is in flight,
  the two extracted-details cards with edit and re-run, the v1 result
  package, and an "Analyze another job with this CV" button that keeps
  the CV and clears only the job side. It calls the tool through
  `anna.tools.invoke` with the bundled `tool_id`, and every storage
  call is guarded so a plain static preview still renders.
- `manifest.json` adds the `system_prompt_addendum` (ask for a CV file
  or pasted CV and one job link or pasted ad, show the extracted
  details, offer the edit-and-re-run check, present the grade card
  first, offer one revision pass) and declares desktop and mobile form
  factors. `app.json` holds the store listing: name, tagline, category
  `productivity` (the listing doc has no Business category), and a long
  description in English with an Indonesian summary paragraph. Listing
  media (logo, cover, screenshots) lives in `listing-assets/` and is
  managed outside this build.

## Honesty rules (baked into prompts, fallback, and listing)

- Never invent or embellish experience, skills, employers, dates,
  metrics, or qualifications that are not in the user's CV text.
- Anything the ad requires that the CV does not support goes to gaps,
  never into the tailored CV.
- Every substantive change to the CV is listed in the change log.
- No hidden keyword stuffing, no ATS trickery advice.
- No em dash character in any output or project file.
- Analysis runs only on text the user can see and correct.
- CV text stays inside the run; the only external calls are host
  sampling and, when the user provides a link, fetching that one job
  ad page.

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

## Test log (8 Nov 2026, this VM, v0.2.0)

Environment: Node v24.22.1, Python 3.12.3, `uv` 0.13.7, `anna-app` CLI
0.1.64. Runtime dependencies added in v2: pypdf 7.0.0 and python-docx
1.2.0 (pyproject dependencies, locked in uv.lock). Sampling is not
exercisable over a plain pipe (no host injects a `sampling_token`), so
all invokes below ran the deterministic fallback, exactly as designed;
stderr showed `sampling unavailable, using fallback: no sampling_token
in invoke context` per invoke.

1. Protocol smoke plus sequential lifecycle test, one plugin process,
   frames in this order: initialize (v2 negotiated, server version
   0.2.0), a notification `initialized` (no id), describe (version
   0.2.0, tools `["analyze_job_fit"]`, parameters now include
   cv_file_base64, cv_filename, cv_source), health (`{"status":
   "ready"}`), a notification `$/cancelRequest`, invoke sample A,
   invoke sample B, a `shutdown` request, an `exit` notification, then
   stdin EOF. Result: exactly 6 response frames, ids in order
   [0, 1, 2, inv-A, inv-B, 3], ZERO frames with id null, process alive
   through both invokes and exited 0 at EOF.
   - The v1 bug this guards: v1 answered the `initialized`
     notification with a spurious `{"id": null, "error": "unknown
     method: initialized"}` frame, shifting response pairing by one
     for any bridge that reads frames in order; that matches the
     harness symptom where the plugin ran and chose the fallback but
     the bundle UI reported "tool returned no result". v2 never
     answers notifications, answers `shutdown` with an empty result,
     and stops on the `exit` notification.
2. v1 regression (pasted texts, tests/samples.json): sample A still
   grade D, score 48 (cv_meta: name "Rani Prasetyo", title "Junior
   Data Analyst"; job_meta company guess "NorthPeak Consulting",
   7 requirements). Sample B unchanged: grade F, score 7 (job_meta
   company guess "PT Sinar Data"). All v1 result fields identical in
   shape.
3. CV file path: a DOCX and a PDF generated from sample A's CV text
   (python-docx and fpdf2 as dev-only generators, not runtime deps)
   and invoked with cv_file_base64 plus the pasted job text. DOCX:
   grade D, score 48, extracted 664 characters (identical to the
   pasted text). PDF: grade D, score 48, 668 characters. TXT file:
   grade D, score 48, source file. A `.png` upload is rejected with
   `success: false`, error code `cv_file_failed`, and a message
   listing the supported types.
4. Job URL path: sample A's ad wrapped in a realistic page (header,
   nav, aside, footer, promo block, article with about and benefits
   filler), served from a local HTTP server on an ephemeral port.
   Invoked with the pasted CV and only job_url: grade D, score 48,
   scenario and language auto-detected (corporate, en),
   job_meta.source `url`, 1054 characters extracted, partial flag
   true with the partial-read warning (the fixture ad is short), and
   job_meta.requirements containing both the SQL line ("Strong SQL
   for querying large datasets") and the Tableau line ("Experience
   building dashboards in Tableau"). A tiny page (21 characters of
   text) returned `success: false`, error code `job_fetch_failed`,
   and `partial_job_text` "Short ad. Apply now.". A 404 URL also
   returned `job_fetch_failed`.
5. `anna-app validate` and `anna-app validate --strict`:
   `[validate] @anna-ai/app-schema dispatcher_version=0.33.0`, then
   `validate passed`, exit 0, for both.
6. `anna-app dev --no-llm` booted on a spare port (5180 was already
   held by another harness): bridge ready, executa
   tool-leira-job-fit-tailor-s56adxsn loaded, dashboard HTTP 200,
   bundle index.html, app.js, and style.css all HTTP 200 under
   /anna-apps/job-fit-tailor/dev/, and the served HTML contains the
   v2 elements (cv-drop, cv-remember, job-edit). Server then stopped.
7. Em dash scan over all project files (excluding .venv and .git):
   zero hits.

## What remains for the publish step (not done here, by design)

- Republish v0.2.0 (push plus cut) so the server DRAFT moves from
  v0.1.0 to v0.2.0, then update screenshots in the listing, submit
  for review, and only after admin approval release and submit the
  DoraHacks Build with the app access token, all on the owner's
  approval. This build touched no server state.
- Exercise the sampling path end to end inside the real harness with a
  logged-in session (PAT) or on the platform, since sampling cannot be
  exercised over a local pipe.
