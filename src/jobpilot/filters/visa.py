"""Sponsorship / citizenship screen.

Two passes, as the spec requires:
  1. literal blocking phrases from `visa.blocking_phrases` -> `blocked`;
  2. the LLM, only for descriptions that mention visas/authorization without
     a blocking phrase -> `ok | unclear | blocked` with a verbatim quote.

Descriptions that never mention the topic are `ok` without an LLM call.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, field_validator

from ..llm import LLMClient
from ..llm.prompts import VISA_CHECK

# Wording that makes a description worth an LLM look even without a blocking phrase.
_TOPIC_RE = re.compile(
    r"sponsor|visa\b|citizen|clearance|work authori[sz]ation|authori[sz]ed to work|"
    r"green card|permanent resident|h-?1b|export control|\bus person|\bitar\b|stem opt|\bf-1\b",
    re.I,
)


class VisaVerdict(BaseModel):
    flag: Literal["ok", "unclear", "blocked"]
    quote: str = ""
    reason: str = ""

    @field_validator("flag", mode="before")
    @classmethod
    def _lower(cls, v):
        return str(v).strip().lower()


def _phrase_re(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])")


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    right_candidates = [i for i in (text.find(".", end), text.find("\n", end)) if i != -1]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    return " ".join(text[left:right].split())[:300]


def find_blocking_phrase(text: str, phrases: list[str]) -> Optional[tuple[str, str]]:
    """Return (phrase, surrounding sentence) for the first blocking phrase, if any."""
    low = text.lower()
    for phrase in phrases:
        m = _phrase_re(phrase).search(low)
        if m:
            return phrase, _sentence_around(text, m.start(), m.end())
    return None


def relevant_excerpts(text: str, limit: int = 8) -> list[str]:
    """Lines of the description that touch visas / authorization / clearance."""
    out: list[str] = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if line and _TOPIC_RE.search(line) and line not in out:
            out.append(line[:400])
            if len(out) >= limit:
                break
    return out


async def llm_check(llm: LLMClient, company: str, title: str, excerpts: list[str]) -> VisaVerdict:
    verdict = await llm.complete_json(
        VISA_CHECK,
        VisaVerdict,
        company=company,
        title=title,
        excerpts="\n".join(f"- {e}" for e in excerpts),
    )
    # A "quote" the model invented is worse than none: keep only verbatim text.
    joined = " ".join(" ".join(excerpts).split()).lower()
    if verdict.quote and " ".join(verdict.quote.split()).lower() not in joined:
        verdict.quote = ""
    return verdict
