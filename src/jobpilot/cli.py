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
config.load_env()


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


async def _fetch_all(entries: list[dict[str, Any]], fresh: bool = True) -> dict[str, Any]:
    """Fetch every configured board concurrently under the shared rate limiter.

    `fresh` asks every board again (only per-posting detail pages may come from the cache),
    so each fetch sees the jobs posted since the last one.
    """
    results: dict[str, Any] = {}
    async with PoliteClient(fresh=fresh) as client:

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


def _slug(name: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in name.lower()).split())[:60] or "unknown"


def _fetch_hn() -> list[str]:
    """Fetch the latest HN thread; returns a table row."""
    from .llm import LLMClient
    from .sources import hn

    async def run():
        async with PoliteClient(fresh=True) as client:  # new comments arrive all month
            return await hn.fetch(client, LLMClient(), config.settings().get("hn", {}))

    postings, info = asyncio.run(run())
    inserted = updated = 0
    with db.session() as sess:
        entries = {p.company_name: {"name": p.company_name, "ats": "hn", "token": _slug(p.company_name)} for p in postings}
        index = db.sync_companies(sess, list(entries.values()))
        for p in postings:
            e = entries[p.company_name]
            if db.upsert_posting(sess, p, index.get((e["ats"], e["token"]))) == "inserted":
                inserted += 1
            else:
                updated += 1
        sess.commit()
    detail = info.get("error") or f"{info['extracted_from']}/{info['comments']} comments read, {info['failed']} failed"
    return ["HN Who is hiring", "hn", str(len(postings)), str(inserted), str(updated), detail]


def _run_discovery(dry_run: bool = False) -> None:
    """Run the settings.yaml `discovery.queries`; append verified new boards to companies.yaml."""
    from .sources import search

    cfg = config.settings().get("discovery", {})
    if not cfg.get("queries"):
        typer.echo("no discovery.queries in config/settings.yaml")
        return

    async def run():
        async with PoliteClient() as client:
            return await search.discover(client, cfg, config.companies())

    report = asyncio.run(run())
    for err in report.errors:
        typer.echo(f"search error: {err}")
    typer.echo(
        f"{report.queries} queries, {report.urls} result URLs, {report.candidates} new board candidates, "
        f"{report.already_known} already in companies.yaml"
    )
    rows = [[f.name, f.ats, f.token, str(f.open_roles), f.query[:50]] for f in report.added]
    if rows:
        typer.echo(_table(rows, ["company", "ats", "token", "open roles", "found by"]))
    if report.added and not dry_run:
        search.append_to_companies(config.project_root() / "config" / "companies.yaml", report.added)
        typer.echo(f"\nadded {len(report.added)} boards to config/companies.yaml")
    elif report.added:
        typer.echo("\n--dry-run: companies.yaml not changed")


@app.command("discover-search")
def discover_search(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be added without editing companies.yaml."),
) -> None:
    """Find new company boards with the web-search queries in settings.yaml (discovery:)."""
    _run_discovery(dry_run)


@app.command()
def fetch(
    hn: bool = typer.Option(None, "--hn/--no-hn", help="Include HN Who's Hiring (default: hn.enabled)."),
    discover: bool = typer.Option(
        None, "--discover/--no-discover", help="Run discovery.queries first (default: discovery.enabled)."
    ),
    screen: bool = typer.Option(
        True, "--screen/--no-screen", help="Apply your filters (no LLM) to every unreviewed job afterwards."
    ),
    cached: bool = typer.Option(
        False, "--cached", help="Reuse board listings fetched in the last http.cache_ttl_hours instead of asking again."
    ),
) -> None:
    """Pull postings from every active board in config/companies.yaml (and HN if enabled), then screen them."""
    run_search = config.settings().get("discovery", {}).get("enabled", False) if discover is None else discover
    if run_search:
        _run_discovery()
    entries = config.companies()
    if not entries:
        typer.echo("no active companies in config/companies.yaml")
        raise typer.Exit(1)

    db.init_db()
    include_hn = config.settings().get("hn", {}).get("enabled", False) if hn is None else hn
    results = asyncio.run(_fetch_all(entries, fresh=not cached))

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
    if include_hn:
        rows.append(_fetch_hn())
    with db.session() as sess:
        total = sess.exec(select(func.count()).select_from(Job)).one()

    typer.echo(
        _table(rows, ["company", "ats", "fetched", "new", "refreshed", "status"])
    )
    typer.echo(f"\ntotal jobs in database: {total}")
    if screen:
        _print_screen()


