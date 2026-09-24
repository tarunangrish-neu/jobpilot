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
POSTED_WITHIN = {"Any time": None, "Last 24 hours": 1, "Last 3 days": 3, "Last 7 days": 7, "Last 30 days": 30}
YEARS_ASKED = {"Any": None, "≤ 1 year": 1, "≤ 2 years": 2, "≤ 3 years": 3, "≤ 5 years": 5}
READY = "Ready to apply"


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


def _live_activity() -> bool:
    """Show the running fetch/tailor, if any; True when new runs must wait."""
    from jobpilot import runs

    reason = runs.busy()
    (_activity_live if reason else _activity)(polling=bool(reason))
    return bool(reason)


def _start(stage: str, args: list[str]) -> None:
    from jobpilot import runs

    try:
        run = runs.start(stage, args)
        st.session_state["flash"] = f"Started {stage} (run #{run.id}); its log is at the top of the page."
    except RuntimeError as exc:
        st.session_state["flash"] = f"⚠️ {exc}"
    st.rerun()


def _go_ready() -> None:
    st.session_state["nav"] = READY


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.replace(",", "\n").splitlines() if ln.strip()]


def _filters_editor(paused: bool) -> None:
    cur = actions.current_filters()
    names = sorted(set(actions.company_names()) | set(cur["companies_exclude"]))
    with st.expander("⚙️ **Fetch filters**: applied after every fetch, no LLM. Edit and re-screen any time."):
        with st.form("fetch-filters", border=False):
            left, right = st.columns(2)
            include = left.text_area("Title contains one of", "\n".join(cur["title_include"]), height=150,
                                     help="One per line (or comma-separated). Empty means any title.")
            exclude = right.text_area("Title contains none of", "\n".join(cur["title_exclude"]), height=150,
                                      help="e.g. senior, staff, intern, sales.")
            locations = left.text_area("Allowed locations", "\n".join(cur["locations_allow"]), height=150,
                                       help="One per line. 'united states' / 'usa' / 'us' also allow any US state; "
                                            "'remote' allows a bare 'Remote'.")
            description = right.text_area("Skip jobs whose description mentions", "\n".join(cur["description_exclude"]),
                                          height=150, help="e.g. security clearance, polygraph, 10+ years.")
            companies = st.multiselect("Skip these companies", names, default=cur["companies_exclude"])
            age, years, flags = st.columns(3, vertical_alignment="bottom")
            max_age = age.number_input("Posted within (days, 0 = any)", 0, 365, int(cur["max_posting_age_days"] or 0))
            max_years = years.number_input("Max years of experience asked (0 = any)", 0, 20,
                                           int(cur["max_years_experience"] or 0),
                                           help="Uses phrases like '5+ years of experience' in the description; "
                                                "jobs that never say are kept.")
            drop_blocked = flags.checkbox("Skip sponsorship blockers", bool(cur["drop_sponsorship_blockers"]),
                                          help="Descriptions with a visa.blocking_phrases match, e.g. 'will not sponsor'.")
            remote_any = flags.checkbox("Keep remote jobs anywhere", bool(cur["allow_remote_anywhere"]),
                                        help="Off: 'Remote - EU' is dropped unless the location is allowed.")
            saved = st.form_submit_button("Save filters & re-screen jobs", type="primary", disabled=paused)
    if saved:
        with st.spinner("Re-screening every fetched job (no network, no LLM)..."):
            report = actions.save_filters_and_rescreen({
                "title_include": _lines(include), "title_exclude": _lines(exclude),
                "locations_allow": _lines(locations), "allow_remote_anywhere": remote_any,
                "max_posting_age_days": int(max_age), "max_years_experience": int(max_years),
                "companies_exclude": companies, "description_exclude": _lines(description),
                "drop_sponsorship_blockers": drop_blocked,
            })
        st.session_state["flash"] = (f"Filters saved: {report.passed:,} jobs pass, "
                                     f"{report.considered - report.passed:,} screened out.")
        st.rerun()


