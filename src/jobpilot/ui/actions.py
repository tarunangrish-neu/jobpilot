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
from dataclasses import dataclass
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
