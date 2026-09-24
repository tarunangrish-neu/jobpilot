"""Discover new company boards by running web-search queries (settings.yaml `discovery:`).

Search is a discovery layer, not a job source: queries like
`site:jobs.ashbyhq.com "backend engineer" payments` return URLs; any that point at a
supported ATS board (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee)
become candidate (ats, token) pairs, each is verified against the ATS's public API,
and verified boards are appended to config/companies.yaml. Jobs are then fetched from
those official APIs like any other board.

Google's result pages are never scraped (terms of service, and CAPTCHAs within a few
queries). Instead one of these APIs is used, each with a free tier and a key in .env:

  brave       Brave Search API        BRAVE_API_KEY
  google_cse  Google Programmable Search (Custom Search JSON API)
                                      GOOGLE_CSE_KEY, GOOGLE_CSE_CX
  serpapi     SerpApi (engine google, or google_jobs for Google Jobs listings)
                                      SERPAPI_API_KEY

LinkedIn, Indeed, and Glassdoor links are dropped unopened (a hard rule).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import yaml

from .discover import candidates_from_html, verify

BLOCKED_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com")
SUPPORTED = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee")


class SearchError(RuntimeError):
    pass


@dataclass
class Found:
    name: str
    ats: str
    token: str
    open_roles: int
    query: str


@dataclass
class DiscoveryReport:
    queries: int = 0
    urls: int = 0
    candidates: int = 0
    already_known: int = 0
    added: list[Found] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _blocked(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS)


# --- providers -------------------------------------------------------------------


def _key(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SearchError(f"{name} is not set (add it to .env; see .env.example)")
    return value


async def _brave(client, query: str, max_results: int) -> list[str]:
    urls: list[str] = []
    headers = {"X-Subscription-Token": _key("BRAVE_API_KEY"), "Accept": "application/json"}
    for offset in range(0, 10):  # Brave pages 20 results at a time, offset 0-9
        if len(urls) >= max_results:
            break
        params = urlencode({"q": query, "count": 20, "offset": offset})
        status, body = await client.get_json(f"https://api.search.brave.com/res/v1/web/search?{params}", headers=headers)
        if status != 200 or body is None:
            if not urls:
                raise SearchError(f"Brave search returned HTTP {status}")
            break
        results = (body.get("web") or {}).get("results") or []
        urls += [r["url"] for r in results if r.get("url")]
        if len(results) < 20:
            break
    return urls[:max_results]


async def _google_cse(client, query: str, max_results: int) -> list[str]:
    key, cx = _key("GOOGLE_CSE_KEY"), _key("GOOGLE_CSE_CX")
    urls: list[str] = []
    for start in range(1, min(max_results, 100) + 1, 10):  # 10 per page, start <= 91
        params = urlencode({"key": key, "cx": cx, "q": query, "num": 10, "start": start})
        status, body = await client.get_json(f"https://www.googleapis.com/customsearch/v1?{params}")
        if status != 200 or body is None:
            if not urls:
                raise SearchError(f"Google Programmable Search returned HTTP {status}")
            break
        items = body.get("items") or []
        urls += [i["link"] for i in items if i.get("link")]
        if len(items) < 10:
            break
    return urls[:max_results]


async def _serpapi(client, query: str, max_results: int, engine: str) -> list[str]:
    params = urlencode({"engine": engine, "q": query, "api_key": _key("SERPAPI_API_KEY")})
    status, body = await client.get_json(f"https://serpapi.com/search.json?{params}")
    if status != 200 or body is None:
        raise SearchError(f"SerpApi returned HTTP {status}")
    if engine == "google_jobs":
        urls = [
            opt["link"]
            for job in body.get("jobs_results") or []
            for opt in job.get("apply_options") or []
            if opt.get("link")
        ]
    else:
        urls = [r["link"] for r in body.get("organic_results") or [] if r.get("link")]
    return urls[:max_results]


async def search(client, cfg: dict[str, Any], query: str) -> list[str]:
    provider = cfg.get("provider", "brave")
    max_results = int(cfg.get("max_results_per_query", 40))
    if provider == "brave":
        return await _brave(client, query, max_results)
    if provider == "google_cse":
        return await _google_cse(client, query, max_results)
    if provider == "serpapi":
        return await _serpapi(client, query, max_results, cfg.get("serpapi_engine", "google"))
    raise SearchError(f"unknown discovery.provider '{provider}' (brave | google_cse | serpapi)")


# --- from URLs to verified boards ---------------------------------------------


def boards_from_urls(urls: list[str]) -> list[tuple[str, str]]:
    """(ats, token) pairs from result URLs, first-seen order, blocked domains skipped."""
    found: list[tuple[str, str]] = []
    for url in urls:
        if _blocked(url):
            continue
        for pair in candidates_from_html(url):
            if pair[0] in SUPPORTED and pair not in found:
                found.append(pair)
    return found


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


async def _company_name(client, ats: str, token: str) -> str:
    """Greenhouse boards carry a display name; elsewhere, prettify the token."""
    if ats == "greenhouse":
        status, body = await client.get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}")
        if status == 200 and body and body.get("name"):
            return str(body["name"])
    return " ".join(w.capitalize() for w in re.split(r"[-_.]+", token) if w) or token


async def discover(client, cfg: dict[str, Any], existing: list[dict[str, Any]]) -> DiscoveryReport:
    report = DiscoveryReport()
    known = {(c["ats"], str(c["token"]).lower()) for c in existing}
    known_names = {_norm(c["name"]) for c in existing}
    candidates: list[tuple[str, str, str]] = []  # (ats, token, query)
    for query in cfg.get("queries") or []:
        report.queries += 1
        try:
            urls = await search(client, cfg, query)
        except SearchError as exc:
            report.errors.append(f"{query!r}: {exc}")
            if "not set" in str(exc):
                break  # same missing key for every query
            continue
        report.urls += len(urls)
        for ats, token in boards_from_urls(urls):
            if (ats, token) in known:
                report.already_known += 1
                continue
            if any(c[:2] == (ats, token) for c in candidates):
                continue
            candidates.append((ats, token, query))
    report.candidates = len(candidates)

    min_roles = int(cfg.get("min_open_roles", 1))
    for ats, token, query in candidates:
        count = await verify(client, ats, token)
        if not count or count < min_roles:
            report.rejected.append(f"{ats}/{token}: {'no such board' if not count else f'{count} open roles'}")
            continue
        name = await _company_name(client, ats, token)
        if _norm(name) in known_names:
            report.already_known += 1
            continue
        known_names.add(_norm(name))
        report.added.append(Found(name, ats, token, count, query))
    return report


def append_to_companies(path: Path, found: list[Found]) -> None:
    """Append verified boards to companies.yaml as plain text, keeping its comments."""
    if not found:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = ["", f"  # --- discovered via search ({stamp} UTC); verified live, edit or remove freely ---"]
    for f in found:
        lines += [
            f"  - name: {yaml.safe_dump(f.name).strip().splitlines()[0]}",
            f"    ats: {f.ats}",
            f"    token: {f.token}",
            f"    # {f.open_roles} open roles; found by: {f.query}",
        ]
    text = path.read_text(encoding="utf-8").rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    yaml.safe_load(text)  # never write a file that no longer parses
    path.write_text(text, encoding="utf-8")
