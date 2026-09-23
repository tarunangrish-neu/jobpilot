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
    # Worked (user, assistant) turns placed between the system prompt and the real
    # question. They are identical on every call, so Ollama reuses their KV cache.
    examples: tuple[tuple[str, str], ...] = ()

    def render(self, **variables: object) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": self.system.strip()}]
        for question, answer in self.examples:
            messages.append({"role": "user", "content": question.strip()})
            messages.append({"role": "assistant", "content": answer.strip()})
        messages.append({"role": "user", "content": Template(self.user).substitute(**variables).strip()})
        return messages


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


# A fictional candidate: the example teaches the moves, never facts to copy.
_TAILOR_EXAMPLE_Q = """
MASTER RESUME
SUMMARY VARIANTS
[backend] Backend engineer with 4 years building payment APIs in Java and PostgreSQL.
[data] Data engineer focused on batch pipelines and reporting.

EXPERIENCE
Software Engineer | Tidewater Bank | 2021 - 2024
  (tw-api) Responsible for maintaining the card authorization API written in Java, which handled 3,000 requests per second
  (tw-batch) Worked on nightly settlement batch jobs and helped reduce their runtime from 6 hours to 90 minutes using Spark
  (tw-oncall) Participated in the on-call rotation for 4 services
  (tw-docs) Wrote internal documentation for onboarding
Software Engineer Intern | Brightline Labs | Summer 2020
  (bl-dash) Built a React dashboard for internal sales metrics

PROJECTS
Ledger CLI | 2023
  (lc-rust) Wrote a double-entry bookkeeping CLI in Rust with property-based tests

JOB: Backend Engineer, Payments at Example Co
You will own high-throughput Java services behind our card-issuing platform, make our
settlement pipelines faster and more reliable, and join the on-call rotation.
Experience with Spark or other batch processing is a plus.
"""

_TAILOR_EXAMPLE_A = """
{"summary": "backend", "bullet_ids": ["tw-api", "tw-batch", "tw-oncall", "lc-rust"], "rephrasings": [{"id": "tw-api", "text": "Maintained the card authorization API written in Java, sustaining 3,000 requests per second"}, {"id": "tw-batch", "text": "Helped cut the runtime of nightly settlement batch jobs from 6 hours to 90 minutes, working on them in Spark"}, {"id": "tw-oncall", "text": "Served in the on-call rotation for 4 services"}]}
"""

TAILOR = Prompt(
    name="tailor",
    version=5,
    # No skills list (ordered in code), top 12 ids, at most 4 rephrasings, compact JSON.
    max_tokens=800,
    system="""
You are an expert technical resume writer. You tailor one candidate's master resume
to one job by SELECTING, ORDERING, and REPHRASING existing bullets. You never add
facts: no new employers, titles, dates, numbers, metrics, tools, technologies,
skills, or scope. Anything new is automatically rejected.

1. "bullet_ids": the 12 most relevant bullets, most relevant first. Every bullet stays
   on the resume; your ranking decides which come first within each role and which
   go if space runs out. Unlisted bullets follow in their original order.
   - Find the job's must-have requirements. Rank bullets that prove them highest.
   - Cover different requirements rather than several bullets proving the same one.
   - Prefer quantified bullets and recent roles.
2. "rephrasings" (at most 4, only for your top bullets, only when it clearly helps).
   Strong resume bullets follow the XYZ pattern, "accomplished X, measured by Y,
   by doing Z":
   - Start with a strong past-tense action verb (Built, Cut, Designed, Led,
     Migrated, Scaled). Drop filler like "Responsible for", "Worked on", "Various".
   - Put the result or metric early, then how it was done.
   - Where the bullet already describes something the job names, use the job's
     wording for it. Never use job wording for something the bullet does not say.
   - Keep EVERY number and technology from the original; add none. Keep the scope
     honest: "helped" stays "helped", "participated" never becomes "led".
   - A rephrasing reorders the original's clauses and strengthens its verbs; it
     removes NO clause. Keep every detail (stack, scale, users, ownership) at about
     the same length, with no added parentheses or keyword lists. A rewrite that
     drops a detail or is much shorter is rejected.
   - Never copy a bullet unchanged. If a bullet is already strong, leave it out.
3. "summary": the KEY (in square brackets) of the best-matching summary variant.

Reply with compact JSON on one line:
{"summary": "key", "bullet_ids": ["id", ...], "rephrasings": [{"id": "id", "text": "..."}]}
""",
    examples=((_TAILOR_EXAMPLE_Q, _TAILOR_EXAMPLE_A),),
    # The resume comes first: system + example + resume is the same for every job, so
    # a local model only has to process the job description fresh each time.
    user="""
MASTER RESUME
$resume

JOB: $title at $company
$description
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
