"""Score stage: embed every filtered job, LLM-rerank the top N, combine.

final_score = w_embed * (cosine * 100) + w_llm * llm_score (+ lca_boost)

Only the top N by embedding similarity reach the LLM and become `scored`;
the rest keep their `embed_score` and stay `new`, so a later run (after the
top jobs move on) can still pick them up.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, select

from .. import config, db
from ..llm import LLMClient
from ..llm.prompts import RERANK
from ..master_resume import MasterResume
from ..models import Company, Job
from .embed import embed_scores

MAX_JD_CHARS = 6000


class RerankResult(BaseModel):
    score: float = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    seniority_fit: Literal["under", "match", "over"] = "match"

    @field_validator("seniority_fit", mode="before")
    @classmethod
    def _lower(cls, v):
        return str(v).strip().lower()


@dataclass
class ScoreReport:
    candidates: int = 0
    embedded: int = 0
    reranked: int = 0
    errors: list[str] = field(default_factory=list)


def final_score(
    embed: Optional[float], llm_score: Optional[float], has_lca: bool, cfg: dict
) -> float:
    weights = cfg.get("weights", {})
    w_embed, w_llm = float(weights.get("embed", 0.3)), float(weights.get("llm", 0.7))
    total = w_embed * (embed or 0.0) * 100.0
    total += w_llm * (llm_score if llm_score is not None else 0.0)
    if has_lca:
        total += float(cfg.get("lca_boost", 0.0))
    return round(total, 2)


async def run_scoring(
    llm: LLMClient, resume: MasterResume, top_n: Optional[int] = None
) -> ScoreReport:
    cfg = config.settings().get("scoring", {})
    top_n = int(top_n or cfg.get("embed_top_n", 60))
    report = ScoreReport()

    with db.session() as sess:
        rows = sess.exec(
            select(Job, Company)
            .join(Company, Job.company_id == Company.id, isouter=True)
            .where(Job.status == "new", Job.filtered_at.is_not(None), Job.visa_flag != "blocked")
        ).all()
        items = [
            (
                j.id,
                j.title,
                j.description_text,
                j.location,
                c.name if c else "",
                bool(c and c.has_lca_history),
            )
            for j, c in rows
        ]
    report.candidates = len(items)
    if not items:
        return report

    sims = await embed_scores(llm, resume, [(i[0], i[1], i[2]) for i in items])
    report.embedded = len(sims)
    top = sorted(items, key=lambda i: sims[i[0]], reverse=True)[:top_n]

    resume_text = resume.flatten()
    results: dict[int, RerankResult | Exception] = {}

    async def rerank(item) -> None:
        job_id, title, desc, location, company, _ = item
        try:
            results[job_id] = await llm.complete_json(
                RERANK,
                RerankResult,
                resume=resume_text,
                title=title,
                company=company,
                location=location,
                description=desc[:MAX_JD_CHARS],
            )
        except Exception as exc:  # noqa: BLE001 - one bad reply must not sink the run
            results[job_id] = exc

    await asyncio.gather(*(rerank(i) for i in top))

    with db.session() as sess:
        lca = {i[0]: i[5] for i in items}
        for job_id, sim in sims.items():
            job = sess.get(Job, job_id)
            job.embed_score = round(sim, 4)
            sess.add(job)
        for job_id, result in results.items():
            job = sess.get(Job, job_id)
            if isinstance(result, Exception):
                report.errors.append(f"job {job_id}: {type(result).__name__}: {result}")
                continue
            job.llm_score = result.score
            job.llm_reason = result.reasons[0] if result.reasons else ""
            job.llm_details_json = result.model_dump_json(exclude={"score"})
            job.final_score = final_score(job.embed_score, result.score, lca[job_id], cfg)
            db.set_status(sess, job, "scored")
            report.reranked += 1
        sess.commit()
    return report


def recompute_final_scores(sess: Session) -> int:
    """Refresh final_score after weights or LCA history change (no model calls)."""
    cfg = config.settings().get("scoring", {})
    n = 0
    for job, company in sess.exec(
        select(Job, Company)
        .join(Company, Job.company_id == Company.id, isouter=True)
        .where(Job.llm_score.is_not(None))
    ).all():
        job.final_score = final_score(
            job.embed_score, job.llm_score, bool(company and company.has_lca_history), cfg
        )
        sess.add(job)
        n += 1
    sess.commit()
    return n


@dataclass
class RankedJob:
    job: Job
    company: Optional[Company]
    details: dict


def ranked(
    sess: Session, statuses: tuple[str, ...] = ("scored",), limit: Optional[int] = None
) -> list[RankedJob]:
    query = (
        select(Job, Company)
        .join(Company, Job.company_id == Company.id, isouter=True)
        .where(Job.status.in_(statuses))
        .order_by(Job.final_score.desc().nulls_last(), Job.id)
    )
    if limit:
        query = query.limit(limit)
    out = []
    for job, company in sess.exec(query).all():
        try:
            details = json.loads(job.llm_details_json or "{}")
        except json.JSONDecodeError:
            details = {}
        out.append(RankedJob(job, company, details))
    return out
