"""JobPilot review UI (Streamlit). Launch with `jobpilot ui`.

Nothing is submitted from anywhere but the "Approve & Submit" button on a
single prefilled job, and that button stays disabled until you tick the
"I reviewed this" box.
"""

from __future__ import annotations

import json

import streamlit as st

from jobpilot import db
from jobpilot.models import JOB_STATUSES
from jobpilot.ui import actions

st.set_page_config(page_title="JobPilot review", layout="wide")
db.init_db()

VISA_STYLE = {"ok": st.success, "unclear": st.warning, "blocked": st.error}


def _form_url(d: actions.Detail) -> str:
    from jobpilot.apply import FILLERS

    if d.job.source in FILLERS and d.company is not None:
        return FILLERS[d.job.source].form_url(d.job, d.company)
    return d.job.apply_url or d.job.url


def _header(d: actions.Detail) -> None:
    job, company = d.job, d.company
    st.subheader(f"{job.title} — {company.name if company else '?'}")
    st.caption(f"{job.location or 'location not stated'} · {job.source} · status **{job.status}** · job #{job.id}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Final score", f"{job.final_score:.1f}" if job.final_score is not None else "–")
    c2.metric("LLM fit", f"{job.llm_score:.0f}" if job.llm_score is not None else "–")
    c3.metric("Embedding", f"{job.embed_score:.3f}" if job.embed_score is not None else "–")
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
    if reasons:
        st.markdown("**Why it ranks here**\n" + "\n".join(f"- {r}" for r in reasons))
    if d.details.get("missing_skills"):
        st.markdown("**Missing skills:** " + ", ".join(d.details["missing_skills"]))
    with st.expander("Job description"):
        st.text(job.description_text)


def _resume_tab(d: actions.Detail) -> None:
    if not d.app or not d.app.resume_path:
        st.info("Not tailored yet (`jobpilot tailor`).")
        return
    png = actions.resume_preview(d.job.id)
    if png:
        st.image(str(png), use_container_width=True)
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
        hide_index=True, use_container_width=True,
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
            st.image(plan["screenshot"], caption="The form as loaded (untouched)", use_container_width=True)
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
                st.image(path, caption=label, use_container_width=True)
                shown = True
            except Exception:  # noqa: BLE001 - file removed
                st.warning(f"{label}: screenshot file missing")
    if not shown:
        st.info("No screenshots yet.")


def _history_tab(d: actions.Detail) -> None:
    st.dataframe(
        [{"when (UTC)": e.created_at, "event": e.type, "details": e.payload_json} for e in d.events],
        hide_index=True, use_container_width=True,
    )


def _actions(d: actions.Detail, edits: dict[str, str]) -> None:
    job = d.job
    st.divider()
    reviewed = st.checkbox(
        "I have reviewed the resume, every answer (including LLM drafts), and the form screenshot.",
        key=f"reviewed-{job.id}", disabled=job.status != "prefilled",
    )
    c1, c2, c3, c4, c5 = st.columns(5)
    if c1.button("Approve & Submit", type="primary", disabled=not (reviewed and job.status == "prefilled"),
                 key=f"approve-{job.id}"):
        from jobpilot.apply.submit import SubmitRefused

        try:
            actions.approve_and_submit(job.id, edits or None)
        except SubmitRefused as exc:
            st.error(f"Not submitted: {exc}")
        else:
            # Rerun the page so "Ready to submit" moves straight on to the next application.
            st.session_state["flash"] = f"Approved job {job.id}; submitting it in the background."
            st.rerun()
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


def job_view(job_id: int) -> None:
    d = actions.detail(job_id)
    if d is None:
        st.error(f"No job {job_id}")
        return
    _header(d)
    tabs = st.tabs(["Resume", "Cover letter", "Answers", "Screenshots", "History"])
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


def _show_flash() -> None:
    if flash := st.session_state.pop("flash", None):
        st.success(flash)


