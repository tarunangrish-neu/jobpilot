"""JobPilot review UI (Streamlit). Launch with `jobpilot ui`.

The Jobs page is the front door: fetch, pick openings, tailor a resume and cover
letter, then apply on the company's site yourself. The old rank/prefill pages sit
behind a sidebar toggle; there, nothing is submitted from anywhere but the
"Approve & Submit" button on a single prefilled job, and that button stays
disabled until you tick the "I reviewed this" box.
"""

from __future__ import annotations

import json
import re

import streamlit as st

from jobpilot import db
from jobpilot.models import JOB_STATUSES
from jobpilot.ui import actions

st.set_page_config(page_title="JobPilot", page_icon="🧭", layout="wide")
db.init_db()

VISA_STYLE = {"ok": st.success, "unclear": st.warning, "blocked": st.error}
STATUS_COLOR = {"scored": "blue", "tailored": "violet", "prefilled": "orange", "needs_human": "red",
                "approved": "green", "submitted": "green", "skipped": "gray"}


def _fmt(value, spec: str) -> str:
    return format(value, spec) if value is not None else "–"


# Slow checks (subprocesses, whole-log scans) are cached so ticking a box doesn't re-run them.
# The project root is part of the key so a different checkout never sees another's results.
@st.cache_data(ttl=60, show_spinner=False)
def _readiness(_root: str) -> list[tuple[str, str]]:
    return actions.readiness()


@st.cache_data(ttl=30, show_spinner=False)
def _llm_stats(_root: str) -> list[dict]:
    return actions.llm_stats()


def _plain(text: str) -> str:
    """Show plain text as readable prose: escape Markdown (a `$` in a salary starts LaTeX) and keep line breaks."""
    return re.sub(r"([\\`*_{}\[\]<>()#+\-.!|$~])", r"\\\1", text).replace("\n", "  \n")


def _root() -> str:
    from jobpilot import config

    return str(config.project_root())


def _form_url(d: actions.Detail) -> str:
    from jobpilot.apply import FILLERS

    if d.job.source in FILLERS and d.company is not None:
        return FILLERS[d.job.source].form_url(d.job, d.company)
    return d.job.apply_url or d.job.url


def _header(d: actions.Detail) -> None:
    job, company = d.job, d.company
    st.subheader(f"{job.title} — {company.name if company else '?'}")
    st.badge(job.status.replace("_", " "), color=STATUS_COLOR.get(job.status, "gray"))
    st.caption(f"📍 {job.location or 'location not stated'} · via {job.source} · job #{job.id}"
               + (f" · [posting]({job.url})" if job.url else ""))

    c1, c2, c3, c4 = st.columns(4, border=True)
    c1.metric("Final score", _fmt(job.final_score, ".1f"), help="Combined rank score, 0–100.")
    c2.metric("LLM fit", _fmt(job.llm_score, ".0f"), help="How well the LLM thinks your resume fits, 0–100.")
    c3.metric("Embedding", _fmt(job.embed_score, ".3f"), help="Resume/posting similarity, 0–1.")
    c4.metric("Seniority fit", d.details.get("seniority_fit", "–"))

    left, right = st.columns(2)
    with left:
        VISA_STYLE.get(job.visa_flag, st.info)(f"Visa: **{job.visa_flag}** — {job.visa_reason or 'no reason recorded'}")
    with right:
        if company and company.has_lca_history:
            st.success(f"LCA history: {company.lca_filings_count} certified filings")
        else:
            st.info("No LCA history (or no LCA file imported)")

    reasons = d.details.get("reasons") or ([job.llm_reason] if job.llm_reason else [])
    why, gaps = st.columns(2)
    if reasons:
        why.markdown("**Why it ranks here**\n" + "\n".join(f"- {r}" for r in reasons))
    if d.details.get("missing_skills"):
        gaps.markdown("**Missing skills**\n" + "\n".join(f"- {m}" for m in d.details["missing_skills"]))
    with st.expander("Job description"):
        st.markdown(_plain(job.description_text) if job.description_text else "_No description._")


