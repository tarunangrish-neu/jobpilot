"""Workable job widget API adapter.

    GET https://apply.workable.com/api/v1/widget/accounts/{token}?details=true
    -> {"name": ..., "description": ..., "jobs": [...]}

Quirks verified against live responses:
  * Without `details=true` the jobs carry no `description` at all.
  * An unknown token returns HTTP 404.
  * `published_on` is a bare date ("2026-07-30"), not a timestamp.
  * `telecommuting` is the board's remote flag; location is split into
    city/state/country, with extra offices under `locations`.
  * `application_url` is the form; `url` is the posting page.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import JobPosting
from .base import html_to_text, parse_iso

log = logging.getLogger(__name__)

SOURCE = "workable"
BOARD_URL = "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"


def _place(*parts: Any) -> str:
    return ", ".join(str(p) for p in parts if p)


def _location(job: dict[str, Any]) -> str:
    places = [_place(job.get("city"), job.get("state"), job.get("country"))]
    for loc in job.get("locations") or []:
        places.append(_place(loc.get("city"), loc.get("region"), loc.get("country")))
    # Segments are joined with ";" so the location filter checks each office separately.
    return "; ".join(dict.fromkeys(p for p in places if p))


def parse(payload: Any, company_name: str) -> list[JobPosting]:
    if not isinstance(payload, dict):
        return []

    postings: list[JobPosting] = []
    for job in payload.get("jobs") or []:
        url = job.get("url") or job.get("shortlink") or ""
        postings.append(
            JobPosting(
                source=SOURCE,
                external_id=str(job.get("shortcode")),
                company_name=company_name,
                title=job.get("title") or "",
                location=_location(job),
                remote=bool(job.get("telecommuting")),
                url=url,
                apply_url=job.get("application_url") or url,
                description_text=html_to_text(job.get("description")),
                posted_at=parse_iso(job.get("published_on")) or parse_iso(job.get("created_at")),
            )
        )
    return postings


async def fetch(client, company: dict[str, Any]) -> list[JobPosting]:
    url = BOARD_URL.format(token=company["token"])
    status, payload = await client.get_json(url)
    if status != 200 or payload is None:
        log.warning("workable/%s returned HTTP %s", company["token"], status)
        return []
    return parse(payload, company["name"])
