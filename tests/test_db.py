"""Database behaviour, above all the idempotency the M1 acceptance check requires."""

from __future__ import annotations

from sqlmodel import func, select

from jobpilot.models import JobPosting


def _posting(external_id="123", title="Backend Engineer"):
    return JobPosting(
        source="greenhouse",
        external_id=external_id,
        company_name="Acme",
        title=title,
        location="Remote - US",
        url="https://example.com/job/123",
        apply_url="https://example.com/job/123",
        description_text="Build payment rails.",
    )


def test_init_creates_all_four_tables(temp_root):
    from jobpilot import db

    db.init_db()
    from sqlalchemy import inspect

    tables = set(inspect(db.get_engine()).get_table_names())
    assert {"companies", "jobs", "applications", "events"} <= tables


def test_refetching_the_same_posting_creates_no_duplicate(temp_root):
    from jobpilot import db
    from jobpilot.models import Job

    db.init_db()
    with db.session() as sess:
        assert db.upsert_posting(sess, _posting(), None) == "inserted"
        sess.commit()
        assert db.upsert_posting(sess, _posting(), None) == "updated"
        sess.commit()
        count = sess.exec(select(func.count()).select_from(Job)).one()

    assert count == 1


def test_refetch_preserves_pipeline_state(temp_root):
    """A re-fetch must not knock a tailored job back to 'new'."""
    from jobpilot import db
    from jobpilot.models import Job

    db.init_db()
    with db.session() as sess:
        db.upsert_posting(sess, _posting(), None)
        sess.commit()

        job = sess.exec(select(Job)).one()
        db.set_status(sess, job, "tailored")
        job.llm_score = 88.0
        sess.add(job)
        sess.commit()

        # Same posting comes back with an edited title.
        db.upsert_posting(sess, _posting(title="Senior Backend Engineer"), None)
        sess.commit()

        job = sess.exec(select(Job)).one()
        assert job.status == "tailored"
        assert job.llm_score == 88.0
        assert job.title == "Senior Backend Engineer"  # refreshable field updated


def test_status_changes_are_audited(temp_root):
    from jobpilot import db
    from jobpilot.models import Event, Job

    db.init_db()
    with db.session() as sess:
        db.upsert_posting(sess, _posting(), None)
        sess.commit()
        job = sess.exec(select(Job)).one()
        db.set_status(sess, job, "skipped", reason="wrong seniority")
        sess.commit()

        events = sess.exec(select(Event).where(Event.type == "status_changed")).all()
        assert len(events) == 1
        assert "wrong seniority" in events[0].payload_json


def test_sync_companies_is_idempotent(temp_root):
    from jobpilot import config, db
    from jobpilot.models import Company

    db.init_db()
    entries = config.companies()
    with db.session() as sess:
        first = db.sync_companies(sess, entries)
        second = db.sync_companies(sess, entries)
        count = sess.exec(select(func.count()).select_from(Company)).one()

    assert first == second
    assert count == len(entries)
