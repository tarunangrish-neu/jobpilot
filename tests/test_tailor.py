"""Tailoring: anti-fabrication (adversarial), document building, Typst rendering."""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from conftest import REPO
from jobpilot import master_resume
from jobpilot.tailor.cover_letter import CoverLetterDraft, clean_letter
from jobpilot.tailor.resume import TailorPlan, build_document, select_bullets
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
    selected, unknown = select_bullets(RESUME, plan, 12)
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
        assert audit["report"]["rejected"][0]["new_entities"] == ["70"]
        assert sess.exec(select(Event).where(Event.type == "tailored")).one()
