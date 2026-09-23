"""Prefill + submit: answer-bank rules (pure) and headless runs on a local form."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
import yaml

from conftest import FIXTURES, REPO
from jobpilot.apply.base import AnswerBank, categorize, choose_option, clean_label, match_static, plan_fill
from jobpilot.apply.dom import FormField

ANSWERS = {
    "contact": {
        "first_name": "Alex", "last_name": "Example", "email": "alex@example.com", "phone": "+1 555 010 0000",
        "location": "Jersey City, NJ", "linkedin": "https://www.linkedin.com/in/alex-example", "github": "", "website": "",
    },
    "work_authorization": {"authorized_to_work_in_us": "Yes", "require_sponsorship_now_or_future": "Yes"},
    "logistics": {"willing_to_relocate": "", "earliest_start_date": "", "notice_period": "", "salary_expectation": ""},
    "eeo": {"gender": "Decline to self-identify", "race_ethnicity": "Decline to self-identify",
            "veteran_status": "Decline to self-identify", "disability_status": "Decline to self-identify"},
    "common_questions": [
        {"q": "Why do you want to work here?", "a": None},
        {"q": "Where do you plan on working from?", "a": "United States"},
    ],
}
BANK = AnswerBank.from_answers(ANSWERS)


def F(id, kind, label, required=False, options=None, **kw):
    return FormField(id=id, kind=kind, label=label, required=required, options=options or [], **kw)


def run(coro):
    return asyncio.run(coro)


# --- pure rules -----------------------------------------------------------------


@pytest.mark.parametrize(
    "label,category",
    [
        ("Will you now or in the future require sponsorship for employment visa status (e.g., H-1B)?", "sponsorship"),
        ("Are you legally authorized to work in the United States?", "work_auth"),
        ("Do you currently hold an active US security clearance?", "work_auth"),
        ("Are you Hispanic/Latino?", "eeo"),
        ("What are your salary expectations?", "salary"),
        ("Why do you want to work here?", "general"),
    ],
)
def test_categorize(label, category):
    assert categorize(label) == category


@pytest.mark.parametrize(
    "label,key",
    [
        ("First Name*", "contact.first_name"),
        ("Full name ✱", "contact.full_name"),
        ("Legal Name", "contact.full_name"),
        ("LinkedIn URL", "contact.linkedin"),
        ("Current location ✱", "contact.location"),
        ("Email", "contact.email"),
    ],
)
def test_contact_labels_match_statically(label, key):
    assert match_static(label, BANK).entry.key == key


def test_choose_option_picks_the_stated_answer_only():
    assert choose_option("Yes", ["Yes", "No"]) == "Yes"
    assert choose_option("Decline to self-identify", ["Male", "Female", "I don't wish to answer"]) == "I don't wish to answer"
    assert choose_option("Yes", ["Yes, I have a disability", "Yes, in the past", "No"]) is None  # ambiguous
    assert choose_option("United States", ["Canada", "United States", "Mexico"]) == "United States"


def test_lever_select_label_is_cleaned_of_its_options():
    f = F("x", "select", "Veteran status Select ... I am a veteran I am not a veteran",
          options=["I am a veteran", "I am not a veteran"])
    assert clean_label(f) == "Veteran status"


def test_sensitive_questions_come_only_from_their_own_category():
    no_sponsorship = AnswerBank.from_answers({**ANSWERS, "work_authorization": {"authorized_to_work_in_us": "Yes"}})

    async def embed_everything_alike(texts):
        return [[1.0, 0.0] for _ in texts]  # every question "matches" every bank entry

    fields = [F("s", "radio", "Do you need a visa to work here? ✱", required=True, options=["Yes", "No"])]
    plan = run(plan_fill(fields, no_sponsorship, None, None, embed=embed_everything_alike))
    # "authorized to work: Yes" must never answer a sponsorship question.
    assert all(a.field_id != "s" for a in plan.actions)
    assert any("sponsorship" in r or "empty in answers.yaml" in r for r in plan.needs_human)


def test_empty_contact_value_is_never_drafted():
    drafts = []

    async def draft(q):
        drafts.append(q)
        return "made up"

    plan = run(plan_fill([F("g", "text", "GitHub URL", required=True)], BANK, None, None, draft=draft))
    assert drafts == [] and plan.actions == []
    assert "empty in answers.yaml" in plan.needs_human[0]


def test_clearance_question_without_an_answer_needs_a_human():
    fields = [F("c", "radio", "Do you currently hold an active US security clearance? ✱", required=True, options=["Yes", "No"])]
    plan = run(plan_fill(fields, BANK, None, None))
    assert plan.actions == [] and plan.needs_human


def test_files_skip_autofill_and_use_tailored_resume(tmp_path):
    pdf = tmp_path / "Alex_Example_Resume.pdf"
    fields = [F("a", "file", "Autofill from resume"), F("r", "file", "Attach", required=True, html_id="resume"),
              F("c", "file", "Attach", html_id="cover_letter")]
    plan = run(plan_fill(fields, BANK, pdf, None))
    assert [(a.field_id, a.source) for a in plan.actions] == [("r", "resume")]
    assert plan.wants_cover_letter and not plan.needs_human


# --- browser: prefill never submits; submit only after UI approval ------------

pytest.importorskip("playwright.async_api")
FORM_URL = (FIXTURES / "forms" / "apply_form.html").resolve().as_uri()
SIMPLE_URL = FORM_URL + "?simple"  # without the sensitive lone checkbox, so prefill completes
DRAFT = "I built USDC payout rails with on-chain reconciliation against PostgreSQL ledgers."


@pytest.fixture
def apply_root(temp_root, fake_llm):
    (temp_root / "config" / "answers.yaml").write_text(yaml.safe_dump(ANSWERS), encoding="utf-8")
    shutil.copytree(REPO / "templates", temp_root / "templates")
    fake_llm.fake.reply = lambda m: json.dumps({"answer": DRAFT})
    return temp_root


def _tailored_job(root) -> int:
    from sqlmodel import select

    from jobpilot import db
    from jobpilot.models import Application, Job, JobPosting

    pdf = root / "output" / "1" / "Alex_Example_Resume.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 test resume\n")
    with db.session() as sess:
        db.sync_companies(sess, [{"name": "Acme", "ats": "lever", "token": "acme"}])
        db.upsert_posting(sess, JobPosting(source="lever", external_id="x", company_name="Acme",
                                           title="Backend Engineer", description_text="Payments."), 1)
        job = sess.exec(select(Job)).one()
        job.status = "tailored"
        sess.add(Application(job_id=job.id, resume_path=str(pdf)))
        sess.commit()
        return job.id


def _resume():
    from jobpilot import master_resume

    return master_resume.load(REPO / "config" / "master_resume.example.yaml")


def _app(sess, job_id):
    from sqlmodel import select

    from jobpilot.models import Application

    return sess.exec(select(Application).where(Application.job_id == job_id)).one()


def test_prefill_fills_everything_and_does_not_submit(apply_root, fake_llm, monkeypatch):
    from jobpilot import db
    from jobpilot.apply import prefill as prefill_mod
    from jobpilot.models import Job

    job_id = _tailored_job(apply_root)
    seen = {}
    original = prefill_mod.prefill_one

    async def inspect(page):
        if "values" in seen:
            return
        seen["submitted"] = await page.evaluate("() => window.__submitted")
        seen["values"] = await page.evaluate(
            "() => { const $ = id => document.getElementById(id); return {"
            " name: $('name').value, sponsor: $('sp-y').checked, auth: $('auth-y').checked,"
            " country: $('country').value, vet: $('vet').value, resume: $('resume').files.length,"
            " autofill: document.querySelector('[name=autofill]').files.length, why: $('why').value}; }"
        )

    async def inspect_before_close(browser, *args, **kwargs):
        make_page = browser.new_page

        async def tracked_page():
            page = await make_page()
            seen["page"], real_close = page, page.close

            async def close_after_inspection():
                await inspect(page)
                await real_close()

            page.close = close_after_inspection
            return page

        browser.new_page = tracked_page
        result = await original(browser, *args, **kwargs)
        await inspect(seen["page"])  # needs_human leaves the page open
        return result

    monkeypatch.setattr(prefill_mod, "prefill_one", inspect_before_close)
    [result] = run(prefill_mod.run_prefill(
        job_ids=[job_id], llm=fake_llm, resume=_resume(), headless=True, url_for=lambda j, c: FORM_URL,
        wait_for_human=False,
    ))

    # The lone "authorized without sponsorship?" checkbox can't be answered from a
    # Yes/No bank value, so it goes to a human instead of being guessed.
    assert len(result.reasons) == 1 and "without company sponsorship" in result.reasons[0]
    assert result.status == "needs_human"
    labels = {a.label for a in result.plan.actions} | set(result.plan.skipped)
    assert "Are you open to relocation to NYC or SF?" in labels  # one question, not one per option
    assert seen["submitted"] is False, "prefill must never submit"
    assert seen["values"] == {
        "name": "Alex Example", "sponsor": True, "auth": True, "country": "United States",
        "vet": "I decline to self-identify for protected veteran status", "resume": 1, "autofill": 0, "why": DRAFT,
    }
    with db.session() as sess:
        app = _app(sess, job_id)
        assert sess.get(Job, job_id).status == "needs_human"
        assert "without company sponsorship" in app.error
        assert Path(app.screenshot_path).exists()
        assert json.loads(app.llm_drafted_answers_json) == {"Why do you want to work at Acme?": DRAFT}
        assert json.loads(app.answers_json)["Full name"] == "Alex Example"


def _prefilled(apply_root, fake_llm) -> int:
    from jobpilot.apply.prefill import run_prefill

    job_id = _tailored_job(apply_root)
    run(run_prefill(job_ids=[job_id], llm=fake_llm, resume=_resume(), headless=True,
                    url_for=lambda j, c: SIMPLE_URL, wait_for_human=False))
    return job_id


def test_submit_refuses_without_ui_approval(apply_root, fake_llm):
    from jobpilot import db
    from jobpilot.apply.submit import SubmitRefused, submit_job
    from jobpilot.models import Job, utcnow

    job_id = _prefilled(apply_root, fake_llm)
    with pytest.raises(SubmitRefused, match="not approved"):
        run(submit_job(job_id, headless=True, url_override=SIMPLE_URL, wait_for_human=False))

    # Forcing the status by hand (e.g. `jobpilot mark <id> approved`) is not an approval.
    with db.session() as sess:
        db.set_status(sess, sess.get(Job, job_id), "approved", reason="manual")
        app = _app(sess, job_id)
        app.approved_at = utcnow()
        sess.add(app)
        sess.commit()
    with pytest.raises(SubmitRefused, match="review UI"):
        run(submit_job(job_id, headless=True, url_override=SIMPLE_URL, wait_for_human=False))


def test_approved_job_submits_exactly_once(apply_root, fake_llm):
    from jobpilot import db
    from jobpilot.apply.submit import SubmitRefused, approve, submit_job
    from jobpilot.models import Job

    job_id = _prefilled(apply_root, fake_llm)
    with db.session() as sess:
        approve(sess, sess.get(Job, job_id), {"Why do you want to work at Acme?": "I built USDC payout rails."})

    status, detail = run(submit_job(job_id, headless=True, url_override=SIMPLE_URL, wait_for_human=False))
    assert status == "submitted", detail
    with db.session() as sess:
        app = _app(sess, job_id)
        assert sess.get(Job, job_id).status == "submitted"
        assert app.submitted_at is not None and Path(app.confirmation_screenshot_path).exists()
        assert "I built USDC payout rails." == json.loads(app.llm_drafted_answers_json)["Why do you want to work at Acme?"]

    with pytest.raises(SubmitRefused, match="never re-apply"):
        run(submit_job(job_id, headless=True, url_override=SIMPLE_URL, wait_for_human=False))


def test_daily_cap_blocks_submission(apply_root, fake_llm, monkeypatch):
    from jobpilot import db
    from jobpilot.apply import submit as submit_mod
    from jobpilot.models import Job

    job_id = _prefilled(apply_root, fake_llm)
    with db.session() as sess:
        submit_mod.approve(sess, sess.get(Job, job_id))
    monkeypatch.setattr(submit_mod, "submitted_today", lambda sess: 40)
    with db.session() as sess:
        job = sess.get(Job, job_id)
        assert "cap of 40" in submit_mod.can_submit(sess, job, _app(sess, job_id))


def test_only_prefilled_jobs_can_be_approved(apply_root, fake_llm):
    from jobpilot import db
    from jobpilot.apply.submit import SubmitRefused, approve
    from jobpilot.models import Job

    job_id = _tailored_job(apply_root)
    with db.session() as sess, pytest.raises(SubmitRefused):
        approve(sess, sess.get(Job, job_id))


def test_dry_run_touches_nothing_and_records_the_plan(apply_root, fake_llm, monkeypatch):
    from jobpilot import db
    from jobpilot.apply import prefill as prefill_mod
    from jobpilot.models import Job

    job_id = _tailored_job(apply_root)
    seen = {}
    original = prefill_mod.prefill_one

    async def capture(browser, *args, **kwargs):
        make_page = browser.new_page

        async def tracked_page():
            page = await make_page()
            real_close = page.close

            async def close_after_inspection():
                seen["form"] = await page.evaluate(
                    "() => ({name: document.getElementById('name').value,"
                    " resume: document.getElementById('resume').files.length, submitted: window.__submitted})"
                )
                await real_close()

            page.close = close_after_inspection
            return page

        browser.new_page = tracked_page
        return await original(browser, *args, **kwargs)

    monkeypatch.setattr(prefill_mod, "prefill_one", capture)
    [result] = run(prefill_mod.run_prefill(
        job_ids=[job_id], llm=fake_llm, resume=_resume(), headless=True, url_for=lambda j, c: FORM_URL,
        wait_for_human=False, dry_run=True,
    ))
    assert seen["form"] == {"name": "", "resume": 0, "submitted": False}  # nothing typed or attached
    with db.session() as sess:
        assert sess.get(Job, job_id).status == "tailored"  # nothing becomes approvable
        plan = json.loads(_app(sess, job_id).dry_run_json)
    by_q = {r["question"]: r for r in plan["rows"]}
    assert by_q["Full name"]["answer"] == "Alex Example" and by_q["Full name"]["source"] == "answers.yaml"
    assert by_q["Resume/CV"]["source"] == "tailored resume"
    assert by_q["Why do you want to work at Acme?"]["source"] == "LLM draft (review)"
    assert by_q["Are you authorized to work in the U.S. without company sponsorship?"]["source"] == "NEEDS YOU"
    assert Path(plan["screenshot"]).exists()


def test_prefill_loads_several_jobs_at_one_company(apply_root):
    """Regression: two jobs sharing a Company made _load detach it twice and crash."""
    from sqlmodel import select

    from jobpilot import db
    from jobpilot.apply.prefill import _load
    from jobpilot.models import Application, Job, JobPosting

    with db.session() as sess:
        db.sync_companies(sess, [{"name": "Acme", "ats": "lever", "token": "acme"}])
        for ext in ("x", "y"):
            db.upsert_posting(sess, JobPosting(source="lever", external_id=ext, company_name="Acme", title="Backend"), 1)
        for job in sess.exec(select(Job)).all():
            job.status, job.final_score = "tailored", 50.0
            sess.add(Application(job_id=job.id, resume_path="r.pdf"))
        sess.commit()
    rows = _load(10, None)
    assert len(rows) == 2 and rows[0][1].name == rows[1][1].name == "Acme"


def test_prefill_runs_forms_in_parallel_tabs_and_keeps_ranking_order(apply_root, fake_llm, monkeypatch):
    """Several forms load at once (the slow part is the page, not the LLM); order is by score."""
    import asyncio

    from sqlmodel import select

    from jobpilot import db
    from jobpilot.apply import prefill as prefill_mod
    from jobpilot.models import Application, Job, JobPosting

    pdf = apply_root / "output" / "resume.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 test resume\n")
    with db.session() as sess:
        db.sync_companies(sess, [{"name": "Acme", "ats": "lever", "token": "acme"}])
        for n in range(4):
            db.upsert_posting(sess, JobPosting(source="lever", external_id=f"j{n}", company_name="Acme",
                                               title="Backend Engineer", description_text="Payments."), 1)
        for job in sess.exec(select(Job)).all():
            job.status, job.final_score = "tailored", float(job.id)
            sess.add(Application(job_id=job.id, resume_path=str(pdf)))
        sess.commit()

    in_flight = peak = 0
    original = prefill_mod.prefill_one

    async def counting(*args, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.2)  # hold the tab long enough for the others to start
        try:
            return await original(*args, **kwargs)
        finally:
            in_flight -= 1

    monkeypatch.setattr(prefill_mod, "prefill_one", counting)
    results = run(prefill_mod.run_prefill(
        top=10, llm=fake_llm, resume=_resume(), headless=True, url_for=lambda j, c: SIMPLE_URL,
        wait_for_human=False, parallel=2,
    ))

    assert peak == 2
    assert [r.job_id for r in results] == [4, 3, 2, 1]  # best score first, as loaded
    assert {r.status for r in results} == {"prefilled"}
    with db.session() as sess:
        assert {j.status for j in sess.exec(select(Job)).all()} == {"prefilled"}


def test_browser_waits_for_the_profile_instead_of_colliding(apply_root):
    """A submit approved mid-prefill must queue for the Chromium profile, not crash into it."""
    import asyncio
    import fcntl

    from jobpilot.apply.browser import Browser, profile_dir

    async def scenario() -> tuple[bool, bool]:
        profile_dir().mkdir(parents=True, exist_ok=True)
        other = open(profile_dir() / ".jobpilot.lock", "a")  # stands in for a running prefill
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        browser = Browser(headless=True)
        waiting = asyncio.create_task(browser._acquire_profile())
        await asyncio.sleep(0.3)
        blocked = not waiting.done()
        fcntl.flock(other, fcntl.LOCK_UN)
        other.close()
        await asyncio.wait_for(waiting, timeout=3)
        browser._release_profile()
        return blocked, waiting.done()

    assert run(scenario()) == (True, True)