def submit_page() -> None:
    """Prefilled applications one at a time: review, approve, and move on to the next.

    Every job still needs its own "I reviewed this" tick and Approve & Submit click;
    this page only saves the hunting through the full queue between them.
    """
    st.title("Ready to submit")
    _show_flash()
    board = actions.submit_board()
    tiles = st.columns(3)
    tiles[0].metric("Submitted today (UTC)", f"{board['today']} / {board['cap']}")
    rows = actions.queue(("prefilled",), exclude_sources=("hn",))
    tiles[1].metric("Waiting for your review", len(rows))
    tiles[2].metric("Submitting now", len(board["in_flight"]))
    if board["in_flight"]:
        with st.expander(f"{len(board['in_flight'])} approved, submitting in the background"):
            for item in board["in_flight"]:
                st.markdown(f"**{item['company']} — {item['title']}** (job {item['id']})")
                st.code(actions.submit_log_tail(item["id"]) or "(waiting for the browser)", language=None)
            st.button("Refresh", key="refresh-submits")
    if not rows:
        st.info("Nothing is prefilled. Run **Prefill** on the Pipeline page, then come back here.")
        return

    ids = [r["id"] for r in rows]
    pos = st.session_state["submit-pos"] = min(st.session_state.get("submit-pos", 0), len(ids) - 1)

    def step(delta: int) -> None:  # a callback, so the buttons below render with the new position
        st.session_state["submit-pos"] += delta

    prev, where, nxt = st.columns([1, 4, 1], vertical_alignment="center")
    prev.button("← Previous", disabled=pos == 0, width="stretch", on_click=step, args=(-1,))
    nxt.button("Next →", disabled=pos >= len(ids) - 1, width="stretch", on_click=step, args=(1,))
    where.markdown(f"**{pos + 1} of {len(ids)}**, best score first")
    st.divider()
    job_view(ids[pos])


def queue_page() -> None:
    st.title("Review queue")
    _show_flash()
    with st.sidebar:
        statuses = st.multiselect("Status", JOB_STATUSES, default=list(actions.QUEUE_STATUSES))
        visa = st.multiselect("Visa flag", ["ok", "unclear", "blocked"], default=[])
        everything = actions.queue(tuple(statuses), exclude_sources=("hn",))
        companies = st.multiselect("Company", sorted({r["company"] for r in everything if r["company"]}), default=[])
        min_score = st.slider("Minimum score", 0, 100, 0)
    rows = actions.queue(tuple(statuses), visa or None, companies or None, float(min_score), exclude_sources=("hn",))
    if not rows:
        st.info("Nothing in the queue for these filters. Run `jobpilot run-daily`.")
        return
    event = st.dataframe(
        rows, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row",
        column_config={"score": st.column_config.NumberColumn(format="%.1f"),
                       "lca": st.column_config.NumberColumn("LCA filings")},
        key="queue-table",
    )
    picked = event.selection.rows if event and event.selection else []
    ids = [r["id"] for r in rows]
    job_id = st.selectbox(
        "Job", ids, index=picked[0] if picked else 0,
        format_func=lambda i: next(f"#{r['rank']} {r['company']} — {r['title']}" for r in rows if r["id"] == i),
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
    ("tailor", "tailor", "4 · Tailor", "One-page resume per top job, built only from your master resume."),
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
        return ["--top", str(top), "--dry-run", *_parallel_arg("dry")]
    if key == "prefill":
        top = st.number_input("Prefill the top N", 1, 50, 5, 1, key="opt-prefill-top")
        parallel = _parallel_arg("prefill")
        ok = st.checkbox("I understand this uploads my resume to each company's form (nothing is submitted)",
                         key="opt-prefill-ok")
        return ["--top", str(top), "--headless", *parallel] if ok else None
    return []


def _parallel_arg(key: str) -> list[str]:
    n = st.number_input("Forms at once (separate tabs)", 1, 6, 3, 1, key=f"opt-{key}-parallel",
                        help="Page loads dominate; LLM drafts still take turns on the model.")
    return ["--parallel", str(n)]


def _one_click(reason: str | None) -> None:
    """fetch -> filter -> rank -> tailor as one run: everything that uploads nothing."""
    from jobpilot import runs

    with st.container(border=True):
        text, top_col, go = st.columns([4, 1, 1], vertical_alignment="center")
        text.markdown("**Find & prepare, one click** — fetch, filter, rank, and tailor in one run. "
                      "Nothing is uploaded or submitted; afterwards, dry-run or prefill the results below.")
        top = top_col.number_input("Top N", 1, 100, 15, 1, key="opt-daily-top")
        if go.button("Run all four", key="run-daily", type="primary", disabled=bool(reason), width="stretch"):
            try:
                run = runs.start("run-daily", ["--top", str(top), "--skip-prefill"])
                st.toast(f"Started fetch → tailor (run #{run.id})")
            except RuntimeError as exc:
                st.error(str(exc))
            st.rerun()


@st.fragment(run_every=3)
def _live_runs() -> None:
    from datetime import datetime, timezone

    from jobpilot import runs

    active = runs.refresh()
    if not active:
        external = runs.external_pipelines()
        if external:
            st.warning("A pipeline started outside the UI is running, so stages here are paused until it ends:\n"
                       + "\n".join(f"- `{e}`" for e in external))
        return
    for run in active:
        started = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc)
        elapsed = int((datetime.now(timezone.utc) - started).total_seconds())
        prog = runs.llm_progress(started)
        st.info(f"**Running: {run.stage}** (run #{run.id}) · {elapsed // 60}m {elapsed % 60:02d}s · "
                f"{prog['calls']} LLM calls so far"
                + (f", ~{prog['median_model_s']:.0f}s each" if prog["median_model_s"] else "")
                + (f", {prog['errors']} failed" if prog["errors"] else ""))
        st.code(runs.log_tail(run, 25) or "(starting...)", language=None)
        if st.button("Stop this run", key=f"stop-{run.id}"):
            runs.stop(run.id)
            st.rerun()


