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
| M2 | Filters, visa screen, LCA import, embedding + LLM rerank | not started |
| M3 | Resume tailoring, anti-fabrication check, Typst rendering | not started |
| M4 | Streamlit review UI (read-only) | not started |
| M5 | Playwright prefill, stop-before-submit | not started |
| M6 | Approve & submit, daily cap, stats | not started |
| M7 | HN "Who's Hiring" + manual-apply drafts | not started |

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

- `config/answers.yaml` — personal details and work-authorization answers. **Gitignored.**
  These are used verbatim; the LLM never generates work-authorization, sponsorship, EEO, or
  salary answers.
- `config/companies.yaml` — the boards to watch. Ships with eight working placeholders.
- `config/settings.yaml` — models, thresholds, filter keywords, rate limits, daily cap.

## Daily usage

```bash
uv run jobpilot fetch                              # pull from every active board
uv run jobpilot discover-token https://x.com/careers   # find a board token to add
uv run jobpilot mark 412 interviewing              # update a job's status by hand
```

`fetch` is idempotent — re-running refreshes existing rows rather than duplicating them.

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
