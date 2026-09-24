from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

from . import amazon, ashby, eightfold, greenhouse, lever, oracle, recruitee, smartrecruiters, workable, workday

# Board URL shapes seen in careers-page markup, embed scripts, and iframes.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"grnhse[^\"']*['\"]?\s*[:=]\s*['\"]([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([a-z0-9_.-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/v\d/widget/accounts/)?([a-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"([a-z0-9-]+)\.workable\.com", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([a-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([a-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"([a-z0-9-]+)\.recruitee\.com", re.I)),
]

_LARGE = {"workday": workday, "amazon": amazon, "eightfold": eightfold, "oracle": oracle}

# Words that appear in board URLs but are never tokens.
_NOT_TOKENS = {"embed", "job_board", "jobs", "careers", "search", "v1", "v0", "boards", "api", "j", "apply", "www"}

_ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workable": workable,
    "smartrecruiters": smartrecruiters,
    "recruitee": recruitee,
}


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
    if ats in _LARGE:
        # Large employer sites verify with their own search call (Workday is a POST).
        return await _LARGE[ats].verify(client, token)
    adapter = _ADAPTERS[ats]
    status, payload = await client.get_json(adapter.BOARD_URL.format(token=token))
    if status != 200 or payload is None:
        return None
    count = len(adapter.parse(payload, token))
    # SmartRecruiters answers 200 with zero postings for companies that do not exist.
    if ats == "smartrecruiters" and count == 0:
        return None
    return count


async def discover(client, careers_url: str) -> list[dict[str, Any]]:
    """Return every confirmed board for a careers URL, best match first."""
    status, body = await client.get_text(careers_url)
    candidates = candidates_from_html(body) if status == 200 else []

    if not candidates:
        candidates = [
            (ats, guess)
            for guess in guess_from_domain(careers_url)
            for ats in _ADAPTERS
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
