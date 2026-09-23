"""Embedding similarity between the master resume and each job description."""

from __future__ import annotations

import math

from ..llm import LLMClient
from ..master_resume import MasterResume

# nomic-embed-text is trained with task prefixes; the resume is the query.
QUERY_PREFIX = "search_query: "
DOC_PREFIX = "search_document: "
MAX_DOC_CHARS = 6000


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def job_document(title: str, description: str) -> str:
    return f"{DOC_PREFIX}{title}\n{description[:MAX_DOC_CHARS]}"


async def embed_scores(
    llm: LLMClient, resume: MasterResume, jobs: list[tuple[int, str, str]]
) -> dict[int, float]:
    """Cosine similarity per job id for (id, title, description) triples."""
    if not jobs:
        return {}
    [resume_vec] = await llm.embed([QUERY_PREFIX + resume.flatten()])
    vectors = await llm.embed([job_document(t, d) for _, t, d in jobs])
    return {job_id: cosine(resume_vec, vec) for (job_id, _, _), vec in zip(jobs, vectors)}