def _resume_tab(d: actions.Detail) -> None:
    if not d.app or not d.app.resume_path:
        st.info("Not tailored yet (`jobpilot tailor`).")
        return
    png = actions.resume_preview(d.job.id)
    if png:
        st.image(str(png), width="stretch")
    try:
        with open(d.app.resume_path, "rb") as fh:
            st.download_button("Download resume PDF", fh.read(), file_name=d.app.resume_path.rsplit("/", 1)[-1])
    except OSError:
        st.warning("Resume PDF is missing on disk.")
    report = d.tailoring.get("report") or {}
    if report.get("rejected"):
        st.markdown("**Rephrasings rejected by the fabrication check** (originals used):")
        for r in report["rejected"]:
            st.markdown(f"- `{r['id']}` added {', '.join(r['new_entities'])}: _{r['rephrase']}_")


def _cover_tab(d: actions.Detail) -> None:
    if d.app and d.app.cover_letter_text:
        st.text_area("Cover letter", d.app.cover_letter_text, height=260, disabled=True)
        return
    dropped = d.tailoring.get("cover_letter_dropped") or []
    st.info("No cover letter." + (" Drafted sentences that failed verification:" if dropped else ""))
    for item in dropped:
        st.markdown(f"- _{item['sentence']}_ — {item['why']}")


def _dry_run_section(d: actions.Detail) -> None:
    """How the live form would be filled, from `prefill --dry-run`. Nothing was typed or uploaded."""
    plan = d.dry_run
    rows = plan["rows"]
    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    st.markdown(f"**Dry run of the live form** ({plan.get('at', '')[:16].replace('T', ' ')} UTC) — "
                "nothing was typed, uploaded, or submitted.")
    tiles = st.columns(4)
    tiles[0].metric("Questions", len(rows))
    tiles[1].metric("From answers.yaml / resume",
                    sum(n for s, n in by_source.items() if s in ("answers.yaml", "tailored resume", "cover letter")))
    tiles[2].metric("LLM drafts to review", by_source.get("LLM draft (review)", 0))
    tiles[3].metric("Needs you", by_source.get("NEEDS YOU", 0))
    st.dataframe(
        [{"question": r["question"], "type": r["type"], "required": "yes" if r["required"] else "",
          "would fill": r["answer"], "from": r["source"]} for r in rows],
        hide_index=True, width="stretch",
    )
    for label, text in (plan.get("drafted") or {}).items():
        st.warning(f"LLM draft for **{label}** (checked against your resume; edit before a real prefill):")
        st.text(text)
    if plan.get("needs_human"):
        st.error("Before this can be prefilled:\n" + "\n".join(f"- {r}" for r in plan["needs_human"]))
    if plan.get("wants_cover_letter"):
        st.info("This form accepts a cover letter; a real prefill will try to write one.")
    if plan.get("screenshot"):
        try:
            st.image(plan["screenshot"], caption="The form as loaded (untouched)", width="stretch")
        except Exception:  # noqa: BLE001
            pass
    st.link_button("Open this form", plan.get("url", ""))


def _answers_tab(d: actions.Detail) -> dict[str, str]:
    """Show filled answers; LLM drafts are highlighted and editable. Returns edits."""
    edits: dict[str, str] = {}
    if d.dry_run.get("rows"):
        _dry_run_section(d)
    elif not d.answers and not d.drafted:
        st.info("Not prefilled yet (`jobpilot prefill`, or `jobpilot prefill --dry-run` to preview).")
    for label, value in d.answers.items():
        if label in d.drafted:
            st.warning(f"LLM-drafted — review before approving: **{label}**")
            new = st.text_area(label, d.drafted[label], key=f"draft-{d.job.id}-{label}", label_visibility="collapsed")
            if new != d.drafted[label]:
                edits[label] = new
        else:
            st.text_input(label, str(value), disabled=True, key=f"ans-{d.job.id}-{label}")
    if d.app and d.app.error:
        st.error("Needs you: " + d.app.error)
    return edits


def _screens_tab(d: actions.Detail) -> None:
    shown = False
    for label, path in (
        ("Filled form (before submit)", d.app.screenshot_path if d.app else ""),
        ("Confirmation page", d.app.confirmation_screenshot_path if d.app else ""),
    ):
        if path:
            try:
                st.image(path, caption=label, width="stretch")
                shown = True
            except Exception:  # noqa: BLE001 - file removed
                st.warning(f"{label}: screenshot file missing")
    if not shown:
        st.info("No screenshots yet.")


