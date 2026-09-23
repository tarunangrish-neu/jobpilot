"""Cross-board dedupe by content hash.

(source, external_id) duplicates can't exist -- the unique constraint in
models.py stops them at insert time. What remains is the same role posted on
two boards (or twice on one), which `JobPosting.content_hash` collapses to a
single hash. The lowest job id with a given hash is canonical.
"""

from __future__ import annotations

from sqlmodel import Session, func, select

from ..models import Job


def find_duplicates(sess: Session, candidate_ids: list[int]) -> dict[int, int]:
    """Map each candidate that duplicates an earlier job to that job's id."""
    if not candidate_ids:
        return {}
    wanted = set(candidate_ids)
    hashes = set(
        sess.exec(
            select(Job.content_hash).where(Job.id.in_(candidate_ids), Job.content_hash != "")
        ).all()
    )
    if not hashes:
        return {}

    canonical = dict(
        sess.exec(
            select(Job.content_hash, func.min(Job.id))
            .where(Job.content_hash.in_(hashes))
            .group_by(Job.content_hash)
        ).all()
    )
    dupes: dict[int, int] = {}
    for job_id, h in sess.exec(
        select(Job.id, Job.content_hash).where(Job.content_hash.in_(hashes))
    ).all():
        if job_id in wanted and job_id != canonical[h]:
            dupes[job_id] = canonical[h]
    return dupes
