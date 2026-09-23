"""Tailoring: anti-fabrication (adversarial), document building, Typst rendering."""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from conftest import REPO
from jobpilot import master_resume
from jobpilot.tailor.cover_letter import CoverLetterDraft, clean_letter
from jobpilot.tailor.resume import (
    TailorPlan,
    _shape_problem,
    build_document,
    order_skills,
    plan_schema,
    select_bullets,
)
from jobpilot.tailor.verify import ResumeFacts, verify_free_text, verify_rephrase

RESUME = master_resume.load(REPO / "config" / "master_resume.example.yaml")
FACTS = ResumeFacts.from_resume(RESUME)
B = RESUME.bullet_index()

needs_typst = pytest.mark.skipif(shutil.which("typst") is None, reason="typst not installed")


# --- verify.py: adversarial rephrasings must be rejected -------------------


@pytest.mark.parametrize(
    "bullet_id,rephrase,expected",
    [
        # invented metric
        ("ll-stablecoin", "Built USDC payout rails moving $50M monthly with on-chain reconciliation against PostgreSQL ledgers", "50"),
        # altered metric
        ("ll-oncall", "Led the on-call rotation for 6 payment services; runbooks cut incident MTTR by 45%", "45"),
        # metric smuggled in as a word
        ("ll-stablecoin", "Built USDC payout rails that doubled payout volume, reconciled on-chain against PostgreSQL ledgers", "doubled"),
        # technology from a different job on the resume (still a fabricated claim here)
        ("ll-ledger", "Designed a double-entry ledger service in Go on kubernetes that settles 2M transactions per day", "kubernetes"),
        # technology not on the resume at all
        ("ll-latency", "Cut p99 authorization latency from 480ms to 120ms by moving risk checks to an async Snowflake pipeline", "Snowflake"),
        # invented employer / proper noun
        ("nw-ci", "Parallelized the CI pipeline in GitHub Actions for Stripe, cutting median build time by 50%", "Stripe"),
        # internal-caps technology
        ("ll-rust", "Rewrote the card-tokenization hot path in Rust with gRPC, lowering CPU usage by 40%", "gRPC"),
        # letter+digit cloud product
        ("nw-k8s", "Automated Kubernetes cluster provisioning on EC2 with Terraform, cutting setup from 2 days to 3 hours", "EC2"),
        # new number even when phrased as a range
        ("ll-rust", "Rewrote the card-tokenization hot path in Rust, lowering CPU usage by 40-60%", "60"),
    ],
)
def test_fabricated_rephrasings_are_rejected(bullet_id, rephrase, expected):
    finding = verify_rephrase(B[bullet_id].text, rephrase, FACTS)
    assert finding, f"accepted a fabrication: {rephrase}"
    assert expected in finding.items()


@pytest.mark.parametrize(
    "bullet_id,rephrase",
    [
        ("ll-ledger", "Settled 2M transactions per day with exactly-once semantics through a double-entry ledger service written in Go"),
        ("ll-latency", "Moved risk checks to an async Kafka pipeline, cutting p99 authorization latency from 480ms to 120ms"),
        ("nw-obs", "Built Grafana dashboards for SLO tracking and added OpenTelemetry tracing across 12 microservices"),
        # Title Case is not a proper noun when every word is already in the bullet
        ("ll-ledger", "Designed A Double-Entry Ledger Service In Go That Settles 2M Transactions Per Day"),
    ],
)
def test_faithful_rephrasings_pass(bullet_id, rephrase):
    assert not verify_rephrase(B[bullet_id].text, rephrase, FACTS)


@pytest.mark.parametrize(
    "bullet_id,rephrase",
    [
        ("ll-ledger", "Designed a double-entry ledger service in Go settling 2 million transactions daily with exactly-once semantics"),
        ("ll-oncall", "Led the on-call rotation for six payment services; runbooks reduced incident MTTR by 35%"),
    ],
)
def test_restated_numbers_are_not_new_numbers(bullet_id, rephrase):
    assert not verify_rephrase(B[bullet_id].text, rephrase, FACTS)