def _history_tab(d: actions.Detail) -> None:
    st.dataframe(
        [{"when (UTC)": e.created_at, "event": e.type, "details": e.payload_json} for e in d.events],
        hide_index=True, width="stretch",
    )


def _actions(d: actions.Detail, edits: dict[str, str]) -> None:
    job = d.job
    st.divider()
    reviewed = st.checkbox(
        "I have reviewed the resume, every answer (including LLM drafts), and the form screenshot.",
        key=f"reviewed-{job.id}", disabled=job.status != "prefilled",
    )
    c1, c2, c3, c4, c5 = st.columns(5)
    if c1.button("Approve & Submit", width="stretch", type="primary", disabled=not (reviewed and job.status == "prefilled"),
                 key=f"approve-{job.id}"):
        from jobpilot.apply.submit import SubmitRefused

        try:
            actions.approve_and_submit(job.id, edits or None)
            st.success("Approved. A browser window is submitting this one application; refresh to see the result.")
        except SubmitRefused as exc:
            st.error(f"Not submitted: {exc}")
    if c2.button("Save edits", disabled=not edits, key=f"edit-{job.id}"):
        actions.save_edits(job.id, edits)
        st.success("Edits saved.")
    if c3.button("Regenerate tailoring", key=f"regen-{job.id}",
                 disabled=job.status not in ("scored", "tailored", "prefilled", "needs_human")):
        with st.spinner("Re-tailoring with the LLM..."):
            st.info(actions.regenerate(job.id))
    if c4.button("Skip", key=f"skip-{job.id}", disabled=job.status in ("submitted", "skipped")):
        actions.skip(job.id)
        st.rerun()
    c5.link_button("Open in browser (manual)", _form_url(d))
    from jobpilot.apply import FILLERS

    # Boards without a form filler (Workable, SmartRecruiters, Recruitee) are never prefilled,
    # so applying by hand is their only route.
    manual_only = job.source not in FILLERS and job.status in ("scored", "tailored")
    if (job.status == "needs_human" or manual_only) and st.button(
        "I applied manually — mark submitted", key=f"manual-{job.id}"
    ):
        actions.mark_applied_manually(job.id)
        st.rerun()


@st.fragment
def job_view(job_id: int) -> None:
    d = actions.detail(job_id)
    if d is None:
        st.error(f"No job {job_id}")
        return
    _header(d)
    tabs = st.tabs(["📄 Resume", "✉️ Cover letter", "📝 Answers", "🖼️ Screenshots", "🕓 History"])
    with tabs[0]:
        _resume_tab(d)
    with tabs[1]:
        _cover_tab(d)
    with tabs[2]:
        edits = _answers_tab(d)
    with tabs[3]:
        _screens_tab(d)
    with tabs[4]:
        _history_tab(d)
    _actions(d, edits)


def queue_page() -> None:
    st.title("Review queue")
    with st.sidebar:
        statuses = st.multiselect("Status", JOB_STATUSES, default=list(actions.QUEUE_STATUSES))
        visa = st.multiselect("Visa flag", ["ok", "unclear", "blocked"], default=[])
        everything = actions.queue(tuple(statuses), exclude_sources=("hn",))
        companies = st.multiselect("Company", sorted({r["company"] for r in everything if r["company"]}), default=[])
        min_score = st.slider("Minimum score", 0, 100, 0)
    rows = actions.queue(tuple(statuses), visa or None, companies or None, float(min_score), exclude_sources=("hn",))
    if not rows:
        st.info("Nothing in the queue for these filters. Run the pipeline stages from the Pipeline page.")
        return
    st.caption(f"{len(rows)} jobs, best first. Click a row (or use the picker below) to review it.")
    ids = [r["id"] for r in rows]

    def pick_from_table() -> None:
        # Callbacks run before the script, so `ids` is the table the user just clicked.
        picked = st.session_state["queue-table"].selection.rows
        if picked:
            st.session_state["queue-job"] = ids[picked[0]]

    st.dataframe(
        rows, hide_index=True, width="stretch", height=min(36 * (len(rows) + 1) + 3, 360),
        on_select=pick_from_table, selection_mode="single-row", key="queue-table",
        column_order=("rank", "company", "title", "score", "visa", "status", "location", "lca"),
        column_config={
            "rank": st.column_config.NumberColumn("#", width="small"),
            "company": st.column_config.TextColumn("Company"),
            "title": st.column_config.TextColumn("Role", width=380),
            "score": st.column_config.ProgressColumn("Score", format="%.0f", min_value=0, max_value=100),
            "visa": st.column_config.TextColumn("Visa", width="small"),
            "status": st.column_config.TextColumn("Status"),
            "location": st.column_config.TextColumn("Location"),
            "lca": st.column_config.NumberColumn("LCA filings", width="small"),
        },
    )
    if st.session_state.get("queue-job") not in ids:
        st.session_state.pop("queue-job", None)
    by_id = {r["id"]: r for r in rows}
    job_id = st.selectbox(
        "Reviewing", ids, key="queue-job",
        format_func=lambda i: f"#{by_id[i]['rank']}  {by_id[i]['company']} — {by_id[i]['title']}",
    )
    st.divider()
    job_view(job_id)


