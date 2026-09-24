# JobPilot

A local, human-in-the-loop job application pipeline. It sources postings from public ATS
APIs, screens them for F-1 STEM OPT viability, ranks them against a master resume, tailors a
resume PDF, pre-fills the application form in a real browser, and parks everything in a
review queue.

**Nothing is ever submitted without an explicit click in the review UI.**

## Status

| Milestone | Scope | State |
|---|---|---|
| M1 | Sourcing (Greenhouse / Lever / Ashby; later Workable / SmartRecruiters / Recruitee) + SQLite | ✅ done |
| M2 | Filters, visa screen, LCA import, embedding + LLM rerank | ✅ done |
| M3 | Resume tailoring, anti-fabrication check, Typst rendering | ✅ done |
| M4 | Streamlit review UI | ✅ done |
| M5 | Playwright prefill, stop-before-submit | ✅ built; live acceptance needs your `answers.yaml` (see below) |
| M6 | Approve & submit, daily cap, stats | ✅ built; tested on a local form only |
| M7 | HN "Who's Hiring" + manual-apply drafts | ✅ done (`hn.enabled`, on by default) |

## Setup

```bash
uv sync --extra dev             # Python deps (pins 3.12)
uv run playwright install chromium   # needed from M5 onward

brew install typst ollama       # PDF rendering (M3) and local LLM (M2+)
brew services start ollama
ollama pull qwen2.5:7b-instruct
ollama pull nomic-embed-text

uv run jobpilot init            # create the DB and config/answers.yaml
```

Then fill in:

- `data/master_resume.yaml` — **every** resume fact, as bullets with ids. `init` copies a
  fictional example ("Alex Example"); replace it all. Tailoring can only select, reorder, and
  rephrase what is here. **Gitignored.**
- `config/answers.yaml` — personal details and work-authorization answers. **Gitignored.**
  These are used verbatim; the LLM never generates work-authorization, sponsorship, EEO, or
  salary answers. Anything left empty is never guessed: the application becomes `needs_human`.
  Add recurring questions to `common_questions` (`a: null` = LLM drafts it for review).
- `config/companies.yaml` — the boards to watch. Ships with ~60 verified fintech, crypto, AI-infra,
  and dev-infra boards; edit freely.
- `config/settings.yaml` — models, thresholds, filter keywords, rate limits, daily cap.

## Daily usage

```bash
make                                 # set up anything missing, then open the UI
```

The UI opens on the **Jobs** page. No filter or rank stage, and no LLM until you tailor:

1. **Fetch openings** pulls every board in `companies.yaml` in parallel (at least 1 s between
   requests to the same site; a cold fetch of ~280 boards takes ~5 minutes, about a minute
   within the 6 h cache window), then **screens** every job with your fetch filters.
