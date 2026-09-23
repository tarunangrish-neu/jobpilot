"""M7 HN source (trimmed real Algolia payloads, 2026-09-23) and the review UI."""

from __future__ import annotations

import json

import pytest
from sqlmodel import select

from conftest import REPO, load
from jobpilot.sources import hn

# --- HN parsing -------------------------------------------------------------


def test_latest_thread_is_the_newest_who_is_hiring_story():
    thread = hn.latest_thread(load("hn_search.json"))
    assert thread["title"] == "Ask HN: Who is hiring? (September 2026)"
    assert thread["objectID"] == "49522897"


def test_top_level_comments_are_plain_text():
    comments = hn.top_level_comments(load("hn_item.json"))
    assert len(comments) == 4
    assert comments[0]["text"].startswith("Modash.io | Senior Product Engineer")
    assert "<p>" not in comments[0]["text"] and "&#x2F;" not in comments[0]["text"]


def test_keyword_prefilter():
    assert hn.keep_comment("Senior Platform & DevOps Engineer", ["platform"])
    assert not hn.keep_comment("Senior Product Designer", ["platform", "backend"])


def test_postings_keep_only_links_present_in_the_comment():
    comment = {"id": "1", "text": "Acme | Backend Engineer | NYC | email jobs@acme.dev", "created_at": "2026-09-01T00:00:00Z"}
    extract = hn.HNExtract(jobs=[
        hn.HNJob(company="Acme", role="Backend Engineer", location="NYC", apply_link="jobs@acme.dev"),
        hn.HNJob(company="Acme", role="Infra Engineer", location="NYC", apply_link="https://acme.dev/careers"),  # invented
        hn.HNJob(company="", role="?"),
    ])
    postings = hn.to_postings(comment, extract)
    assert [p.external_id for p in postings] == ["1-0", "1-1"]
    assert postings[0].apply_url == "mailto:jobs@acme.dev"
    assert postings[1].apply_url == ""  # the model made that URL up
    assert postings[0].url == "https://news.ycombinator.com/item?id=1"
    assert postings[0].source == "hn"


# --- review UI --------------------------------------------------------------

APP = str(REPO / "src" / "jobpilot" / "ui" / "review_app.py")


@pytest.fixture
def ui_db(temp_root):
    from jobpilot import db
    from jobpilot.models import Application, Job, JobPosting

    db.init_db()
    with db.session() as sess:
        db.sync_companies(sess, [{"name": "Acme", "ats": "lever", "token": "acme"}])
        for ext, title in (("a", "Backend Engineer"), ("b", "Platform Engineer")):
            db.upsert_posting(sess, JobPosting(source="lever", external_id=ext, company_name="Acme", title=title,
                                               location="New York, NY", description_text="Build ledgers."), 1)
        jobs = {j.external_id: j for j in sess.exec(select(Job)).all()}
        for ext, (status, score) in {"a": ("prefilled", 80.0), "b": ("scored", 70.0)}.items():
            jobs[ext].status, jobs[ext].final_score, jobs[ext].visa_flag = status, score, "ok"
            jobs[ext].llm_details_json = json.dumps({"reasons": ["ledger work"], "missing_skills": ["Scala"], "seniority_fit": "match"})
        sess.add(Application(
            job_id=jobs["a"].id,
            answers_json=json.dumps({"Full name": "Alex Example", "Why Acme?": "I built ledgers."}),
            llm_drafted_answers_json=json.dumps({"Why Acme?": "I built ledgers."}),
        ))
        sess.commit()
        return {k: j.id for k, j in jobs.items()}


def _button(at, label):
    return next(b for b in at.button if b.label == label)


def test_ui_renders_queue_and_gates_approval(ui_db, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from jobpilot import db
    from jobpilot.apply import submit
    from jobpilot.models import Event, Job

    launched = []
    monkeypatch.setattr(submit, "launch_submit", lambda job_id: launched.append(job_id))

    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("Backend Engineer" in s.value for s in at.subheader)  # top-ranked job shown first
    assert _button(at, "Approve & Submit").disabled  # not until the review box is ticked
    assert any("LLM-drafted" in w.value for w in at.warning)

    at.checkbox[0].check().run()
    assert not _button(at, "Approve & Submit").disabled
    _button(at, "Approve & Submit").click().run()
    assert not at.exception
    assert launched == [ui_db["a"]]
    with db.session() as sess:
        assert sess.get(Job, ui_db["a"]).status == "approved"
        approvals = sess.exec(select(Event).where(Event.type == "approved")).all()
        assert [json.loads(e.payload_json)["source"] for e in approvals] == ["review_ui"]


def test_ui_skip_and_stats(ui_db):
    from streamlit.testing.v1 import AppTest

    from jobpilot import db
    from jobpilot.models import Job

    at = AppTest.from_file(APP, default_timeout=30).run()
    at.selectbox[0].select(ui_db["b"]).run()
    _button(at, "Skip").click().run()
    assert not at.exception
    with db.session() as sess:
        assert sess.get(Job, ui_db["b"]).status == "skipped"

    at.sidebar.radio[0].set_value("Stats").run()
    assert not at.exception
    assert [m.label for m in at.metric][:4] == ["Submitted", "Rejected", "Interviewing", "Offer"]