def manual_page() -> None:
    st.title("Manual apply")
    st.caption("HN “Who's Hiring” and other non-ATS jobs. Copy the drafted message and send it yourself.")
    rows = actions.queue(("scored", "tailored", "needs_human"), sources=["hn"])
    if not rows:
        st.info("No manual-apply jobs yet. Set `hn.enabled: true` in settings.yaml, then run-daily.")
        return
    for r in rows:
        d = actions.detail(r["id"])
        with st.expander(f"{r['company']} — {r['title']} ({r['location'] or 'location n/a'})"):
            VISA_STYLE.get(d.job.visa_flag, st.info)(f"Visa: {d.job.visa_flag} — {d.job.visa_reason}")
            links = f"[HN comment]({d.job.url})" + (f" · [apply link]({d.job.apply_url})" if d.job.apply_url else "")
            st.markdown(links)
            st.text(d.job.description_text[:1500])
            draft = d.app.outreach_draft if d.app else ""
            if draft:
                st.code(draft, language=None)  # code blocks have a copy button
            if st.button("Redraft message" if draft else "Draft message", key=f"out-{r['id']}"):
                with st.spinner("Drafting from your resume..."):
                    st.info(actions.draft_outreach(r["id"]))
            if st.button("I sent it — mark submitted", key=f"sent-{r['id']}"):
                actions.mark_applied_manually(r["id"])
                st.rerun()


def stats_page() -> None:
    st.title("Stats")
    s = actions.stats()
    tiles = st.columns(4)
    tiles[0].metric("Submitted", s["submitted"])
    for col, (status, n) in zip(tiles[1:], s["responses"].items()):
        col.metric(status.capitalize(), n)
    st.caption("Update responses by hand: `jobpilot mark <job_id> rejected|interviewing|offer`.")

    st.subheader("Applications submitted per day (UTC)")
    if s["per_day"]:
        chart, table = st.columns([3, 1])
        chart.bar_chart({"submitted": s["per_day"]}, y_label="applications", x_label="day")
        table.dataframe([{"day": k, "submitted": v} for k, v in s["per_day"].items()], hide_index=True)
    else:
        st.info("No submissions yet.")

    st.subheader("Jobs by status")
    st.dataframe([{"status": k, "jobs": v} for k, v in s["by_status"].items()], hide_index=True)


# --- Pipeline: the landing page -------------------------------------------------

# (stage key, CLI command, title, what it does)
STAGE_CARDS = (
    ("fetch", "fetch", "1 · Fetch", "Pull every board in companies.yaml (and HN if enabled). Network only, cached 6h."),
    ("filter", "filter", "2 · Filter", "Dedupe, title/location/age rules, visa screen (LLM only for ambiguous wording)."),
    ("score", "score", "3 · Rank", "Embed every filtered job, LLM-rerank the top N against your resume."),
    ("tailor", "tailor", "4 · Tailor", "Resume per top job: your master resume reordered and sharpened for the role, same length."),
    ("dry-run", "prefill", "5 · Dry-run forms", "Read each live form and plan every answer. Types and uploads nothing."),
    ("prefill", "prefill", "6 · Prefill", "Fill forms in the browser and stop before submit. Uploads your resume."),
)


