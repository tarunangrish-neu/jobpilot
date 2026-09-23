"""Approve & submit -- the only code that ever clicks a submit button.

There is deliberately no CLI command and no batch mode. The review UI calls
`approve()` for one job at a time (recording source="review_ui"), then
`launch_submit()` runs this module as a worker for that single job.
`submit_job` refuses unless every guard in `can_submit` passes:

  * the job is `approved` and its application has `approved_at`;
  * an `approved` event from the review UI exists in the audit log;
  * the job was never submitted before (never re-apply);
  * today's (UTC) submissions are under apply.daily_submission_cap.

It re-opens the form, refills it from the reviewed answers, re-verifies every
field, clicks submit, and waits for a confirmation page. A CAPTCHA, a
validation error, or a form that changed since review -> `needs_human`, with
a screenshot and the page left open.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import Session, func, select

from .. import config, db
from ..models import Application, Company, Event, Job, utcnow
from ..tailor import output_dir
from . import FILLERS
from .base import AnswerBank, plan_fill
from .browser import Browser, blocker, execute, extract_fields, open_form, page_errors, screenshot, verify, wait_until_closed

UI_SOURCE = "review_ui"

CONFIRM_TEXT = re.compile(
    r"thank you for (applying|your application|your interest)|application (has been |was )?(submitted|received)"
    r"|we(?:'|’)ve received your application|thanks for applying|application complete",
    re.I,
)
CONFIRM_URL = re.compile(r"confirm|thank|success|submitted", re.I)


class SubmitRefused(RuntimeError):
    pass


def submitted_today(sess: Session) -> int:
    midnight = datetime.combine(utcnow().date(), time.min, tzinfo=timezone.utc)
    return sess.exec(
        select(func.count()).select_from(Application).where(Application.submitted_at >= midnight)
    ).one()


def _app(sess: Session, job_id: int) -> Optional[Application]:
    return sess.exec(select(Application).where(Application.job_id == job_id)).first()


def approve(sess: Session, job: Job, edited_answers: Optional[dict[str, str]] = None) -> Application:
    """Record the human's approval from the review UI. Does not submit."""
    app = _app(sess, job.id)
    if app is None or job.status != "prefilled":
        raise SubmitRefused(f"job {job.id} is '{job.status}'; only prefilled jobs can be approved")
    if edited_answers is not None:
        answers = json.loads(app.answers_json or "{}")
        drafted = json.loads(app.llm_drafted_answers_json or "{}")
        for label, value in edited_answers.items():
            answers[label] = value
            if label in drafted:
                drafted[label] = value
        app.answers_json = json.dumps(answers)
        app.llm_drafted_answers_json = json.dumps(drafted)
    app.approved_at = utcnow()
    sess.add(app)
    db.log_event(sess, job.id, "approved", source=UI_SOURCE, edited=sorted((edited_answers or {}).keys()))
    db.set_status(sess, job, "approved", reason="approved in review UI")
    sess.commit()
    return app


def can_submit(sess: Session, job: Job, app: Optional[Application]) -> Optional[str]:
    """None when submission is allowed, else the reason it is refused."""
    if app is None:
        return "no application record"
    if job.status == "submitted" or app.submitted_at is not None:
        return "already submitted -- never re-apply"
    if job.status != "approved" or app.approved_at is None:
        return f"job is '{job.status}', not approved"
    approvals = sess.exec(select(Event).where(Event.job_id == job.id, Event.type == "approved")).all()
    if not any(json.loads(e.payload_json or "{}").get("source") == UI_SOURCE for e in approvals):
        return "no approval from the review UI in the audit log"
    if job.source not in FILLERS:
        return f"'{job.source}' jobs are applied to manually"
    cap = int(config.settings().get("apply", {}).get("daily_submission_cap", 40))
    if submitted_today(sess) >= cap:
        return f"daily submission cap of {cap} reached"
    return None


async def _click_submit(page, filler) -> bool:
    for selector in filler.SUBMIT_SELECTORS:
        button = page.locator(selector).first
        if await button.count() and await button.is_visible():
            await button.click()
            return True
    return False


