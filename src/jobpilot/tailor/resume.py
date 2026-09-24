from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import re

from pydantic import BaseModel, Field, field_validator

from ..master_resume import MasterResume
from .verify import ResumeFacts, numbers_in, unsupported_claims, verify_rephrase, vocabulary

# Output tokens are the cost (a local 7B writes ~17 tok/s): rank the top bullets only
# (the rest follow in master order) and rewrite at most a few of them.
MAX_RANKED = 12
MAX_REPHRASINGS = 4


class TailorPlan(BaseModel):
    summary: str = ""
    bullet_ids: list[str] = Field(default_factory=list)
    rephrasings: dict[str, str] = Field(default_factory=dict)
    # Optional: prompt v1 chose skills; now they are ordered in code (order_skills).
    skills: list[str] = Field(default_factory=list)

    @field_validator("rephrasings", mode="before")
    @classmethod
    def _pairs_to_dict(cls, value: Any) -> Any:
        """Prompt v2 replies with [{"id", "text"}] so ids can be schema-constrained."""
        if isinstance(value, list):
            return {
                str(item.get("id", "")): str(item.get("text", ""))
                for item in value
                if isinstance(item, dict) and item.get("id")
            }
        return value


def plan_schema(resume: MasterResume, max_rephrasings: int = MAX_REPHRASINGS) -> dict[str, Any]:
    """JSON schema for the model's reply with every id pinned to the master resume.

    Ollama compiles this into a decoding grammar, so the model cannot emit a
    bullet id or summary key that does not exist, and it stops after
    MAX_RANKED ids and `max_rephrasings` rewrites instead of spending tokens on more
    (0 turns rewriting off: the reply is then just a ranking, about half the tokens).
    """
    ids = [b.id for b in resume.all_bullets()]
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "enum": list(resume.summary) or [""]},
            "bullet_ids": {
                "type": "array",
                "items": {"type": "string", "enum": ids},
                "maxItems": min(MAX_RANKED, len(ids)),
            },
            "rephrasings": {
                "type": "array",
                "maxItems": max_rephrasings,
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "enum": ids}, "text": {"type": "string"}},
                    "required": ["id", "text"],
                },
            },
        },
        "required": ["summary", "bullet_ids", "rephrasings"],
    }


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


def select_bullets(
    resume: MasterResume, plan: TailorPlan, max_bullets: Optional[int] = None, fill: bool = True
) -> tuple[list[str], list[str]]:
    """Valid bullet ids in the plan's priority order, plus the unknown ones.

    With `fill`, bullets the plan left out follow in master order, so the
    tailored resume keeps the master's length; the page limit trims from the end.
    """
    index = resume.bullet_index()
    chosen: list[str] = []
    unknown: list[str] = []
    for bid in plan.bullet_ids:
        if bid in index and bid not in chosen:
            chosen.append(bid)
        elif bid not in index:
            unknown.append(bid)
    if fill or not chosen:
        chosen += [b.id for b in resume.all_bullets() if b.id not in chosen]
    return (chosen[:max_bullets] if max_bullets else chosen), unknown


def _mentions(skill: str, text: str) -> bool:
    """Whether the job text names a skill: "AWS (EC2, S3)" counts via "AWS", "OAuth 2.0/JWT" via either."""
    head = skill.split(" (")[0]
    for name in (n.strip() for n in head.split("/")):
        if not name:
            continue
        # Short names ("Go", "SQL") only as written, so "go" and "ago" don't match "Go".
        flags = 0 if len(name) <= 3 else re.I
        if re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text, flags):
            return True
    return False


def order_skills(resume: MasterResume, job_text: str) -> list[str]:
    """Every master skill, with the ones the job names first (group order is kept by the caller)."""
    skills = resume.all_skills()
    return [s for s in skills if _mentions(s, job_text)] + [s for s in skills if not _mentions(s, job_text)]


def _shape_problem(original: str, rephrase: str, facts: ResumeFacts) -> str:
    """A reason when a rephrase pads, shrinks, or thins the bullet instead of restating it.

    Seen from qwen2.5-7b: "...from 2 days to 3 hours (Kubernetes, Terraform)" (padding),
    and "Built an LLM-powered analytics platform, reducing ... by 40%" for a bullet that
    also named its stack and "12,000+ internal analysts" (thinning). Nothing invented
    either way, but the tailored resume should say everything the master says.
    """
    if "(" in rephrase and "(" not in original:
        return "added a parenthetical"
    if len(rephrase) > len(original) * 1.3 + 20:
        return "much longer than the original"
    dropped = sorted(numbers_in(original) - numbers_in(rephrase))
    kept = vocabulary(rephrase)
    dropped += sorted({t for t in vocabulary(original) if t in facts.tech and t not in kept})
    if dropped:
        return "dropped " + ", ".join(dropped)
    if len(rephrase) < len(original) * 0.8:
        return "much shorter than the original"
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
    job_text: str = "",
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
        # A verbatim copy (often minus the final period) is not a rephrasing.
        if not new or new.rstrip(". ") == original.strip().rstrip(". "):
            texts[bid] = original
            continue
        finding = verify_rephrase(original, new, facts)
        stuffing = _shape_problem(original, new, facts)
        # Job wording the resume never uses ("led cross-functional stakeholders") is a
        # claim too, even with no new number or name in it.
        borrowed = unsupported_claims(new, facts, job_text) if job_text else []
        if finding or stuffing or borrowed:
            reasons = finding.items() + ([stuffing] if stuffing else [])
            reasons += [f"job wording: {', '.join(borrowed)}"] if borrowed else []
            report.rejected.append({"id": bid, "rephrase": new, "new_entities": reasons})
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

    # Skills: only ones that exist in the master resume. A v1 plan's own list wins;
    # otherwise every skill stays, the ones the job names first.
    canonical = {s.lower(): s for s in resume.all_skills()}
    wanted = [canonical[s.strip().lower()] for s in plan.skills if s.strip().lower() in canonical]
    report.unknown_skills = [s for s in plan.skills if s.strip().lower() not in canonical]
    if not plan.skills and job_text:
        wanted = order_skills(resume, job_text)
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
