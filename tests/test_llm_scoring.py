"""LLM client contract (retry, validation, cache, log) and the score stage."""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest
from pydantic import BaseModel

from conftest import REPO


class Answer(BaseModel):
    value: int


def test_invalid_json_gets_one_corrective_retry(fake_llm):
    from jobpilot.llm.prompts import Prompt

    replies = iter(["sure! here you go", '```json\n{"value": 7}\n```'])
    fake_llm.fake.reply = lambda m: next(replies)
    prompt = Prompt("t", 1, "sys", "q=$q")

    out = asyncio.run(fake_llm.complete_json(prompt, Answer, q="x"))
    assert out.value == 7
    assert len(fake_llm.fake.chat_calls) == 2
    # the retry tells the model what was wrong
    assert "not valid JSON" in fake_llm.fake.chat_calls[1][-1]["content"]


def test_schema_mismatch_exhausts_budget_and_raises(fake_llm):
    from jobpilot.llm import LLMError
    from jobpilot.llm.prompts import Prompt

    fake_llm.fake.reply = lambda m: '{"value": "not a number"}'
    with pytest.raises(LLMError):
        asyncio.run(fake_llm.complete_json(Prompt("t", 1, "s", "u"), Answer))
    assert len(fake_llm.fake.chat_calls) == 2  # max_retries=1


def test_cache_hit_skips_model_and_version_bump_misses(fake_llm, temp_root):
    from jobpilot.llm.prompts import Prompt

    fake_llm.fake.reply = lambda m: '{"value": 1}'
    p1 = Prompt("t", 1, "s", "u=$u")
    asyncio.run(fake_llm.complete_json(p1, Answer, u="a"))
    asyncio.run(fake_llm.complete_json(p1, Answer, u="a"))
    assert len(fake_llm.fake.chat_calls) == 1
    asyncio.run(fake_llm.complete_json(Prompt("t", 2, "s", "u=$u"), Answer, u="a"))
    assert len(fake_llm.fake.chat_calls) == 2

    log_lines = (temp_root / "logs" / "llm.jsonl").read_text().splitlines()
    assert len(log_lines) == 2
    assert json.loads(log_lines[0])["prompt"] == "t"


def test_embeddings_are_cached(fake_llm):
    asyncio.run(fake_llm.embed(["a b", "c d", "a b"]))
    calls = fake_llm.fake.embed_calls
    asyncio.run(fake_llm.embed(["c d"]))
    assert fake_llm.fake.embed_calls == calls


def test_migration_adds_new_columns_to_an_old_database(temp_root):
    """A DB created by M1 must gain the M2 columns without losing rows."""
    import sqlite3

    from jobpilot import db

    path = temp_root / "data" / "jobpilot.db"
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, company_id INTEGER, source VARCHAR, "
        "external_id VARCHAR, title VARCHAR, location VARCHAR, remote BOOLEAN, url VARCHAR, "
        "apply_url VARCHAR, description_text VARCHAR, posted_at DATETIME, fetched_at DATETIME, "
        "content_hash VARCHAR, status VARCHAR, filter_reason VARCHAR, visa_flag VARCHAR, "
        "embed_score FLOAT, llm_score FLOAT, llm_reason VARCHAR)"
    )
    con.execute("INSERT INTO jobs (id, source, external_id, status) VALUES (1, 'lever', 'x', 'new')")
    con.commit()
    con.close()

    db.init_db()
    from jobpilot.models import Job

    with db.session() as sess:
        job = sess.get(Job, 1)
        assert job.filtered_at is None
        assert job.llm_details_json == "{}"
        assert job.visa_reason == ""


def test_final_score_weights_and_lca_boost():
    from jobpilot.scoring.rank import final_score

    cfg = {"weights": {"embed": 0.3, "llm": 0.7}, "lca_boost": 5.0}
    assert final_score(0.5, 80, False, cfg) == pytest.approx(0.3 * 50 + 0.7 * 80)
    assert final_score(0.5, 80, True, cfg) == pytest.approx(0.3 * 50 + 0.7 * 80 + 5)


def test_score_stage_reranks_only_top_n(fake_llm, temp_root):
    from sqlmodel import select

    from jobpilot import db, master_resume
    from jobpilot.models import Job, JobPosting
    from jobpilot.scoring.rank import ranked, run_scoring

    shutil.copy(REPO / "config" / "master_resume.example.yaml", temp_root / "resume.yaml")
    resume = master_resume.load(temp_root / "resume.yaml")

    descs = {
        "a": "Go ledger payments Kafka PostgreSQL Rust settlement",
        "b": "Rust payments ledger Go",
        "c": "Marketing brand campaigns social media",
    }
    with db.session() as sess:
        for ext, desc in descs.items():
            db.upsert_posting(
                sess,
                JobPosting(source="ashby", external_id=ext, company_name="Acme", title="Backend Engineer", description_text=desc),
                None,
            )
        for job in sess.exec(select(Job)).all():
            job.filtered_at = job.fetched_at
            job.visa_flag = "ok"
        sess.commit()

    fake_llm.fake.reply = lambda m: json.dumps(
        {"score": 90 if "Kafka" in m[-1]["content"] else 60, "reasons": ["payments ledger experience"],
         "missing_skills": ["Scala"], "seniority_fit": "Match"}
    )
    report = asyncio.run(run_scoring(fake_llm, resume, top_n=2))
    assert (report.candidates, report.reranked) == (3, 2)

    with db.session() as sess:
        jobs = {j.external_id: j for j in sess.exec(select(Job)).all()}
        assert jobs["c"].status == "new" and jobs["c"].llm_score is None
        assert jobs["c"].embed_score is not None
        assert jobs["a"].status == "scored" and jobs["a"].llm_score == 90
        order = [r.job.external_id for r in ranked(sess)]
        assert order == ["a", "b"]
        assert ranked(sess)[0].details["missing_skills"] == ["Scala"]
        assert ranked(sess)[0].details["seniority_fit"] == "match"