async def _await_outcome(page, timeout_s: float = 30.0) -> tuple[str, str]:
    """('submitted' | 'captcha' | 'errors' | 'unknown', detail)."""
    start_url = page.url
    loop = asyncio.get_running_loop()
    started = loop.time()
    while loop.time() - started < timeout_s:
        await page.wait_for_timeout(1000)
        body = await page.evaluate("() => document.body ? document.body.innerText : ''")
        if CONFIRM_TEXT.search(body) or (page.url != start_url and CONFIRM_URL.search(page.url)):
            return "submitted", page.url
        if await blocker(page) == "captcha":
            return "captcha", "CAPTCHA challenge after clicking submit"
        errors = await page_errors(page)
        if errors and loop.time() - started > 4:
            return "errors", "; ".join(errors)
    return "unknown", f"no confirmation page within {timeout_s:.0f}s"


async def submit_job(
    job_id: int, headless: Optional[bool] = None, url_override: Optional[str] = None, wait_for_human: bool = True
) -> tuple[str, str]:
    with db.session() as sess:
        job = sess.get(Job, job_id)
        if job is None:
            raise SubmitRefused(f"no job {job_id}")
        app = _app(sess, job_id)
        refusal = can_submit(sess, job, app)
        if refusal:
            db.log_event(sess, job_id, "submit_refused", reason=refusal)
            sess.commit()
            raise SubmitRefused(refusal)
        company = sess.get(Company, job.company_id) if job.company_id else None
        filler = FILLERS[job.source]
        url = url_override or filler.form_url(job, company)
        overrides = json.loads(app.answers_json or "{}")
        overrides.update(json.loads(app.llm_drafted_answers_json or "{}"))
        resume = Path(app.resume_path) if app.resume_path else None
        cover = Path(app.cover_letter_path) if app.cover_letter_path else None

    outcome, detail, shot = "needs_human", "", None
    async with Browser(headless=headless) as browser:
        page = await browser.new_page()
        try:
            await open_form(page, url)
            blocked = await blocker(page)
            if blocked:
                raise RuntimeError(f"{blocked} on the application page")
            fields = await extract_fields(page)
            plan = await plan_fill(fields, AnswerBank.load(), resume, cover, overrides=overrides)
            problems = plan.needs_human + await execute(page, plan, fields) + await verify(page, plan, fields)
            if problems:
                raise RuntimeError("form differs from the reviewed version: " + "; ".join(problems))
            if not await _click_submit(page, filler):
                raise RuntimeError("could not find the submit button")
            result, detail = await _await_outcome(page)
            name = "confirmation.png" if result == "submitted" else "submit_failed.png"
            shot = await screenshot(page, output_dir(job_id) / name)
            outcome = "submitted" if result == "submitted" else "needs_human"
        except Exception as exc:  # noqa: BLE001 - every failure is handed to the human
            detail = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
            try:
                shot = await screenshot(page, output_dir(job_id) / "submit_failed.png")
            except Exception:  # noqa: BLE001
                pass

        with db.session() as sess:
            job = sess.get(Job, job_id)
            app = _app(sess, job_id)
            if outcome == "submitted":
                app.submitted_at = utcnow()
                app.confirmation_screenshot_path = str(shot or "")
                app.error = ""
            else:
                app.error = detail
                if shot:
                    app.screenshot_path = str(shot)
            sess.add(app)
            db.log_event(sess, job_id, "submit_attempt", outcome=outcome, detail=detail)
            db.set_status(sess, job, outcome, reason=detail[:500])
            sess.commit()

        if outcome != "submitted" and wait_for_human and not browser.headless:
            await wait_until_closed(page)  # leave it open for the human
    return outcome, detail


def launch_submit(job_id: int) -> subprocess.Popen:
    """Run the submit worker for ONE approved job in a separate process."""
    log_dir = config.project_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / f"submit_{job_id}.log").open("a", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "jobpilot.apply.submit", str(job_id)],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        sys.exit("usage: python -m jobpilot.apply.submit <job_id>  (started by the review UI)")
    try:
        status, info = asyncio.run(submit_job(int(sys.argv[1])))
        print(f"job {sys.argv[1]}: {status} {info}")
    except SubmitRefused as refused:
        sys.exit(f"refused: {refused}")
