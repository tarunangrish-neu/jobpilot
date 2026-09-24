"""Fetch-time screening: your `filters:` rules, applied without the LLM.

`fetch` runs `rescreen()` after every pull, and the Jobs page runs it when you
save new filters. A job that fails a rule becomes `filtered_out` with the rule
as its `filter_reason`; nothing is deleted, so loosening a filter brings jobs
straight back (to `new`). Jobs you have tailored, applied to, or skipped are
never touched.

Rules, cheapest first (all in `filters:`, overridable from config/filters.yaml):
  companies_exclude, title_include / title_exclude, locations_allow /
  allow_remote_anywhere, max_posting_age_days, max_years_experience,
  description_exclude, drop_sponsorship_blockers (visa.blocking_phrases).

`role_types`, `seniority` and `years_required` label a job for the Jobs table.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlmodel import select

from .. import config, db
from ..models import Application, Company, Job, utcnow
from . import rules

# Only these statuses are screened; anything further along is your decision, not a filter's.
SCREENABLE = ("new", "filtered_out")

ROLE_TYPES = (
    ("Backend", r"back[\s-]?end|server[\s-]side|\bapi\b|payments?"),
    ("Full-stack", r"full[\s-]?stack"),
    ("Frontend", r"front[\s-]?end|\bui\b|\bweb (developer|engineer)"),
    ("Infra / Platform / SRE", r"infrastructure|platform|\bsre\b|site reliability|devops|\bcloud\b|reliability|"
                               r"distributed systems|kubernetes|systems engineer"),
    ("ML / AI", r"machine learning|\bml\b|\bai\b|\bllm|deep learning|generative|genai|applied scientist|"
                r"research (scientist|engineer)|computer vision|\bnlp\b"),
    ("Data", r"\bdata\b|analytics|\betl\b"),
    ("Mobile", r"\bios\b|android|mobile"),
    ("Security", r"security"),
    ("Software (general)", r"software|developer|\bswe\b|programmer|\bengineer\b"),
    ("Manager", r"\bmanager\b|head of|\bdirector\b|\bvp\b|vice president"),
)
_ROLE_RES = [(name, re.compile(pattern, re.I)) for name, pattern in ROLE_TYPES]

LEVELS = ("Intern", "Entry / new grad", "Mid", "Senior", "Staff+", "Lead", "Manager")
_LEVEL_RES = (
    ("Intern", re.compile(r"\bintern(ship)?\b|co-?op\b", re.I)),
    ("Manager", re.compile(r"\bmanager\b|head of|\bdirector\b|\bvp\b|vice president", re.I)),
    ("Staff+", re.compile(r"\bstaff\b|principal|distinguished|\bfellow\b|architect", re.I)),
    ("Senior", re.compile(r"\bsenior\b|\bsr\b\.?|\b(engineer|developer)\s+(iii|3|iv|4)\b", re.I)),
    ("Lead", re.compile(r"\blead\b", re.I)),
    ("Entry / new grad", re.compile(r"new grad|graduate|entry[\s-]level|\bjunior\b|\bjr\b\.?|early career|"
                                    r"university|\b(engineer|developer)\s+(i|1)\b", re.I)),
)

# "5+ years of professional experience", "3-5 years of experience", "at least 4 yrs relevant experience".
_YEARS_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b(?:\s+of)?(?:[\s,/]+[\w+#.-]+){0,5}?"
    r"\s+experience",
    re.I,
)


def role_types(title: str) -> list[str]:
    found = [name for name, rx in _ROLE_RES if rx.search(title or "")]
    # "Software (general)" only when nothing more specific matched.
    specific = [f for f in found if f != "Software (general)"]
    return specific or found or ["Other"]


def seniority(title: str) -> str:
    for name, rx in _LEVEL_RES:
        if rx.search(title or ""):
            return name
    return "Mid"


def years_required(text: str) -> Optional[int]:
    """The most years of experience the description asks for, or None if it never says."""
    found = [int(m.group(1)) for m in _YEARS_RE.finditer(text or "")]
    found = [n for n in found if 0 < n <= 20]  # "150 years of history" is not a requirement
    return max(found) if found else None


def _phrases_re(phrases: list[str]) -> Optional[re.Pattern[str]]:
    phrases = sorted({p.strip().lower() for p in phrases if p and p.strip()}, key=len, reverse=True)
    if not phrases:
        return None
    return re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(p) for p in phrases) + r")(?![a-z0-9])")


@dataclass
class Screen:
    """Compiled rules; build once per run with `Screen.from_settings()`."""

    cfg: dict[str, Any]
    companies_exclude: set[str]
    blocking: Optional[re.Pattern[str]]
    description_exclude: Optional[re.Pattern[str]]
    max_years: int

    @classmethod
    def from_settings(cls) -> "Screen":
        settings = config.settings()
        cfg = settings.get("filters", {}) or {}
        blocking = (settings.get("visa", {}) or {}).get("blocking_phrases") or []
        return cls(
            cfg=cfg,
            companies_exclude={c.strip().lower() for c in cfg.get("companies_exclude") or [] if c},
            blocking=_phrases_re(blocking) if cfg.get("drop_sponsorship_blockers", True) else None,
            description_exclude=_phrases_re(cfg.get("description_exclude") or []),
            max_years=int(cfg.get("max_years_experience") or 0),
        )

    def reason(self, company: str, title: str, location: str, remote: bool, posted_at: Optional[datetime],
               description: str, visa_flag: str = "", now: Optional[datetime] = None) -> Optional[str]:
        """Why this job is screened out, or None if it passes every rule."""
        if company and company.strip().lower() in self.companies_exclude:
            return f"company: '{company}' excluded"
        why = (rules.check_title(title, self.cfg) or rules.check_location(location, remote, self.cfg)
               or rules.check_age(posted_at, self.cfg, now))
        if why:
            return why
        low = (description or "").lower()
        if self.max_years:
            years = years_required(description)
            if years is not None and years > self.max_years:
                return f"experience: asks for {years}+ years (max {self.max_years})"
        if self.description_exclude and (hit := self.description_exclude.search(low)):
            return f"description: mentions '{hit.group(1)}'"
        if self.blocking is not None:
            if hit := self.blocking.search(low):
                return f"visa: blocking phrase '{hit.group(1)}'"
            if visa_flag == "blocked":
                return "visa: blocked by an earlier visa screen"
        return None


@dataclass
class ScreenReport:
    considered: int = 0
    passed: int = 0
    screened_out: int = 0
    restored: int = 0
    reasons: Counter = field(default_factory=Counter)


def rescreen() -> ScreenReport:
    """Apply the current filters to every `new` / `filtered_out` job; only changes are written."""
    screen = Screen.from_settings()
    report = ScreenReport()
    now = utcnow()
    with db.session() as sess:
        names = {c.id: c.name for c in sess.exec(select(Company)).all()}
        # A job you already tailored a resume for is yours to decide on, whatever its status.
        tailored = set(sess.exec(select(Application.job_id).where(Application.resume_path != "")).all())
        for job in sess.exec(select(Job).where(Job.status.in_(SCREENABLE))).all():
            if job.id in tailored:
                continue
            report.considered += 1
            why = screen.reason(names.get(job.company_id, ""), job.title, job.location, job.remote,
                                job.posted_at, job.description_text, job.visa_flag, now)
            if why:
                report.reasons[why.split(":", 1)[0]] += 1
                if job.status != "filtered_out" or job.filter_reason != why:
                    report.screened_out += job.status != "filtered_out"
                    db.set_status(sess, job, "filtered_out", reason=why)
                continue
            report.passed += 1
            if job.status == "filtered_out":
                report.restored += 1
                job.filter_reason = ""
                db.set_status(sess, job, "new", reason="passes the current filters")
        sess.commit()
    return report
