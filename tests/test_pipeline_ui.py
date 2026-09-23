"""UI-first pipeline: background runs of stages, and the Pipeline landing page."""

from __future__ import annotations

import json
import os
import time

import pytest
from sqlmodel import select

from conftest import REPO

APP = str(REPO / "src" / "jobpilot" / "ui" / "review_app.py")


@pytest.fixture
def quiet_machine(monkeypatch):
    """Pipelines really running on this machine must not affect the tests."""
    from jobpilot import runs

    monkeypatch.setattr(runs, "external_pipelines", lambda: [])
    return runs


def _wait_done(run_id, timeout=90):
    from jobpilot import db
    from jobpilot.models import Run

    deadline = time.time() + timeout
    while time.time() < deadline:
        with db.session() as sess:
            run = sess.get(Run, run_id)
            if run.status != "running":
                return run
        time.sleep(0.5)
    raise AssertionError("run did not finish")


def test_stage_runs_in_background_and_records_its_result(temp_root, quiet_machine):
    from jobpilot import db

    db.init_db()
    run = quiet_machine.start("filter", ["--no-llm"])
    assert run.pid and run.log_path.endswith("-filter.log")
    done = _wait_done(run.id)
    assert (done.status, done.exit_code) == ("done", 0)
    assert "considered 0" in quiet_machine.log_tail(done)
    assert "$ jobpilot filter --no-llm" in quiet_machine.log_tail(done)


def test_failed_stage_is_recorded_as_failed(temp_root, quiet_machine):
    from jobpilot import db

    db.init_db()
    # `score` with no master resume exits 1 with a message.
    done = _wait_done(quiet_machine.start("score", ["--top", "5"]).id)
    assert (done.status, done.exit_code) == ("failed", 1)


def test_only_one_stage_at_a_time(temp_root, quiet_machine):
    from jobpilot import db
    from jobpilot.models import Run

    db.init_db()
    with db.session() as sess:
        sess.add(Run(stage="score", pid=os.getpid(), status="running"))  # alive: this process
        sess.commit()
    assert "score" in quiet_machine.busy()
    with pytest.raises(RuntimeError, match="not starting tailor"):
        quiet_machine.start("tailor", [])


def test_dead_worker_is_marked_failed(temp_root, quiet_machine):
    from jobpilot import db
    from jobpilot.models import Run

    db.init_db()
    with db.session() as sess:
        sess.add(Run(stage="fetch", pid=999_999, status="running"))
        sess.commit()
    assert quiet_machine.refresh() == []
    assert quiet_machine.recent()[0].status == "failed"
    assert quiet_machine.busy() is None


def test_unknown_stage_is_rejected(temp_root, quiet_machine):
    with pytest.raises(ValueError):
        quiet_machine.start("submit", [])  # there is no batch submit, ever


def test_funnel_counts_each_stage(temp_root):
    from jobpilot import db
    from jobpilot.models import Application, Job, JobPosting
    from jobpilot.ui import actions

    db.init_db()
    with db.session() as sess:
        for i, (status, filtered) in enumerate([("new", False), ("new", True), ("filtered_out", True),
                                                ("tailored", True), ("prefilled", True)]):
            db.upsert_posting(sess, JobPosting(source="lever", external_id=str(i), company_name="A", title=f"T{i}"), None)
            sess.flush()
            job = sess.exec(select(Job).where(Job.external_id == str(i))).one()
            job.status = status
            job.filtered_at = job.fetched_at if filtered else None
            if status == "tailored":
                sess.add(Application(job_id=job.id, dry_run_json=json.dumps({"rows": []})))
        sess.commit()
    counts = actions.funnel()["counts"]
    assert counts["fetched"] == 5
    assert (counts["awaiting_filter"], counts["awaiting_score"], counts["filtered_out"]) == (1, 1, 1)
    assert (counts["tailored"], counts["dry_run"], counts["prefilled"]) == (1, 1, 1)
    hints = dict(actions.next_steps(counts))
    assert {"filter", "score", "review"} <= set(hints)


def test_pipeline_page_renders_and_pauses_while_busy(temp_root, quiet_machine):
    from streamlit.testing.v1 import AppTest

    from jobpilot import db
    from jobpilot.models import Run

    db.init_db()
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert at.title[0].value == "Pipeline"
    labels = [m.label for m in at.metric]
    assert "Waiting for filter" in labels and "Needs you" in labels
    run_buttons = [b for b in at.button if b.label.startswith("Run ")]
    assert len(run_buttons) == 6
    # Prefill stays disabled until the upload warning is acknowledged.
    assert next(b for b in run_buttons if b.label == "Run prefill").disabled
    assert not next(b for b in run_buttons if b.label == "Run filter").disabled

    with db.session() as sess:
        sess.add(Run(stage="score", pid=os.getpid(), status="running"))
        sess.commit()
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert all(b.disabled for b in at.button if b.label.startswith("Run "))
