"""Ashby job board API adapter.

    GET https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true
    -> {"jobs": [...], "apiVersion": "..."}

Quirks verified against live responses:
  * Postings carry an `isListed` flag; unlisted ones must be dropped.
  * `applyUrl` and `jobUrl` differ -- the former is the application form.
  * `isRemote` is authoritative, so no string sniffing is needed for it.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import JobPosting
from .base import html_to_text, parse_iso

log = logging.getLogger(__name__)

SOURCE = "ashby"
BOARD_URL = (
    "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
)


def parse(payload: Any, company_name: str) -> list[JobPosting]:
    if not isinstance(payload, dict):
        return []

    postings: list[JobPosting] = []
    for job in payload.get("jobs") or []:
        # Unlisted postings are drafts or internal-only; never surface them.
        if job.get("isListed") is False:
            continue

        secondary = ", ".join(
            (loc or {}).get("location", "") for loc in (job.get("secondaryLocations") or [])
        )
        location = job.get("location") or ""

        postings.append(
            JobPosting(
                source=SOURCE,
                external_id=str(job.get("id")),
                company_name=company_name,
                title=job.get("title") or "",
                location=", ".join(p for p in (location, secondary) if p),
                remote=bool(job.get("isRemote")),
                url=job.get("jobUrl") or "",
                apply_url=job.get("applyUrl") or job.get("jobUrl") or "",
                description_text=job.get("descriptionPlain")
                or html_to_text(job.get("descriptionHtml")),
                posted_at=parse_iso(job.get("publishedAt")),
            )
        )
    return postings


async def fetch(client, company: dict[str, Any]) -> list[JobPosting]:
    url = BOARD_URL.format(token=company["token"])
    status, payload = await client.get_json(url)
    if status != 200 or payload is None:
        log.warning("ashby/%s returned HTTP %s", company["token"], status)
        return []
    return parse(payload, company["name"])
