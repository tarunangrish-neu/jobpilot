"""JobPilot command line interface."""

from __future__ import annotations

import asyncio
import logging
import shutil
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

    answers = root / "config" / "answers.yaml"
    example = root / "config" / "answers.example.yaml"
    if not answers.exists() and example.exists():
        shutil.copy(example, answers)
        typer.echo(f"created {answers.relative_to(root)} - fill it in truthfully")
    else:
        typer.echo("config/answers.yaml already present, left untouched")

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
