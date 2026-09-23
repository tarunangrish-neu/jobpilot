"""Token detection from careers-page markup."""

from __future__ import annotations

import pytest

from jobpilot.sources.discover import candidates_from_html, guess_from_domain


@pytest.mark.parametrize(
    "markup, expected",
    [
        ('<a href="https://boards.greenhouse.io/stripe">Jobs</a>', ("greenhouse", "stripe")),
        ('<iframe src="https://job-boards.greenhouse.io/databricks/jobs"></iframe>', ("greenhouse", "databricks")),
        ('src="https://boards.greenhouse.io/embed/job_board?for=gitlab"', ("greenhouse", "gitlab")),
        ('<a href="https://jobs.lever.co/palantir/">Careers</a>', ("lever", "palantir")),
        ('fetch("https://api.lever.co/v0/postings/matchgroup?mode=json")', ("lever", "matchgroup")),
        ('<a href="https://jobs.ashbyhq.com/ramp">Open roles</a>', ("ashby", "ramp")),
    ],
)
def test_candidates_from_markup(markup, expected):
    assert expected in candidates_from_html(markup)


def test_embed_url_does_not_yield_literal_embed_as_token():
    found = candidates_from_html('"https://boards.greenhouse.io/embed/job_board?for=gitlab"')
    assert ("greenhouse", "embed") not in found
    assert ("greenhouse", "gitlab") in found


def test_no_board_means_no_candidates():
    assert candidates_from_html("<html><body>We are not hiring.</body></html>") == []


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.ramp.com/careers", "ramp"),
        ("https://careers.stripe.com", "stripe"),
        ("https://jobs.match-group.com/x", "matchgroup"),
        ("https://about.gitlab.com/jobs/", "gitlab"),
        ("https://boards.databricks.com", "databricks"),
    ],
)
def test_guess_from_domain(url, expected):
    assert expected in guess_from_domain(url)


def test_guess_from_domain_never_returns_a_generic_subdomain():
    for url in ("https://about.gitlab.com/jobs/", "https://careers.stripe.com"):
        assert not ({"about", "careers", "jobs", "www"} & set(guess_from_domain(url)))
