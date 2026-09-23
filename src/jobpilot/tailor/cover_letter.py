"""Cover letters: <=200 words, specific, no clichés, every claim from the resume.

The LLM drafts paragraphs; then each sentence is checked with
verify.verify_free_text. A sentence with a number or technology not in the
resume, a proper noun found in neither the resume nor the job posting, or a
banned cliché is dropped. If too little survives, the result is not `ok` and
no letter is rendered -- better none than a fabricated one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..llm import LLMClient
from ..llm.prompts import COVER_LETTER
from ..master_resume import MasterResume
from .verify import ResumeFacts, unsupported_claims, verify_free_text

CLICHES = (
    "i am writing to express", "i am writing to apply", "passionate", "team player",
    "hit the ground running", "fast-paced", "synergy", "perfect fit", "dream job",
    "think outside the box", "go-getter", "self-starter", "results-driven",
)
MIN_WORDS = 60

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


class CoverLetterDraft(BaseModel):
    paragraphs: list[str] = Field(default_factory=list)


@dataclass
class CoverLetterResult:
    paragraphs: list[str]
    dropped: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def ok(self) -> bool:
        return self.words >= MIN_WORDS


def clean_letter(
    draft: CoverLetterDraft, facts: ResumeFacts, context: str, max_words: int = 200, company: str = ""
) -> CoverLetterResult:
    """Keep only verified, cliché-free sentences, within the word limit."""
    paragraphs: list[str] = []
    dropped: list[dict] = []
    words = 0
    for paragraph in draft.paragraphs:
        kept: list[str] = []
        for sentence in _SENTENCE_RE.split(" ".join(paragraph.split())):
            if not sentence:
                continue
            low = sentence.lower()
            cliche = next((c for c in CLICHES if c in low), None)
            if cliche:
                dropped.append({"sentence": sentence, "why": f"cliché '{cliche}'"})
                continue
            finding = verify_free_text(sentence, facts, allowed_context=context)
            if finding:
                dropped.append({"sentence": sentence, "why": "unsupported: " + ", ".join(finding.items())})
                continue
            borrowed = unsupported_claims(sentence, facts, context, company)
            if borrowed:
                dropped.append({"sentence": sentence, "why": "claims not on resume: " + ", ".join(borrowed)})
                continue
            n = len(sentence.split())
            if words + n > max_words:
                dropped.append({"sentence": sentence, "why": "over word limit"})
                continue
            kept.append(sentence)
            words += n
        if kept:
            paragraphs.append(" ".join(kept))
    return CoverLetterResult(paragraphs, dropped)


async def write_cover_letter(
    llm: LLMClient,
    resume: MasterResume,
    facts: ResumeFacts,
    company: str,
    title: str,
    description: str,
    max_words: int = 200,
    use_cache: bool = True,
) -> CoverLetterResult:
    draft = await llm.complete_json(
        COVER_LETTER,
        CoverLetterDraft,
        use_cache=use_cache,
        company=company,
        title=title,
        description=description[:5000],
        resume=resume.flatten(),
        max_words=max_words,
    )
    return clean_letter(draft, facts, f"{company}\n{title}\n{description}", max_words, company)