2. **⚙️ Fetch filters** (under the Fetch button) edits those filters and re-screens every
   fetched job in seconds, with no network or LLM: title contains / doesn't contain,
   allowed locations, remote-anywhere, posted within N days, max years of experience the
   description asks for, companies to skip, description phrases to skip (e.g. "security
   clearance"), and sponsorship blockers. They are saved to `config/filters.yaml`
   (gitignored), which overrides `filters:` in settings.yaml. Screened-out jobs are only
   hidden, never deleted, so loosening a filter brings them back; jobs you tailored,
   applied to, or skipped are never screened. `jobpilot screen` does the same from a terminal.
3. **Openings** is one table of what passed, newest first, cross-posts collapsed, with an
   **Apply ↗** link per row. It narrows instantly by search, posted within, role type
   (Backend, Full-stack, Infra/Platform/SRE, ML/AI, Data, ...), level (Entry, Mid, Senior,
   Staff+, Lead, Manager), company, years of experience asked, sponsorship blockers,
   "not tailored yet", and can show screened-out jobs with the reason.
4. **Tick rows** and click **✨ Tailor resume + cover letter** (the bar above the table):
   one background run, ~45 s per job on local Ollama, live log at the top of the page.
5. **Ready to apply** (sidebar, with a count) lists every tailored job: **Apply on company
   site ↗**, resume and cover-letter PDF downloads, a resume preview, the letter as copyable
   text, **Write cover letter** (or all missing ones at once) for jobs tailored without one,
   **Re-tailor from scratch**, and **✅ I applied** / **Skip**.

Only one run (fetch or tailor) goes at a time, including ones started from a terminal. The
old flow — Pipeline (filter, rank, dry-run and prefill cards), Review queue (approve &
submit), Manual apply — is behind **Show the old auto-fill pipeline** in the sidebar.

Without the UI: `uv run jobpilot run-daily --top 30` (never submits), then `uv run jobpilot ui`.

`make` is idempotent, so rerunning it every day is cheap. `make help` lists the individual
targets (e.g. `make daily TOP=20`, `make tailor JOB=412`, `make test`).
`make ui-start` runs the UI in the background (log in `logs/ui.log`), `make ui-restart`
reloads it after a code change, `make ui-stop` stops it; `PORT=` picks another port.

Or stage by stage:

```bash
uv run jobpilot fetch [--hn]         # pull every active board (and HN if enabled), then screen
uv run jobpilot screen               # re-apply your filters to fetched jobs (no network, no LLM)
uv run jobpilot filter               # dedupe, title/location/age rules, visa screen
uv run jobpilot score                # embed + LLM rerank; prints the ranked table
uv run jobpilot tailor --top 30      # tailored resume PDFs, master length (+ --cover-letter)
uv run jobpilot prefill --top 30     # fill forms in Chromium, screenshot, STOP
uv run jobpilot draft-outreach       # HN / manual-apply messages
uv run jobpilot import-lca FY2025_Q4.csv   # DOL LCA disclosure data (CSV export of the xlsx)
uv run jobpilot discover-token https://x.com/careers
uv run jobpilot mark 412 interviewing      # rejected / interviewing / offer, by hand
```

`fetch` is idempotent — re-running refreshes existing rows rather than duplicating them.
Every stage only picks up jobs the previous stage finished, so re-running any of them is safe.

### Speeding it up

Almost all the time goes to local LLM calls. The Pipeline page's "Where the time goes"
panel splits each prompt's time into *waiting* (queued for a model slot) and *model*
(generation). On an M-series Mac, qwen2.5-7b takes ~10 s per call, and one daily run
makes ~150-250 calls. Levers, biggest first:

1. **Don't run two pipelines at once.** They share one local model and just take turns;
   the UI refuses to start a stage while another is running.
2. **Keep `llm.concurrency: 1` for local Ollama.** Ollama generates about one reply at a
   time; more in-flight requests only queue (and used to time out and retry). Raise it to
   4-8 only for a hosted provider.
3. **Make fewer calls.** Rerank fewer jobs (Rank card "top N", default 30 in the UI) and
   tailor fewer (top 10-15). Leave HN off unless you want it (~1 call per comment).
   Tighten `filters.title_include` / `title_exclude`: every job that passes the filters
   costs an embedding, and the top N cost a rerank each.
4. **Use a small model for the easy tasks.** `ollama pull qwen2.5:3b-instruct`, then
   `llm.task_models: {visa_check: "qwen2.5:3b-instruct", hn_extract: "qwen2.5:3b-instruct"}`
   is 2-3x faster for those; keep the 7B for ranking and tailoring.
5. **Or use a hosted model** (`llm.provider: openai_compatible`, e.g. Groq): 10x+ faster,
   but job descriptions and your resume leave your machine.
6. **Output tokens are the cost.** qwen2.5-7b writes ~17 tokens/s on an M5; reading the
   ~3k-token prompt takes ~4 s, or ~0 s when the previous call shared its prefix (prompts put
   the fixed part — instructions, example, resume — first so Ollama reuses its cache). That
   is why tailoring asks for ids, not text, and `tailor.max_rephrasings` exists.
7. **Reruns are cheap**: HTTP responses (6 h), embeddings, and LLM answers are cached, so
   re-running a stage only pays for new jobs or changed prompts. `keep_alive: 30m` keeps
   models loaded between stages.

### How submission works

`prefill` never submits. In `jobpilot ui`, a job's **Approve & Submit** button is disabled
until you tick "I have reviewed…", and it acts on that one job only: it records the approval
(`events.type = approved`, `source = review_ui`) and starts a browser that refills the form
from the reviewed answers, re-verifies every field, clicks submit, and screenshots the
confirmation page. The submitter refuses anything without a UI approval in the audit log, any
job already submitted, and anything past `apply.daily_submission_cap` (UTC day). A CAPTCHA,
validation error, or changed form leaves the browser open and marks the job `needs_human`.

### What tailoring will and won't do

A tailored resume is your master resume, same length, re-ordered and sharpened for one job:
the model ranks every bullet against the job's requirements (the most relevant lead each
role) and can rewrite up to `tailor.max_rephrasings` of the top ones in the XYZ pattern
recruiters recommend — action verb, result/metric, then how — using the job's wording only
for things the bullet already says. A rewrite that drops a number, technology, or ~20% of
the text is rejected like a fabrication. With qwen2.5:7b and long bullets nearly every
rewrite fails that check (it condenses), so the shipped setting is `max_rephrasings: 0`:
ranking only, ~9 s per job instead of ~26 s. Skills are ordered in code (the ones the
posting names first); none are dropped. Bullets are cut, lowest-ranked first, only if the result runs longer than the full
master resume renders (`tailor.max_pages` / `tailor.max_bullets` shorten it on purpose).
The model's reply is schema-constrained to real bullet ids, so it cannot select something
that isn't in your resume.

