# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync --extra dev                  # install deps (Python pinned to 3.12 via .python-version)
uv run pytest                        # full suite (no network calls)
uv run pytest tests/test_db.py -q    # one file
uv run pytest -k idempot -q          # one test by name

uv run jobpilot init                 # create DB + config/answers.yaml
uv run jobpilot fetch                # pull all active boards
uv run jobpilot discover-token URL   # detect a careers page's ATS and board token
uv run jobpilot mark <job_id> <status>
```

There is no linter or formatter configured. Don't add one without asking.

## Build order

The project is built milestone by milestone, stopping for review after each. **M1 (sourcing +
SQLite) is complete; M2–M7 are not started.** `README.md` has the milestone table. Do not
scaffold a later milestone's package until that milestone is being worked on — `filters/`,
`scoring/`, `tailor/`, `apply/`, `ui/`, and `llm/` deliberately do not exist yet.

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

## ATS quirks that tests pin

These were verified against live responses and each one silently corrupts data if it regresses.
`tests/fixtures/` holds trimmed real payloads captured 2026-09-23.

- **Greenhouse** returns `{"jobs": [...], "meta": {}}`. The `content` field is HTML whose angle
  brackets arrive as `&lt;`/`&gt;` entities — it must be `html.unescape`d *before* parsing or the
  whole description reads as literal markup. `location` is an object `{"name": ...}`, never a
  string. There is no `posted_at`; use `first_published`.
- **Lever** returns a **bare JSON list**, not an envelope. An unknown token 404s with a JSON
  *object*, so adapters must check the status code rather than sniff the payload shape.
  `createdAt` is a millisecond epoch. `applyUrl` and `hostedUrl` are different URLs.
- **Ashby** returns `{"jobs": [...], "apiVersion": ...}`. Postings carry `isListed`; unlisted
  ones are drafts and must be dropped at parse time. `applyUrl` differs from `jobUrl`.

`jobs.remote` means "the board says this role is remote", not "remote in the US" — boards mark
roles `Remote - Japan` and `Remote - EU` as remote too. Geography is the location filter's job.

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