def _stage_args(key: str) -> list[str] | None:
    """Options for one stage card; returns CLI args, or None if the card can't run."""
    if key == "fetch":
        hn = st.checkbox("Include HN Who's Hiring (slow: ~1 LLM call per comment)", key="opt-fetch-hn")
        return ["--hn"] if hn else ["--no-hn"]
    if key == "filter":
        no_llm = st.checkbox("Skip the LLM visa check (ambiguous jobs become 'unclear')", key="opt-filter-nollm")
        return ["--no-llm"] if no_llm else []
    if key == "score":
        top = st.number_input("LLM-rerank the top N", 5, 200, 30, 5, key="opt-score-top")
        rescore = st.checkbox("Re-rank already ranked jobs (after editing your resume)", key="opt-score-re")
        return ["--top", str(top), *(["--rescore"] if rescore else [])]
    if key == "tailor":
        top = st.number_input("Tailor the top N", 1, 100, 15, 1, key="opt-tailor-top")
        cover = st.checkbox("Also write cover letters", key="opt-tailor-cover")
        return ["--top", str(top), "--cover-letter" if cover else "--no-cover-letter"]
    if key == "dry-run":
        top = st.number_input("Dry-run the top N tailored jobs", 1, 100, 15, 1, key="opt-dry-top")
        return ["--top", str(top), "--dry-run"]
    if key == "prefill":
        top = st.number_input("Prefill the top N", 1, 50, 5, 1, key="opt-prefill-top")
        ok = st.checkbox("I understand this uploads my resume to each company's form (nothing is submitted)",
                         key="opt-prefill-ok")
        return ["--top", str(top), "--headless"] if ok else None
    return []


FUNNEL_GROUPS = (
    ("🔎 Find", "Boards fetched and screened", ("fetched", "awaiting_filter", "filtered_out", "awaiting_score")),
    ("🛠️ Prepare", "Ranked and made ready to apply", ("scored", "tailored", "dry_run", "prefilled")),
    ("✅ Finish", "Waiting on you, then sent", ("needs_human", "approved", "submitted")),
)


def _setup_checks() -> None:
    checks = _readiness(_root())
    errors = [m for level, m in checks if level == "error"]
    warnings = [m for level, m in checks if level == "warning"]
    if errors:
        st.error("**Fix before running the pipeline**\n" + "\n".join(f"- {m}" for m in errors))
    if warnings:
        st.warning("\n".join(f"- {m}" for m in warnings))
    if not errors and not warnings:
        st.success(checks[0][1], icon="✅")


def _funnel() -> None:
    data = actions.funnel()
    counts, labels = data["counts"], {key: (label, why) for key, label, why in actions.FUNNEL}
    # One row per group: side-by-side groups leave tiles too narrow, truncating labels and counts.
    for title, blurb, keys in FUNNEL_GROUPS:
        with st.container(border=True):
            head, *tiles = st.columns([1.3, 1, 1, 1, 1], vertical_alignment="center")
            head.markdown(f"**{title}**")
            head.caption(blurb)
            for tile, key in zip(tiles, keys):
                label, why = labels[key]
                tile.metric(label, f"{counts.get(key, 0):,}", help=why)
    steps = actions.next_steps(counts)
    if steps:
        st.info("**What to do next**\n" + "\n".join(f"- {why}" for _, why in steps), icon="👉")


def _now_running(active: list, external: list[str]) -> None:
    from datetime import datetime, timezone

    from jobpilot import runs

    if external and not active:
        st.warning("A pipeline started outside the UI is running, so stages here are paused until it ends:\n"
                   + "\n".join(f"- `{e}`" for e in external))
    for run in active:
        started = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc)
        elapsed = int((datetime.now(timezone.utc) - started).total_seconds())
        prog = runs.llm_progress(started)
        with st.container(border=True):
            head, stop = st.columns([5, 1], vertical_alignment="center")
            head.markdown(f"**⏳ Running: {run.stage}** · run #{run.id} · {elapsed // 60}m {elapsed % 60:02d}s · "
                          f"{prog['calls']} LLM calls"
                          + (f", ~{prog['median_model_s']:.0f}s each" if prog["median_model_s"] else "")
                          + (f", {prog['errors']} failed" if prog["errors"] else ""))
            if stop.button("Stop this run", key=f"stop-{run.id}", width="stretch"):
                runs.stop(run.id)
                st.rerun()
            # Fixed height, so the page doesn't jump as the log grows.
            st.code(runs.log_tail(run, 40) or "(starting...)", language=None, height=280)


