"""Data access and button actions for the review UI, kept out of the
Streamlit script so they can be tested without a browser.

The one action that leads to a submission is `approve_and_submit`: it records
the approval (source="review_ui") for a single job and starts the submit
worker for that job only.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlmodel import select

from .. import config, db
from ..models import Application, Company, Event, Job, utcnow

QUEUE_STATUSES = ("scored", "tailored", "prefilled", "needs_human", "approved")
RESPONSE_STATUSES = ("rejected", "interviewing", "offer")


@dataclass
class Detail:
    job: Job
    company: Optional[Company]
    app: Optional[Application]
    details: dict[str, Any]
    answers: dict[str, str]
    drafted: dict[str, str]
    tailoring: dict[str, Any]
    events: list[Event]
    dry_run: dict[str, Any] = field(default_factory=dict)


def _loads(text: Optional[str]) -> dict:
    try:
        return json.loads(text or "{}") or {}
    except json.JSONDecodeError:
        return {}


def queue(
    statuses: tuple[str, ...] = QUEUE_STATUSES,
    visa_flags: Optional[list[str]] = None,
    companies: Optional[list[str]] = None,
    min_score: float = 0.0,
    sources: Optional[list[str]] = None,
    exclude_sources: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    with db.session() as sess:
        query = (
            select(Job, Company)
            .join(Company, Job.company_id == Company.id, isouter=True)
            .where(Job.status.in_(statuses))
            .order_by(Job.final_score.desc().nulls_last(), Job.id)
        )
        rows = []
        for job, company in sess.exec(query).all():
            name = company.name if company else ""
            if visa_flags and job.visa_flag not in visa_flags:
                continue
            if companies and name not in companies:
                continue
            if sources and job.source not in sources:
                continue
            if job.source in exclude_sources:
                continue
            if (job.final_score or 0.0) < min_score:
                continue
            rows.append(
                {
                    "rank": len(rows) + 1,
                    "id": job.id,
                    "company": name,
                    "title": job.title,
                    "score": job.final_score,
                    "visa": job.visa_flag,
                    "lca": company.lca_filings_count if company and company.has_lca_history else 0,
                    "status": job.status,
                    "location": job.location,
                    "source": job.source,
                }
            )
        return rows


def detail(job_id: int) -> Optional[Detail]:
    with db.session() as sess:
        job = sess.get(Job, job_id)
        if job is None:
            return None
        company = sess.get(Company, job.company_id) if job.company_id else None
        app = sess.exec(select(Application).where(Application.job_id == job_id)).first()
        events = list(sess.exec(select(Event).where(Event.job_id == job_id).order_by(Event.id)).all())
        for obj in (job, company, app, *events):
            if obj is not None:
                sess.expunge(obj)
    return Detail(
        job=job,
        company=company,
        app=app,
        details=_loads(job.llm_details_json),
        answers=_loads(app.answers_json) if app else {},
        drafted=_loads(app.llm_drafted_answers_json) if app else {},
        tailoring=_loads(app.tailoring_json) if app else {},
        events=events,
        dry_run=_loads(app.dry_run_json) if app else {},
    )


def skip(job_id: int) -> None:
    with db.session() as sess:
        db.set_status(sess, sess.get(Job, job_id), "skipped", reason="skipped in review UI")
        sess.commit()


def save_edits(job_id: int, edits: dict[str, str]) -> None:
    """Store edited answers (e.g. reworded LLM drafts) without approving."""
    with db.session() as sess:
        app = sess.exec(select(Application).where(Application.job_id == job_id)).one()
        answers, drafted = _loads(app.answers_json), _loads(app.llm_drafted_answers_json)
        for label, value in edits.items():
            answers[label] = value
            if label in drafted:
                drafted[label] = value
        app.answers_json, app.llm_drafted_answers_json = json.dumps(answers), json.dumps(drafted)
        sess.add(app)
        db.log_event(sess, job_id, "answers_edited", labels=sorted(edits))
        sess.commit()


def approve_and_submit(job_id: int, edits: Optional[dict[str, str]] = None):
    """The human clicked Approve & Submit for this one job."""
    from ..apply import submit

    with db.session() as sess:
        job = sess.get(Job, job_id)
        submit.approve(sess, job, edits)
        refusal = submit.can_submit(sess, job, submit._app(sess, job_id))
    if refusal:
        raise submit.SubmitRefused(refusal)
    return submit.launch_submit(job_id)


def regenerate(job_id: int) -> str:
    """Re-run tailoring for one job, ignoring cached LLM plans."""
    from .. import master_resume
    from ..llm import LLMClient
    from ..tailor import run_tailoring

    outcomes = asyncio.run(
        run_tailoring(LLMClient(), master_resume.load(), job_ids=[job_id], regenerate=True)
    )
    if not outcomes:
        return "job is not in a tailorable state"
    outcome = outcomes[0]
    return outcome.error or f"regenerated: {len(outcome.report.bullets)} bullets, {outcome.report.pages} page(s)"


def draft_outreach(job_id: int) -> str:
    from .. import master_resume
    from ..apply.manual import draft_outreach as run
    from ..llm import LLMClient

    text = asyncio.run(run(LLMClient(), master_resume.load(), job_id, use_cache=False))
    return "Drafted." if text else "Every drafted sentence failed verification; write this one yourself."


def mark_applied_manually(job_id: int) -> None:
    """For jobs the human applied to outside JobPilot (HN, needs_human)."""
    with db.session() as sess:
        job = sess.get(Job, job_id)
        app = sess.exec(select(Application).where(Application.job_id == job_id)).first()
        if app is None:
            app = Application(job_id=job_id)
        app.submitted_at = app.submitted_at or utcnow()
        sess.add(app)
        db.log_event(sess, job_id, "applied_manually", source="review_ui")
        db.set_status(sess, job, "submitted", reason="applied manually")
        sess.commit()


def resume_preview(job_id: int) -> Optional[Path]:
    """PNG of the tailored resume (Streamlit can't embed PDFs reliably)."""
    from ..tailor import output_dir
    from ..tailor.render import RenderError, _run, _typst

    out = output_dir(job_id)
    data = out / "resume.json"
    png = out / "resume.png"
    if not data.exists():
        return None
    if png.exists() and png.stat().st_mtime >= data.stat().st_mtime:
        return png
    root = config.project_root()
    try:
        _run([
            _typst(), "compile", "--root", str(root),
            "--input", f"data=/{data.resolve().relative_to(root.resolve()).as_posix()}",
            str(root / "templates" / "resume.typ"), str(png), "--ppi", "110",
        ])
    except RenderError:
        return None
    return png if png.exists() else None


def stats() -> dict[str, Any]:
    with db.session() as sess:
        apps = sess.exec(select(Application).where(Application.submitted_at.is_not(None))).all()
        per_day = Counter(a.submitted_at.date().isoformat() for a in apps)
        by_status = Counter(sess.exec(select(Job.status)).all())
    return {
        "per_day": dict(sorted(per_day.items())),
        "by_status": dict(by_status.most_common()),
        "submitted": len(apps),
        "responses": {s: by_status.get(s, 0) for s in RESPONSE_STATUSES},
    }


# --- pipeline overview (UI-first landing page) ---------------------------------

FUNNEL = (
    ("fetched", "All jobs fetched"),
    ("awaiting_filter", "Waiting for filter"),
    ("filtered_out", "Dropped by filters"),
    ("awaiting_score", "Passed filters, not ranked yet"),
    ("scored", "Ranked"),
    ("tailored", "Resume tailored"),
    ("dry_run", "Form dry-run done"),
    ("prefilled", "Prefilled, ready to review"),
    ("needs_human", "Needs you"),
    ("approved", "Approved"),
    ("submitted", "Submitted"),
)


def funnel() -> dict[str, Any]:
    from sqlmodel import func

    with db.session() as sess:
        by_status = Counter(dict(sess.exec(select(Job.status, func.count()).group_by(Job.status)).all()))
        awaiting_filter = sess.exec(
            select(func.count()).select_from(Job).where(Job.status == "new", Job.filtered_at.is_(None))
        ).one()
        awaiting_score = sess.exec(
            select(func.count()).select_from(Job).where(
                Job.status == "new", Job.filtered_at.is_not(None), Job.visa_flag != "blocked"
            )
        ).one()
        dry_run = sess.exec(
            select(func.count()).select_from(Application).where(Application.dry_run_json != "{}")
        ).one()
        by_source = sess.exec(select(Job.source, Job.status, func.count()).group_by(Job.source, Job.status)).all()
    counts = {
        "fetched": sum(by_status.values()),
        "awaiting_filter": awaiting_filter,
        "awaiting_score": awaiting_score,
        "dry_run": dry_run,
        **{s: by_status.get(s, 0) for s in ("filtered_out", "scored", "tailored", "prefilled",
                                             "needs_human", "approved", "submitted", "skipped")},
        **{s: by_status.get(s, 0) for s in RESPONSE_STATUSES},
    }
    table: dict[str, dict[str, int]] = {}
    for source, status, n in by_source:
        table.setdefault(source, {})[status] = n
    return {"counts": counts, "by_source": table}


def next_steps(counts: dict[str, int]) -> list[tuple[str, str]]:
    """(stage, why) suggestions, in pipeline order."""
    steps = []
    if counts["fetched"] == 0:
        steps.append(("fetch", "No jobs yet: fetch your boards."))
    if counts["awaiting_filter"]:
        steps.append(("filter", f"{counts['awaiting_filter']} jobs are waiting for the filter."))
    if counts["awaiting_score"]:
        steps.append(("score", f"{counts['awaiting_score']} jobs passed the filters but aren't ranked."))
    if counts["scored"]:
        steps.append(("tailor", f"{counts['scored']} ranked jobs have no tailored resume yet."))
    if counts["tailored"] > counts["dry_run"]:
        steps.append(("dry-run", "Preview how the tailored jobs' forms would be filled."))
    if counts["prefilled"]:
        steps.append(("review", f"{counts['prefilled']} prefilled applications are waiting for your review."))
    return steps


def readiness() -> list[tuple[str, str]]:
    """(level, message) checks shown on the Pipeline page. level: error | warning | ok."""
    import shutil
    import subprocess

    import yaml

    root = config.project_root()
    out: list[tuple[str, str]] = []
    resume, example = root / "data" / "master_resume.yaml", root / "config" / "master_resume.example.yaml"
    if not resume.exists():
        out.append(("error", "data/master_resume.yaml is missing (run `jobpilot init`)."))
    elif example.exists() and resume.read_bytes() == example.read_bytes():
        out.append(("error", "data/master_resume.yaml is still the fictional example; replace it with your resume."))

    answers_path = root / "config" / "answers.yaml"
    try:
        answers = yaml.safe_load(answers_path.read_text(encoding="utf-8")) or {}
    except OSError:
        answers = {}
        out.append(("error", "config/answers.yaml is missing (run `jobpilot init`)."))
    wanted = {
        "contact": ("first_name", "last_name", "email", "phone", "linkedin"),
        "work_authorization": ("authorized_to_work_in_us", "require_sponsorship_now_or_future"),
    }
    blank = [f"{sec}.{k}" for sec, keys in wanted.items() for k in keys if not (answers.get(sec) or {}).get(k)]
    if answers and blank:
        out.append(("warning", "answers.yaml is blank for " + ", ".join(blank)
                    + " -- forms asking for these will be flagged 'needs you' instead of filled."))

    llm_cfg = config.settings().get("llm", {})
    if llm_cfg.get("provider", "ollama") == "ollama":
        try:
            listing = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
            have = {line.split()[0] for line in listing.stdout.splitlines()[1:] if line.strip()}
            names = have | {n.removesuffix(":latest") for n in have}
            needed = {llm_cfg.get("text_model"), llm_cfg.get("embed_model"), *(llm_cfg.get("task_models") or {}).values()}
            missing = sorted(m for m in needed if m and m not in names)
            if listing.returncode != 0:
                out.append(("error", "Ollama isn't running (`brew services start ollama` or `make ollama-up`)."))
            elif missing:
                out.append(("error", "Ollama models not pulled: " + ", ".join(missing) + " (`make models`)."))
        except (OSError, subprocess.SubprocessError):
            out.append(("error", "Ollama isn't installed or didn't answer (`brew install ollama`)."))
    if not shutil.which("typst"):
        out.append(("error", "typst isn't installed, so resumes can't be rendered (`brew install typst`)."))
    if not out:
        out.append(("ok", "Resume, answers, models, and typst all look ready."))
    return out


def llm_stats(hours: int = 24) -> list[dict[str, Any]]:
    """Per-prompt call counts and time split (waiting vs model) over the last `hours`."""
    from datetime import datetime, timedelta, timezone

    path = config.project_root() / config.settings().get("llm", {}).get("log_path", "logs/llm.jsonl")
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows: dict[str, dict[str, list]] = {}
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    ts = datetime.fromisoformat(rec["ts"])
                except (ValueError, KeyError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < since:
                    continue
                r = rows.setdefault(rec.get("prompt", "?"), {"total": [], "model": [], "queued": [], "errors": []})
                if "error" in rec:
                    r["errors"].append(1)
                    continue
                r["total"].append(float(rec.get("seconds") or 0))
                if "model_s" in rec:
                    r["model"].append(float(rec["model_s"]))
                    r["queued"].append(float(rec.get("queued_s") or 0))
    except OSError:
        return []

    def median(xs):
        xs = sorted(xs)
        return round(xs[len(xs) // 2], 1) if xs else None

    return [
        {"prompt": name, "calls": len(r["total"]), "failed": len(r["errors"]),
         "median total s": median(r["total"]), "median model s": median(r["model"]),
         "median waiting s": median(r["queued"]), "model minutes": round(sum(r["model"] or r["total"]) / 60, 1)}
        for name, r in sorted(rows.items(), key=lambda kv: -len(kv[1]["total"]))
    ]
