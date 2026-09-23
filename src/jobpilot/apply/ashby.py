"""Ashby filler.

The form lives at jobs.ashbyhq.com/{token}/{id}/application (the posting's
`applyUrl`). System fields are named `_systemfield_*`; other questions use
UUID names, so matching is by label. There are two file inputs: "Autofill
from resume" (parses the file into the form -- skipped) and the real "Resume".
"""

SUBMIT_SELECTORS = ('button:has-text("Submit Application")', 'button[type="submit"]')


def form_url(job, company) -> str:
    url = job.apply_url or job.url
    return url if url.rstrip("/").endswith("/application") else url.rstrip("/") + "/application"
