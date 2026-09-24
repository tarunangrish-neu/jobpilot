"""Title, location, and posting-age rules driven by `filters:` in settings.yaml.

Each check returns a human-readable reason when the job should be dropped,
or None when it passes. They are pure functions so tests can pin them.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia",
    "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
    "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada", "NH": "new hampshire",
    "NJ": "new jersey", "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee",
    "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}
# Full state names that are also common non-US places; matched only via abbreviation.
_AMBIGUOUS_STATE_NAMES = {"georgia"}

_SEGMENT_SPLIT = re.compile(r";|\||/| or |\n", re.I)
_REMOTE_WORDS = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh)\b", re.I)


def _phrase_re(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])")


def _first_match(text: str, phrases: list[str]) -> Optional[str]:
    low = text.lower()
    for phrase in phrases:
        if phrase and _phrase_re(phrase).search(low):
            return phrase
    return None


def check_title(title: str, cfg: dict[str, Any]) -> Optional[str]:
    excluded = _first_match(title, cfg.get("title_exclude") or [])
    if excluded:
        return f"title: excluded keyword '{excluded}'"
    include = cfg.get("title_include") or []
    if include and not _first_match(title, include):
        return "title: no include keyword"
    return None


def _us_signal(segment: str, allow: list[str]) -> bool:
    """True when a location segment names a US place from config or a US state."""
    low = segment.lower()
    if _first_match(low, [a for a in allow if not _REMOTE_WORDS.search(a)]):
        return True
    us_allowed = bool(_first_match(" ".join(allow), ["united states", "usa", "us"]))
    if not us_allowed:
        return False
    # "Palo Alto, CA" / "Washington, D.C." -- abbreviations only count after a comma.
    for abbr in re.findall(r",\s*([A-Z]\.?[A-Z]\.?)(?![A-Za-z])", segment):
        if abbr.replace(".", "") in US_STATES:
            return True
    return any(
        name not in _AMBIGUOUS_STATE_NAMES and _phrase_re(name).search(low)
        for name in US_STATES.values()
    )


def check_location(location: str, remote: bool, cfg: dict[str, Any]) -> Optional[str]:
    """Pass when any listed location is in the allow list (US + remote-US by default), or none is set.

    Boards mark "Remote - Japan" as remote, so a remote segment only passes
    bare ("Remote") or when its qualifier is itself allowed ("Remote - US").
    """
    allow = [a.lower() for a in (cfg.get("locations_allow") or [])]
    if not allow:  # no allow list means any location, as an empty title_include means any title
        return None
    if remote and cfg.get("allow_remote_anywhere"):
        return None

    text = (location or "").strip()
    if not text or text.lower() in {"n/a", "na", "tbd", "-"}:
        return None if remote and "remote" in allow else "location: not stated"

    bare_remote_ok = "remote" in allow
    for segment in _SEGMENT_SPLIT.split(text):
        segment = segment.strip()
        if not segment:
            continue
        if _REMOTE_WORDS.search(segment):
            qualifier = _REMOTE_WORDS.sub(" ", segment)
            qualifier = re.sub(r"[\s\-–—,:()\[\]]+", " ", qualifier).strip()
            if not qualifier:
                if bare_remote_ok:
                    return None
                continue
            if _us_signal(qualifier, allow):
                return None
            continue
        if _us_signal(segment, allow):
            return None
    return f"location: '{text}' not in allowed locations"


def check_age(
    posted_at: Optional[datetime], cfg: dict[str, Any], now: Optional[datetime] = None
) -> Optional[str]:
    max_days = cfg.get("max_posting_age_days")
    if not max_days or posted_at is None:
        return None
    now = now or datetime.now(timezone.utc)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    age = now - posted_at
    if age > timedelta(days=int(max_days)):
        return f"age: posted {age.days} days ago (max {max_days})"
    return None


def apply_rules(job, cfg: dict[str, Any], now: Optional[datetime] = None) -> Optional[str]:
    return (
        check_title(job.title, cfg)
        or check_location(job.location, job.remote, cfg)
        or check_age(job.posted_at, cfg, now)
    )
