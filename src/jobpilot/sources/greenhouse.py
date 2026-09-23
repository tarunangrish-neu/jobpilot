"""Greenhouse board API adapter.

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
    -> {"jobs": [...], "meta": {...}}

Quirks verified against live responses:
  * `content` is HTML with entity-escaped angle brackets (needs unescaping first).
  * `location` is an object {"name": ...}, never a bare string.
  * There is no `posted_at`; `first_published` is the closest equivalent.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import JobPosting
from .base import html_to_text, looks_remote, parse_iso

log = logging.getLogger(__name__)

SOURCE = "greenhouse"
BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def parse(payload: Any, company_name: str) -> list[JobPosting]:
    if not isinstance(payload, dict):
        return []

    postings: list[JobPosting] = []
    for job in payload.get("jobs") or []:
        location = (job.get("location") or {}).get("name") or ""
        offices = " ".join(
            (o or {}).get("name", "") for o in (job.get("offices") or [])
        )
        description = html_to_text(job.get("content"))
        url = job.get("absolute_url") or ""

        postings.append(
            JobPosting(
                source=SOURCE,
                external_id=str(job.get("id")),
                company_name=job.get("company_name") or company_name,
                title=job.get("title") or "",
                location=location,
                remote=looks_remote(location, offices, job.get("title")),
                url=url,
                # Greenhouse serves the application form on the posting page itself.
                apply_url=url,
                description_text=description,
                posted_at=parse_iso(job.get("first_published"))
                or parse_iso(job.get("updated_at")),
            )
        )
    return postings


async def fetch(client, company: dict[str, Any]) -> list[JobPosting]:
    url = BOARD_URL.format(token=company["token"])
    status, payload = await client.get_json(url)
    if status != 200 or payload is None:
        log.warning("greenhouse/%s returned HTTP %s", company["token"], status)
        return []
    return parse(payload, company["name"])
