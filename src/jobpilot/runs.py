"""Background runs of pipeline stages, started and watched from the review UI.

`start("score", ["--rescore"])` records a `runs` row and launches
`python -m jobpilot.runs <id>` as its own process, with stdout/stderr going to
logs/runs/<id>-<stage>.log. That worker runs the normal CLI command and
records the exit code, so the UI can show progress, the log tail, and the
result even across Streamlit reruns or restarts.

Only one pipeline stage runs at a time: `busy()` also notices stages started
from a terminal or `make`, because two pipelines on one local LLM just slow
each other down.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import select

from . import config, db
from .models import Run, utcnow

# Stages the UI may start (each is the CLI command of the same name).
STAGES = ("fetch", "filter", "score", "tailor", "prefill", "draft-outreach", "run-daily")
_PIPELINE_RE = re.compile(r"jobpilot(?:/cli\.py)? (fetch|filter|score|tailor|prefill|draft-outreach|run-daily)\b")


def _alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A finished child nobody reaped is a zombie: treat it as gone.
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return bool(out.stdout.strip()) and not out.stdout.strip().startswith("Z")
    except (OSError, subprocess.SubprocessError):
        return True


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def refresh() -> list[Run]:
    """Mark runs whose worker died without recording an exit code; return active runs."""
    active = []
    with db.session() as sess:
        for run in sess.exec(select(Run).where(Run.status == "running")).all():
            if _alive(run.pid):
                active.append(run)
                continue
            run.status, run.finished_at = "failed", run.finished_at or utcnow()
            sess.add(run)
        sess.commit()
        for run in active:
            sess.refresh(run)  # commit expired it; reload before detaching
            sess.expunge(run)
    return active


def external_pipelines() -> list[str]:
    """Pipeline commands running outside the UI (terminal, make), as 'pid command'."""
    try:
        out = subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.stdout.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if not pid_text.isdigit() or "jobpilot.runs" in command:
            continue
        # `uv run jobpilot x` spawns python running jobpilot x; report the python process only.
        match = _PIPELINE_RE.search(command)
        if match and "python" in command:
            found.append(f"pid {pid_text}: jobpilot {command[match.start(1):][:70]}")
    return found


def busy() -> Optional[str]:
    """Why a new stage can't start right now, or None."""
    active = refresh()
    if active:
        return f"'{active[0].stage}' is running (run #{active[0].id})"
    others = external_pipelines()
    if others:
        return f"a pipeline started outside the UI is running: {others[0]}"
    return None


def start(stage: str, args: Optional[list[str]] = None) -> Run:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    reason = busy()
    if reason:
        raise RuntimeError(f"not starting {stage}: {reason}")
    args = list(args or [])
    log_dir = config.project_root() / "logs" / "runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with db.session() as sess:
        run = Run(stage=stage, args_json=json.dumps(args), started_at=utcnow())
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        log_path = log_dir / f"{run.id}-{stage}.log"
        log = log_path.open("a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "jobpilot.runs", str(run.id)],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        run.pid, run.log_path = proc.pid, str(log_path)
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        sess.expunge(run)
    return run


def stop(run_id: int) -> None:
    """Stop a run (and anything it spawned, e.g. a browser)."""
    with db.session() as sess:
        run = sess.get(Run, run_id)
        if run and run.status == "running" and _alive(run.pid):
            try:
                os.killpg(run.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            run.status, run.finished_at = "stopped", utcnow()
            sess.add(run)
            sess.commit()


def recent(limit: int = 15) -> list[Run]:
    refresh()
    with db.session() as sess:
        runs = sess.exec(select(Run).order_by(Run.id.desc()).limit(limit)).all()
        for run in runs:
            sess.expunge(run)
        return list(runs)


def log_tail(run: Run, lines: int = 40) -> str:
    try:
        text = Path(run.log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # Drop uv's harmless VIRTUAL_ENV warning so the tail shows the stage's own output.
    kept = [ln for ln in text.splitlines() if "VIRTUAL_ENV" not in ln]
    return "\n".join(kept[-lines:])


def llm_progress(since: datetime) -> dict:
    """LLM calls logged since `since`: count per prompt, errors, typical model time."""
    path = config.project_root() / config.settings().get("llm", {}).get("log_path", "logs/llm.jsonl")
    since = _as_utc(since)
    counts: dict[str, int] = {}
    errors, model_times = 0, []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    ts = datetime.fromisoformat(rec["ts"])
                except (ValueError, KeyError):
                    continue
                if _as_utc(ts) < since:
                    continue
                name = rec.get("prompt", "?")
                counts[name] = counts.get(name, 0) + 1
                if "error" in rec:
                    errors += 1
                elif rec.get("model_s") is not None:
                    model_times.append(float(rec["model_s"]))
    except OSError:
        pass
    model_times.sort()
    return {
        "calls": sum(counts.values()),
        "by_prompt": counts,
        "errors": errors,
        "median_model_s": model_times[len(model_times) // 2] if model_times else None,
    }


def _main(run_id: int) -> int:
    """Worker body: run the CLI stage, then record how it ended."""
    from .cli import app

    with db.session() as sess:
        run = sess.get(Run, run_id)
        argv = [run.stage, *json.loads(run.args_json or "[]")]
    print(f"$ jobpilot {' '.join(argv)}", flush=True)
    code = 0
    try:
        # With standalone_mode=False, click returns typer.Exit's code instead of exiting.
        result = app(args=argv, standalone_mode=False)
        code = result if isinstance(result, int) else 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # noqa: BLE001 - recorded, not re-raised
        import traceback

        traceback.print_exc()
        print(f"run failed: {type(exc).__name__}: {exc}", flush=True)
        code = 1
    with db.session() as sess:
        run = sess.get(Run, run_id)
        if run.status == "running":
            run.status = "done" if code == 0 else "failed"
        run.exit_code, run.finished_at = code, utcnow()
        sess.add(run)
        sess.commit()
    return code


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        sys.exit("usage: python -m jobpilot.runs <run_id>  (started by the review UI)")
    sys.exit(_main(int(sys.argv[1])))
