"""Database engine, schema creation, and the write helpers used by every stage."""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from sqlmodel import Session, SQLModel, create_engine, select

from . import config
from .models import Application, Company, Event, Job, JobPosting, utcnow  # noqa: F401

_engine = None

# Fields refreshed when a posting we already have is seen again. Everything
# else (status, scores, filter_reason, visa_flag) is pipeline state and must
# survive a re-fetch -- otherwise a re-run would reset tailored jobs to "new".
_REFRESHABLE = (
    "title",
    "location",
    "remote",
    "url",
    "apply_url",
    "description_text",
    "posted_at",
)


def get_engine(echo: bool = False):
    global _engine
    if _engine is None:
        path = config.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{path}", echo=echo)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine. Used by tests that relocate JOBPILOT_ROOT."""
    global _engine
    _engine = None


def init_db() -> None:
    """Create all four tables. Idempotent."""
    SQLModel.metadata.create_all(get_engine())


def session() -> Session:
    return Session(get_engine())


def log_event(sess: Session, job_id: Optional[int], type_: str, **payload: Any) -> None:
    """Append to the audit log. Every state change goes through here."""
    sess.add(
        Event(
            job_id=job_id,
            type=type_,
            payload_json=json.dumps(payload, default=str),
            created_at=utcnow(),
        )
    )


def sync_companies(sess: Session, entries: Iterable[dict[str, Any]]) -> dict[tuple[str, str], int]:
    """Upsert config companies into the table; return {(ats, token): company_id}."""
    index: dict[tuple[str, str], int] = {}
    for entry in entries:
        ats, token = entry["ats"], entry["token"]
        existing = sess.exec(
            select(Company).where(Company.ats == ats, Company.token == token)
        ).first()
        if existing is None:
            existing = Company(name=entry["name"], ats=ats, token=token)
            sess.add(existing)
            sess.flush()
        else:
            existing.name = entry["name"]
            existing.active = entry.get("active", True)
            sess.add(existing)
        index[(ats, token)] = existing.id
    sess.commit()
    return index


def upsert_posting(sess: Session, posting: JobPosting, company_id: Optional[int]) -> str:
    """Insert or refresh one posting.

    Returns "inserted" or "updated". The (source, external_id) uniqueness
    constraint is what makes `fetch` idempotent -- re-running it must not
    create duplicate rows.
    """
    existing = sess.exec(
        select(Job).where(
            Job.source == posting.source, Job.external_id == posting.external_id
        )
    ).first()

    if existing is None:
        job = Job(
            company_id=company_id,
            source=posting.source,
            external_id=posting.external_id,
            content_hash=posting.content_hash(),
            status="new",
            fetched_at=utcnow(),
            **{f: getattr(posting, f) for f in _REFRESHABLE},
        )
        sess.add(job)
        sess.flush()
        log_event(sess, job.id, "job_fetched", source=posting.source, title=posting.title)
        return "inserted"

    for field in _REFRESHABLE:
        setattr(existing, field, getattr(posting, field))
    existing.content_hash = posting.content_hash()
    existing.fetched_at = utcnow()
    if existing.company_id is None:
        existing.company_id = company_id
    sess.add(existing)
    return "updated"


def set_status(sess: Session, job: Job, status: str, reason: str = "") -> None:
    """Change a job's status and record it in the audit log."""
    previous = job.status
    job.status = status
    if reason:
        job.filter_reason = reason
    sess.add(job)
    sess.flush()
    log_event(sess, job.id, "status_changed", **{"from": previous, "to": status, "reason": reason})
