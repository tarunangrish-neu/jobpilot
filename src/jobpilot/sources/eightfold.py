"""Eightfold career sites (Microsoft; also used by other large employers).

    GET https://{host}/api/pcsx/search?domain={domain}&query=...&location=United%20States&start=N
    -> {"data": {"count": T, "positions": [{id, name, locations, standardizedLocations,
                                              postedTs, positionUrl, workLocationOption}]}}
    GET https://{host}/api/pcsx/position_details?position_id={id}&domain={domain}&hl=en
    -> {"data": {jobDescription (HTML), publicUrl, ...}}

Pages are 10 positions. `postedTs` is epoch seconds. Microsoft's robots.txt
disallows the site but explicitly allows /api/pcsx, which is all this uses.
Token in companies.yaml: `<host>/<domain>`, e.g.
`apply.careers.microsoft.com/microsoft.com`. No form filler: applied to by hand.
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

SOURCE = "eightfold"
PAGE_SIZE = 10


def split_token(token: str) -> tuple[str, str]:
    host, _, domain = token.partition("/")
    return host, domain


def _location(pos: dict[str, Any]) -> str:
    return "; ".join(pos.get("standardizedLocations") or pos.get("locations") or [])


def parse_detail(detail: dict[str, Any], pos: dict[str, Any], token: str, company_name: str) -> JobPosting:
    host, _ = split_token(token)
    location = _location(pos)
    url = detail.get("publicUrl") or f"https://{host}{pos.get('positionUrl', '')}"
    ts = pos.get("postedTs") or detail.get("postedTs")
    return JobPosting(
        source=SOURCE,
        external_id=str(pos.get("displayJobId") or pos.get("id")),
        company_name=company_name,
        title=pos.get("name") or detail.get("name") or "",
        location=location,
        remote=looks_remote(location, str(pos.get("workLocationOption") or "")),
        url=url,
        apply_url=url,
        description_text=html_to_text(detail.get("jobDescription")),
        posted_at=datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None,
    )


async def _search(client, token: str, term: str, limit: int) -> list[dict[str, Any]]:
    host, domain = split_token(token)
    out: list[dict[str, Any]] = []
    for start in range(0, limit, PAGE_SIZE):
        params = urlencode({"domain": domain, "query": term, "location": "United States", "start": start})
        status, body = await client.get_json(f"https://{host}/api/pcsx/search?{params}")
        data = (body or {}).get("data") if status == 200 else None
        if not data:
            if not out:
                log.warning("eightfold/%s search %r returned HTTP %s", token, term, status)
            break
        page = data.get("positions") or []
        out += page
        if len(page) < PAGE_SIZE or start + PAGE_SIZE >= int(data.get("count") or 0):
            break
    return out


async def verify(client, token: str) -> Optional[int]:
    host, domain = split_token(token)
    params = urlencode({"domain": domain, "query": "", "start": 0})
    status, body = await client.get_json(f"https://{host}/api/pcsx/search?{params}")
    data = (body or {}).get("data") if status == 200 else None
    if not data:
        return None
    return int(data.get("count") or 0) or None


async def fetch(client, company: dict[str, Any]) -> list[JobPosting]:
    token, cfg = company["token"], large.settings()
    host, domain = split_token(token)
    seen: dict[str, dict[str, Any]] = {}
    for term in cfg["search_terms"]:
        for pos in await _search(client, token, term, cfg["max_per_term"]):
            seen.setdefault(str(pos.get("id")), pos)
    keep = [p for p in seen.values() if large.worth_detail(p.get("name", ""), _location(p))]
    postings = []
    for pos in keep[: cfg["max_details"]]:
        params = urlencode({"position_id": pos["id"], "domain": domain, "hl": "en"})
        status, body = await client.get_json(f"https://{host}/api/pcsx/position_details?{params}", reuse=True)
        detail = (body or {}).get("data") if status == 200 else None
        if detail:
            postings.append(parse_detail(detail, pos, token, company["name"]))
    return postings
