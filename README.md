# JobPilot

A local, human-in-the-loop job application pipeline. It sources postings from public ATS
APIs, screens them for F-1 STEM OPT viability, ranks them against a master resume, tailors a
resume PDF, pre-fills the application form in a real browser, and parks everything in a
review queue.

**Nothing is ever submitted without an explicit click in the review UI.**

## Status

| Milestone | Scope | State |
|---|---|---|
| M1 | Sourcing (Greenhouse / Lever / Ashby) + SQLite | ✅ done |
| M2 | Filters, visa screen, LCA import, embedding + LLM rerank | ✅ done |
| M3 | Resume tailoring, anti-fabrication check, Typst rendering | ✅ done |
| M4 | Streamlit review UI | ✅ done |
| M5 | Playwright prefill, stop-before-submit | ✅ built; live acceptance needs your `answers.yaml` (see below) |
| M6 | Approve & submit, daily cap, stats | ✅ built; tested on a local form only |
| M7 | HN "Who's Hiring" + manual-apply drafts | ✅ done (opt-in: `hn.enabled`) |

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
- `config/companies.yaml` — the boards to watch. Ships with eight working placeholders.
- `config/settings.yaml` — models, thresholds, filter keywords, rate limits, daily cap.

## Daily usage

```bash
uv run jobpilot run-daily --top 30   # fetch -> filter -> score -> tailor -> prefill; never submits
uv run jobpilot ui                   # review, edit, approve & submit one at a time
```

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

### How submission works

`prefill` never submits. In `jobpilot ui`, a job's **Approve & Submit** button is disabled
until you tick "I have reviewed…", and it acts on that one job only: it records the approval
(`events.type = approved`, `source = review_ui`) and starts a browser that refills the form
from the reviewed answers, re-verifies every field, clicks submit, and screenshots the
confirmation page. The submitter refuses anything without a UI approval in the audit log, any
job already submitted, and anything past `apply.daily_submission_cap` (UTC day). A CAPTCHA,
validation error, or changed form leaves the browser open and marks the job `needs_human`.

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
