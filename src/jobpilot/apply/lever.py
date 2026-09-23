"""Lever filler.

The form lives at jobs.lever.co/{token}/{id}/apply (the posting's `applyUrl`).
Standard fields have stable names (`name`, `email`, `phone`, `location`, `org`,
`urls[LinkedIn]`, ...); custom questions are `cards[...]` radios, checkboxes,
and textareas. Required fields are marked with a ✱. Lever runs an invisible
hCaptcha that can turn into a visible challenge on submit.
"""

SUBMIT_SELECTORS = ('#btn-submit', 'button:has-text("Submit application")', 'button[type="submit"]')


def form_url(job, company) -> str:
    url = job.apply_url or job.url
    return url if url.rstrip("/").endswith("/apply") else url.rstrip("/") + "/apply"
