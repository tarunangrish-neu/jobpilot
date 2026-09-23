"""SmartRecruiters public postings API adapter.

    GET https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset=N
    -> {"offset": N, "limit": 100, "totalFound": T, "content": [...]}
    GET https://api.smartrecruiters.com/v1/companies/{token}/postings/{id}
    -> one posting with `jobAd.sections` (the description) and `applyUrl`

Quirks verified against live responses:
  * An unknown company returns HTTP 200 with `totalFound: 0`, not a 404, so a
    bad token looks exactly like a board with no openings.
  * The list endpoint has no description. Each posting needs a detail call,
    and large boards list hundreds of roles, so details are fetched only for
    postings whose title and location already pass the rules filter. The rest
    are still returned (without a description) and the filter drops them.
  * `location.remote` is a real boolean; `fullLocation` is the display string.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .. import config
from ..filters.rules import check_location, check_title
from ..models import JobPosting
from .base import html_to_text, parse_iso

log = logging.getLogger(__name__)

SOURCE = "smartrecruiters"
API = "https://api.smartrecruiters.com/v1/companies/{token}/postings"
BOARD_URL = API + "?limit=100&offset=0"
PAGE_URL = API + "?limit=100&offset={offset}"
DETAIL_URL = API + "/{id}"
MAX_PAGES = 20  # 2,000 postings; beyond that a board is not worth paging through daily

_SECTIONS = ("jobDescription", "qualifications", "additionalInformation")


def _location(job: dict[str, Any]) -> str:
    loc = job.get("location") or {}
    return loc.get("fullLocation") or ", ".join(
        p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p
    )


def _description(detail: dict[str, Any]) -> str:
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    return "\n\n".join(
        t for t in (html_to_text((sections.get(k) or {}).get("text")) for k in _SECTIONS) if t
    )


def parse(
    payload: Any, company_name: str, details: Optional[dict[str, dict[str, Any]]] = None
) -> list[JobPosting]:
    """Normalize a postings page; `details` maps posting id -> detail payload."""
    if not isinstance(payload, dict):
        return []
    details = details or {}

    postings: list[JobPosting] = []
    for job in payload.get("content") or []:
        job_id = str(job.get("id"))
        detail = details.get(job_id) or {}
        if detail.get("active") is False:
            continue
        identifier = (job.get("company") or {}).get("identifier") or ""
        url = detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{identifier}/{job_id}"
        postings.append(
            JobPosting(
                source=SOURCE,
                external_id=job_id,
                company_name=company_name,
                title=job.get("name") or "",
                location=_location(job),
                remote=bool((job.get("location") or {}).get("remote")),
                url=url,
                apply_url=detail.get("applyUrl") or url,
                description_text=_description(detail),
                posted_at=parse_iso(job.get("releasedDate")),
            )
        )
    return postings


def worth_detail(job: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Would this posting survive the title and location rules?"""
    remote = bool((job.get("location") or {}).get("remote"))
    return check_title(job.get("name") or "", cfg) is None and check_location(_location(job), remote, cfg) is None


async def fetch(client, company: dict[str, Any]) -> list[JobPosting]:
    token = company["token"]
    jobs: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        status, payload = await client.get_json(PAGE_URL.format(token=token, offset=page * 100))
        if status != 200 or not isinstance(payload, dict):
            log.warning("smartrecruiters/%s returned HTTP %s", token, status)
            break
        content = payload.get("content") or []
        jobs.extend(content)
        if not content or len(jobs) >= int(payload.get("totalFound") or 0):
            break
    if not jobs:
        log.warning("smartrecruiters/%s has no postings (unknown tokens also look like this)", token)
        return []

    cfg = config.settings().get("filters", {})
    details: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if worth_detail(job, cfg):
            status, detail = await client.get_json(DETAIL_URL.format(token=token, id=job["id"]))
            if status == 200 and isinstance(detail, dict):
                details[str(job["id"])] = detail
    return parse({"content": jobs}, company["name"], details)