def _fetch_card(paused: bool) -> None:
    with st.container(border=True):
        go, opts = st.columns([1, 3], vertical_alignment="center")
        hn = opts.checkbox("Include HN Who's Hiring (slow: ~1 LLM call per comment)", key="opt-fetch-hn")
        opts.caption("Every board in companies.yaml, fetched in parallel (≥1 s between requests to the same "
                     "site), then screened with your fetch filters. Responses are cached for 6 h.")
        if go.button("Fetch openings", key="run-fetch", type="primary", disabled=paused, width="stretch"):
            _start("fetch", ["--hn" if hn else "--no-hn"])
        _filters_editor(paused)


def _filtered(rows: list[dict]) -> tuple[list[dict], tuple, bool]:
    """The table's own filters, instant and without the LLM.

    Returns the matching rows, the settings that produced them, and whether
    screened-out jobs are shown.
    """
    from datetime import date, timedelta

    from jobpilot.filters.screen import LEVELS, ROLE_TYPES

    search, when, kind, level = st.columns([3, 1.3, 2, 2], vertical_alignment="bottom")
    words = search.text_input("Search", key="jobs-q",
                              placeholder="title, company, or location, e.g. backend new york").lower().split()
    days = POSTED_WITHIN[when.selectbox("Posted", list(POSTED_WITHIN), key="jobs-age")]
    kinds = set(kind.multiselect("Role type", [n for n, _ in ROLE_TYPES] + ["Other"], key="jobs-type",
                                 placeholder="Any role"))
    levels = set(level.multiselect("Level", LEVELS, key="jobs-level", placeholder="Any level"))
    company, years, flags1, flags2 = st.columns([3, 1.3, 2, 2], vertical_alignment="bottom")
    companies = set(company.multiselect("Company", sorted({r["company"] for r in rows if r["company"]}),
                                        key="jobs-company", placeholder="Any company"))
    max_years = YEARS_ASKED[years.selectbox("Experience asked", list(YEARS_ASKED), key="jobs-years",
                                            help="Jobs whose description never states years are kept.")]
    hide_blocked = flags1.checkbox("Hide sponsorship blockers", key="jobs-noblock")
    untailored = flags1.checkbox("Not tailored yet", key="jobs-untailored")
    show_screened = flags2.checkbox("Show screened-out jobs", key="jobs-screened",
                                    help="Jobs your fetch filters dropped; the reason is in 'Screened out because'.")
    show_done = flags2.checkbox("Show applied / skipped", key="jobs-done")

    cutoff = date.today() - timedelta(days=days) if days else None
    out = []
    for r in rows:
        if r["status"] == "filtered_out" and not show_screened:
            continue
        if r["status"] in actions.DONE_STATUSES and not show_done:
            continue
        if (hide_blocked and r["sponsorship"]) or (untailored and r["tailored"]):
            continue
        if cutoff and (r["posted"] is None or r["posted"] < cutoff):
            continue
        if kinds and not kinds & set(r["type"].split(", ")):
            continue
        if levels and r["level"] not in levels:
            continue
        if companies and r["company"] not in companies:
            continue
        if max_years is not None and r["years"] is not None and r["years"] > max_years:
            continue
        if words:
            haystack = f"{r['title']} {r['company']} {r['location']}".lower()
            if not all(w in haystack for w in words):
                continue
        out.append(r)
    settings = (tuple(words), days, tuple(sorted(kinds)), tuple(sorted(levels)), tuple(sorted(companies)),
                max_years, hide_blocked, untailored, show_screened, show_done)
    return out, settings, show_screened