def pipeline_page() -> None:
    from jobpilot import runs

    st.title("Pipeline")
    st.caption("Run each stage here and watch jobs move through it. Nothing is ever submitted from this page.")

    for level, message in actions.readiness():
        {"error": st.error, "warning": st.warning, "ok": st.success}[level](message)

    data = actions.funnel()
    counts = data["counts"]
    for chunk in (actions.FUNNEL[:6], actions.FUNNEL[6:]):
        cols = st.columns(len(chunk))
        for col, (key, label) in zip(cols, chunk):
            col.metric(label, counts.get(key, 0))

    steps = actions.next_steps(counts)
    if steps:
        st.markdown("**Next:** " + " · ".join(why for _, why in steps))

    st.subheader("Run a stage")
    _live_runs()
    reason = runs.busy()
    _one_click(reason)
    for row in (STAGE_CARDS[:3], STAGE_CARDS[3:]):
        cols = st.columns(3)
        for col, (key, command, title, what) in zip(cols, row):
            with col.container(border=True):
                st.markdown(f"**{title}**")
                st.caption(what)
                args = _stage_args(key)
                if st.button(f"Run {title.split('· ')[1].lower()}", key=f"run-{key}",
                             disabled=bool(reason) or args is None, use_container_width=True):
                    try:
                        run = runs.start(command, args)
                        st.toast(f"Started {command} (run #{run.id})")
                    except RuntimeError as exc:
                        st.error(str(exc))
                    st.rerun()
    if reason:
        st.caption(f"Stages are paused: {reason}.")

    with st.expander("Where the time goes (LLM calls, last 24h)"):
        stats = actions.llm_stats()
        if stats:
            st.dataframe(stats, hide_index=True, use_container_width=True)
            st.caption("'waiting' is time queued for a free model slot; 'model' is generation time. "
                       "See README → Speeding it up.")
        else:
            st.caption("No LLM calls logged in the last 24h.")

    with st.expander("Jobs by source and status"):
        statuses = sorted({s for per in data["by_source"].values() for s in per})
        st.dataframe(
            [{"source": src, **{s: per.get(s, 0) for s in statuses}} for src, per in sorted(data["by_source"].items())],
            hide_index=True, use_container_width=True,
        )

    with st.expander("Recent runs"):
        recent = runs.recent()
        if recent:
            st.dataframe(
                [{"#": r.id, "stage": r.stage, "args": " ".join(json.loads(r.args_json)), "status": r.status,
                  "started (UTC)": r.started_at, "finished (UTC)": r.finished_at, "exit": r.exit_code}
                 for r in recent],
                hide_index=True, use_container_width=True,
            )
            pick = st.selectbox("Show log for run", [r.id for r in recent], key="log-pick")
            chosen = next(r for r in recent if r.id == pick)
            st.code(runs.log_tail(chosen, 80) or "(empty)", language=None)
        else:
            st.caption("No runs started from the UI yet.")


PAGES = {"Pipeline": pipeline_page, "Ready to submit": submit_page, "Review queue": queue_page,
         "Manual apply": manual_page, "Stats": stats_page}
PAGES[st.sidebar.radio("View", list(PAGES))]()
