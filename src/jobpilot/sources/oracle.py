"""Oracle Recruiting Cloud career sites (JPMorgan Chase and other large employers).

    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber={site},keyword="...",limit=25,offset=N
    -> {"items": [{"TotalJobsCount": T, "requisitionList": [{Id, Title, PostedDate,
                   PrimaryLocation, PrimaryLocationCountry, secondaryLocations,
                   ShortDescriptionStr, ExternalQualificationsStr, ExternalResponsibilitiesStr}]}]}
    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
        ?expand=all&onlyData=true&finder=ById;Id="{Id}",siteNumber={site}
    -> {"items": [{ExternalDescriptionStr, ExternalQualificationsStr, ...}]}

The REST API the public "Candidate Experience" site loads. Token in
companies.yaml: `<host prefix>/<site number>`, e.g. `jpmc.fa/CX_1001` for
https://jpmc.fa.oraclecloud.com (regional hosts look like `abcd.fa.us2`). The
host's firewall rejects some non-browser requests (robots.txt returned 403 on
2026-09-23 while the API answered), so failures are logged and skipped.
No form filler: applied to by hand.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

from ..models import JobPosting
from . import large
from .base import html_to_text, looks_remote, parse_iso

log = logging.getLogger(__name__)

SOURCE = "oracle"
PAGE_SIZE = 25
_API = "/hcmRestApi/resources/latest/"


def split_token(token: str) -> tuple[str, str]:
    prefix, _, site = token.partition("/")
    return f"{prefix}.oraclecloud.com", site


def _list_url(token: str, term: str, offset: int, limit: int = PAGE_SIZE) -> str:
    host, site = split_token(token)
    finder = f'findReqs;siteNumber={site},keyword="{term}",limit={limit},offset={offset}' if term else \
        f"findReqs;siteNumber={site},limit={limit},offset={offset}"
    return (f"https://{host}{_API}recruitingCEJobRequisitions?onlyData=true"
            f"&expand=requisitionList.secondaryLocations&finder={quote(finder, safe=';=,')}")


def _location(req: dict[str, Any]) -> str:
    names = [req.get("PrimaryLocation") or ""] + [s.get("Name", "") for s in req.get("secondaryLocations") or []]
    return "; ".join(dict.fromkeys(n for n in names if n))


def parse_detail(detail: dict[str, Any], req: dict[str, Any], token: str, company_name: str) -> JobPosting:
    host, site = split_token(token)
    location = _location(req)
    url = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{req.get('Id')}"
    text = "\n\n".join(t for t in (
        html_to_text(detail.get("ExternalDescriptionStr")),
        html_to_text(detail.get("ExternalResponsibilitiesStr") or req.get("ExternalResponsibilitiesStr")),
        html_to_text(detail.get("ExternalQualificationsStr") or req.get("ExternalQualificationsStr")),
    ) if t)
    return JobPosting(
        source=SOURCE,
        external_id=str(req.get("Id")),
        company_name=company_name,
        title=req.get("Title") or detail.get("Title") or "",
        location=location,
        remote=looks_remote(location, req.get("WorkplaceType") or ""),
        url=url,
        apply_url=url,
        description_text=text,
        posted_at=parse_iso(req.get("PostedDate")),
    )


async def _search(client, token: str, term: str, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for offset in range(0, limit, PAGE_SIZE):
        status, body = await client.get_json(_list_url(token, term, offset))
        items = (body or {}).get("items") if status == 200 else None
        if not items:
            if not out:
                log.warning("oracle/%s search %r returned HTTP %s", token, term, status)
            break
        page = items[0].get("requisitionList") or []
        out += page
        if len(page) < PAGE_SIZE or offset + PAGE_SIZE >= int(items[0].get("TotalJobsCount") or 0):
            break
    return out


async def verify(client, token: str) -> Optional[int]:
    status, body = await client.get_json(_list_url(token, "", 0, limit=1))
    items = (body or {}).get("items") if status == 200 else None
    if not items:
        return None
    return int(items[0].get("TotalJobsCount") or 0) or None


async def fetch(client, company: dict[str, Any]) -> list[JobPosting]:
    token, cfg = company["token"], large.settings()
    host, site = split_token(token)
    seen: dict[str, dict[str, Any]] = {}
    for term in cfg["search_terms"]:
        for req in await _search(client, token, term, cfg["max_per_term"]):
            seen.setdefault(str(req.get("Id")), req)
    keep = [r for r in seen.values() if large.worth_detail(r.get("Title", ""), _location(r))]
    postings = []
    for req in keep[: cfg["max_details"]]:
        finder = quote(f'ById;Id="{req["Id"]}",siteNumber={site}', safe=';=,')
        status, body = await client.get_json(
            f"https://{host}{_API}recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder={finder}",
            reuse=True,
        )
        items = (body or {}).get("items") if status == 200 else None
        postings.append(parse_detail(items[0] if items else {}, req, token, company["name"]))
    return postings
