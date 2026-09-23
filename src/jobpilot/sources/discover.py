"""Detect which ATS a careers page uses and what its board token is.

Used by `jobpilot discover-token <careers_url>` to grow companies.yaml
quickly. Works in two passes: scrape the page for a board URL, and if that
finds nothing (common on JS-rendered pages), guess tokens from the domain
name. Every candidate is confirmed with a real API call before being
reported, so a returned token is always one that actually works.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

from . import ashby, greenhouse, lever

# Board URL shapes seen in careers-page markup, embed scripts, and iframes.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"grnhse[^\"']*['\"]?\s*[:=]\s*['\"]([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([a-z0-9_.-]+)", re.I)),
]

# Words that appear in board URLs but are never tokens.
_NOT_TOKENS = {"embed", "job_board", "jobs", "careers", "search", "v1", "v0", "boards"}

_ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def candidates_from_html(body: str) -> list[tuple[str, str]]:
    """Extract (ats, token) pairs from page markup, preserving first-seen order."""
    found: list[tuple[str, str]] = []
    for ats, pattern in PATTERNS:
        for match in pattern.finditer(body):
            token = match.group(1).strip().lower()
            if token and token not in _NOT_TOKENS and (ats, token) not in found:
                found.append((ats, token))
    return found


# Subdomains that describe the page, not the company. "about.gitlab.com"
# must guess "gitlab", never "about".
_GENERIC_LABELS = {
    "www", "careers", "career", "jobs", "job", "about", "work", "hiring",
    "join", "life", "apply", "talent", "boards", "people", "recruiting",
}


def guess_from_domain(url: str) -> list[str]:
    """Derive plausible board tokens from a careers URL's hostname.

    Tries the registrable label (second from the right) first, then any
    non-generic subdomain, then a hyphen-stripped variant -- board tokens
    are usually the bare company name with punctuation removed.
    """
    host = (urlparse(url).hostname or "").lower()
    parts = [p for p in host.split(".") if p]
    if not parts:
        return []

    guesses: list[str] = []
    if len(parts) >= 2:
        guesses.append(parts[-2])
    guesses.extend(p for p in parts[:-1] if p not in _GENERIC_LABELS)

    expanded: list[str] = []
    for g in guesses:
        expanded.extend([g, g.replace("-", "")])
    return list(dict.fromkeys(g for g in expanded if g and g not in _GENERIC_LABELS))


async def verify(client, ats: str, token: str) -> Optional[int]:
    """Return the posting count if this board exists, else None."""
    adapter = _ADAPTERS[ats]
    status, payload = await client.get_json(adapter.BOARD_URL.format(token=token))
    if status != 200 or payload is None:
        return None
    return len(adapter.parse(payload, token))


async def discover(client, careers_url: str) -> list[dict[str, Any]]:
    """Return every confirmed board for a careers URL, best match first."""
    status, body = await client.get_text(careers_url)
    candidates = candidates_from_html(body) if status == 200 else []

    if not candidates:
        candidates = [
            (ats, guess)
            for guess in guess_from_domain(careers_url)
            for ats in ("greenhouse", "lever", "ashby")
        ]

    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ats, token in candidates:
        if (ats, token) in seen:
            continue
        seen.add((ats, token))
        count = await verify(client, ats, token)
        if count is not None:
            results.append({"ats": ats, "token": token, "job_count": count})

    results.sort(key=lambda r: r["job_count"], reverse=True)
    return results