Local models are not fine-tuned here. Ollama has no training step; the prompt carries the
rubric plus one worked example on a fictional resume (`llm/prompts.py`), which is the
practical equivalent and costs nothing per job once cached.

`tailor/verify.py` rejects any rephrased bullet that adds a number, technology, or proper noun
not in the original bullet, borrows two or more of the posting's words that appear nowhere in
your resume, or pads it with a keyword list; the original is used instead.
Cover letters, drafted answers, and outreach messages are checked sentence by sentence; a
sentence that states a new fact, or describes you in the job posting's words rather than your
resume's, is dropped. With `qwen2.5:7b` most cover letters don't survive that and none is
produced — by design. Rejections are shown in the UI and stored in `applications.tailoring_json`.

### Not yet done

- **M5 live acceptance** ("prefill 3 real postings per ATS") has not been run. Prefill was
  tested headless against a local form and dry-run (extract + plan, nothing typed or
  uploaded) against real Greenhouse, Lever, and Ashby forms. Greenhouse and Ashby upload
  attachments the moment a file is chosen, so a real prefill sends your resume to the company.
  Run it yourself once `answers.yaml` and `master_resume.yaml` are real:
  `uv run jobpilot prefill --job <id>`.
- **LCA import reads CSV only.** Reading the DOL `.xlsx` directly needs `openpyxl`, which is
  outside the spec's stack. Export the sheet to CSV.

## Adding a company

```bash
uv run jobpilot discover-token https://www.ramp.com/careers
# ats    token  open roles
# ashby  ramp   150
```

Paste the printed block into `config/companies.yaml`. Detection reads the careers page for a
board URL and falls back to guessing tokens from the domain; every candidate is confirmed
with a real API call, so a reported token always works.

Six boards are supported: Greenhouse, Lever, Ashby, Workable, SmartRecruiters, and Recruitee.
Only the first three have form fillers. Workable, SmartRecruiters, and Recruitee jobs are
fetched, filtered, scored, and tailored like any other, but never prefilled: open the posting
from the review UI, apply by hand, then click **I applied manually**. SmartRecruiters' list
endpoint has no descriptions, so `fetch` requests details only for postings whose title and
location already pass your filters.

The first `fetch` with `hn.enabled` runs one local-LLM extraction per HN comment (up to
`hn.max_comments`, roughly 15 s each on a 7B model); later runs hit the cache.

## Tests

```bash
uv run pytest                      # whole suite
uv run pytest tests/test_sources.py -q
uv run pytest -k idempot -q        # a single test by name
```

Parser tests run against trimmed real API responses in `tests/fixtures/`, captured
2026-09-23. No test makes a network call.

## Rules this project holds to

- No LinkedIn, Indeed, or Glassdoor scraping or automation.
- No CAPTCHA bypassing — a CAPTCHA or login wall marks the application `needs_human`.
- No fabrication: tailoring may only select, reorder, and rephrase content that already
  exists in `data/master_resume.yaml`.
- ≥1 second between requests to the same host, responses cached, descriptive User-Agent.
- Personal data and secrets stay in `.env` and gitignored `config/` files.