def test_borrowed_job_vocabulary_is_an_unsupported_claim():
    from jobpilot.tailor.verify import unsupported_claims

    jd = (
        "Stripe Billing: subscription management, financial reporting, mentorship of engineers. "
        "A strong collaborative, user-first approach with stakeholders."
    )
    # Real LLM output from the M3 acceptance run; both passed the entity check.
    claim = "Additionally, my background in subscription management and financial reporting positions me well."
    assert set(unsupported_claims(claim, FACTS, jd, "Stripe")) >= {"subscription", "management", "financial", "reporting"}
    no_pronoun = "Cut p99 latency from 480ms to 120ms, demonstrating a strong collaborative, user-first approach with stakeholders."
    assert unsupported_claims(no_pronoun, FACTS, jd, "Stripe")
    # Talking about the company, by name, with its own words is fine...
    assert unsupported_claims("Stripe's subscription management and financial reporting matter.", FACTS, jd, "Stripe") == []
    # ...and so is restating the resume.
    assert unsupported_claims("I built USDC payout rails with on-chain reconciliation.", FACTS, jd, "Stripe") == []


def test_free_text_allows_target_company_but_not_new_claims():
    ctx = "Ramp\nBackend Engineer\nRamp builds corporate cards and bill pay."
    assert not verify_free_text("I would bring my ledger work at Ledgerline to Ramp.", FACTS, ctx)
    assert verify_free_text("I hold a PhD in databases.", FACTS, ctx)  # fake degree
    assert "300" in verify_free_text("I scaled systems to 300 engineers.", FACTS, ctx).numbers
    assert verify_free_text("I have used Snowflake daily.", FACTS, ctx)  # tech not on resume


# --- document building ------------------------------------------------------


def test_build_document_uses_only_master_content():
    plan = TailorPlan(
        summary="infra",
        bullet_ids=["nw-k8s", "made-up-id", "ll-ledger", "nw-k8s"],
        rephrasings={
            "ll-ledger": "Designed a ledger service in Go settling 9M transactions per day",  # fake metric
            "nw-k8s": "Used Terraform to automate Kubernetes cluster provisioning, reducing environment setup from 2 days to 3 hours",
        },
        skills=["terraform", "Kubernetes", "COBOL"],
    )
    selected, unknown = select_bullets(RESUME, plan, 12, fill=False)
    assert selected == ["nw-k8s", "ll-ledger"] and unknown == ["made-up-id"]

    doc, report = build_document(RESUME, plan, FACTS, selected)
    assert doc["summary"] == RESUME.summary["infra"]
    ledger = doc["experience"][0]["bullets"]
    assert ledger == [B["ll-ledger"].text]  # rejected rephrase fell back to the original
    assert report.rejected[0]["id"] == "ll-ledger" and "9M" not in json.dumps(doc)
    assert report.rephrased == ["nw-k8s"]
    assert [e["company"] for e in doc["experience"]] == ["Ledgerline", "Northwind Cloud"]  # every role kept
    assert doc["projects"] == []  # no project bullet selected
    assert doc["skills"] == [{"group": "Infrastructure", "items": ["Terraform", "Kubernetes"]}]
    assert report.unknown_skills == ["COBOL"]


def test_select_bullets_keeps_every_master_bullet_in_plan_order():
    plan = TailorPlan(bullet_ids=["nw-k8s", "ll-ledger"])
    selected, _ = select_bullets(RESUME, plan)
    rest = [b.id for b in RESUME.all_bullets() if b.id not in ("nw-k8s", "ll-ledger")]
    assert selected == ["nw-k8s", "ll-ledger"] + rest
    assert select_bullets(RESUME, plan, 3)[0] == ["nw-k8s", "ll-ledger", rest[0]]


def test_plan_schema_pins_ids_and_accepts_rephrasing_pairs():
    schema = plan_schema(RESUME)
    ids = schema["properties"]["bullet_ids"]["items"]["enum"]
    assert set(ids) == set(B) and schema["properties"]["summary"]["enum"] == list(RESUME.summary)
    assert schema["properties"]["rephrasings"]["items"]["properties"]["id"]["enum"] == ids
    plan = TailorPlan.model_validate(
        {"summary": "infra", "bullet_ids": ["nw-k8s"], "rephrasings": [{"id": "nw-k8s", "text": "x"}]}
    )
    assert plan.rephrasings == {"nw-k8s": "x"}
    # v1 replies (a dict) cached before the change still validate.
    assert TailorPlan.model_validate({"rephrasings": {"a": "b"}}).rephrasings == {"a": "b"}


