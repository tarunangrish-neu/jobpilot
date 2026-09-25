"""Parser tests built from real ATS responses captured on 2026-09-23.

Each assertion below pins a quirk that was verified against a live call --
these are the things that silently corrupt a job description if they regress.
"""

from __future__ import annotations

from datetime import timezone

import pytest

from jobpilot.models import JobPosting
from jobpilot.sources import ashby, greenhouse, lever, recruitee, smartrecruiters, workable
from jobpilot.sources.base import html_to_text, parse_epoch_ms, parse_iso

from conftest import load


# --- Greenhouse ------------------------------------------------------------


def test_greenhouse_parses_and_unescapes_content():
    postings = greenhouse.parse(load("greenhouse_gitlab.json"), "GitLab")

    assert len(postings) == 3
    job = postings[0]
    assert job.source == "greenhouse"
    assert job.external_id and job.external_id.isdigit()
    assert job.title

    # The raw field is entity-escaped HTML. If unescaping is skipped the text
    # still contains "&lt;" and the visa screen would match literal markup.
    assert "&lt;" not in job.description_text
    assert "<p>" not in job.description_text
    assert "<div" not in job.description_text
    assert len(job.description_text) > 200


def test_greenhouse_location_is_flattened_from_object():
    postings = greenhouse.parse(load("greenhouse_gitlab.json"), "GitLab")
    # location arrives as {"name": ...}; a naive parser yields a dict here.
    assert all(isinstance(p.location, str) for p in postings)
    assert any(p.location for p in postings)


def test_greenhouse_uses_first_published_for_posted_at():
    postings = greenhouse.parse(load("greenhouse_gitlab.json"), "GitLab")
    dated = [p for p in postings if p.posted_at]
    assert dated, "expected at least one dated posting"
    assert dated[0].posted_at.tzinfo is not None
    assert dated[0].posted_at.utcoffset() == timezone.utc.utcoffset(None)


# --- Lever -----------------------------------------------------------------


def test_lever_parses_bare_list():
    postings = lever.parse(load("lever_palantir.json"), "Palantir")

    assert len(postings) == 3
    job = postings[0]
    assert job.source == "lever"
    assert job.title
    assert job.description_text


def test_lever_apply_url_differs_from_hosted_url():
    postings = lever.parse(load("lever_palantir.json"), "Palantir")
    job = postings[0]
    assert job.apply_url.endswith("/apply")
    assert job.apply_url != job.url


def test_lever_created_at_is_epoch_milliseconds():
    postings = lever.parse(load("lever_palantir.json"), "Palantir")
    dated = [p for p in postings if p.posted_at]
    assert dated
    # Treating a ms epoch as seconds lands ~50,000 years in the future.
    assert 2000 < dated[0].posted_at.year < 2100


def test_lever_rejects_non_list_payload():
    # An unknown token 404s with a JSON *object*; the parser must not crash.
    assert lever.parse({"ok": False, "error": "not found"}, "Nope") == []


# --- Ashby -----------------------------------------------------------------


def test_ashby_drops_unlisted_postings():
    payload = load("ashby_ramp.json")
    assert len(payload["jobs"]) == 4, "fixture should contain one unlisted job"

    postings = ashby.parse(payload, "Ramp")

    assert len(postings) == 3
    assert all("Internal Draft" not in p.title for p in postings)


def test_ashby_uses_is_remote_flag_and_apply_url():
    postings = ashby.parse(load("ashby_ramp.json"), "Ramp")
    job = postings[0]
    assert job.apply_url
    assert job.apply_url != job.url or "application" in job.apply_url
    assert isinstance(job.remote, bool)


# --- Workable --------------------------------------------------------------


def test_workable_parses_details_payload():
    postings = workable.parse(load("workable_huggingface.json"), "Hugging Face")

    assert len(postings) == 3
    job = postings[0]
    assert job.source == "workable"
    assert job.external_id == "F4C096B22E"  # the shortcode, which the posting URL is built on
    # Only `?details=true` includes descriptions; an empty one means the flag was dropped.
    assert len(job.description_text) > 200
    assert "<p>" not in job.description_text


def test_workable_location_apply_url_and_bare_date():
    postings = workable.parse(load("workable_huggingface.json"), "Hugging Face")
    ny = next(p for p in postings if "New York" in p.location)
    assert ny.location == "New York, New York, United States"
    assert ny.remote is True  # telecommuting
    assert ny.apply_url.endswith("/apply") and ny.apply_url != ny.url
    # published_on is a bare date; it must still come out as aware UTC.
    assert ny.posted_at.utcoffset() == timezone.utc.utcoffset(None)


# --- Recruitee -------------------------------------------------------------


def test_recruitee_keeps_description_and_requirements():
    payload = load("recruitee_bunq.json")
    postings = recruitee.parse(payload, "bunq")

    assert len(postings) == 3
    job = postings[0]
    assert job.source == "recruitee"
    # Visa language often lives in `requirements`; dropping it blinds the visa screen.
    requirements = html_to_text(payload["offers"][0]["requirements"])
    assert requirements.splitlines()[0] in job.description_text
    assert len(job.description_text) > len(html_to_text(payload["offers"][0]["description"]))