def _status(polling: bool) -> None:
    """Funnel counts and the live run panel."""
    from jobpilot import runs

    active = runs.refresh()
    external = [] if active else runs.external_pipelines()
    if polling and not active and not external:
        # The run just ended: redraw the whole page once, so the stage buttons unlock.
        st.rerun()
    _funnel()
    _now_running(active, external)


# Only poll while something is running; an idle page never reruns on its own.
_status_live = st.fragment(run_every=3)(_status)


@st.fragment
def _stage_card(key: str, command: str, title: str, what: str, paused: bool) -> None:
    """A fragment, so changing a card's options reruns just that card, not the page."""
    from jobpilot import runs

    with st.container(border=True, height="stretch"):
        st.markdown(f"**{title}**")
        st.caption(what)
        args = _stage_args(key)
        if st.button(f"Run {title.split('· ')[1].lower()}", key=f"run-{key}", type="primary",
                     disabled=paused or args is None, width="stretch"):
            try:
                run = runs.start(command, args)
                st.session_state["flash"] = f"Started {command} (run #{run.id})"
            except RuntimeError as exc:
                st.session_state["flash"] = f"⚠️ {exc}"
            st.rerun()


@st.fragment
def _recent_runs() -> None:
    from jobpilot import runs

    recent = runs.recent()
    if not recent:
        st.caption("No runs started from the UI yet.")
        return
    st.dataframe(
        [{"#": r.id, "stage": r.stage, "args": " ".join(json.loads(r.args_json)), "status": r.status,
          "started (UTC)": r.started_at, "finished (UTC)": r.finished_at, "exit": r.exit_code}
         for r in recent],
        hide_index=True, width="stretch",
    )
    pick = st.selectbox("Show log for run", [r.id for r in recent], key="log-pick")
    chosen = next(r for r in recent if r.id == pick)
    st.code(runs.log_tail(chosen, 80) or "(empty)", language=None, height=320)


def pipeline_page() -> None:
    from jobpilot import runs

    st.title("Pipeline")
    st.caption("Run each stage here and watch jobs move through it. Nothing is ever submitted from this page.")
    if flash := st.session_state.pop("flash", None):
        st.toast(flash)

    head, recheck = st.columns([6, 1], vertical_alignment="center")
    head.subheader("Setup")
    if recheck.button("Re-check", key="recheck", width="stretch"):
        _readiness.clear()
    _setup_checks()

    st.subheader("Where your jobs are")
    reason = runs.busy()
    (_status_live if reason else _status)(polling=bool(reason))

    st.subheader("Run a stage")
    if reason:
        st.caption(f"Stages are paused: {reason}.")
    for row in (STAGE_CARDS[:3], STAGE_CARDS[3:]):
        for col, card in zip(st.columns(3), row):
            with col:
                _stage_card(*card, paused=bool(reason))

    st.subheader("Details")
    with st.expander("Where the time goes (LLM calls, last 24h)"):
        stats = _llm_stats(_root())
        if stats:
            st.dataframe(stats, hide_index=True, width="stretch")
            st.caption("'waiting' is time queued for a free model slot; 'model' is generation time. "
                       "See README → Speeding it up.")
        else:
            st.caption("No LLM calls logged in the last 24h.")

    with st.expander("Jobs by source and status"):
        by_source = actions.funnel()["by_source"]
        statuses = sorted({s for per in by_source.values() for s in per})
        st.dataframe(
            [{"source": src, **{s: per.get(s, 0) for s in statuses}} for src, per in sorted(by_source.items())],
            hide_index=True, width="stretch",
        )

    with st.expander("Recent runs"):
        _recent_runs()


# --- Jobs: the landing page --------------------------------------------------------

# One tailor run at a time is ~half a minute per job on local Ollama (resume + cover letter).
MAX_TAILOR = 25
POSTED_WITHIN = {"Any time": None, "Last 24 hours": 1, "Last 7 days": 7, "Last 30 days": 30}


