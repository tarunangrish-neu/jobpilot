"""Prefill stage: open each tailored job's form, fill it, screenshot, STOP.

Nothing here submits. The page is filled, every value is read back, a
full-page screenshot is saved, and the job becomes `prefilled` -- or
`needs_human` (with the reasons and the page left open) on a CAPTCHA, login
wall, unknown required field, or a control that wouldn't take its value.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel
from sqlmodel import select

from .. import config, db
from ..llm import LLMClient
from ..llm.prompts import DRAFT_ANSWER
from ..master_resume import MasterResume
from ..models import Application, Company, Job, utcnow
from ..tailor import JobInput, output_dir, render_cover_letter
from ..tailor.cover_letter import CoverLetterDraft, clean_letter, write_cover_letter
from ..tailor.verify import ResumeFacts
from . import FILLERS
from .base import SENSITIVE_CATEGORIES, AnswerBank, FillPlan, categorize, classify_file, plan_fill
from .browser import Browser, blocker, execute, extract_fields, open_form, screenshot, verify, wait_until_closed

UrlFn = Callable[[Job, Optional[Company]], str]


class DraftedAnswer(BaseModel):
    answer: str = ""


@dataclass
class PrefillResult:
    job_id: int
    status: str = "prefilled"
    reasons: list[str] = field(default_factory=list)
    screenshot: Optional[Path] = None
    plan: Optional[FillPlan] = None
    page: object = None  # left open when a human is needed
    dry_rows: list[dict] = field(default_factory=list)


def make_drafter(llm: LLMClient, resume: MasterResume, facts: ResumeFacts, job: JobInput):
    """LLM drafts for novel free-text questions, vetted like a cover letter."""
    context = f"{job.company}\n{job.title}\n{job.description}"

    async def draft(question: str) -> Optional[str]:
        if categorize(question) in SENSITIVE_CATEGORIES:
            return None  # never LLM-answered, whatever the caller thinks
        try:
            reply = await llm.complete_json(
                DRAFT_ANSWER, DraftedAnswer, question=question, company=job.company, title=job.title,
                description=job.description[:3000], resume=resume.flatten(),
            )
        except Exception:  # noqa: BLE001 - no draft is a fine outcome
            return None
        if not reply.answer.strip():
            return None
        vetted = clean_letter(CoverLetterDraft(paragraphs=[reply.answer]), facts, context, 150, job.company)
        return vetted.text or None

    return draft


def _url(job: Job, company: Optional[Company]) -> str:
    return FILLERS[job.source].form_url(job, company)


def _load(top: int, job_ids: Optional[list[int]]):
    with db.session() as sess:
        query = (
            select(Job, Company, Application)
            .join(Company, Job.company_id == Company.id, isouter=True)
            .join(Application, Application.job_id == Job.id)
            .where(Job.source.in_(list(FILLERS)))
        )
        if job_ids:
            query = query.where(Job.id.in_(job_ids), Job.status.in_(("tailored", "prefilled", "needs_human")))
        else:
            query = query.where(Job.status == "tailored").order_by(Job.final_score.desc()).limit(top)
        rows = sess.exec(query).all()
        # Detach each object once: jobs at the same company share one Company instance.
        for obj in {id(o): o for row in rows for o in row if o is not None}.values():
            sess.expunge(obj)
        return rows


async def _ensure_cover_letter(llm, resume, facts, job_in: JobInput, app: Application) -> Optional[Path]:
    if app.cover_letter_path and Path(app.cover_letter_path).exists():
        return Path(app.cover_letter_path)
    if config.settings().get("tailor", {}).get("cover_letter", "auto") == "never":
        return None
    letter = await write_cover_letter(llm, resume, facts, job_in.company, job_in.title, job_in.description)
    app.cover_letter_text = letter.text if letter.ok else ""
    if not letter.ok:
        return None
    path = await render_cover_letter(resume, job_in, letter)
    app.cover_letter_path = str(path)
    return path


def dry_run_rows(fields, plan: FillPlan) -> list[dict]:
    """One row per form question: what would go in it, and where the answer comes from."""
    from .base import clean_label

    by_field = {a.field_id: a for a in plan.actions}
    rows = []
    for f in fields:
        label = clean_label(f) or f.name or f.id
        action = by_field.get(f.id)
        if action is not None:
            source = {"bank": "answers.yaml", "llm": "LLM draft (review)", "resume": "tailored resume",
                      "cover_letter": "cover letter", "review": "reviewed answer"}.get(action.source, action.source)
            answer = Path(action.value).name if f.kind == "file" else action.value
        elif any(label[:60] in reason for reason in plan.needs_human):
            source, answer = "NEEDS YOU", ""
        else:
            source, answer = "left blank (optional)", ""
        rows.append({"question": label, "type": f.kind, "required": f.required, "answer": answer,
                     "source": source, "detail": action.detail if action else ""})
    return rows


async def prefill_one(
    browser, llm, bank, resume, facts, job, company, app, url: str, dry_run: bool = False
) -> PrefillResult:
    result = PrefillResult(job.id)
    job_in = JobInput(job.id, job.title, company.name if company else "", job.description_text)
    page = await browser.new_page()
    shot = output_dir(job.id) / ("dry_run.png" if dry_run else "prefill.png")
    try:
        await open_form(page, url)
        blocked = await blocker(page)
        if blocked:
            result.reasons.append(f"{blocked} on the application page")
        fields = [] if blocked else await extract_fields(page)
        if not blocked and not fields:
            result.reasons.append("no application form found on the page")

        if fields:
            cover = Path(app.cover_letter_path) if app.cover_letter_path else None
            # A dry run never generates or uploads anything; it only reports the need.
            if not dry_run and any(f.kind == "file" and classify_file(f) == "cover_letter" for f in fields):
                cover = await _ensure_cover_letter(llm, resume, facts, job_in, app)
            threshold = float(config.settings().get("apply", {}).get("answer_match_threshold", 0.82))
            plan = await plan_fill(
                fields, bank, Path(app.resume_path) if app.resume_path else None, cover,
                embed=llm.embed, draft=make_drafter(llm, resume, facts, job_in), threshold=threshold,
            )
            result.plan = plan
            result.reasons += plan.needs_human
            if dry_run:
                result.dry_rows = dry_run_rows(fields, plan)
            else:
                result.reasons += await execute(page, plan, fields)
                result.reasons += await verify(page, plan, fields)
        result.screenshot = await screenshot(page, shot)
    except Exception as exc:  # noqa: BLE001 - any browser failure is a human's job
        result.reasons.append(f"prefill error: {type(exc).__name__}: {str(exc)[:200]}")
        try:
            result.screenshot = await screenshot(page, shot)
        except Exception:  # noqa: BLE001
            pass

    if dry_run:
        result.status = "dry_run"
        await page.close()
    elif result.reasons:
        result.status = "needs_human"
        result.page = page  # leave it open, per the spec
    else:
        await page.close()
    return result


def _record_dry_run(result: PrefillResult, app: Application, url: str) -> None:
    """Store the plan for the UI. Job status is untouched, so nothing becomes approvable."""
    with db.session() as sess:
        row = sess.get(Application, app.id)
        row.dry_run_json = json.dumps({
            "at": utcnow().isoformat(),
            "url": url,
            "rows": result.dry_rows,
            "needs_human": result.reasons,
            "drafted": result.plan.drafted if result.plan else {},
            "wants_cover_letter": bool(result.plan and result.plan.wants_cover_letter),
            "screenshot": str(result.screenshot or ""),
        })
        sess.add(row)
        db.log_event(sess, result.job_id, "prefill_dry_run", needs_human=len(result.reasons))
        sess.commit()


def _record(result: PrefillResult, app: Application) -> None:
    with db.session() as sess:
        job = sess.get(Job, result.job_id)
        row = sess.get(Application, app.id)
        row.cover_letter_path, row.cover_letter_text = app.cover_letter_path, app.cover_letter_text
        if result.plan is not None:
            row.answers_json = json.dumps(result.plan.answers())
            row.llm_drafted_answers_json = json.dumps(result.plan.drafted)
        row.prefilled_at = utcnow()
        row.screenshot_path = str(result.screenshot or "")
        row.error = "; ".join(result.reasons)
        sess.add(row)
        db.log_event(sess, job.id, "prefilled", status=result.status, reasons=result.reasons)
        if job.status != result.status:
            db.set_status(sess, job, result.status, reason="; ".join(result.reasons)[:500])
        sess.commit()


async def run_prefill(
    top: int = 30,
    job_ids: Optional[list[int]] = None,
    llm: Optional[LLMClient] = None,
    resume: Optional[MasterResume] = None,
    bank: Optional[AnswerBank] = None,
    headless: Optional[bool] = None,
    url_for: Optional[UrlFn] = None,
    wait_for_human: bool = True,
    dry_run: bool = False,
) -> list[PrefillResult]:
    from .. import master_resume

    llm = llm or LLMClient()
    resume = resume or master_resume.load()
    bank = bank or AnswerBank.load()
    facts = ResumeFacts.from_resume(resume)
    url_for = url_for or _url

    results: list[PrefillResult] = []
    async with Browser(headless=headless) as browser:
        for job, company, app in _load(top, job_ids):
            url = url_for(job, company)
            result = await prefill_one(browser, llm, bank, resume, facts, job, company, app, url, dry_run=dry_run)
            if dry_run:
                _record_dry_run(result, app, url)
            else:
                _record(result, app)
            results.append(result)

        waiting = [r for r in results if r.page is not None]
        if waiting and wait_for_human and not browser.headless:
            print(
                f"\n{len(waiting)} application(s) need you: jobs {', '.join(str(r.job_id) for r in waiting)}.\n"
                "Their tabs are open. Close each tab when done (the browser closes after the last).",
                file=sys.stderr,
            )
            for r in waiting:
                await wait_until_closed(r.page)
    return results
