"""Lever postings API adapter.

    GET https://api.lever.co/v0/postings/{token}?mode=json
    -> a bare JSON *list* of postings (no envelope object)

Quirks verified against live responses:
  * The top level is a list, so an unknown token returns HTTP 404 with a JSON
    error *object*; status must be checked rather than the payload shape.
  * `createdAt` is a millisecond epoch, not ISO-8601.
  * `applyUrl` and `hostedUrl` are different URLs -- the former is the form.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import JobPosting
from .base import html_to_text, looks_remote, parse_epoch_ms

log = logging.getLogger(__name__)

SOURCE = "lever"
BOARD_URL = "https://api.lever.co/v0/postings/{token}?mode=json"


def parse(payload: Any, company_name: str) -> list[JobPosting]:
    if not isinstance(payload, list):
        return []

    postings: list[JobPosting] = []
    for job in payload:
        categories = job.get("categories") or {}
        location = categories.get("location") or ""
        all_locations = ", ".join(categories.get("allLocations") or [])
        workplace = job.get("workplaceType") or ""

        # Lever splits the body across several fields; plain-text variants
        # exist for all of them, so prefer those over the HTML.
        body = "\n\n".join(
            part
            for part in (
                job.get("descriptionPlain") or html_to_text(job.get("description")),
                job.get("additionalPlain") or html_to_text(job.get("additional")),
            )
            if part
        )

        postings.append(
            JobPosting(
                source=SOURCE,
                external_id=str(job.get("id")),
                company_name=company_name,
                title=job.get("text") or "",
                location=all_locations or location,
                remote=workplace.lower() == "remote"
                or looks_remote(location, all_locations),
                url=job.get("hostedUrl") or "",
                apply_url=job.get("applyUrl") or job.get("hostedUrl") or "",
                description_text=body.strip(),
                posted_at=parse_epoch_ms(job.get("createdAt")),
            )
        )
    return postings


async def fetch(client, company: dict[str, Any]) -> list[JobPosting]:
    url = BOARD_URL.format(token=company["token"])
    status, payload = await client.get_json(url)
    if status != 200 or payload is None:
        log.warning("lever/%s returned HTTP %s", company["token"], status)
        return []
    return parse(payload, company["name"])