def _openings_table(paused: bool) -> None:
    rows = _openings(_root(), actions.jobs_version())
    view, settings, show_screened = _filtered(rows)
    # Filled in after the table, but shown above it: the button is the next step, so keep it in view.
    bar = st.container(border=True)
    columns = ["apply", "company", "title", "type", "level", "years", "location", "posted", "tailored",
               "sponsorship"] + (["why_hidden"] if show_screened else []) + ["status"]
    table = st.dataframe(
        view, hide_index=True, width="stretch", height=520, on_select="rerun", selection_mode="multi-row",
        key=f"jobs-table-{hash(settings)}", column_order=columns,
        column_config={
            "apply": st.column_config.LinkColumn("Apply", display_text="Apply ↗", width="small"),
            "company": st.column_config.TextColumn("Company"),
            "title": st.column_config.TextColumn("Role", width=320),
            "type": st.column_config.TextColumn("Type"),
            "level": st.column_config.TextColumn("Level", width="small"),
            "years": st.column_config.NumberColumn("Yrs asked", width="small",
                                                   help="Most years of experience the description asks for."),
            "location": st.column_config.TextColumn("Location"),
            "posted": st.column_config.DateColumn("Posted", width="small"),
            "tailored": st.column_config.CheckboxColumn("Tailored", width="small"),
            "sponsorship": st.column_config.TextColumn("Sponsorship", help="A visa.blocking_phrases hit in the description."),
            "why_hidden": st.column_config.TextColumn("Screened out because"),
            "status": st.column_config.TextColumn("Status", width="small"),
        },
    )
    picked = [view[i]["id"] for i in table.selection.rows]
    with bar:
        go, note = st.columns([1.4, 3], vertical_alignment="center")
        label = f"✨ Tailor resume + cover letter for {len(picked)} job{'s' if len(picked) != 1 else ''}"
        if go.button(label, key="tailor-picked", type="primary", width="stretch",
                     disabled=paused or not picked or len(picked) > MAX_TAILOR):
            _start("tailor", actions.tailor_args(picked))
        if not picked:
            note.markdown(f"**{len(view):,}** of {len(rows):,} openings. **Tick the box at the left of a row** to "
                          "pick it, then tailor. **Apply ↗** opens the posting on the company's site.")
        elif len(picked) > MAX_TAILOR:
            note.warning(f"{len(picked)} ticked; tailor at most {MAX_TAILOR} per run.")
        else:
            note.markdown(f"{len(picked)} ticked · about {len(picked) * 45 // 60 or 1} min with local Ollama. "
                          f"The results appear on **{READY}**.")
    if picked:
        d = actions.detail(picked[-1])
        if d is not None:
            with st.expander(f"Job description: {d.job.title} — {d.company.name if d.company else ''}"):
                st.markdown(_plain(d.job.description_text) if d.job.description_text else "_No description._")


def jobs_page() -> None:
    st.title("Jobs")
    st.caption("① Fetch → ② tick the openings you want → ③ **Tailor resume + cover letter** → "
               f"④ apply from **{READY}** with your tailored files.")
    if flash := st.session_state.pop("flash", None):
        st.toast(flash)
    _setup_checks()
    paused = _live_activity()
    _fetch_card(paused)

    ready = actions.ready_to_apply()
    if ready:
        covers = sum(1 for r in ready if r["cover_letter"])
        note, go = st.columns([3, 1], vertical_alignment="center")
        note.success(f"**{len(ready)}** tailored job{'s are' if len(ready) != 1 else ' is'} ready to apply "
                     f"({covers} with a cover letter).")
        go.button("Open Ready to apply →", on_click=_go_ready, width="stretch", type="primary")

    st.subheader("Openings")
    _openings_table(paused)


# --- Ready to apply ---------------------------------------------------------------


def _download(col, label: str, path: str, key: str) -> None:
    try:
        with open(path, "rb") as fh:
            col.download_button(label, fh.read(), file_name=path.rsplit("/", 1)[-1], key=key, width="stretch")
    except OSError:
        col.caption(f"{label}: file missing")


