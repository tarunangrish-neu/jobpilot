"""Hacker News "Who is hiring?" source (via the public Algolia HN API).

    GET https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring
    GET https://hn.algolia.com/api/v1/items/{story_id}   -> nested comment tree

Only top-level comments are postings. They are free text, so the LLM extracts
{company, role, location, remote, visa mention, apply link}; one comment can
yield several roles. A cheap keyword pre-filter keeps the LLM bill bounded.
HN jobs can't be auto-filled: they go to the review UI's manual-apply queue.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..llm import LLMClient
from ..llm.prompts import HN_EXTRACT
from ..models import JobPosting
from .base import html_to_text, parse_iso

log = logging.getLogger(__name__)

SOURCE = "hn"
SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=20"
ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"
_TITLE_RE = re.compile(r"who is hiring", re.I)


class HNJob(BaseModel):
    company: str = ""
    role: str = ""
    location: str = ""
    remote: bool = False
    visa_mention: str = ""
    apply_link: str = ""


class HNExtract(BaseModel):
    jobs: list[HNJob] = Field(default_factory=list)


def latest_thread(search_payload: Any) -> Optional[dict]:
    """Newest 'Ask HN: Who is hiring? (Month Year)' story from the search results."""
    hits = (search_payload or {}).get("hits") or []
    threads = [h for h in hits if _TITLE_RE.search(h.get("title") or "")]
    threads.sort(key=lambda h: h.get("created_at_i") or 0, reverse=True)
    return threads[0] if threads else None


def top_level_comments(item_payload: Any) -> list[dict]:
    out = []
    for child in (item_payload or {}).get("children") or []:
        text = html_to_text(child.get("text"))
        if text and child.get("id"):
            out.append({"id": str(child["id"]), "text": text, "created_at": child.get("created_at")})
    return out


def keep_comment(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(re.search(r"(?<![a-z])" + re.escape(k.lower()) + r"(?![a-z])", low) for k in keywords)


def to_postings(comment: dict, extract: HNExtract) -> list[JobPosting]:
    postings = []
    for i, job in enumerate(extract.jobs):
        if not job.company.strip() or not job.role.strip():
            continue
        link = job.apply_link.strip()
        # Only keep a link the comment really contains (the model must not invent one).
        if link and link.lower() not in comment["text"].lower():
            link = ""
        if link and "@" in link and not link.startswith(("mailto:", "http")):
            link = f"mailto:{link}"
        description = comment["text"]
        if job.visa_mention and job.visa_mention not in description:
            description += f"\n\nVisa mention: {job.visa_mention}"
        postings.append(
            JobPosting(
                source=SOURCE,
                external_id=f"{comment['id']}-{i}",
                company_name=job.company.strip(),
                title=job.role.strip(),
                location=job.location.strip(),
                remote=bool(job.remote),
                url=f"https://news.ycombinator.com/item?id={comment['id']}",
                apply_url=link,
                description_text=description,
                posted_at=parse_iso(comment.get("created_at")),
            )
        )
    return postings


async def fetch(client, llm: LLMClient, cfg: dict[str, Any]) -> tuple[list[JobPosting], dict[str, Any]]:
    """Postings from the latest thread, plus stats for the fetch table."""
    status, payload = await client.get_json(SEARCH_URL)
    thread = latest_thread(payload) if status == 200 else None
    if thread is None:
        return [], {"error": f"no 'Who is hiring' thread found (HTTP {status})"}
    status, item = await client.get_json(ITEM_URL.format(id=thread["objectID"]))
    if status != 200:
        return [], {"error": f"thread {thread['objectID']} returned HTTP {status}"}

    comments = top_level_comments(item)
    keywords = cfg.get("keywords") or []
    kept = [c for c in comments if not keywords or keep_comment(c["text"], keywords)]
    kept = kept[: int(cfg.get("max_comments", 80))]

    postings: list[JobPosting] = []
    failures = 0
    for comment in kept:
        try:
            extract = await llm.complete_json(HN_EXTRACT, HNExtract, comment=comment["text"][:4000])
        except Exception as exc:  # noqa: BLE001 - skip unparseable comments
            failures += 1
            log.warning("hn comment %s: %s", comment["id"], exc)
            continue
        postings += to_postings(comment, extract)
    return postings, {
        "thread": thread.get("title"),
        "comments": len(comments),
        "extracted_from": len(kept),
        "failed": failures,
    }
