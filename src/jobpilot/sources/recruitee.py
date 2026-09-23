"""Recruitee careers-site API adapter.

    GET https://{token}.recruitee.com/api/offers/
    -> {"offers": [...]}

Quirks verified against live responses:
  * The token is a subdomain, not a path segment. Unknown tokens return 404.
  * The posting text is split across `description` and `requirements`, both
    HTML; the visa language often sits in `requirements`, so both are kept.
  * `published_at` is "2026-09-23 09:10:19 UTC", not ISO-8601.
  * `careers_apply_url` is the form; `careers_url` is the posting page.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import JobPosting
from .base import html_to_text, parse_iso

log = logging.getLogger(__name__)

SOURCE = "recruitee"
BOARD_URL = "https://{token}.recruitee.com/api/offers/"


def _location(offer: dict[str, Any]) -> str:
    places = [
        ", ".join(p for p in (loc.get("city"), loc.get("state"), loc.get("country")) if p)
        for loc in offer.get("locations") or []
    ]
    places = [p for p in dict.fromkeys(places) if p]
    return "; ".join(places) or offer.get("location") or ""


def parse(payload: Any, company_name: str) -> list[JobPosting]:
    if not isinstance(payload, dict):
        return []

    postings: list[JobPosting] = []
    for offer in payload.get("offers") or []:
        if offer.get("status", "published") != "published":
            continue
        description = "\n\n".join(
            t for t in (html_to_text(offer.get("description")), html_to_text(offer.get("requirements"))) if t
        )
        url = offer.get("careers_url") or ""
        postings.append(
            JobPosting(
                source=SOURCE,
                external_id=str(offer.get("id")),
                company_name=company_name,
                title=offer.get("title") or "",
                location=_location(offer),
                remote=bool(offer.get("remote")),
                url=url,
                apply_url=offer.get("careers_apply_url") or url,
                description_text=description,
                posted_at=parse_iso(offer.get("published_at")) or parse_iso(offer.get("created_at")),
            )
        )
    return postings


async def fetch(client, company: dict[str, Any]) -> list[JobPosting]:
    url = BOARD_URL.format(token=company["token"])
    status, payload = await client.get_json(url)
    if status != 200 or payload is None:
        log.warning("recruitee/%s returned HTTP %s", company["token"], status)
        return []
    return parse(payload, company["name"])
