"""JobPilot review UI (Streamlit). Launch with `jobpilot ui`.

Nothing is submitted from anywhere but the "Approve & Submit" button on a
single prefilled job, and that button stays disabled until you tick the
"I reviewed this" box.
"""

from __future__ import annotations

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


def _answers_tab(d: actions.Detail) -> dict[str, str]:
    """Show filled answers; LLM drafts are highlighted and editable. Returns edits."""
    edits: dict[str, str] = {}
    if not d.answers and not d.drafted:
        st.info("Not prefilled yet (`jobpilot prefill`).")
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
    if job.status == "needs_human" and st.button("I applied manually — mark submitted", key=f"manual-{job.id}"):
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


PAGES = {"Queue": queue_page, "Manual apply": manual_page, "Stats": stats_page}
PAGES[st.sidebar.radio("View", list(PAGES))]()
