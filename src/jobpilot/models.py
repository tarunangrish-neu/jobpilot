"""Database tables and the normalized posting DTO shared by all sources."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

# --- Status vocabulary (see spec section 5) --------------------------------

JOB_STATUSES = (
    "new",
    "filtered_out",
    "scored",
    "tailored",
    "prefilled",
    "approved",
    "submitted",
    "needs_human",
    "skipped",
    "rejected",
    "interviewing",
)

VISA_FLAGS = ("ok", "unclear", "blocked")

ATS_SOURCES = ("greenhouse", "lever", "ashby", "hn")


def utcnow() -> datetime:
    """Timezone-aware UTC now. All timestamps in JobPilot are UTC."""
    return datetime.now(timezone.utc)


# --- Source DTO ------------------------------------------------------------


class JobPosting(BaseModel):
    """Normalized posting emitted by every source adapter.

    Sources differ wildly in shape (Lever returns a bare list, Greenhouse
    returns entity-escaped HTML, Ashby nests location data); they all
    normalize down to this before touching the database.
    """

    source: str
    external_id: str
    company_name: str
    title: str
    location: str = ""
    remote: bool = False
    url: str = ""
    apply_url: str = ""
    description_text: str = ""
    posted_at: Optional[datetime] = None

    def content_hash(self) -> str:
        """Stable hash for cross-board dedupe of the same role.

        Deliberately excludes source/external_id/urls so the same role posted
        on two boards collapses to one hash.
        """
        basis = "\n".join(
            [
                self.company_name.strip().lower(),
                self.title.strip().lower(),
                self.location.strip().lower(),
                " ".join(self.description_text.split()).lower(),
            ]
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# --- Tables ----------------------------------------------------------------


class Company(SQLModel, table=True):
    __tablename__ = "companies"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    ats: str
    token: str
    has_lca_history: bool = False
    lca_filings_count: int = 0
    everify_confirmed: bool = False
    notes: str = ""
    active: bool = True

    __table_args__ = (UniqueConstraint("ats", "token", name="uq_company_board"),)


class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: Optional[int] = Field(default=None, foreign_key="companies.id", index=True)
    source: str = Field(index=True)
    external_id: str
    title: str = ""
    location: str = ""
    remote: bool = False
    url: str = ""
    apply_url: str = ""
    description_text: str = ""
    posted_at: Optional[datetime] = None
    fetched_at: datetime = Field(default_factory=utcnow)
    content_hash: str = Field(default="", index=True)
    status: str = Field(default="new", index=True)
    filter_reason: str = ""
    visa_flag: str = Field(default="unclear", index=True)
    embed_score: Optional[float] = None
    llm_score: Optional[float] = None
    llm_reason: str = ""
    # Set when a job clears the filter stage; `score` only considers these.
    filtered_at: Optional[datetime] = None
    visa_reason: str = ""
    final_score: Optional[float] = Field(default=None, index=True)
    # Full rerank payload: {"reasons": [...], "missing_skills": [...], "seniority_fit": ...}
    llm_details_json: str = "{}"

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_job_source_external"),
    )


class Application(SQLModel, table=True):
    __tablename__ = "applications"

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    resume_path: str = ""
    cover_letter_path: str = ""
    answers_json: str = "{}"
    llm_drafted_answers_json: str = "{}"
    prefilled_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    error: str = ""
    screenshot_path: str = ""
    # Tailoring audit: plan, bullets used, rejected rephrasings, dropped letter sentences.
    tailoring_json: str = "{}"
    cover_letter_text: str = ""
    confirmation_screenshot_path: str = ""


class Event(SQLModel, table=True):
    __tablename__ = "events"

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: Optional[int] = Field(default=None, foreign_key="jobs.id", index=True)
    type: str = Field(index=True)
    payload_json: str = "{}"
    created_at: datetime = Field(default_factory=utcnow)


# --- Caches ----------------------------------------------------------------


class Embedding(SQLModel, table=True):
    """Embedding vectors keyed by sha256(model + text), so re-scoring is cheap."""

    __tablename__ = "embeddings"

    key: str = Field(primary_key=True)
    model: str
    vector_json: str
    created_at: datetime = Field(default_factory=utcnow)


class LLMCache(SQLModel, table=True):
    """LLM responses keyed by a hash of provider, model, prompt version, and input."""

    __tablename__ = "llm_cache"

    key: str = Field(primary_key=True)
    prompt: str = Field(index=True)
    response: str
    created_at: datetime = Field(default_factory=utcnow)
