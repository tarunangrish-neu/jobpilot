"""Greenhouse filler.

Many companies front Greenhouse with their own careers site (stripe.com/jobs,
databricks.com/...), so `absolute_url` is not always a fillable form -- and
even job-boards.greenhouse.io/{token}/jobs/{id} redirects there for some
boards (verified for Stripe, 2026-09-23). The embed form
job-boards.greenhouse.io/embed/job_app?for={token}&token={id} always serves it.
Questions use react-select comboboxes; resume and cover letter are two file
inputs both labelled "Attach", told apart by their ids (`resume`, `cover_letter`).
"""

SUBMIT_SELECTORS = ('button:has-text("Submit application")', 'button[type="submit"]', 'input[type="submit"]')


def form_url(job, company) -> str:
    return f"https://job-boards.greenhouse.io/embed/job_app?for={company.token}&token={job.external_id}"