def _print_screen() -> None:
    from .filters.screen import rescreen

    report = rescreen()
    typer.echo(f"screened {report.considered} jobs with your filters: {report.passed} pass, "
               f"{report.considered - report.passed} screened out ({report.restored} brought back)")
    for reason, n in report.reasons.most_common():
        typer.echo(f"  {n:6}  {reason}")


@app.command("screen")
def screen_cmd() -> None:
    """Re-apply your filters (settings.yaml + config/filters.yaml) to fetched jobs. No network, no LLM."""
    db.init_db()
    _print_screen()


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
        items = ranked(sess, statuses=("scored", "tailored", "prefilled", "needs_human"), limit=limit)
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
    rescore: bool = typer.Option(
        False, "--rescore", help="Re-rank already scored/tailored jobs too (after editing your resume)."
    ),
) -> None:
    """Embed filtered jobs, LLM-rerank the top N, and print the ranked table."""
    from .scoring.rank import run_scoring

    db.init_db()
    report = asyncio.run(run_scoring(_llm(), _resume(), top or None, rescore=rescore))
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
    """Build tailored resumes (and cover letters) from the master resume."""
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
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Only read each form and record how it would be filled (shown in the UI). "
        "Types nothing, uploads nothing, changes no job status.",
    ),
) -> None:
    """Fill application forms in the browser and STOP before submitting."""
    from .apply.prefill import run_prefill

    db.init_db()
    if dry_run and headless is None:
        headless = True
    try:
        results = asyncio.run(
            run_prefill(top=top, job_ids=job or None, llm=_llm(), resume=_resume(), headless=headless,
                        dry_run=dry_run)
        )
    except FileNotFoundError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1)
    if dry_run:
        rows = []
        for r in results:
            counts = {}
            for row in r.dry_rows:
                key = "you" if row["source"] == "NEEDS YOU" else ("llm" if row["source"].startswith("LLM") else
                      ("blank" if row["source"].startswith("left") else "auto"))
                counts[key] = counts.get(key, 0) + 1
            rows.append([str(r.job_id), str(len(r.dry_rows)), str(counts.get("auto", 0)), str(counts.get("llm", 0)),
                         str(counts.get("you", 0)), "; ".join(r.reasons)[:80]])
        typer.echo(_table(rows, ["job", "fields", "auto", "llm draft", "needs you", "why"]))
        typer.echo("\nDry run: nothing typed, uploaded, or submitted. See each job's Answers tab in `jobpilot ui`.")
        return
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


@app.command("draft-outreach")
def draft_outreach_cmd(top: int = typer.Option(20, "--top", help="Draft for the N best manual-apply (HN) jobs.")) -> None:
    """Draft verified outreach messages for HN / manual-apply jobs."""
    from .apply.manual import draft_outreach
    from .ui import actions

    db.init_db()
    llm, resume = _llm(), _resume()
    rows = actions.queue(("scored", "tailored"), sources=["hn"])[:top]
    for r in rows:
        text = asyncio.run(draft_outreach(llm, resume, r["id"]))
        typer.echo(f"{r['id']:>6}  {r['company'][:24]:<24}  {'drafted' if text else 'nothing survived verification'}")
    if not rows:
        typer.echo("no scored HN jobs (enable hn in settings.yaml and run fetch/filter/score)")


@app.command("run-daily")
def run_daily(
    top: int = typer.Option(30, "--top", help="How many jobs to tailor and prefill."),
    skip_prefill: bool = typer.Option(False, "--skip-prefill", help="Stop after tailoring."),
) -> None:
    """fetch -> filter -> score -> tailor -> prefill. Never submits anything."""
    steps = [
        ("fetch", lambda: fetch(hn=None, discover=None)),
        ("filter", lambda: filter_cmd(no_llm=False)),
        ("score", lambda: score(top=0, limit=top, rescore=False)),
        ("tailor", lambda: tailor(top=top, job=[], cover_letter=None, regenerate=False)),
    ]
    if config.settings().get("hn", {}).get("enabled", False):
        steps.append(("draft-outreach", lambda: draft_outreach_cmd(top=top)))
    if not skip_prefill:
        steps.append(("prefill", lambda: prefill(top=top, job=[], headless=None)))
    for name, step in steps:
        typer.echo(f"\n=== {name} ===")
        step()
    typer.echo("\nDone. Review and approve in `jobpilot ui` -- nothing has been submitted.")


@app.command()
def ui(port: int = typer.Option(8501, "--port")) -> None:
    """Launch the Streamlit review app."""
    import subprocess
    import sys

    script = Path(__file__).resolve().parent / "ui" / "review_app.py"
    raise typer.Exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(script), "--server.port", str(port)]))


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