@st.cache_data(ttl=600, show_spinner="Loading openings...")
def _openings(root: str, version: tuple) -> list[dict]:
    return actions.openings()


def _activity(polling: bool) -> None:
    """The live log of a running fetch or tailor run."""
    from jobpilot import runs

    active = runs.refresh()
    external = [] if active else runs.external_pipelines()
    if polling and not active and not external:
        st.rerun()  # the run just ended: reload the table and unlock the buttons
    _now_running(active, external)


_activity_live = st.fragment(run_every=3)(_activity)


def _start(stage: str, args: list[str]) -> None:
    from jobpilot import runs

    try:
        run = runs.start(stage, args)
        st.session_state["flash"] = f"Started {stage} (run #{run.id})"
    except RuntimeError as exc:
        st.session_state["flash"] = f"⚠️ {exc}"
    st.rerun()


def _fetch_card(paused: bool) -> None:
    with st.container(border=True):
        go, opts = st.columns([1, 3], vertical_alignment="center")
        hn = opts.checkbox("Include HN Who's Hiring (slow: ~1 LLM call per comment)", key="opt-fetch-hn")
        opts.caption("Every board in companies.yaml, fetched in parallel (≥1 s between requests to the same "
                     "site). Responses are cached for 6 h, so fetching again within that window takes seconds.")
        if go.button("Fetch openings", key="run-fetch", type="primary", disabled=paused, width="stretch"):
            _start("fetch", ["--hn" if hn else "--no-hn"])


def _filtered(rows: list[dict]) -> tuple[list[dict], tuple]:
    """The table's own filters: plain text matching, nothing that calls the LLM.

    Returns the matching rows and the filter settings that produced them.
    """
    from datetime import date, timedelta

    from jobpilot import config
    from jobpilot.filters.rules import check_location, check_title

    search, when, status, flags = st.columns([3, 1.2, 2, 2], vertical_alignment="bottom")
    words = search.text_input("Search", key="jobs-q", placeholder="title, company, or location, e.g. backend new york").lower().split()
    days = POSTED_WITHIN[when.selectbox("Posted", list(POSTED_WITHIN), key="jobs-age")]
    present = sorted({r["status"] for r in rows})
    default = [s for s in present if s not in actions.DONE_STATUSES]
    statuses = set(status.multiselect("Status", present, default=default, key="jobs-status"))
    hide_blocked = flags.checkbox("Hide sponsorship blockers", key="jobs-noblock")
    my_rules = flags.checkbox("Only my title/location rules", key="jobs-rules",
                              help="The title_include / title_exclude / locations_allow lists in settings.yaml.")

    cutoff = date.today() - timedelta(days=days) if days else None
    cfg = config.settings().get("filters", {})
    out = []
    for r in rows:
        if r["status"] not in statuses or (hide_blocked and r["sponsorship"]):
            continue
        if cutoff and (r["posted"] is None or r["posted"] < cutoff):
            continue
        if words:
            haystack = f"{r['title']} {r['company']} {r['location']}".lower()
            if not all(w in haystack for w in words):
                continue
        if my_rules and (check_title(r["title"], cfg) or check_location(r["location"], r["remote"], cfg)):
            continue
        out.append(r)
    return out, (tuple(words), days, tuple(sorted(statuses)), hide_blocked, my_rules)


