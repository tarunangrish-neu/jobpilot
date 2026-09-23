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

The UI opens on the **Pipeline** page: how many jobs sit at each stage (fetched →
filtered → ranked → tailored → dry-run → prefilled → submitted), what to run next, and a
card per stage (Fetch, Filter, Rank, Tailor, Dry-run forms, Prefill) with its options.
A stage runs in the background; the page shows its live log and LLM progress, and you
can stop it. Only one stage runs at a time, including ones started from a terminal.
**Dry-run forms** reads each live application form and shows, question by question,
what would be filled and from where (answers.yaml, your tailored resume, an LLM draft
to review, or "needs you"), without typing or uploading anything. The **Review queue**
page is where you review, edit, and approve & submit one application at a time.

Without the UI: `uv run jobpilot run-daily --top 30` (never submits), then `uv run jobpilot ui`.

`make` is idempotent, so rerunning it every day is cheap. `make help` lists the individual
targets (e.g. `make daily TOP=20`, `make tailor JOB=412`, `make test`).

Or stage by stage:

```bash
uv run jobpilot fetch [--hn]         # pull every active board (and HN if enabled)
uv run jobpilot filter               # dedupe, title/location/age rules, visa screen
uv run jobpilot score                # embed + LLM rerank; prints the ranked table
uv run jobpilot tailor --top 30      # one-page resume PDFs (+ --cover-letter)
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
6. **Reruns are cheap**: HTTP responses (6 h), embeddings, and LLM answers are cached, so
   re-running a stage only pays for new jobs or changed prompts. `keep_alive: 30m` keeps
   models loaded between stages.

Fetch and prefill are not LLM-bound:

- **Fetch** is bounded by the busiest API host, since requests to one host are ≥1 s apart
  (every Greenhouse board shares `boards-api.greenhouse.io`). All hosts run in parallel and
  HN overlaps the boards; the log prints each board as it lands. A cold fetch of the seed list
  takes ~3 min, almost all of it SmartRecruiters detail pages (one per plausible posting, one
  host). Those are cached for `http.detail_cache_ttl_hours` (72 h), so a daily fetch only
  pays for postings that are new.
- **Prefill / dry run** time is page loads and screenshots, so forms run in parallel tabs
  (`apply.prefill_concurrency`, or "Forms at once" on the card; default 3). LLM drafts still
  take turns on the model. Only one process holds the browser profile at a time: a submit
  approved during a prefill run waits for it instead of failing.

### How submission works

`prefill` never submits. In `jobpilot ui`, a job's **Approve & Submit** button is disabled
until you tick "I have reviewed…", and it acts on that one job only: it records the approval
(`events.type = approved`, `source = review_ui`) and starts a browser that refills the form
from the reviewed answers, re-verifies every field, clicks submit, and screenshots the
confirmation page. The submitter refuses anything without a UI approval in the audit log, any
job already submitted, and anything past `apply.daily_submission_cap` (UTC day). A CAPTCHA,
validation error, or changed form leaves the browser open and marks the job `needs_human`.

The **Ready to submit** page walks the prefilled jobs best-first: review one, tick the box,
approve, and it moves to the next while that one submits in the background. It shows today's
count against the cap and a log line for each submission in flight. There is no approve-all.

### What tailoring will and won't do

`tailor/verify.py` rejects any rephrased bullet that adds a number, technology, or proper noun
not in the original bullet (or pads it with a keyword list); the original is used instead.
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