def ready_page() -> None:
    st.title(READY)
    st.caption("Your tailored resume and cover letter per job. Open the posting, apply on the company's "
               "site with these files, then mark it applied.")
    if flash := st.session_state.pop("flash", None):
        st.toast(flash)
    paused = _live_activity()
    ready = actions.ready_to_apply()
    if not ready:
        st.info("Nothing tailored yet. On **Jobs**, tick the openings you want and click "
                "**Tailor resume + cover letter**.")
        return

    by_id = {r["id"]: r for r in ready}
    ids = list(by_id)
    missing = [r["id"] for r in ready if not r["cover_letter"]]
    if missing:
        note, go = st.columns([3, 1.3], vertical_alignment="center")
        note.info(f"{len(missing)} of these have no cover letter yet (tailored before letters were written by "
                  f"default). Each takes ~15 s; up to {MAX_TAILOR} per run.")
        batch = missing[:MAX_TAILOR]
        if go.button(f"✨ Write {len(batch)} missing cover letter{'s' if len(batch) != 1 else ''}", key="covers-missing", disabled=paused,
                     width="stretch"):
            _start("tailor", actions.tailor_args(batch))

    def pick_from_table() -> None:
        rows = st.session_state["ready-table"].selection.rows
        if rows:
            st.session_state["ready-job"] = ids[rows[0]]

    st.dataframe(
        [{**r, "has_cover": bool(r["cover_letter"])} for r in ready], hide_index=True, width="stretch",
        height=min(36 * (len(ready) + 1) + 3, 320), on_select=pick_from_table, selection_mode="single-row",
        key="ready-table", column_order=("apply", "company", "title", "location", "has_cover"),
        column_config={
            "apply": st.column_config.LinkColumn("Apply", display_text="Apply ↗", width="small"),
            "company": st.column_config.TextColumn("Company"),
            "title": st.column_config.TextColumn("Role", width=380),
            "location": st.column_config.TextColumn("Location"),
            "has_cover": st.column_config.CheckboxColumn("Cover letter", width="small"),
        },
    )
    if st.session_state.get("ready-job") not in by_id:
        st.session_state.pop("ready-job", None)
    job_id = st.selectbox("Job", ids, key="ready-job",
                          format_func=lambda i: f"{by_id[i]['company']} — {by_id[i]['title']}")
    r = by_id[job_id]

    st.subheader(f"{r['title']} — {r['company']}")
    st.caption(f"📍 {r['location'] or 'location not stated'} · job #{job_id}")
    apply_col, resume_col, cover_col, done_col, skip_col = st.columns(5)
    apply_col.link_button("Apply on company site ↗", r["apply"] or "about:blank", type="primary",
                          disabled=not r["apply"], width="stretch")
    _download(resume_col, "⬇ Resume PDF", r["resume"], f"dl-resume-{job_id}")
    if r["cover_letter"]:
        _download(cover_col, "⬇ Cover letter PDF", r["cover_letter"], f"dl-cover-{job_id}")
    elif cover_col.button("✨ Write cover letter", key=f"cover-{job_id}", disabled=paused, width="stretch",
                          help="The resume plan is cached, so this only pays for the letter (~15 s)."):
        _start("tailor", actions.tailor_args([job_id]))
    if done_col.button("✅ I applied", key=f"applied-{job_id}", width="stretch"):
        actions.mark_applied_manually(job_id)
        st.session_state["flash"] = f"Marked {r['company']} — {r['title']} as applied."
        st.rerun()
    if skip_col.button("Skip", key=f"ready-skip-{job_id}", width="stretch"):
        actions.skip(job_id)
        st.rerun()

    resume_tab, cover_tab, job_tab = st.tabs(["📄 Resume", "✉️ Cover letter", "🧾 Job description"])
    with resume_tab:
        png = actions.resume_preview(job_id)
        if png:
            st.image(str(png), width="stretch")
        else:
            st.caption("No preview available; download the PDF above.")
        if st.button("Re-tailor from scratch", key=f"regen-{job_id}", disabled=paused,
                     help="Ignores the cached plan and asks the model again (~45 s)."):
            _start("tailor", actions.tailor_args([job_id]) + ["--regenerate"])
    with cover_tab:
        if r["cover_letter_text"]:
            st.caption("Copy it into the form's text box, or upload the PDF.")
            st.code(r["cover_letter_text"], language=None, wrap_lines=True)
        else:
            st.info("No cover letter yet: click **Write cover letter** above. Sentences the fabrication check "
                    "can't verify against your resume are dropped, so letters are short and factual.")
    with job_tab:
        d = actions.detail(job_id)
        st.markdown(_plain(d.job.description_text) if d and d.job.description_text else "_No description._")


PAGES = {"Jobs": jobs_page, READY: ready_page, "Stats": stats_page}
# The rank / prefill / approve-and-submit flow, kept for anyone who still wants it.
OLD_PAGES = {"Pipeline": pipeline_page, "Review queue": queue_page, "Manual apply": manual_page}
with st.sidebar:
    st.markdown("## 🧭 JobPilot")
    st.caption("Find openings, tailor your resume and cover letter, apply yourself.")
    old = st.toggle("Show the old auto-fill pipeline", key="show-old")
    pages = {**PAGES, **(OLD_PAGES if old else {})}
    if st.session_state.get("nav") not in pages:
        st.session_state["nav"] = "Jobs"
    n_ready = len(actions.ready_to_apply())
    page = st.radio("View", list(pages), key="nav",
                    format_func=lambda p: f"{p} ({n_ready})" if p == READY else p)
    st.divider()
pages[page]()