def _openings_table(paused: bool) -> None:
    rows = _openings(_root(), actions.jobs_version())
    view, settings = _filtered(rows)
    st.caption(f"{len(view):,} of {len(rows):,} openings, newest first. Tick rows to tailor them; "
               "**Apply ↗** opens the posting on the company's site.")
    # The key follows the filters, so a ticked row can't silently point at a different job.
    table = st.dataframe(
        view, hide_index=True, width="stretch", height=460, on_select="rerun", selection_mode="multi-row",
        key=f"jobs-table-{hash(settings)}",
        column_order=("apply", "company", "title", "location", "posted", "tailored", "sponsorship", "status"),
        column_config={
            "apply": st.column_config.LinkColumn("Apply", display_text="Apply ↗", width="small"),
            "company": st.column_config.TextColumn("Company"),
            "title": st.column_config.TextColumn("Role", width=360),
            "location": st.column_config.TextColumn("Location"),
            "posted": st.column_config.DateColumn("Posted", width="small"),
            "tailored": st.column_config.CheckboxColumn("Tailored", width="small"),
            "sponsorship": st.column_config.TextColumn("Sponsorship", help="A visa.blocking_phrases hit in the description."),
            "status": st.column_config.TextColumn("Status", width="small"),
        },
    )
    picked = [view[i]["id"] for i in table.selection.rows]
    go, note = st.columns([1, 3], vertical_alignment="center")
    if go.button(f"Tailor resume + cover letter ({len(picked)})", key="tailor-picked", type="primary",
                 disabled=paused or not picked or len(picked) > MAX_TAILOR, width="stretch"):
        _start("tailor", actions.tailor_args(picked))
    note.caption(f"About half a minute per job with local Ollama; up to {MAX_TAILOR} per run. "
                 "Tailored files show up under **Ready to apply** below.")


def _download(col, label: str, path: str, key: str) -> None:
    try:
        with open(path, "rb") as fh:
            col.download_button(label, fh.read(), file_name=path.rsplit("/", 1)[-1], key=key, width="stretch")
    except OSError:
        col.caption(f"{label}: file missing")


@st.fragment
def _ready_section() -> None:
    ready = actions.ready_to_apply()
    if not ready:
        st.info("Nothing tailored yet. Tick openings above and tailor them.")
        return
    by_id = {r["id"]: r for r in ready}
    job_id = st.selectbox("Job", list(by_id), key="ready-job",
                          format_func=lambda i: f"{by_id[i]['company']} — {by_id[i]['title']} ({by_id[i]['location'] or 'n/a'})")
    r = by_id[job_id]
    apply_col, resume_col, cover_col, done_col, skip_col = st.columns(5)
    apply_col.link_button("Apply on company site ↗", r["apply"] or "about:blank", type="primary",
                          disabled=not r["apply"], width="stretch")
    _download(resume_col, "Download resume", r["resume"], f"dl-resume-{job_id}")
    if r["cover_letter"]:
        _download(cover_col, "Download cover letter", r["cover_letter"], f"dl-cover-{job_id}")
    else:
        cover_col.caption("No cover letter (every draft failed the fabrication check, or none was asked for).")
    if done_col.button("I applied", key=f"applied-{job_id}", width="stretch"):
        actions.mark_applied_manually(job_id)
        st.rerun(scope="app")
    if skip_col.button("Skip", key=f"ready-skip-{job_id}", width="stretch"):
        actions.skip(job_id)
        st.rerun(scope="app")
    if r["cover_letter_text"]:
        with st.expander("Cover letter text (for forms with a text box)"):
            st.code(r["cover_letter_text"], language=None, wrap_lines=True)
    with st.expander("Resume preview"):
        png = actions.resume_preview(job_id)
        if png:
            st.image(str(png), width="stretch")
        else:
            st.caption("No preview available.")


def jobs_page() -> None:
    from jobpilot import runs

    st.title("Jobs")
    st.caption("Fetch every opening, tick the ones you want, tailor a resume and cover letter for them, "
               "then apply on the company's site yourself.")
    if flash := st.session_state.pop("flash", None):
        st.toast(flash)
    _setup_checks()

    reason = runs.busy()
    (_activity_live if reason else _activity)(polling=bool(reason))
    if reason:
        st.caption(f"Fetch and tailor are paused: {reason}.")
    _fetch_card(paused=bool(reason))

    st.subheader("Openings")
    _openings_table(paused=bool(reason))

    st.subheader("Ready to apply")
    _ready_section()


PAGES = {"Jobs": jobs_page, "Stats": stats_page}
# The rank / prefill / approve-and-submit flow, kept for anyone who still wants it.
OLD_PAGES = {"Pipeline": pipeline_page, "Review queue": queue_page, "Manual apply": manual_page}
with st.sidebar:
    st.markdown("## 🧭 JobPilot")
    st.caption("Find openings, tailor your resume and cover letter, apply yourself.")
    old = st.toggle("Show the old auto-fill pipeline", key="show-old")
    pages = {**PAGES, **(OLD_PAGES if old else {})}
    page = st.radio("View", list(pages))
    st.divider()
pages[page]()
