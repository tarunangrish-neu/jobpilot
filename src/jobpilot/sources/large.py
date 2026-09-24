"""Shared helpers for large employer career sites (Workday, Amazon, Eightfold, Oracle).

These boards list thousands of roles, so their adapters don't page through
everything: they run the site's own keyword search once per
`large_boards.search_terms` entry, keep postings whose title (and location, when
the listing states it) already pass the rules filter, and fetch full
descriptions only for those, capped per board. Everything still flows through
the normal filter stage afterwards.
"""

from __future__ import annotations

import re
from typing import Any

from .. import config
from ..filters.rules import check_location, check_title

DEFAULT_TERMS = ["software engineer", "backend engineer", "platform engineer", "infrastructure engineer",
                 "distributed systems"]
# Listings that summarize several sites ("3 Locations") can't be judged until the detail call.
_MULTI_LOCATION = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def settings() -> dict[str, Any]:
    cfg = config.settings().get("large_boards", {}) or {}
    return {
        "search_terms": cfg.get("search_terms") or DEFAULT_TERMS,
        "max_per_term": int(cfg.get("max_per_term", 200)),
        "max_details": int(cfg.get("max_details", 150)),
    }


def worth_detail(title: str, location: str, remote: bool = False) -> bool:
    """Would this listing survive the title and location rules?"""
    cfg = config.settings().get("filters", {})
    if check_title(title or "", cfg) is not None:
        return False
    if not location or _MULTI_LOCATION.match(location):
        return True
    return check_location(location, remote, cfg) is None
