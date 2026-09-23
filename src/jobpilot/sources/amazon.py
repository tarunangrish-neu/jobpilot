"""Amazon (amazon.jobs), including subsidiaries such as AWS and Twitch.

    GET https://www.amazon.jobs/en/search.json?base_query=...&country=USA&result_limit=100&offset=N
    -> {"hits": T, "jobs": [{id_icims, title, normalized_location, location, description,
                              basic_qualifications, preferred_qualifications, posted_date,
                              job_path, company_name, is_intern, ...}]}

The JSON the public search page loads; robots.txt disallows only /internal.
Descriptions come with the search results, so no detail calls. `posted_date` is
"December 11, 2025". Token in companies.yaml: `amazon` (optionally
`amazon/<country code>`, default USA). Applications go through Amazon's own
account-based flow, so these jobs are applied to by hand.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from ..models import JobPosting
from . import large
from .base import html_to_text, looks_remote

log = logging.getLogger(__name__)

SOURCE = "amazon"
PAGE_SIZE = 100
SEARCH_URL = "https://www.amazon.jobs/en/search.json?"


def _country(token: str) -> str:
    return token.partition("/")[2] or "USA"


def _posted(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime(value or "", "%B %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse(payload: Any, company_name: str) -> list[JobPosting]:
    postings = []
    for job in (payload or {}).get("jobs") or []:
        if job.get("is_intern"):
            continue
        location = job.get("normalized_location") or job.get("location") or ""
        url = f"https://www.amazon.jobs{job.get('job_path', '')}"
        text = "\n\n".join(
            t for t in (
                html_to_text(job.get("description")),
                "Basic qualifications:\n" + html_to_text(job.get("basic_qualifications")) if job.get("basic_qualifications") else "",
                "Preferred qualifications:\n" + html_to_text(job.get("preferred_qualifications")) if job.get("preferred_qualifications") else "",
            ) if t
        )
        postings.append(JobPosting(
            source=SOURCE,
            external_id=str(job.get("id_icims") or job.get("id")),
            company_name=company_name,
            title=job.get("title") or "",
            location=location,
            remote=looks_remote(location, job.get("title") or ""),
            url=url,
            apply_url=url,
            description_text=text,
            posted_at=_posted(job.get("posted_date")),
        ))
    return postings


async def _search(client, country: str, term: str, limit: int) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for offset in range(0, limit, PAGE_SIZE):
        params = urlencode({"base_query": term, "country": country, "result_limit": PAGE_SIZE,
                            "offset": offset, "sort": "recent"})
        status, body = await client.get_json(SEARCH_URL + params)
        if status != 200 or not isinstance(body, dict):
            log.warning("amazon search %r returned HTTP %s", term, status)
            break
        page = body.get("jobs") or []
        jobs += page
        if len(page) < PAGE_SIZE or offset + PAGE_SIZE >= int(body.get("hits") or 0):
            break
    return jobs


async def verify(client, token: str) -> Optional[int]:
    params = urlencode({"base_query": "", "country": _country(token), "result_limit": 1, "offset": 0})
    status, body = await client.get_json(SEARCH_URL + params)
    if status != 200 or not isinstance(body, dict):
        return None
    return int(body.get("hits") or 0) or None


async def fetch(client, company: dict[str, Any]) -> list[JobPosting]:
    cfg = large.settings()
    seen: dict[str, dict[str, Any]] = {}
    for term in cfg["search_terms"]:
        for job in await _search(client, _country(company["token"]), term, cfg["max_per_term"]):
            seen.setdefault(str(job.get("id_icims") or job.get("id")), job)
    keep = [j for j in seen.values() if large.worth_detail(j.get("title", ""), j.get("normalized_location") or j.get("location") or "")]
    return parse({"jobs": keep}, company["name"])
