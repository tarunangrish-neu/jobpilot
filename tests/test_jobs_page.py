"""The Jobs page: every fetched opening, tailor on demand, apply on the company's site."""

from __future__ import annotations

import pytest
from sqlmodel import select

from conftest import REPO

APP = str(REPO / "src" / "jobpilot" / "ui" / "review_app.py")


@pytest.fixture
def jobs_db(temp_root, monkeypatch):
    from jobpilot import db, runs
    from jobpilot.models import Application, Job, JobPosting

    monkeypatch.setattr(runs, "external_pipelines", lambda: [])
    db.init_db()
    with db.session() as sess:
        db.sync_companies(sess, [{"name": "Acme", "ats": "lever", "token": "acme"},
                                 {"name": "Acme", "ats": "ashby", "token": "acme"}])
        same = dict(company_name="Acme", title="Backend Engineer", location="New York, NY",
                    description_text="Build ledgers.")
        db.upsert_posting(sess, JobPosting(source="lever", external_id="a", url="https://jobs.lever.co/acme/a",
                                           apply_url="https://jobs.lever.co/acme/a/apply", **same), 1)
        # The same role cross-posted to a second board: one row, not two.
        db.upsert_posting(sess, JobPosting(source="ashby", external_id="a2", url="https://jobs.ashbyhq.com/acme/a2",
                                           **same), 2)
        db.upsert_posting(sess, JobPosting(source="lever", external_id="b", company_name="Acme", title="Sales Lead",
                                           url="https://jobs.lever.co/acme/b", location="Tokyo",
                                           description_text="We will not sponsor visas."), 1)
        db.upsert_posting(sess, JobPosting(source="lever", external_id="c", company_name="Acme",
                                           title="Platform Engineer", url="https://jobs.lever.co/acme/c"), 1)
        sess.flush()
        jobs = {j.external_id: j for j in sess.exec(select(Job)).all()}
        jobs["b"].status = "filtered_out"  # an earlier filter run dropped it; it still shows
        resume = temp_root / "output" / str(jobs["c"].id) / "Alex_Resume.pdf"
        resume.parent.mkdir(parents=True)
        resume.write_bytes(b"%PDF-1.4")
        sess.add(Application(job_id=jobs["c"].id, resume_path=str(resume), cover_letter_text="Dear Acme, ..."))
        sess.commit()
        return {k: j.id for k, j in jobs.items()}


def test_openings_lists_every_job_once_with_apply_links_and_blockers(jobs_db):
    from jobpilot.ui import actions

    rows = {r["id"]: r for r in actions.openings()}
    assert set(rows) == {jobs_db["a"], jobs_db["b"], jobs_db["c"]}  # the cross-post collapsed
    assert rows[jobs_db["a"]]["apply"] == "https://jobs.lever.co/acme/a/apply"
    assert rows[jobs_db["c"]]["apply"] == "https://jobs.lever.co/acme/c"  # no apply_url: the posting
    assert rows[jobs_db["b"]]["sponsorship"] == "blocked: will not sponsor"
    assert rows[jobs_db["b"]]["status"] == "filtered_out"
    assert [r["tailored"] for r in (rows[jobs_db["a"]], rows[jobs_db["c"]])] == [False, True]


def test_tailor_args_and_new_jobs_are_tailorable(jobs_db):
    from jobpilot.tailor import _load_jobs
    from jobpilot.ui import actions

    assert actions.tailor_args([3, 7]) == ["--job", "3", "--job", "7", "--cover-letter"]
    # Straight from fetch (never filtered or ranked), and ones the old filter dropped.
    assert {j.id for j in _load_jobs(0, [jobs_db["a"], jobs_db["b"]])} == {jobs_db["a"], jobs_db["b"]}


def test_jobs_page_fetches_lists_and_marks_applied(jobs_db, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from jobpilot import db, runs
    from jobpilot.models import Event, Job

    started = []
    monkeypatch.setattr(runs, "start", lambda stage, args=None: started.append((stage, args)) or
                        type("R", (), {"id": 1})())

    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert at.title[0].value == "Jobs"
    assert at.dataframe[0].value.shape[0] == 3
    assert next(b for b in at.button if b.label.startswith("Tailor resume")).disabled  # nothing ticked

    at.text_input(key="jobs-q").input("platform").run()
    assert list(at.dataframe[0].value["title"]) == ["Platform Engineer"]

    next(b for b in at.button if b.label == "Fetch openings").click().run()
    assert started == [("fetch", ["--no-hn"])]

    # Ready to apply: the tailored job, with its resume and an "I applied" button.
    next(b for b in at.button if b.label == "I applied").click().run()
    assert not at.exception
    with db.session() as sess:
        assert sess.get(Job, jobs_db["c"]).status == "submitted"
        assert sess.exec(select(Event).where(Event.type == "applied_manually")).first() is not None
