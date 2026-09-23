"""Filter stage: rules, visa screen, dedupe, LCA import."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
import yaml

from jobpilot.filters import rules, visa

from conftest import REPO

SETTINGS = yaml.safe_load((REPO / "config" / "settings.yaml").read_text())
CFG = SETTINGS["filters"]
PHRASES = SETTINGS["visa"]["blocking_phrases"]


# --- location ------------------------------------------------------------
# Strings below are real values from the M1 fetch.


@pytest.mark.parametrize(
    "location,remote",
    [
        ("San Francisco", False),
        ("New York, NY (HQ), San Francisco, CA", True),
        ("Remote, United States", True),
        ("Remote, Canada; Remote, United States", True),
        ("Remote - US, Remote - Canada", True),
        ("Mountain View, California; San Francisco, California", False),
        ("Palo Alto, CA", False),
        ("Washington, D.C.", False),
        ("Bellevue, Washington", False),
        ("Remote", True),
        ("London; New York", False),
    ],
)
def test_us_locations_pass(location, remote):
    assert rules.check_location(location, remote, CFG) is None


@pytest.mark.parametrize(
    "location,remote",
    [
        ("London, United Kingdom", False),
        ("Bengaluru, India", False),
        ("Remote, United Kingdom", True),
        ("Tokyo, Japan", True),
        ("Remote - Japan", True),
        ("Remote, Canada", True),
        ("Singapore", True),
        ("N/A", False),
        ("Remote - Belarus", True),  # "us" inside a word is not the US
    ],
)
def test_non_us_locations_drop(location, remote):
    assert rules.check_location(location, remote, CFG) is not None


def test_remote_anywhere_switch_only_affects_remote_jobs():
    cfg = {**CFG, "allow_remote_anywhere": True}
    assert rules.check_location("Remote - Japan", True, cfg) is None
    assert rules.check_location("Tokyo, Japan", False, cfg) is not None


# --- title / age ---------------------------------------------------------


@pytest.mark.parametrize(
    "title,ok",
    [
        ("Backend Engineer, Payments", True),
        ("Software Engineer, Infrastructure", True),
        ("Staff Software Engineer", False),
        ("Software Engineering Intern", False),
        ("Frontend Engineer", False),
        ("Engineering Manager, Platform", False),
        ("Account Executive", False),
        ("Founding Engineer", True),
    ],
)
def test_title_rules(title, ok):
    assert (rules.check_title(title, CFG) is None) is ok


def test_age_rule():
    from jobpilot.models import utcnow

    now = utcnow()
    assert rules.check_age(now - timedelta(days=5), CFG, now) is None
    assert "age" in rules.check_age(now - timedelta(days=45), CFG, now)
    assert rules.check_age(None, CFG, now) is None  # unknown date is kept


# --- visa ------------------------------------------------------------------


def test_blocking_phrase_found_with_sentence():
    text = "About us.\nWe are unable to sponsor visas for this role.\nBenefits."
    phrase, sentence = visa.find_blocking_phrase(text, PHRASES)
    assert phrase == "unable to sponsor"
    assert sentence == "We are unable to sponsor visas for this role."


def test_blocking_phrase_needs_word_boundaries():
    # "itar" must not fire inside "guitars".
    assert visa.find_blocking_phrase("We use the Guitars API.", PHRASES) is None
    assert visa.find_blocking_phrase("This role is subject to ITAR.", PHRASES)[0] == "itar"


def test_excerpts_only_when_topic_mentioned():
    assert visa.relevant_excerpts("Build payments. Great benefits.") == []
    text = "Build things.\nCandidates must be authorized to work in the US.\nPerks."
    assert visa.relevant_excerpts(text) == ["Candidates must be authorized to work in the US."]


def test_llm_quote_that_is_not_verbatim_is_discarded(fake_llm):
    fake_llm.fake.reply = lambda m: json.dumps(
        {"flag": "unclear", "quote": "we never hire foreigners", "reason": "x"}
    )
    verdict = asyncio.run(
        visa.llm_check(fake_llm, "Acme", "Backend", ["Must be authorized to work in the US."])
    )
    assert verdict.flag == "unclear"
    assert verdict.quote == ""


# --- full stage ------------------------------------------------------------


def _add(sess, ext, title="Backend Engineer", location="New York, NY", desc="Build ledgers."):
    from jobpilot import db
    from jobpilot.models import JobPosting

    db.upsert_posting(
        sess,
        JobPosting(
            source="greenhouse",
            external_id=ext,
            company_name="Acme",
            title=title,
            location=location,
            description_text=desc,
        ),
        None,
    )


def test_run_filters_end_to_end(fake_llm):
    from sqlmodel import select

    from jobpilot import db
    from jobpilot.filters import run_filters
    from jobpilot.models import Event, Job

    def reply(messages):
        text = messages[-1]["content"]
        if "US person" in text:
            return json.dumps({"flag": "blocked", "quote": "Applicants must be a US person.", "reason": "export"})
        return json.dumps({"flag": "unclear", "quote": "", "reason": "mentions authorization"})

    fake_llm.fake.reply = reply
    with db.session() as sess:
        _add(sess, "1")
        _add(sess, "2", desc="Build ledgers.")  # same content as 1 -> duplicate
        _add(sess, "3", title="Staff Backend Engineer", desc="x")
        _add(sess, "4", location="London, UK", desc="y")
        _add(sess, "5", desc="We will not sponsor visas.")
        _add(sess, "6", desc="Applicants must be a US person.")
        _add(sess, "7", desc="You must be authorized to work in the US.")
        sess.commit()

    report = asyncio.run(run_filters(fake_llm))
    assert report.considered == 7
    assert report.passed == 2

    with db.session() as sess:
        jobs = {j.external_id: j for j in sess.exec(select(Job)).all()}
        assert jobs["1"].status == "new" and jobs["1"].filtered_at is not None
        assert jobs["1"].visa_flag == "ok"
        assert jobs["2"].filter_reason == f"duplicate of job #{jobs['1'].id}"
        assert jobs["3"].filter_reason.startswith("title:")
        assert jobs["4"].filter_reason.startswith("location:")
        assert jobs["5"].visa_flag == "blocked" and jobs["5"].status == "filtered_out"
        assert jobs["6"].visa_flag == "blocked" and "US person" in jobs["6"].visa_reason
        assert jobs["7"].visa_flag == "unclear" and jobs["7"].status == "new"
        # every drop is in the audit log
        changes = sess.exec(select(Event).where(Event.type == "status_changed")).all()
        assert len(changes) == 5

    # The phrase list caught job 5 without a model; only 6 and 7 reached the LLM.
    assert len(fake_llm.fake.chat_calls) == 2

    # Re-running is a no-op: already-filtered jobs are not reconsidered.
    again = asyncio.run(run_filters(fake_llm))
    assert again.considered == 0


def test_llm_failure_keeps_job_as_unclear(fake_llm):
    from sqlmodel import select

    from jobpilot import db
    from jobpilot.filters import run_filters
    from jobpilot.models import Job

    fake_llm.fake.reply = lambda m: "not json at all"
    with db.session() as sess:
        _add(sess, "1", desc="Must be authorized to work in the US.")
        sess.commit()
    report = asyncio.run(run_filters(fake_llm))
    assert report.llm_errors == 1 and report.passed == 1
    with db.session() as sess:
        job = sess.exec(select(Job)).one()
        assert job.visa_flag == "unclear" and job.status == "new"


# --- LCA -----------------------------------------------------------------


def test_lca_import_matches_legal_names(temp_root):
    from sqlmodel import select

    from jobpilot import db
    from jobpilot.filters.lca import import_lca
    from jobpilot.models import Company

    csv_path = temp_root / "lca.csv"
    csv_path.write_text(
        "CASE_NUMBER,CASE_STATUS,EMPLOYER_NAME\n"
        "1,Certified,\"STRIPE, INC.\"\n"
        "2,Certified,Stripe Inc\n"
        "3,Denied,\"STRIPE, INC.\"\n"
        "4,Certified,Palantir Technologies Inc.\n"
        "5,Certified,Stripes Car Wash LLC\n"
        "6,Withdrawn,Ramp Business Corporation\n",
        encoding="utf-8",
    )
    db.init_db()
    with db.session() as sess:
        db.sync_companies(
            sess,
            [
                {"name": "Stripe", "ats": "greenhouse", "token": "stripe"},
                {"name": "Palantir", "ats": "lever", "token": "palantir"},
                {"name": "Ramp", "ats": "ashby", "token": "ramp"},
            ],
        )
        import_lca(sess, [csv_path])
        got = {c.name: (c.has_lca_history, c.lca_filings_count) for c in sess.exec(select(Company))}
        # Re-import replaces rather than doubles.
        import_lca(sess, [csv_path])
        again = {c.name: c.lca_filings_count for c in sess.exec(select(Company))}

    assert got == {"Stripe": (True, 2), "Palantir": (True, 1), "Ramp": (False, 0)}
    assert again == {"Stripe": 2, "Palantir": 1, "Ramp": 0}


def test_lca_rejects_xlsx(temp_root):
    from jobpilot.filters.lca import read_lca_counts

    with pytest.raises(ValueError, match="export to CSV"):
        read_lca_counts(temp_root / "LCA_FY2025.xlsx")
