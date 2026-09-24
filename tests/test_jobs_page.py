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


def test_jobs_page_fetches_filters_and_tailors(jobs_db, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from jobpilot import runs

    started = []
    monkeypatch.setattr(runs, "start", lambda stage, args=None: started.append((stage, args)) or
                        type("R", (), {"id": 1})())

    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert at.title[0].value == "Jobs"
    assert at.dataframe[0].value.shape[0] == 2  # the screened-out job is hidden...
    at.checkbox(key="jobs-screened").check().run()
    assert at.dataframe[0].value.shape[0] == 3  # ...until asked for
    tailor = next(b for b in at.button if b.label.startswith("✨ Tailor resume"))
    assert tailor.disabled  # nothing ticked yet

    at.text_input(key="jobs-q").input("platform").run()
    assert list(at.dataframe[0].value["title"]) == ["Platform Engineer"]
    at.text_input(key="jobs-q").input("").run()
    at.multiselect(key="jobs-type").select("Backend").run()
    assert list(at.dataframe[0].value["title"]) == ["Backend Engineer"]

    next(b for b in at.button if b.label == "Fetch openings").click().run()
    assert started == [("fetch", ["--no-hn"])]


def test_ready_page_applies_and_writes_missing_cover_letters(jobs_db, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from jobpilot import db, runs
    from jobpilot.models import Event, Job

    started = []
    monkeypatch.setattr(runs, "start", lambda stage, args=None: started.append((stage, args)) or
                        type("R", (), {"id": 1})())
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.sidebar.radio(key="nav").set_value("Ready to apply").run()
    assert not at.exception
    assert at.title[0].value == "Ready to apply"
    assert any(b.label == "⬇ Resume PDF" for b in at.get("download_button"))

    # The fixture's letter exists only as text, not a PDF: offer to write one.
    next(b for b in at.button if b.label == "✨ Write cover letter").click().run()
    assert started == [("tailor", ["--job", str(jobs_db["c"]), "--cover-letter"])]

    next(b for b in at.button if b.label == "✅ I applied").click().run()
    assert not at.exception
    with db.session() as sess:
        assert sess.get(Job, jobs_db["c"]).status == "submitted"
        assert sess.exec(select(Event).where(Event.type == "applied_manually")).first() is not None


def test_screen_rules_and_rescreen_are_reversible(jobs_db):
    from jobpilot import config, db
    from jobpilot.filters.screen import rescreen, role_types, seniority, years_required
    from jobpilot.models import Job

    assert role_types("Generative AI, Backend Engineer") == ["Backend", "ML / AI"]
    assert role_types("Software Engineer II") == ["Software (general)"]
    assert (seniority("Senior Software Engineer"), seniority("Software Engineer I"),
            seniority("Staff Platform Engineer"), seniority("Engineering Manager")) == \
        ("Senior", "Entry / new grad", "Staff+", "Manager")
    assert years_required("3-5 years of experience; 7+ years of professional software experience") == 7
    assert years_required("a company with 150 years of history") is None

    config.save_filters({"title_include": [], "title_exclude": ["sales"], "locations_allow": [],
                         "companies_exclude": [], "max_years_experience": 0, "drop_sponsorship_blockers": True})
    report = rescreen()
    with db.session() as sess:
        b, c = sess.get(Job, jobs_db["b"]), sess.get(Job, jobs_db["c"])
        assert b.status == "filtered_out" and b.filter_reason == "title: excluded keyword 'sales'"
        assert c.status == "new"  # no location, but it has a tailored resume: never screened
    assert report.reasons["title"] == 1

    # Loosen the filters: the job comes straight back, nothing was deleted.
    config.save_filters({"title_include": [], "title_exclude": [], "locations_allow": [],
                         "drop_sponsorship_blockers": False})
    assert rescreen().restored == 1
    with db.session() as sess:
        assert sess.get(Job, jobs_db["b"]).status == "new"

    config.save_filters({"title_include": [], "locations_allow": [], "companies_exclude": ["Acme"]})
    rescreen()
    with db.session() as sess:
        assert sess.get(Job, jobs_db["a"]).filter_reason == "company: 'Acme' excluded"
