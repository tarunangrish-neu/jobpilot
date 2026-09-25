"""Workday career sites (Fidelity, Morgan Stanley, NVIDIA, Capital One, BofA, Citi, ...).

    POST https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
         {"limit": 20, "offset": N, "searchText": "...", "appliedFacets": {}}
    -> {"total": T, "jobPostings": [{title, externalPath, locationsText, postedOn, bulletFields}]}
    GET  https://{host}/wday/cxs/{tenant}/{site}{externalPath}
    -> {"jobPostingInfo": {jobDescription (HTML), location, additionalLocations, startDate,
                           jobReqId, externalUrl, remoteType, ...}}

This is the JSON the public career site itself loads. Token format in
companies.yaml: `<tenant>.<wdN>/<site>`, e.g. `fmr.wd1/FidelityCareers` for
https://fmr.wd1.myworkdayjobs.com/FidelityCareers. Quirks verified live
(2026-09-23): pages are at most 20 postings; `locationsText` is often
"3 Locations" (the real list is only in the detail); `postedOn` is relative
("Posted 8 Days Ago"), so the detail's `startDate` is used instead; each
tenant's robots.txt allows the career site and disallows only /targeted/ and
/refreshFacet/. There is no form filler: Workday applications need an account,
so these jobs are applied to by hand.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..models import JobPosting
from . import large
from .base import html_to_text, looks_remote, parse_iso

log = logging.getLogger(__name__)

SOURCE = "workday"
PAGE_SIZE = 20


def split_token(token: str) -> tuple[str, str, str]:
    """`fmr.wd1/FidelityCareers` -> (host, tenant, site)."""
    host_part, _, site = token.partition("/")
    tenant = host_part.split(".")[0]
    return f"{host_part}.myworkdayjobs.com", tenant, site


def _api(token: str) -> str:
    host, tenant, site = split_token(token)
    return f"https://{host}/wday/cxs/{tenant}/{site}"


def parse_detail(info: dict[str, Any], listing: dict[str, Any], token: str, company_name: str) -> Optional[JobPosting]:
    host, _, site = split_token(token)
    path = listing.get("externalPath") or ""
    external_id = str(info.get("jobReqId") or (listing.get("bulletFields") or [path])[0] or path)
    locations = [info.get("location") or listing.get("locationsText") or ""]
    locations += [loc for loc in info.get("additionalLocations") or [] if loc]
    location = "; ".join(dict.fromkeys(loc for loc in locations if loc))
    url = info.get("externalUrl") or f"https://{host}/{site}{path}"
    return JobPosting(
        source=SOURCE,
        external_id=external_id,
        company_name=company_name,
        title=info.get("title") or listing.get("title") or "",
        location=location,
        remote=looks_remote(location, info.get("remoteType") or "", info.get("title") or ""),
        url=url,
        apply_url=url,
        description_text=html_to_text(info.get("jobDescription")),
        posted_at=parse_iso(info.get("startDate")),
    )


async def _search(client, token: str, term: str, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for offset in range(0, limit, PAGE_SIZE):
        status, body = await client.post_json(
            f"{_api(token)}/jobs", {"limit": PAGE_SIZE, "offset": offset, "searchText": term, "appliedFacets": {}}
        )
        if status != 200 or not isinstance(body, dict):
            if not out:
                log.warning("workday/%s search %r returned HTTP %s", token, term, status)
            break
        page = body.get("jobPostings") or []
        out += page
        if len(page) < PAGE_SIZE or offset + PAGE_SIZE >= int(body.get("total") or 0):
            break
    return out


async def verify(client, token: str) -> Optional[int]:
    status, body = await client.post_json(f"{_api(token)}/jobs", {"limit": 1, "offset": 0, "searchText": "", "appliedFacets": {}})
    if status != 200 or not isinstance(body, dict):
        return None
    return int(body.get("total") or 0) or None


async def fetch(client, company: dict[str, Any]) -> list[JobPosting]:
    token, cfg = company["token"], large.settings()
    listings: dict[str, dict[str, Any]] = {}
    for term in cfg["search_terms"]:
        for job in await _search(client, token, term, cfg["max_per_term"]):
            listings.setdefault(job.get("externalPath") or job.get("title"), job)
    keep = [j for j in listings.values() if large.worth_detail(j.get("title", ""), j.get("locationsText", ""))]
    postings: list[JobPosting] = []
    for job in keep[: cfg["max_details"]]:
        status, body = await client.get_json(f"{_api(token)}{job.get('externalPath', '')}", reuse=True)
        info = (body or {}).get("jobPostingInfo") if status == 200 else None
        if info:
            posting = parse_detail(info, job, token, company["name"])
            if posting:
                postings.append(posting)
    return postings
