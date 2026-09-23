from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..master_resume import MasterResume
from .verify import ResumeFacts, verify_rephrase


class TailorPlan(BaseModel):
    summary: str = ""
    bullet_ids: list[str] = Field(default_factory=list)
    rephrasings: dict[str, str] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)


@dataclass
class TailorReport:
    summary_key: str = ""
    bullets: list[str] = field(default_factory=list)  # ids used, in priority order
    rephrased: list[str] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    unknown_bullet_ids: list[str] = field(default_factory=list)
    unknown_skills: list[str] = field(default_factory=list)
    pages: int = 0
    trimmed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def select_bullets(resume: MasterResume, plan: TailorPlan, max_bullets: int) -> tuple[list[str], list[str]]:
    """Valid bullet ids in the plan's priority order, plus the unknown ones."""
    index = resume.bullet_index()
    chosen: list[str] = []
    unknown: list[str] = []
    for bid in plan.bullet_ids:
        if bid in index and bid not in chosen:
            chosen.append(bid)
        elif bid not in index:
            unknown.append(bid)
    if not chosen:
        chosen = [b.id for b in resume.all_bullets()]
    return chosen[:max_bullets], unknown


def _keyword_stuffing(original: str, rephrase: str) -> str:
    """A reason when a rephrase pads the bullet instead of restating it.

    Seen from qwen2.5-7b: "...from 2 days to 3 hours (Kubernetes, Terraform)".
    Nothing invented, but a tacked-on keyword list reads worse, not better.
    """
    if "(" in rephrase and "(" not in original:
        return "added a parenthetical"
    if len(rephrase) > len(original) * 1.3 + 20:
        return "much longer than the original"
    return ""


def _contact_items(resume: MasterResume) -> list[str]:
    items = [resume.contact.email, resume.contact.phone, resume.contact.location]
    items += [
        url.removeprefix("https://").removeprefix("http://").removeprefix("www.")
        for url in resume.links.values()
    ]
    return [i for i in items if i]


def build_document(
    resume: MasterResume,
    plan: TailorPlan,
    facts: ResumeFacts,
    selected: list[str],
    report: Optional[TailorReport] = None,
) -> tuple[dict[str, Any], TailorReport]:
    """Resume data for templates/resume.typ from the selected bullet ids."""
    report = report or TailorReport()
    index = resume.bullet_index()
    rank = {bid: i for i, bid in enumerate(selected)}

    # Rephrasings: verified against the bullet they replace.
    texts: dict[str, str] = {}
    report.rephrased, report.rejected = [], []
    for bid in selected:
        original = index[bid].text
        new = (plan.rephrasings.get(bid) or "").strip()
        if not new or new == original:
            texts[bid] = original
            continue
        finding = verify_rephrase(original, new, facts)
        stuffing = _keyword_stuffing(original, new)
        if finding or stuffing:
            report.rejected.append(
                {"id": bid, "rephrase": new, "new_entities": finding.items() + ([stuffing] if stuffing else [])}
            )
            texts[bid] = original
        else:
            report.rephrased.append(bid)
            texts[bid] = new

    def ordered(bullets) -> list[str]:
        picked = sorted((b for b in bullets if b.id in rank), key=lambda b: rank[b.id])
        return [texts[b.id] for b in picked]

    experience = []
    for e in resume.experience:
        bullets = ordered(e.bullets)
        # Keep every role on the page so the timeline has no gaps.
        if not bullets and e.bullets:
            bullets = [e.bullets[0].text]
        experience.append(
            {"title": e.title, "company": e.company, "dates": e.dates, "location": e.location, "bullets": bullets}
        )
    projects = [
        {"name": p.name, "dates": p.dates, "bullets": ordered(p.bullets)}
        for p in resume.projects
        if ordered(p.bullets)
    ]

    # Skills: only ones that exist in the master resume, in the plan's order.
    canonical = {s.lower(): s for s in resume.all_skills()}
    wanted = [canonical[s.strip().lower()] for s in plan.skills if s.strip().lower() in canonical]
    report.unknown_skills = [s for s in plan.skills if s.strip().lower() not in canonical]
    order = {s: i for i, s in enumerate(dict.fromkeys(wanted))}
    skills = []
    for group, items in resume.skills.items():
        keep = sorted((s for s in items if s in order), key=order.get) if order else list(items)
        if keep:
            skills.append({"group": group.replace("_", " ").title(), "items": keep})

    summary_key = plan.summary if plan.summary in resume.summary else next(iter(resume.summary), "")
    report.summary_key = summary_key
    report.bullets = list(selected)

    doc = {
        "contact": {"name": resume.contact.name, "items": _contact_items(resume)},
        "summary": resume.summary.get(summary_key, ""),
        "experience": experience,
        "projects": projects,
        "skills": skills,
        "education": [
            {"degree": ed.degree, "school": ed.school, "dates": ed.dates, "location": ed.location, "details": ed.details}
            for ed in resume.education
        ],
    }
    return doc, report
