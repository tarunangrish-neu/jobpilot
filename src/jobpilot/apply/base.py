"""Shared filler logic: the answer bank, question matching, and the fill plan.

Everything here is browser-free so it can be unit-tested. The per-ATS
fillers extract fields (apply/dom.py), call `plan_fill`, then execute it.

Rules from the spec, enforced here:
  * Work-authorization, sponsorship, EEO/demographic, and salary questions are
    answered ONLY from config/answers.yaml. No confident, same-category match
    (or an empty answer) -> leave blank and mark `needs_human`.
  * Contact and logistics fields also come only from answers.yaml; an empty
    value is never "filled in" by the LLM.
  * Matching is exact -> normalized -> embedding similarity (threshold in
    settings.apply.answer_match_threshold).
  * Other novel free-text questions get an LLM draft from resume facts,
    stored separately and highlighted for review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import yaml

from .. import config
from .dom import FormField

# --- question categories ---------------------------------------------------

SENSITIVE_CATEGORIES = ("sponsorship", "work_auth", "eeo", "salary")

_CATEGORY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("sponsorship", re.compile(r"sponsor|\bvisa\b|h-?1b|immigration", re.I)),
    ("work_auth", re.compile(
        r"authori[sz]ed to work|authori[sz]ation to work|eligible to work|legally (?:able|eligible|permitted)|"
        r"right to work|work permit|security clearance|\bclearance\b|citizenship|\bcitizen\b|us person|export control",
        re.I)),
    ("eeo", re.compile(
        r"gender|\bsex\b|race|ethnic|hispanic|latin[oax]|veteran|disabilit|pronoun|sexual orientation|"
        r"transgender|lgbt|demographic|self-identify", re.I)),
    ("salary", re.compile(r"salary|compensation|pay expectation|desired pay|expected pay|pay range|\bwage", re.I)),
]


def categorize(label: str) -> str:
    for category, pattern in _CATEGORY_RULES:
        if pattern.search(label):
            return category
    return "general"


# --- normalization ---------------------------------------------------------

_MARKERS = re.compile(r"[*✱]|\((?:required|optional)\)", re.I)


def clean_label(f: FormField) -> str:
    """Label without required markers, and without option text Lever appends to selects."""
    text = _MARKERS.sub(" ", f.label)
    if f.kind == "select" and f.options:
        cut = text.find("Select ...")
        if cut > 0:
            text = text[:cut]
        for opt in f.options:
            pos = text.find(opt)
            if pos > 0:
                text = text[:pos]
    return " ".join(text.split())


def normalize(text: str) -> str:
    text = _MARKERS.sub(" ", text.lower())
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(text.split())


# --- answer bank -------------------------------------------------------------


@dataclass
class BankEntry:
    key: str
    category: str
    questions: list[str]
    answer: Optional[str]  # None -> LLM drafts it (common_questions with a: null)


_CANONICAL: dict[str, tuple[str, list[str]]] = {
    "contact.first_name": ("contact", ["First Name", "Legal First Name", "Given Name", "Preferred First Name"]),
    "contact.last_name": ("contact", ["Last Name", "Legal Last Name", "Family Name", "Surname"]),
    "contact.full_name": ("contact", ["Full Name", "Name", "Legal Name", "Your Name"]),
    "contact.email": ("contact", ["Email", "Email Address", "E-mail"]),
    "contact.phone": ("contact", ["Phone", "Phone Number", "Mobile Phone", "Mobile"]),
    "contact.location": ("contact", ["Location", "Current Location", "City", "Where are you located?", "Location (City)"]),
    "contact.linkedin": ("contact", ["LinkedIn", "LinkedIn Profile", "LinkedIn URL", "LinkedIn Profile URL"]),
    "contact.github": ("contact", ["GitHub", "GitHub URL", "GitHub Profile"]),
    "contact.website": ("contact", ["Website", "Portfolio", "Portfolio URL", "Personal Website", "Other Website"]),
    "work_authorization.authorized_to_work_in_us": ("work_auth", [
        "Are you legally authorized to work in the United States?",
        "Are you authorized to work in the US?",
        "Are you legally authorized to work in the country for which you are applying?",
        "Are you eligible to work in the United States?",
    ]),
    "work_authorization.require_sponsorship_now_or_future": ("sponsorship", [
        "Will you now or in the future require sponsorship for employment visa status (e.g. H-1B)?",
        "Do you require visa sponsorship?",
        "Will you require sponsorship to work in the United States?",
        "Will you now or in the future require sponsorship for a visa?",
    ]),
    "logistics.willing_to_relocate": ("general", ["Are you willing to relocate?", "Willing to relocate?"]),
    "logistics.earliest_start_date": ("general", ["Earliest start date", "When can you start?", "Start date"]),
    "logistics.notice_period": ("general", ["Notice period", "What is your notice period?"]),
    "logistics.salary_expectation": ("salary", [
        "What are your salary expectations?", "Desired salary", "Expected compensation", "Salary expectation",
    ]),
    "eeo.gender": ("eeo", ["Gender", "What is your gender?"]),
    "eeo.race_ethnicity": ("eeo", ["Race", "Ethnicity", "Race/Ethnicity", "Are you Hispanic/Latino?"]),
    "eeo.veteran_status": ("eeo", ["Veteran status", "Are you a protected veteran?"]),
    "eeo.disability_status": ("eeo", ["Disability status", "Do you have a disability?"]),
}

# Short contact labels matched by pattern (the "normalized" tier).
_CONTACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("contact.first_name", re.compile(r"^(legal |preferred )?first name$|^given name$|^preferred name\b")),
    ("contact.last_name", re.compile(r"^(legal )?last name$|^family name$|^surname$")),
    ("contact.full_name", re.compile(r"^(full |legal |your )?name$")),
    ("contact.email", re.compile(r"^e ?mail( address)?$")),
    ("contact.phone", re.compile(r"^(mobile |cell )?phone( number)?$|^mobile$")),
    ("contact.linkedin", re.compile(r"^linkedin( profile)?( url)?$")),
    ("contact.github", re.compile(r"^github( profile)?( url)?$")),
    ("contact.website", re.compile(r"^(personal )?(website|portfolio)( url)?$|^other website$")),
    ("contact.location", re.compile(r"^(current )?location( city)?$|^city$")),
]


_PREFERRED_NAME = re.compile(
    r"^preferred (first )?name\b|name (you d|you would) prefer|what (would you like|should we) (us to )?call you"
)


class AnswerBank:
    def __init__(self, entries: list[BankEntry]):
        self.entries = entries
        self.by_key = {e.key: e for e in entries}

    @classmethod
    def from_answers(cls, data: dict[str, Any]) -> "AnswerBank":
        def get(path: str) -> Optional[str]:
            node: Any = data
            for part in path.split("."):
                node = node.get(part) if isinstance(node, dict) else None
            return None if node is None else str(node).strip()

        contact = data.get("contact") or {}
        full = " ".join(p for p in (contact.get("first_name"), contact.get("last_name")) if p).strip()
        entries: list[BankEntry] = []
        for key, (category, questions) in _CANONICAL.items():
            value = full if key == "contact.full_name" else get(key)
            entries.append(BankEntry(key, category, questions, value or ""))
        for i, item in enumerate(data.get("common_questions") or []):
            q = (item or {}).get("q")
            if q:
                a = item.get("a")
                entries.append(BankEntry(f"common.{i}", categorize(q), [q], None if a is None else str(a)))
        return cls(entries)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AnswerBank":
        path = path or config.project_root() / "config" / "answers.yaml"
        if not path.exists():
            raise FileNotFoundError(f"{path} not found; run `jobpilot init` and fill it in")
        return cls.from_answers(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def question_texts(self) -> list[tuple[BankEntry, str]]:
        return [(e, q) for e in self.entries for q in e.questions]


# --- matching ----------------------------------------------------------------


@dataclass
class Match:
    entry: BankEntry
    how: str  # exact | normalized | embedding
    score: float = 1.0


EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]
DraftFn = Callable[[str], Awaitable[Optional[str]]]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def match_static(label: str, bank: AnswerBank) -> Optional[Match]:
    """Exact, then normalized match (including short contact-label patterns)."""
    stripped = label.strip()
    for entry, q in bank.question_texts():
        if q == stripped:
            return Match(entry, "exact")
    norm = normalize(label)
    for entry, q in bank.question_texts():
        if normalize(q) == norm:
            return Match(entry, "normalized")
    # "What's the name you'd prefer us to use...?" is a first name, never an LLM draft.
    if _PREFERRED_NAME.search(norm):
        return Match(bank.by_key["contact.first_name"], "normalized")
    if len(norm) <= 40:
        for key, pattern in _CONTACT_PATTERNS:
            if pattern.search(norm):
                return Match(bank.by_key[key], "normalized")
    return None


async def match_embedding(
    labels: list[str], bank: AnswerBank, embed: EmbedFn, threshold: float
) -> dict[str, Match]:
    pairs = bank.question_texts()
    if not labels or not pairs:
        return {}
    vectors = await embed([*labels, *(q for _, q in pairs)])
    label_vecs, bank_vecs = vectors[: len(labels)], vectors[len(labels):]
    out: dict[str, Match] = {}
    for label, lv in zip(labels, label_vecs):
        best, best_score = None, 0.0
        for (entry, _), bv in zip(pairs, bank_vecs):
            s = _cosine(lv, bv)
            if s > best_score:
                best, best_score = entry, s
        if best is not None and best_score >= threshold:
            out[label] = Match(best, "embedding", round(best_score, 3))
    return out


# --- options ---------------------------------------------------------------

_DECLINE = ("decline", "prefer not", "don't wish", "do not wish", "not to answer", "rather not", "do not want to answer")


def choose_option(answer: str, options: list[str]) -> Optional[str]:
    """Pick the option that states `answer`; None when nothing clearly does."""
    a = normalize(answer)
    if not a or not options:
        return None
    norm = {opt: normalize(opt) for opt in options}
    for opt, n in norm.items():
        if n == a:
            return opt
    if a in ("yes", "no"):
        hits = [opt for opt, n in norm.items() if n.split()[:1] == [a]]
        return hits[0] if len(hits) == 1 else None
    if any(d in answer.lower() for d in _DECLINE):
        hits = [opt for opt in options if any(d in opt.lower() for d in _DECLINE)]
        return hits[0] if len(hits) == 1 else None
    hits = [opt for opt, n in norm.items() if n.startswith(a) or a.startswith(n)]
    if len(hits) == 1:
        return hits[0]
    a_tokens = set(a.split())
    scored = sorted(
        ((len(a_tokens & set(n.split())) / len(a_tokens | set(n.split())), opt) for opt, n in norm.items()),
        reverse=True,
    )
    if scored and scored[0][0] >= 0.6 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
        return scored[0][1]
    return None


# --- the plan ------------------------------------------------------------------


@dataclass
class Action:
    field_id: str
    kind: str
    label: str
    value: str  # text to type, option to pick, or a file path
    source: str  # bank | review | llm | resume | cover_letter
    detail: str = ""  # bank key / match method


@dataclass
class FillPlan:
    actions: list[Action] = field(default_factory=list)
    needs_human: list[str] = field(default_factory=list)
    drafted: dict[str, str] = field(default_factory=dict)  # label -> LLM draft
    skipped: list[str] = field(default_factory=list)
    wants_cover_letter: bool = False

    def answers(self) -> dict[str, str]:
        """label -> value for everything filled (files as file names)."""
        return {a.label: (Path(a.value).name if a.kind == "file" else a.value) for a in self.actions}


def classify_file(f: FormField) -> str:
    """resume | cover_letter | other, from label, id, and name."""
    text = f"{f.label} {f.html_id} {f.name}".lower()
    if "autofill" in text:
        return "other"  # Ashby's parse-my-resume helper, not the resume field
    if "cover" in text:
        return "cover_letter"
    if re.search(r"resume|\bcv\b|curriculum", text):
        return "resume"
    return "other"


def _fill_value(f: FormField, answer: str) -> Optional[str]:
    if f.kind in ("select", "radio", "checkbox"):
        return choose_option(answer, f.options)
    return answer  # text, textarea, combobox (options resolved in the page)


async def plan_fill(
    fields: list[FormField],
    bank: AnswerBank,
    resume_pdf: Optional[Path],
    cover_pdf: Optional[Path],
    embed: Optional[EmbedFn] = None,
    draft: Optional[DraftFn] = None,
    threshold: float = 0.82,
    overrides: Optional[dict[str, str]] = None,
) -> FillPlan:
    """Decide what goes in every field.

    `overrides` (label -> value) are answers the human already reviewed; on
    submit they are used verbatim instead of re-deriving anything.
    """
    plan = FillPlan()
    overrides = overrides or {}
    labels = {f.id: clean_label(f) for f in fields}

    static = {f.id: match_static(labels[f.id], bank) for f in fields if f.kind != "file"}
    unmatched = [labels[f.id] for f in fields if f.kind != "file" and static.get(f.id) is None]
    semantic = await match_embedding(unmatched, bank, embed, threshold) if embed else {}

    for f in fields:
        label = labels[f.id] or f.name or f.id
        if f.kind == "file":
            which = classify_file(f)
            if which == "resume":
                if resume_pdf:
                    plan.actions.append(Action(f.id, "file", label, str(resume_pdf), "resume"))
                else:
                    plan.needs_human.append(f"resume upload '{label}' but no tailored resume exists")
            elif which == "cover_letter":
                if cover_pdf:
                    plan.actions.append(Action(f.id, "file", label, str(cover_pdf), "cover_letter"))
                else:
                    plan.wants_cover_letter = True
                    if f.required:
                        plan.needs_human.append(f"required cover letter upload '{label}' has no letter")
            elif f.required:
                plan.needs_human.append(f"required upload '{label}' is not a resume or cover letter")
            continue

        category = categorize(label)
        sensitive = category in SENSITIVE_CATEGORIES

        if label in overrides:
            value = _fill_value(f, overrides[label])
            if value:
                plan.actions.append(Action(f.id, f.kind, label, value, "review", "reviewed answer"))
            elif f.required:
                plan.needs_human.append(f"reviewed answer for '{label}' no longer fits the form")
            continue

        m = static.get(f.id) or semantic.get(labels[f.id])
        # A sensitive question may only take an answer from its own category:
        # "require sponsorship?" must never be answered with "authorized to work".
        if m and sensitive and m.entry.category != category:
            m = None

        if m is not None and m.entry.answer is not None:
            if not m.entry.answer:
                # Known question, but answers.yaml leaves it empty: never guess.
                if f.required or sensitive:
                    plan.needs_human.append(f"'{label}': {m.entry.key} is empty in answers.yaml")
                else:
                    plan.skipped.append(label)
                continue
            value = _fill_value(f, m.entry.answer)
            if value:
                plan.actions.append(Action(f.id, f.kind, label, value, "bank", f"{m.entry.key} ({m.how})"))
            elif f.required or sensitive:
                plan.needs_human.append(f"'{label}': answer '{m.entry.answer}' matches no option")
            else:
                plan.skipped.append(label)
            continue

        if sensitive:
            # Blank is neutral for text/select/radio, but an unticked checkbox
            # ("authorized without sponsorship?") is itself an answer.
            if f.required or f.kind == "checkbox":
                plan.needs_human.append(f"{category} question '{label}' has no answer in answers.yaml")
            else:
                plan.skipped.append(label)
            continue

        # Novel question (or a common question whose answer is null): LLM draft for free
        # text. Optional boxes that aren't questions ("Additional information") stay empty.
        wants_draft = m is not None or f.required or label.rstrip().endswith("?")
        if wants_draft and f.kind in ("text", "textarea") and draft is not None:
            text = await draft(label)
            if text:
                plan.drafted[label] = text
                plan.actions.append(Action(f.id, f.kind, label, text, "llm", "drafted - review"))
                continue
        if f.required:
            plan.needs_human.append(f"required field '{label}' ({f.kind}) has no confident answer")
        else:
            plan.skipped.append(label)
    return plan
