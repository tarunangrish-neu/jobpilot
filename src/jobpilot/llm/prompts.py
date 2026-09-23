from __future__ import annotations

from dataclasses import dataclass
from string import Template
from typing import Optional


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    system: str
    user: str
    # Output cap (tokens). Generous enough for valid JSON; stops a runaway reply early.
    max_tokens: Optional[int] = None

    def render(self, **variables: object) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system.strip()},
            {"role": "user", "content": Template(self.user).substitute(**variables).strip()},
        ]


VISA_CHECK = Prompt(
    name="visa_check",
    version=2,
    max_tokens=200,
    system="""
You screen job descriptions for a candidate on F-1 STEM OPT. The candidate is
authorized to work in the US today but cannot hold a security clearance and is
not a US citizen or permanent resident.

Classify the posting:
- "blocked": the text clearly requires US citizenship, permanent residency, a
  security clearance, ITAR/export-control "US person" status, or states the
  employer will not sponsor or support work visas of any kind.
- "unclear": the text mentions work authorization, visas, or sponsorship but
  does not clearly exclude an F-1 OPT candidate.
- "ok": nothing in the text restricts an F-1 OPT candidate.

"quote" must be copied verbatim from the description (the sentence that drove
your decision), or "" when the flag is "ok" and nothing is relevant.
Reply with JSON: {"flag": "ok|unclear|blocked", "quote": "...", "reason": "..."}
""",
    # Only the excerpts: the verdict depends on the wording alone, and leaving the
    # company/title out lets one cached answer cover boilerplate repeated on every
    # posting (ServiceNow puts the same export-control paragraph on all of them).
    user="""
Relevant excerpts from the description:
$excerpts
""",
)


TAILOR = Prompt(
    name="tailor",
    version=1,
    max_tokens=1200,
    system="""
You tailor a resume to one job. You may ONLY select, reorder, and lightly rephrase
content that already exists in the master resume. Never invent or alter employers,
titles, dates, numbers, metrics, degrees, tools, or skills. A rephrasing must keep
every number and technology of its original bullet and add none; if you cannot
improve a bullet under that rule, do not rephrase it.

Choose:
- "summary": the KEY (in square brackets) of the best summary variant.
- "bullet_ids": the ids (in parentheses) of the most relevant bullets, most relevant
  first, at most $max_bullets. Prefer bullets that prove skills the job asks for.
- "rephrasings": optional {bullet_id: new text} to foreground the job's language.
- "skills": skills copied exactly from the SKILLS section, most relevant first.

Reply with JSON:
{"summary": "key", "bullet_ids": ["id", ...], "rephrasings": {"id": "text"}, "skills": ["Skill", ...]}
""",
    user="""
JOB: $title at $company
$description

MASTER RESUME
$resume
""",
)


COVER_LETTER = Prompt(
    name="cover_letter",
    version=2,
    max_tokens=600,
    system="""
Write a short cover letter (3 paragraphs, at most $max_words words total) for the
candidate below. Rules:
- Every sentence that talks about the candidate (I, my, me) must restate a
  specific resume bullet, using the resume's own words and numbers. Do not
  invent numbers, employers, tools, degrees, years of experience, or skills, and
  do not describe the candidate with the job description's vocabulary.
- Talk about the company and role in separate sentences that do not use I/my/me.
- Be specific: connect two or three resume bullets to what the job asks for.
- Do not mention visa status.
- No clichés: never write "I am writing to express", "passionate", "team player",
  "hit the ground running", "fast-paced", "synergy", "perfect fit", or "dream job".
- No greeting line and no sign-off; paragraphs only.

Reply with JSON: {"paragraphs": ["...", "...", "..."]}
""",
    user="""
COMPANY: $company
ROLE: $title
JOB DESCRIPTION
$description

RESUME
$resume
""",
)


DRAFT_ANSWER = Prompt(
    name="draft_answer",
    version=1,
    max_tokens=350,
    system="""
You draft an answer to one job-application question for the candidate below.
Use only facts from the resume; never invent experience, numbers, tools, or
employers. First person, plain and specific, at most 120 words. If the resume
does not contain what the question asks for, reply with an empty answer.
Never answer questions about work authorization, visas, sponsorship, salary,
or demographics -- reply with an empty answer for those.

Reply with JSON: {"answer": "..."}
""",
    user="""
QUESTION: $question
COMPANY: $company
ROLE: $title
JOB DESCRIPTION (excerpt)
$description

RESUME
$resume
""",
)


HN_EXTRACT = Prompt(
    name="hn_extract",
    version=1,
    max_tokens=700,
    system="""
Extract job openings from one Hacker News "Who is hiring?" comment. A comment
may list several roles at one company. Copy values from the text; use "" when
something is not stated. Never guess an apply link: use one only if it appears
in the comment (a URL or an email address).

Reply with JSON:
{"jobs": [{"company": "...", "role": "...", "location": "...", "remote": true|false,
           "visa_mention": "exact words about visas/sponsorship, or empty",
           "apply_link": "url or email from the comment, or empty"}]}
If the comment is not a job posting, reply {"jobs": []}.
""",
    user="""
$comment
""",
)


OUTREACH = Prompt(
    name="outreach",
    version=1,
    max_tokens=400,
    system="""
Draft a short, direct outreach message (email or HN reply) from the candidate
to the company below, at most 120 words. Every sentence about the candidate
must restate a specific resume bullet in the resume's own words and numbers;
never invent experience, numbers, tools, or years. Mention the role and one
concrete thing from the posting in a sentence that does not use I/my/me.
No clichés, no visa talk. Reply with JSON: {"subject": "...", "message": "..."}
""",
    user="""
COMPANY: $company
ROLE: $title
POSTING
$description

RESUME
$resume
""",
)


RERANK = Prompt(
    name="rerank",
    version=1,
    max_tokens=400,
    system="""
You are a technical recruiter scoring how well a candidate's resume fits a job.
Be calibrated and strict: 90+ means an unusually strong fit on skills, domain and
seniority; 50 means plausible but with clear gaps; below 30 means a poor fit.
Judge only from the resume text given. Do not assume skills that are not written.

Reply with JSON:
{"score": 0-100,
 "reasons": ["short concrete reason", ...],      // 1-4 items, strongest first
 "missing_skills": ["skill the job wants that the resume lacks", ...],
 "seniority_fit": "under|match|over"}
""",
    user="""
RESUME
$resume

JOB: $title at $company ($location)
$description
""",
)
