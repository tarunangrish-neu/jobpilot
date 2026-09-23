"""Filter stage: dedupe -> rules -> visa screen, in the order the spec fixes.

Only jobs that are `new` and have never been filtered are considered. A job
that fails a step becomes `filtered_out` with `filter_reason` set; a job
that clears every step stays `new`, gets `filtered_at`, and is what `score`
picks up. LCA history is imported separately and never filters.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field

from sqlmodel import select

from .. import config, db
from ..models import Company, Job, utcnow
from . import dedupe, rules, visa


@dataclass
class FilterReport:
    considered: int = 0
    passed: int = 0
    dropped: Counter = field(default_factory=Counter)
    visa_flags: Counter = field(default_factory=Counter)
    llm_checked: int = 0
    llm_errors: int = 0


def _drop(sess, job: Job, reason: str, report: FilterReport, bucket: str) -> None:
    db.set_status(sess, job, "filtered_out", reason=reason)
    report.dropped[bucket] += 1


def _pass(sess, job: Job, now, report: FilterReport) -> None:
    job.filtered_at = now
    report.passed += 1
    report.visa_flags[job.visa_flag] += 1
    sess.add(job)
    db.log_event(sess, job.id, "filter_passed", visa_flag=job.visa_flag)


async def run_filters(llm=None) -> FilterReport:
    settings = config.settings()
    fcfg = settings.get("filters", {})
    vcfg = settings.get("visa", {})
    phrases = vcfg.get("blocking_phrases") or []
    use_llm = bool(vcfg.get("llm_check_ambiguous", True))
    report = FilterReport()
    now = utcnow()

    # Phase 1 (sync, one transaction): dedupe, rules, blocking phrases.
    ambiguous: list[tuple[int, str, str, list[str]]] = []
    with db.session() as sess:
        jobs = sess.exec(
            select(Job).where(Job.status == "new", Job.filtered_at.is_(None))
        ).all()
        report.considered = len(jobs)
        names = {c.id: c.name for c in sess.exec(select(Company)).all()}
        dupes = dedupe.find_duplicates(sess, [j.id for j in jobs])

        for job in jobs:
            if job.id in dupes:
                _drop(sess, job, f"duplicate of job #{dupes[job.id]}", report, "duplicate")
                continue
            reason = rules.apply_rules(job, fcfg, now)
            if reason:
                _drop(sess, job, reason, report, reason.split(":", 1)[0])
                continue

            hit = visa.find_blocking_phrase(job.description_text, phrases)
            if hit:
                phrase, sentence = hit
                job.visa_flag = "blocked"
                job.visa_reason = f"'{phrase}': \"{sentence}\""
                report.visa_flags["blocked"] += 1
                _drop(sess, job, f"visa: blocking phrase '{phrase}'", report, "visa")
                continue

            excerpts = visa.relevant_excerpts(job.description_text)
            if excerpts and use_llm and llm is not None:
                ambiguous.append((job.id, names.get(job.company_id, ""), job.title, excerpts))
                continue
            if excerpts:
                job.visa_flag = "unclear"
                job.visa_reason = f"mentions authorization, not checked: \"{excerpts[0][:200]}\""
            else:
                job.visa_flag = "ok"
                job.visa_reason = "no sponsorship/citizenship language found"
            _pass(sess, job, now, report)
        sess.commit()

    # Phase 2 (async, no open transaction): LLM verdicts on ambiguous wording.
    verdicts: dict[int, visa.VisaVerdict | Exception] = {}

    async def check(item):
        job_id, company, title, excerpts = item
        try:
            verdicts[job_id] = await visa.llm_check(llm, company, title, excerpts)
        except Exception as exc:  # noqa: BLE001 - a bad LLM reply must not lose the job
            verdicts[job_id] = exc

    await asyncio.gather(*(check(item) for item in ambiguous))

    # Phase 3: write verdicts.
    with db.session() as sess:
        for job_id, verdict in verdicts.items():
            job = sess.get(Job, job_id)
            report.llm_checked += 1
            if isinstance(verdict, Exception):
                report.llm_errors += 1
                job.visa_flag = "unclear"
                job.visa_reason = f"LLM check failed ({type(verdict).__name__}); review manually"
                _pass(sess, job, now, report)
                continue
            quoted = f": \"{verdict.quote}\"" if verdict.quote else ""
            job.visa_flag = verdict.flag
            job.visa_reason = f"{verdict.reason}{quoted}".strip() or verdict.flag
            if verdict.flag == "blocked":
                report.visa_flags["blocked"] += 1
                _drop(sess, job, f"visa: LLM blocked{quoted}"[:500], report, "visa")
            else:
                _pass(sess, job, now, report)
        sess.commit()
    return report
