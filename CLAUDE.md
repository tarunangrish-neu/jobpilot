# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --extra dev                  # install deps (Python pinned to 3.12 via .python-version)
uv run pytest                        # full suite (no network calls)
uv run pytest tests/test_db.py -q    # one file
uv run pytest -k idempot -q          # one test by name

uv run jobpilot init                 # create DB, config/answers.yaml, data/master_resume.yaml
uv run jobpilot run-daily --top 30   # fetch -> filter -> score -> tailor -> prefill (never submits)
uv run jobpilot ui                   # Streamlit review app (the only path to submission)
uv run jobpilot fetch | filter | score | tailor | prefill | draft-outreach | import-lca FILE
uv run jobpilot discover-token URL   # detect a careers page's ATS and board token
uv run jobpilot mark <job_id> <status>
```

Tests need no Ollama (a `FakeBackend` in conftest stands in), but the tailoring and browser
tests use the real `typst` CLI and Playwright Chromium (`uv run playwright install chromium`).

There is no linter or formatter configured. Don't add one without asking.

## Build order

All seven milestones are built (`README.md` has the table and what is still unverified).
M5's live acceptance — prefilling real postings — has not been run, because Greenhouse and
Ashby upload an attached resume to the company as soon as it is chosen. Don't run a real
prefill without the user's go-ahead; use extract + `plan_fill` for read-only checks.

## Architecture

Postings flow through one pipeline, and each stage advances `jobs.status`:

```
fetch → filter → score → tailor → prefill → [HUMAN APPROVAL] → submit
new     filtered_out  scored  tailored  prefilled   approved    submitted
```

Three things tie the stages together and are worth understanding before editing anything:

**`JobPosting` (models.py) is the seam between sources and the database.** Every ATS returns a
different shape; adapters normalize to this DTO and nothing downstream knows which board a job
came from. Its `content_hash()` deliberately excludes source, IDs, and URLs so the same role
cross-posted to two boards collapses to a single hash for dedupe.

**`db.upsert_posting` is what makes `fetch` re-runnable.** Only the fields in `db._REFRESHABLE`
are overwritten when a known posting reappears. Everything else — `status`, `embed_score`,
`llm_score`, `visa_flag`, `filter_reason` — is pipeline state that must survive a re-fetch, or
a daily run would reset already-tailored jobs to `new`. Add new columns to `_REFRESHABLE` only
if they come from the board rather than from our own processing.

**`http.PoliteClient` is the only way to reach the network.** It owns the per-host ≥1s spacing,
the concurrency semaphore, and the on-disk response cache under `.cache/http/`. Never construct
a bare `httpx` client — the politeness rules are a hard requirement, not a nicety.

Every status change goes through `db.set_status`, which writes an `events` row. Keep it that
way; `events` is the audit log.

How the later stages hand off (each stage only picks up what the previous one finished):

- **filter** considers `status == new AND filtered_at IS NULL`. Passing jobs stay `new` but get
  `filtered_at`; that, not the status, is what `score` selects on.
- **score** embeds every filtered job but only LLM-reranks the top N; the rest stay `new` with an
  `embed_score` and can be picked up later.
- **LLM calls** all go through `llm/client.py` (retry, schema validation, SQLite `llm_cache`,
  `logs/llm.jsonl`). Prompts live in `llm/prompts.py`; **bump a prompt's `version` whenever you
  change its text** — the version is part of the cache key.
- **LLM concurrency** is `llm.concurrency` (default 1), enforced across processes by lock files
  in `.cache/llm_slots/` (`llm/client.LLMSlots`). Local Ollama generates ~one reply at a time;
  don't raise it for Ollama, and never reuse `http.concurrency` for model calls.
- **The UI is the front door.** `jobpilot ui` opens on the Pipeline page, which starts stages via
  `jobpilot/runs.py` (a `runs` row + `python -m jobpilot.runs <id>` worker, log in `logs/runs/`)
  and refuses to start one while any pipeline (UI or terminal) is running. New stages need a
  card in `ui/review_app.STAGE_CARDS` and an entry in `runs.STAGES`; never add a submit stage.
- **Tests use `tests/fixtures/settings.test.yaml`**, not `config/settings.yaml`, so tuning your
  own settings can't change test results.
- **Schema changes** are applied by `db._add_missing_columns` (forward-only `ALTER TABLE ADD
  COLUMN` with the Python default as SQL default). New model fields must have a default.
- **Submission** lives only in `apply/submit.py` and requires an `approved` event with
  `source="review_ui"` (written by `submit.approve`, called from the UI). `jobpilot mark <id>
  approved` does not count. Don't add a CLI or batch path to it.

## ATS quirks that tests pin

These were verified against live responses and each one silently corrupts data if it regresses.
`tests/fixtures/` holds trimmed real payloads captured 2026-09-23 (all six boards).

- **Greenhouse** returns `{"jobs": [...], "meta": {}}`. The `content` field is HTML whose angle
  brackets arrive as `&lt;`/`&gt;` entities — it must be `html.unescape`d *before* parsing or the
  whole description reads as literal markup. `location` is an object `{"name": ...}`, never a
  string. There is no `posted_at`; use `first_published`.
- **Lever** returns a **bare JSON list**, not an envelope. An unknown token 404s with a JSON
  *object*, so adapters must check the status code rather than sniff the payload shape.
  `createdAt` is a millisecond epoch. `applyUrl` and `hostedUrl` are different URLs.
- **Ashby** returns `{"jobs": [...], "apiVersion": ...}`. Postings carry `isListed`; unlisted
  ones are drafts and must be dropped at parse time. `applyUrl` differs from `jobUrl`.
- **Workable** needs `?details=true` or jobs arrive with no description. `published_on` is a
  bare date. Unknown tokens 404.
- **SmartRecruiters** answers an unknown company with **200 and `totalFound: 0`**, so
  `discover.verify` treats an empty board as not found. The list has no descriptions; the
  adapter fetches details only for postings that pass `check_title`/`check_location`.
- **Recruitee** tokens are subdomains. Text is split across `description` and `requirements`
  (both kept — visa language lives in either). `published_at` is `"YYYY-MM-DD HH:MM:SS UTC"`.
- **Large employers** (`sources/workday.py`, `amazon.py`, `eightfold.py`, `oracle.py`, shared
  logic in `sources/large.py`) are searched by keyword (`large_boards.search_terms`), never paged
  in full; details are fetched only for listings that already pass `check_title`/`check_location`,
  capped by `large_boards.max_details`. Tokens pack several parts: Workday `<tenant>.<wdN>/<site>`,
  Eightfold `<host>/<domain>`, Oracle `<host prefix>/<site number>`, Amazon `amazon`. Workday
  `locationsText` is often "3 Locations" (real list only in the detail); `postedOn` is relative,
  so `startDate` is used. Verify goes through each adapter's own `verify()` (Workday is a POST).
  Goldman Sachs (robots `Disallow: /`) and Meta (scraping terms) are deliberately not supported.
- Workable, SmartRecruiters, Recruitee, and the large-employer sites have **no form filler** (`apply.FILLERS`), so their
  jobs never reach `prefilled` and cannot be submitted by the app; the UI offers "I applied
  manually" instead.

`jobs.remote` means "the board says this role is remote", not "remote in the US" — boards mark
roles `Remote - Japan` and `Remote - EU` as remote too. Geography is the location filter's job.

Application forms (probed live 2026-09-23; `tests/fixtures/forms/apply_form.html` mimics them):

- **Greenhouse** `job-boards.greenhouse.io/{token}/jobs/{id}` redirects to the company's own site
  for some boards (Stripe). The embed form `job-boards.greenhouse.io/embed/job_app?for={token}&token={id}`
  always works. Yes/No and EEO questions are react-select comboboxes; resume and cover letter
  are two file inputs both labelled "Attach", distinguished only by id `resume` / `cover_letter`.
- **Lever** marks required fields with ✱ (U+2731), and a `<select>`'s label text includes all
  its options (`clean_label` strips them).
- **Ashby** has an "Autofill from resume" file input next to the real "Resume" one (skip it).
  Checkboxes are each named after their option, so group them by fieldset, not name. A lone
  checkbox can itself be a sensitive question ("authorized without sponsorship?") — an
  unticked box is an answer, so it goes to `needs_human`.
- Greenhouse and Ashby **upload attachments on selection**, before any submit.

## Hard rules

These come from the build spec and are not negotiable:

- **No LinkedIn / Indeed / Glassdoor** scraping or automation. Only public ATS JSON endpoints,
  the HN Algolia API, and pages explicitly added to `config/companies.yaml`.
- **Human-in-the-loop submission.** The submit click happens only after explicit approval in the
  review UI. Never build an auto-submit-all path.
- **No CAPTCHA bypassing.** A CAPTCHA, login wall, or unknown required field means: stop, leave
  the browser open, mark the application `needs_human`.
- **No fabrication.** Tailoring may only select, reorder, and rephrase content already present
  in `data/master_resume.yaml`. It must never invent employers, titles, dates, metrics, degrees,
  or skills. `tailor/verify.py` enforces this and must reject any rephrase introducing a new
  number, proper noun, or technology.
- **Work-authorization, sponsorship, EEO, and salary answers come only from
  `config/answers.yaml`** — never LLM-generated, and they must be truthful.
- **Ask before adding a dependency** outside the stack in the spec, or before adding a job
  source. `pyyaml` is the only addition so far, required to read the spec's own YAML configs.

## Gitignored, never commit

`config/answers.yaml`, `.env`, `data/` (including `jobpilot.db` and LCA files), `output/`,
`browser_profile/`, `.cache/`, `logs/`. `config/answers.example.yaml` is the committed template.