def test_skills_the_job_names_come_first_and_none_are_dropped():
    ordered = order_skills(RESUME, "We run Kafka on Kubernetes; ago, go-to attitude.")
    assert set(ordered) == set(RESUME.all_skills())
    assert ordered[:2] == [s for s in RESUME.all_skills() if s in ("Kafka", "Kubernetes")]
    assert "Go" not in ordered[:2]  # "ago" / "go-to" are not the language
    doc, _ = build_document(RESUME, TailorPlan(), FACTS, ["nw-k8s"], job_text="Kubernetes and Terraform")
    infra = next(g for g in doc["skills"] if g["group"] == "Infrastructure")
    assert infra["items"][:2] == ["Kubernetes", "Terraform"]
    assert sum(len(g["items"]) for g in doc["skills"]) == len(RESUME.all_skills())


def test_rephrase_borrowing_job_claims_is_rejected():
    jd = "Lead cross-functional stakeholders across product and design to drive roadmap strategy."
    plan = TailorPlan(rephrasings={
        "nw-k8s": "Led cross-functional stakeholders to automate Kubernetes cluster provisioning with Terraform, "
                  "cutting setup from 2 days to 3 hours",
    })
    _, report = build_document(RESUME, plan, FACTS, ["nw-k8s"], job_text=jd)
    assert report.rejected and "job wording" in report.rejected[0]["new_entities"][-1]


def test_prompt_examples_sit_between_system_and_question():
    from jobpilot.llm.prompts import TAILOR

    msgs = TAILOR.render(resume="R", title="T", company="C", description="D")
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[-1]["content"].startswith("MASTER RESUME\nR")  # stable prefix first, job last
    TailorPlan.model_validate_json(msgs[2]["content"])  # the worked answer matches the schema


def test_worked_example_obeys_the_fabrication_rules():
    from jobpilot.llm import prompts
    from jobpilot.master_resume import MasterResume

    # Rebuild the fictional resume the example shows, then vet its rephrasings like real ones.
    bullets = {}
    for line in prompts._TAILOR_EXAMPLE_Q.splitlines():
        line = line.strip()
        if line.startswith("(") and ")" in line:
            bid, text = line[1:].split(") ", 1)
            bullets[bid] = text
    fake = MasterResume.model_validate({
        "contact": {"name": "Ex Ample"},
        "experience": [{"company": "Tidewater Bank", "title": "Software Engineer", "dates": "2021 - 2024",
                        "bullets": [{"id": k, "text": v} for k, v in bullets.items()]}],
    })
    facts = ResumeFacts.from_resume(fake)
    plan = TailorPlan.model_validate_json(prompts._TAILOR_EXAMPLE_A)
    for bid, text in plan.rephrasings.items():
        assert not verify_rephrase(bullets[bid], text, facts), bid
        assert not _shape_problem(bullets[bid], text, facts), bid


def test_rephrase_that_drops_details_falls_back_to_original():
    # Real qwen2.5-7b output shape: the stack and the user count quietly disappear.
    original = B["ll-ledger"].text
    thinned = "Designed a double-entry ledger service settling transactions with exactly-once semantics"
    assert _shape_problem(original, thinned, FACTS).startswith("dropped")
    shrunk = "Designed a double-entry ledger in Go settling 2M transactions per day"
    assert _shape_problem(original, shrunk, FACTS) == "much shorter than the original"


def test_keyword_stuffed_rephrase_falls_back_to_original():
    # Real qwen2.5-7b output from the M3 acceptance run.
    plan = TailorPlan(bullet_ids=["nw-k8s"], rephrasings={"nw-k8s": B["nw-k8s"].text + " (Kubernetes, Terraform)"})
    doc, report = build_document(RESUME, plan, FACTS, ["nw-k8s"])
    assert doc["experience"][1]["bullets"] == [B["nw-k8s"].text]
    assert report.rejected[0]["new_entities"] == ["added a parenthetical"]