def test_recruitee_timestamp_and_multi_location():
    job = recruitee.parse(load("recruitee_bunq.json"), "bunq")[0]
    # "2026-09-23 09:10:19 UTC" is not ISO-8601; a naive parser returns None.
    assert job.posted_at is not None and job.posted_at.year == 2026
    # Offices are ";"-separated so the location filter checks each one.
    assert ";" in job.location
    assert job.apply_url != job.url


def test_recruitee_skips_unpublished_offers():
    payload = load("recruitee_bunq.json")
    payload["offers"][0]["status"] = "closed"
    assert len(recruitee.parse(payload, "bunq")) == 2


def test_parse_iso_accepts_trailing_utc():
    assert parse_iso("2026-09-23 09:10:19 UTC").hour == 9


# --- SmartRecruiters -------------------------------------------------------


class _FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []

    async def get_json(self, url, reuse=False):
        self.calls.append(url)
        return self.responses.get(url, (404, None))


def _sr_client(listing=None, token="ServiceNow"):
    listing = load("smartrecruiters_servicenow.json") if listing is None else listing
    detail = load("smartrecruiters_servicenow_detail.json")
    return _FakeClient({
        smartrecruiters.PAGE_URL.format(token=token, offset=0): (200, listing),
        smartrecruiters.DETAIL_URL.format(token=token, id=detail["id"]): (200, detail),
    })


def test_smartrecruiters_fetches_details_only_for_plausible_postings(temp_root):
    import asyncio

    client = _sr_client()
    postings = asyncio.run(smartrecruiters.fetch(client, {"name": "ServiceNow", "token": "ServiceNow"}))

    assert len(postings) == 3
    detail_calls = [u for u in client.calls if "?" not in u]
    # One of the three titles passes the rules; the finance/manager roles cost no request.
    assert len(detail_calls) == 1
    described = [p for p in postings if p.description_text]
    assert [p.title for p in described] == ["Senior Software Engineer"]
    assert len(described[0].description_text) > 500
    assert "?oga=true" in described[0].apply_url


def test_smartrecruiters_unknown_company_is_200_with_nothing(temp_root):
    import asyncio

    empty = {"offset": 0, "limit": 100, "totalFound": 0, "content": []}
    client = _sr_client(listing=empty, token="Visa")
    assert asyncio.run(smartrecruiters.fetch(client, {"name": "Visa", "token": "Visa"})) == []


def test_smartrecruiters_parse_without_detail_still_normalizes():
    postings = smartrecruiters.parse(load("smartrecruiters_servicenow.json"), "ServiceNow")
    job = postings[0]
    assert job.source == "smartrecruiters"
    assert job.location.endswith("United States")
    assert job.url.startswith("https://jobs.smartrecruiters.com/ServiceNow/")
    assert job.posted_at.tzinfo is not None


# --- Shared helpers --------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected_in, not_expected_in",
    [
        ("&lt;p&gt;Hello&lt;/p&gt;", "Hello", "&lt;"),
        ("<ul><li>No sponsorship</li><li>US only</li></ul>", "No sponsorship", "<li>"),
        ("<script>var x=1;</script><p>Body</p>", "Body", "var x"),
        ("<p>A&amp;B</p>", "A&B", "&amp;"),
    ],
)
def test_html_to_text(raw, expected_in, not_expected_in):
    out = html_to_text(raw)
    assert expected_in in out
    assert not_expected_in not in out


def test_html_to_text_keeps_list_items_on_separate_lines():
    # Critical for the visa screen: two bullets must not merge into one line.
    out = html_to_text("<ul><li>Alpha</li><li>Beta</li></ul>")
    assert "AlphaBeta" not in out
    assert "Alpha" in out and "Beta" in out


def test_html_to_text_handles_empty():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""


def test_parse_helpers_tolerate_garbage():
    assert parse_iso(None) is None
    assert parse_iso("not a date") is None
    assert parse_epoch_ms(None) is None
    assert parse_epoch_ms("abc") is None
    assert parse_iso("2026-01-15T10:00:00Z").year == 2026


# --- Dedupe hash -----------------------------------------------------------


def _posting(**over):
    base = dict(
        source="greenhouse",
        external_id="1",
        company_name="Acme",
        title="Backend Engineer",
        location="Remote - US",
        description_text="Build things.",
    )
    base.update(over)
    return JobPosting(**base)


def test_content_hash_matches_across_sources():
    # Same role cross-posted to two boards must collapse to one hash.
    a = _posting(source="greenhouse", external_id="1")
    b = _posting(source="lever", external_id="zzz")
    assert a.content_hash() == b.content_hash()


def test_content_hash_differs_on_title():
    assert _posting().content_hash() != _posting(title="Frontend Engineer").content_hash()
