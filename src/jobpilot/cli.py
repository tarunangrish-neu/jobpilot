"""JobPilot command line interface."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

import typer
from sqlmodel import func, select

from . import config, db
from .http import PoliteClient
from .models import JOB_STATUSES, Job
from .sources import ADAPTERS
from .sources import discover as discover_mod

app = typer.Typer(
    add_completion=False,
    help="Local AI job application pipeline. Nothing is ever submitted without your approval.",
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = "\n".join(
        "  ".join(str(r[i]).ljust(widths[i]) for i in range(len(headers))) for r in rows
    )
    return f"{line}\n{sep}\n{body}" if rows else f"{line}\n{sep}\n(no rows)"


@app.command()
def init() -> None:
    """Create the database and copy example configs into place."""
    root = config.project_root()
    for sub in ("data/lca", "output", "browser_profile", "logs", "templates"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    for target, example, note in (
        ("config/answers.yaml", "config/answers.example.yaml", "fill it in truthfully"),
        (
            "data/master_resume.yaml",
            "config/master_resume.example.yaml",
            "replace the example person with your real resume",
        ),
    ):
        if not (root / target).exists() and (root / example).exists():
            shutil.copy(root / example, root / target)
            typer.echo(f"created {target} - {note}")
        else:
            typer.echo(f"{target} already present, left untouched")

    db.init_db()
    with db.session() as sess:
        index = db.sync_companies(sess, config.companies())
    typer.echo(f"database ready at {config.db_path()}")
    typer.echo(f"{len(index)} companies registered from config/companies.yaml")


async def _fetch_all(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Fetch every configured board concurrently under the shared rate limiter."""
    results: dict[str, Any] = {}
    async with PoliteClient() as client:

        async def one(entry: dict[str, Any]) -> None:
            adapter = ADAPTERS.get(entry["ats"])
            key = f"{entry['ats']}/{entry['token']}"
            if adapter is None:
                results[key] = (entry, [], f"unknown ats '{entry['ats']}'")
                return
            try:
                postings = await adapter.fetch(client, entry)
                results[key] = (entry, postings, "")
            except Exception as exc:  # noqa: BLE001 - one bad board must not kill the run
                results[key] = (entry, [], f"{type(exc).__name__}: {exc}")

        await asyncio.gather(*(one(e) for e in entries))
    return results


@app.command()
def fetch() -> None:
    """Pull postings from every active board in config/companies.yaml."""
    entries = config.companies()
    if not entries:
        typer.echo("no active companies in config/companies.yaml")
        raise typer.Exit(1)

    db.init_db()
    results = asyncio.run(_fetch_all(entries))

    rows: list[list[str]] = []
    with db.session() as sess:
        index = db.sync_companies(sess, entries)
        for key in sorted(results):
            entry, postings, error = results[key]
            company_id = index.get((entry["ats"], entry["token"]))
            inserted = updated = 0
            for posting in postings:
                if db.upsert_posting(sess, posting, company_id) == "inserted":
                    inserted += 1
                else:
                    updated += 1
            sess.commit()
            rows.append(
                [
                    entry["name"],
                    entry["ats"],
                    str(len(postings)),
                    str(inserted),
                    str(updated),
                    error or "ok",
                ]
            )
        total = sess.exec(select(func.count()).select_from(Job)).one()

    typer.echo(
        _table(rows, ["company", "ats", "fetched", "new", "refreshed", "status"])
    )
    typer.echo(f"\ntotal jobs in database: {total}")


@app.command("discover-token")
def discover_token(careers_url: str) -> None:
    """Detect the ATS and board token behind a careers page."""

    async def run() -> list[dict[str, Any]]:
        async with PoliteClient(use_cache=False) as client:
            return await discover_mod.discover(client, careers_url)

    found = asyncio.run(run())
    if not found:
        typer.echo("no Greenhouse, Lever, or Ashby board found for that URL")
        raise typer.Exit(1)

    typer.echo(
        _table(
            [[f["ats"], f["token"], str(f["job_count"])] for f in found],
            ["ats", "token", "open roles"],
        )
    )
    best = found[0]
    typer.echo(
        f"\nAdd to config/companies.yaml:\n"
        f"  - name: \n    ats: {best['ats']}\n    token: {best['token']}"
    )


def _llm():
    from .llm import LLMClient, LLMError

    try:
        return LLMClient()
    except LLMError as exc:
        typer.echo(f"LLM unavailable: {exc}")
        raise typer.Exit(1)


def _resume():
    from . import master_resume

    try:
        return master_resume.load()
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1)


@app.command("filter")
def filter_cmd(
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the LLM visa check; ambiguous jobs become 'unclear'."),
) -> None:
    """Dedupe, apply title/location/age rules, and screen for visa blockers."""
    from .filters import run_filters

    db.init_db()
    report = asyncio.run(run_filters(None if no_llm else _llm()))
    typer.echo(f"considered {report.considered}, passed {report.passed}")
    rows = [[k, str(v)] for k, v in report.dropped.most_common()]
    typer.echo(_table(rows, ["dropped by", "jobs"]))
    flags = ", ".join(f"{k}={v}" for k, v in sorted(report.visa_flags.items()))
    typer.echo(f"\nvisa flags: {flags or '-'}")
    if report.llm_checked:
        typer.echo(f"LLM visa checks: {report.llm_checked} ({report.llm_errors} failed -> unclear)")