def test_cover_letter_sentences_are_filtered():
    draft = CoverLetterDraft(
        paragraphs=[
            "I am writing to express my interest in the Backend Engineer role at Ramp. "
            "At Ledgerline I designed a double-entry ledger service in Go that settles 2M transactions per day. "
            "I also led a team of engineers at Google.",
            "I cut p99 authorization latency from 480ms to 120ms by moving risk checks to an async Kafka pipeline. "
            "I rewrote the card-tokenization hot path in Rust, lowering CPU usage by 40%. "
            "Ramp's work on bill pay maps directly onto the payout rails and reconciliation I built with PostgreSQL.",
        ]
    )
    result = clean_letter(draft, FACTS, "Ramp\nBackend Engineer\nRamp bill pay and cards", company="Ramp")
    text = result.text
    assert "writing to express" not in text  # cliché
    assert "Google" not in text  # invented employer
    assert "Ledgerline" in text and "Ramp's work" in text
    assert len(result.dropped) == 2
    assert result.ok


# --- end to end with Typst --------------------------------------------------


@pytest.fixture
def tailor_root(fake_llm, temp_root):
    shutil.copytree(REPO / "templates", temp_root / "templates")
    (temp_root / "data").mkdir(exist_ok=True)
    shutil.copy(REPO / "config" / "master_resume.example.yaml", temp_root / "data" / "master_resume.yaml")
    return temp_root


@needs_typst
def test_tailor_renders_one_page_and_records(tailor_root, fake_llm):
    from sqlmodel import select

    from jobpilot import db
    from jobpilot.models import Application, Event, Job, JobPosting
    from jobpilot.tailor import run_tailoring

    with db.session() as sess:
        db.upsert_posting(
            sess,
            JobPosting(source="lever", external_id="x", company_name="Acme", title="Backend Engineer, Payments",
                       description_text="Go, Kafka, ledgers."),
            None,
        )
        job = sess.exec(select(Job)).one()
        job.status, job.final_score = "scored", 80.0
        sess.commit()
        job_id = job.id

    def reply(messages):
        if "cover letter" in messages[0]["content"]:
            return json.dumps({"paragraphs": [
                "At Ledgerline I designed a double-entry ledger service in Go that settles 2M transactions per day. "
                "I cut p99 authorization latency from 480ms to 120ms by moving risk checks to an async Kafka pipeline. "
                "I also built USDC payout rails with on-chain reconciliation against PostgreSQL ledgers.",
                "Acme is building payments infrastructure, and that ledger and reconciliation work is the part of "
                "my experience I would bring first to the Backend Engineer role.",
            ]})
        # Every bullet (with duplicates the selector must drop).
        return json.dumps({
            "summary": "backend",
            "bullet_ids": [b.id for b in RESUME.all_bullets()] * 3,
            "rephrasings": {"ll-oncall": "Led on-call for 6 payment services and cut MTTR by 70%"},
            "skills": ["Go", "Kafka", "PostgreSQL"],
        })

    fake_llm.fake.reply = reply
    [outcome] = asyncio.run(run_tailoring(fake_llm, RESUME, top=5, cover_letter=True))
    assert outcome.error == ""
    assert outcome.report.pages == 1
    assert outcome.upload_pdf.name == "Alex_Example_Resume.pdf" and outcome.upload_pdf.exists()
    assert outcome.cover_pdf.name == "Alex_Example_Cover_Letter.pdf"
    assert outcome.report.rejected and outcome.report.rejected[0]["id"] == "ll-oncall"

    with db.session() as sess:
        job = sess.get(Job, job_id)
        app = sess.exec(select(Application).where(Application.job_id == job_id)).one()
        assert job.status == "tailored"
        assert app.resume_path.endswith("Alex_Example_Resume.pdf")
        assert "Ledgerline" in app.cover_letter_text
        audit = json.loads(app.tailoring_json)
        assert audit["report"]["rejected"][0]["new_entities"] == ["70", "dropped 35"]
        assert sess.exec(select(Event).where(Event.type == "tailored")).one()
