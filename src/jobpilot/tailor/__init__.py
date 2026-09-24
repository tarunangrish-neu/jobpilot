"""Tailor stage: plan with the LLM, verify, render to one page, record.

For each job: ask the LLM to rank and rephrase the master bullets, build the
resume from master content only (verify.py vets rephrasings), render with
Typst and drop the lowest-ranked bullet only while it runs longer than the
full master resume does, optionally write a cover letter, then store paths
and the audit trail on the job's Application.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import select

from .. import config, db
from ..llm import LLMClient
from ..llm.prompts import TAILOR
from ..master_resume import MasterResume
from ..models import Application, Company, Job
from .cover_letter import CoverLetterResult, write_cover_letter
from .render import copy_for_upload, render
from .resume import MAX_REPHRASINGS, TailorPlan, TailorReport, build_document, plan_schema, select_bullets
from .verify import ResumeFacts

log = logging.getLogger(__name__)

MIN_BULLETS = 4
# Statuses a job may be (re)tailored from. Submitted jobs are never touched. `new` and
# `filtered_out` are here because the Jobs page tailors any opening you pick, unranked.
TAILORABLE = ("new", "filtered_out", "scored", "tailored", "prefilled", "needs_human")


@dataclass
class JobInput:
    id: int
    title: str
    company: str
    description: str


@dataclass
class TailorOutcome:
    job_id: int
    resume_pdf: Optional[Path] = None
    upload_pdf: Optional[Path] = None
    cover_pdf: Optional[Path] = None
    cover: Optional[CoverLetterResult] = None
    plan: Optional[TailorPlan] = None
    report: TailorReport = field(default_factory=TailorReport)
    error: str = ""


def _tcfg() -> dict:
    return config.settings().get("tailor", {})


def output_dir(job_id: int) -> Path:
    return config.project_root() / "output" / str(job_id)


_master_pages: dict[str, int] = {}


async def page_limit(resume: MasterResume, facts: ResumeFacts) -> int:
    """Pages a tailored resume may use: `tailor.max_pages`, else what the full master takes.

    A tailored resume should read like the master -- same length, better order --
    so bullets are only dropped when tailoring would make it longer than that.
    """
    configured = _tcfg().get("max_pages")
    if configured:
        return int(configured)
    key = resume.model_dump_json()
    if key not in _master_pages:
        everything = [b.id for b in resume.all_bullets()]
        doc, _ = build_document(resume, TailorPlan(), facts, everything)
        pdf = config.project_root() / "output" / "_master" / "resume.pdf"
        _master_pages[key] = max(1, await asyncio.to_thread(render, "resume.typ", doc, pdf))
    return _master_pages[key]


async def render_cover_letter(
    resume: MasterResume, job: JobInput, letter: CoverLetterResult
) -> Path:
    data = {
        "contact": {
            "name": resume.contact.name,
            "items": [i for i in (resume.contact.email, resume.contact.phone, resume.contact.location) if i],
        },
        "date": datetime.now(timezone.utc).strftime("%B %d, %Y"),
        "company": job.company,
        "role": job.title,
        "paragraphs": letter.paragraphs,
    }
    pdf = output_dir(job.id) / "cover_letter.pdf"
    await asyncio.to_thread(render, "cover_letter.typ", data, pdf)
    return copy_for_upload(pdf, resume.contact.name, "Cover_Letter")


async def tailor_one(
    llm: LLMClient,
    resume: MasterResume,
    facts: ResumeFacts,
    job: JobInput,
    want_cover: bool = False,
    use_cache: bool = True,
) -> TailorOutcome:
    cfg = _tcfg()
    outcome = TailorOutcome(job.id)
    try:
        plan = await llm.complete_json(
            TAILOR,
            TailorPlan,
            use_cache=use_cache,
            json_schema=plan_schema(resume, int(cfg.get("max_rephrasings", MAX_REPHRASINGS))),
            title=job.title,
            company=job.company,
            description=job.description[:6000],
            resume=resume.for_prompt(),
        )
        outcome.plan = plan
        # Every master bullet, in the plan's order; `max_bullets` (0 = no cap) can shorten it.
        selected, unknown = select_bullets(resume, plan, int(cfg.get("max_bullets") or 0) or None)
        max_pages = await page_limit(resume, facts)
        pdf = output_dir(job.id) / "resume.pdf"
        trimmed = 0
        while True:
            doc, report = build_document(resume, plan, facts, selected, job_text=job.description)
            pages = await asyncio.to_thread(render, "resume.typ", doc, pdf)
            if pages <= max_pages or len(selected) <= MIN_BULLETS:
                break
            selected = selected[:-1]
            trimmed += 1
        report.pages, report.trimmed, report.unknown_bullet_ids = pages, trimmed, unknown
        outcome.report = report
        outcome.resume_pdf = pdf
        outcome.upload_pdf = copy_for_upload(pdf, resume.contact.name)
        for rejected in report.rejected:
            log.info("job %s: rejected rephrase of %s (%s)", job.id, rejected["id"], rejected["new_entities"])

        if want_cover:
            letter = await write_cover_letter(
                llm, resume, facts, job.company, job.title, job.description,
                max_words=int(cfg.get("cover_letter_max_words", 200)), use_cache=use_cache,
            )
            outcome.cover = letter
            if letter.ok:
                outcome.cover_pdf = await render_cover_letter(resume, job, letter)
    except Exception as exc:  # noqa: BLE001 - report per job, keep going
        outcome.error = f"{type(exc).__name__}: {exc}"
    return outcome


def _record(sess, job: Job, outcome: TailorOutcome) -> None:
    app = sess.exec(select(Application).where(Application.job_id == job.id)).first()
    if app is None:
        app = Application(job_id=job.id)
    app.resume_path = str(outcome.upload_pdf or "")
    if outcome.cover is not None:
        app.cover_letter_path = str(outcome.cover_pdf or "")
        app.cover_letter_text = outcome.cover.text if outcome.cover.ok else ""
        if not outcome.cover.ok:
            # A letter from an earlier run must not be uploaded once it no longer verifies.
            for stale in output_dir(job.id).glob("*over_letter*"):
                stale.unlink()
    audit = {
        "plan": outcome.plan.model_dump() if outcome.plan else None,
        "report": outcome.report.to_dict(),
        "cover_letter_dropped": outcome.cover.dropped if outcome.cover else [],
    }
    app.tailoring_json = json.dumps(audit)
    app.error = ""
    sess.add(app)
    db.log_event(
        sess,
        job.id,
        "tailored",
        pages=outcome.report.pages,
        bullets=outcome.report.bullets,
        rephrased=outcome.report.rephrased,
        rejected=outcome.report.rejected,
        unknown_skills=outcome.report.unknown_skills,
        cover_letter=bool(outcome.cover_pdf),
    )
    # A new resume invalidates any earlier prefill.
    if job.status != "tailored":
        db.set_status(sess, job, "tailored")


def _load_jobs(top: int, job_ids: Optional[list[int]]) -> list[JobInput]:
    with db.session() as sess:
        query = select(Job, Company).join(Company, Job.company_id == Company.id, isouter=True)
        if job_ids:
            query = query.where(Job.id.in_(job_ids), Job.status.in_(TAILORABLE))
        else:
            query = query.where(Job.status == "scored").order_by(Job.final_score.desc()).limit(top)
        return [
            JobInput(j.id, j.title, c.name if c else "", j.description_text)
            for j, c in sess.exec(query).all()
        ]


async def run_tailoring(
    llm: LLMClient,
    resume: MasterResume,
    top: int = 30,
    job_ids: Optional[list[int]] = None,
    cover_letter: Optional[bool] = None,
    regenerate: bool = False,
) -> list[TailorOutcome]:
    if cover_letter is None:
        cover_letter = _tcfg().get("cover_letter", "auto") == "always"
    facts = ResumeFacts.from_resume(resume)
    jobs = _load_jobs(top, job_ids)
    outcomes = await asyncio.gather(
        *(tailor_one(llm, resume, facts, j, cover_letter, use_cache=not regenerate) for j in jobs)
    )
    with db.session() as sess:
        for outcome in outcomes:
            job = sess.get(Job, outcome.job_id)
            if outcome.error:
                db.log_event(sess, job.id, "tailor_failed", error=outcome.error)
                continue
            _record(sess, job, outcome)
        sess.commit()
    return list(outcomes)