def _print_ranked(limit: int) -> None:
    from .scoring.rank import ranked

    with db.session() as sess:
        items = ranked(sess, limit=limit)
        rows = [
            [
                str(i + 1),
                str(r.job.id),
                (r.company.name if r.company else "")[:18],
                r.job.title[:48],
                f"{r.job.final_score:.1f}" if r.job.final_score is not None else "-",
                r.job.visa_flag,
                f"LCA {r.company.lca_filings_count}" if r.company and r.company.has_lca_history else "",
                (r.job.llm_reason or "")[:70],
            ]
            for i, r in enumerate(items)
        ]
    typer.echo(_table(rows, ["rank", "id", "company", "title", "score", "visa", "lca", "top reason"]))


@app.command()
def score(
    top: int = typer.Option(0, "--top", help="How many jobs to LLM-rerank (default: scoring.embed_top_n)."),
    limit: int = typer.Option(30, "--limit", help="Rows to print."),
) -> None:
    """Embed filtered jobs, LLM-rerank the top N, and print the ranked table."""
    from .scoring.rank import run_scoring

    db.init_db()
    report = asyncio.run(run_scoring(_llm(), _resume(), top or None))
    typer.echo(
        f"candidates {report.candidates}, embedded {report.embedded}, reranked {report.reranked}"
    )
    for err in report.errors[:10]:
        typer.echo(f"  rerank failed: {err}")
    typer.echo("")
    _print_ranked(limit)


@app.command()
def tailor(
    top: int = typer.Option(30, "--top", help="Tailor the N highest-ranked scored jobs."),
    job: list[int] = typer.Option(None, "--job", help="Tailor specific job id(s) instead."),
    cover_letter: bool = typer.Option(None, "--cover-letter/--no-cover-letter", help="Override tailor.cover_letter."),
    regenerate: bool = typer.Option(False, "--regenerate", help="Ignore cached LLM plans."),
) -> None:
    """Build one-page tailored resumes (and cover letters) from the master resume."""
    from .tailor import run_tailoring

    db.init_db()
    root = config.project_root()
    outcomes = asyncio.run(
        run_tailoring(_llm(), _resume(), top=top, job_ids=job or None, cover_letter=cover_letter, regenerate=regenerate)
    )
    rows = []
    for o in outcomes:
        if o.error:
            rows.append([str(o.job_id), "-", "-", "-", "-", o.error[:70]])
            continue
        rows.append(
            [
                str(o.job_id),
                str(o.report.pages),
                str(len(o.report.bullets)),
                f"{len(o.report.rephrased)}/{len(o.report.rejected)}",
                "yes" if o.cover_pdf else ("dropped" if o.cover else "-"),
                str(o.upload_pdf.relative_to(root)) if o.upload_pdf else "",
            ]
        )
    typer.echo(_table(rows, ["job", "pages", "bullets", "rephrased/rejected", "cover", "resume"]))


@app.command()
def prefill(
    top: int = typer.Option(30, "--top", help="Prefill the N highest-ranked tailored jobs."),
    job: list[int] = typer.Option(None, "--job", help="Prefill specific job id(s) instead."),
    headless: bool = typer.Option(None, "--headless/--headed", help="Override apply.headless."),
) -> None:
    """Fill application forms in the browser and STOP before submitting."""
    from .apply.prefill import run_prefill

    db.init_db()
    try:
        results = asyncio.run(
            run_prefill(top=top, job_ids=job or None, llm=_llm(), resume=_resume(), headless=headless)
        )
    except FileNotFoundError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1)
    root = config.project_root()
    rows = [
        [
            str(r.job_id),
            r.status,
            str(len(r.plan.actions)) if r.plan else "0",
            str(len(r.plan.drafted)) if r.plan else "0",
            str(r.screenshot.relative_to(root)) if r.screenshot else "",
            "; ".join(r.reasons)[:90],
        ]
        for r in results
    ]
    typer.echo(_table(rows, ["job", "status", "filled", "drafted", "screenshot", "needs you because"]))
    typer.echo("\nNothing was submitted. Review and approve in `jobpilot ui`.")


@app.command("import-lca")
def import_lca(files: list[Path] = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    """Load DOL LCA disclosure CSVs and mark companies with H-1B/LCA history."""
    from .filters.lca import import_lca as run_import
    from .scoring.rank import recompute_final_scores

    db.init_db()
    with db.session() as sess:
        try:
            report = run_import(sess, list(files))
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1)
        rescored = recompute_final_scores(sess)
    rows = [
        [c.name, str(total), "; ".join(names)[:80] or "-"]
        for c, total, names in sorted(report, key=lambda r: -r[1])
    ]
    typer.echo(_table(rows, ["company", "certified LCAs", "matched employer names"]))
    typer.echo(f"\nrecomputed final scores for {rescored} jobs")


@app.command()
def mark(job_id: int, status: str) -> None:
    """Set a job's status by hand (e.g. rejected, interviewing)."""
    if status not in JOB_STATUSES:
        typer.echo(f"invalid status '{status}'. one of: {', '.join(JOB_STATUSES)}")
        raise typer.Exit(1)

    with db.session() as sess:
        job = sess.get(Job, job_id)
        if job is None:
            typer.echo(f"no job with id {job_id}")
            raise typer.Exit(1)
        previous = job.status
        db.set_status(sess, job, status, reason="manual")
        sess.commit()
        typer.echo(f"job {job_id} ({job.title}): {previous} -> {status}")


if __name__ == "__main__":
    app()
