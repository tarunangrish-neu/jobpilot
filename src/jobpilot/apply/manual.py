"""Manual-apply drafts for jobs with no fillable form (HN "Who is hiring").

The LLM drafts a short message; every sentence goes through the same checks
as a cover letter (tailor/cover_letter.clean_letter), so nothing unverified
reaches the draft. The human copies and sends it.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel
from sqlmodel import select

from .. import db
from ..llm import LLMClient
from ..llm.prompts import OUTREACH
from ..master_resume import MasterResume
from ..models import Application, Company, Job
from ..tailor.cover_letter import CoverLetterDraft, clean_letter
from ..tailor.verify import ResumeFacts


class OutreachDraft(BaseModel):
    subject: str = ""
    message: str = ""


async def draft_outreach(llm: LLMClient, resume: MasterResume, job_id: int, use_cache: bool = True) -> Optional[str]:
    """Draft, verify, and store an outreach message. Returns it, or None if nothing survived."""
    with db.session() as sess:
        job = sess.get(Job, job_id)
        company = sess.get(Company, job.company_id) if job.company_id else None
        name = company.name if company else ""
        title, description = job.title, job.description_text

    reply = await llm.complete_json(
        OUTREACH, OutreachDraft, use_cache=use_cache, company=name, title=title,
        description=description[:4000], resume=resume.flatten(),
    )
    facts = ResumeFacts.from_resume(resume)
    vetted = clean_letter(
        CoverLetterDraft(paragraphs=reply.message.split("\n\n")), facts, f"{name}\n{title}\n{description}", 120, name
    )
    text = None
    if vetted.words >= 25:
        subject = reply.subject.strip() or f"{title} at {name}"
        text = f"Subject: {subject}\n\n{vetted.text}\n\n{resume.contact.name}"

    with db.session() as sess:
        app = sess.exec(select(Application).where(Application.job_id == job_id)).first() or Application(job_id=job_id)
        app.outreach_draft = text or ""
        sess.add(app)
        db.log_event(sess, job_id, "outreach_drafted", ok=bool(text), dropped=vetted.dropped)
        sess.commit()
    return text
