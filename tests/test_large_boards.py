"""Large-employer career sites: Workday, Amazon, Eightfold (Microsoft), Oracle (JPMorgan).

Fixtures are trimmed real responses captured 2026-09-23 from Fidelity (Workday),
amazon.jobs, apply.careers.microsoft.com, and jpmc.fa.oraclecloud.com.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import load
from jobpilot.sources import amazon, eightfold, large, oracle, workday
from jobpilot.sources.discover import verify


class FakeClient:
    """Routes GET/POST by URL prefix to (status, body); unknown URLs 404."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _reply(self, url):
        self.calls.append(url)
        for prefix, reply in self.routes.items():
            if url.startswith(prefix):
                return reply
        return 404, None

    async def get_json(self, url, headers=None):
        return self._reply(url)

    async def post_json(self, url, payload, headers=None):
        return self._reply(url)


def run(coro):
    return asyncio.run(coro)


# --- tokens ---------------------------------------------------------------------


def test_token_formats():
    assert workday.split_token("fmr.wd1/FidelityCareers") == ("fmr.wd1.myworkdayjobs.com", "fmr", "FidelityCareers")
    assert eightfold.split_token("apply.careers.microsoft.com/microsoft.com") == ("apply.careers.microsoft.com", "microsoft.com")
    assert oracle.split_token("jpmc.fa/CX_1001") == ("jpmc.fa.oraclecloud.com", "CX_1001")
    assert "keyword=%22software%20engineer%22" in oracle._list_url("jpmc.fa/CX_1001", "software engineer", 0)


def test_multi_location_listings_are_kept_for_the_detail_call(temp_root):
    assert large.worth_detail("Software Engineer", "3 Locations")        # can't judge yet
    assert large.worth_detail("Backend Engineer", "New York, NY")
    assert not large.worth_detail("Backend Engineer", "Bangalore, India")
    assert not large.worth_detail("Staff Software Engineer", "New York, NY")


# --- parsing real payloads ----------------------------------------------------------


def test_workday_detail_parses():
    info = load("workday_detail.json")["jobPostingInfo"]
    p = workday.parse_detail(info, {"externalPath": "/job/Westlake-TX/x", "locationsText": "Westlake, TX"},
                             "fmr.wd1/FidelityCareers", "Fidelity Investments")
    assert (p.source, p.external_id, p.title) == ("workday", "2135674", "Senior Software Engineer/Developer")
    assert p.location.startswith("Westlake, TX")
    assert p.posted_at.isoformat().startswith("2026-09-23")
    assert len(p.description_text) > 1000 and "<p>" not in p.description_text
    assert p.url.startswith("https://") and "FidelityCareers" in p.url


def test_amazon_search_parses_with_qualifications():
    posts = amazon.parse(load("amazon_search.json"), "Amazon")
    assert [p.external_id for p in posts] == ["10558292", "10558154", "10558429"]
    assert all(p.posted_at is not None and p.url.startswith("https://www.amazon.jobs/en/jobs/") for p in posts)
    assert "Basic qualifications:" in posts[0].description_text and "<br" not in posts[0].description_text


def test_eightfold_detail_parses():
    pos = load("eightfold_search.json")["data"]["positions"][1]
    p = eightfold.parse_detail(load("eightfold_detail.json")["data"], pos,
                               "apply.careers.microsoft.com/microsoft.com", "Microsoft")
    assert (p.source, p.external_id) == ("eightfold", "200044839")
    assert p.posted_at is not None and len(p.description_text) > 1000
    assert p.url.startswith("https://apply.careers.microsoft.com/")


def test_oracle_detail_parses():
    req = load("oracle_list.json")["items"][0]["requisitionList"][0]
    p = oracle.parse_detail(load("oracle_detail.json")["items"][0], req, "jpmc.fa/CX_1001", "JPMorgan Chase")
    assert (p.source, p.external_id) == ("oracle", "210748454")
    assert p.location.startswith("Tampa, FL") and p.posted_at is not None
    assert "FircoSoft" in p.description_text and "<span>" not in p.description_text
    assert p.url == "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210748454"


# --- fetch flow: search -> rules -> details (capped) -----------------------------


def test_workday_fetch_details_only_plausible_listings(temp_root, monkeypatch):
    from jobpilot import config

    config.settings()["large_boards"] = {"search_terms": ["software engineer"], "max_per_term": 20, "max_details": 5}
    listing = {"total": 3, "jobPostings": [
        {"title": "Senior Software Engineer/Developer", "externalPath": "/job/Westlake-TX/a", "locationsText": "Westlake, TX"},
        {"title": "Software Engineer", "externalPath": "/job/Dublin/b", "locationsText": "Dublin, Ireland"},
        {"title": "Account Executive", "externalPath": "/job/NY/c", "locationsText": "New York, NY"},
    ]}
    api = "https://fmr.wd1.myworkdayjobs.com/wday/cxs/fmr/FidelityCareers"
    client = FakeClient({f"{api}/jobs": (200, listing), f"{api}/job/Westlake-TX/a": (200, load("workday_detail.json"))})
    posts = run(workday.fetch(client, {"name": "Fidelity", "token": "fmr.wd1/FidelityCareers"}))
    assert [p.external_id for p in posts] == ["2135674"]
    # Only the plausible listing got a detail call (Dublin and the sales role did not).
    assert [u for u in client.calls if "/job/" in u] == [f"{api}/job/Westlake-TX/a"]


@pytest.mark.parametrize("ats,token,route,body,expected", [
    ("workday", "fmr.wd1/FidelityCareers", "https://fmr.wd1.myworkdayjobs.com/wday/cxs/fmr/FidelityCareers/jobs", {"total": 844}, 844),
    ("amazon", "amazon", "https://www.amazon.jobs/en/search.json", {"hits": 10000, "jobs": []}, 10000),
    ("eightfold", "apply.careers.microsoft.com/microsoft.com", "https://apply.careers.microsoft.com/api/pcsx/search", {"data": {"count": 2421}}, 2421),
    ("oracle", "jpmc.fa/CX_1001", "https://jpmc.fa.oraclecloud.com/hcmRestApi/", {"items": [{"TotalJobsCount": 7393}]}, 7393),
])
def test_verify_counts_and_failures(ats, token, route, body, expected):
    assert run(verify(FakeClient({route: (200, body)}), ats, token)) == expected
    # A blocked or missing board is "not found", never an exception.
    assert run(verify(FakeClient({route: (403, None)}), ats, token)) is None
