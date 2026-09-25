"""Search discovery: provider parsing, board extraction, verification, companies.yaml append."""

from __future__ import annotations

import asyncio
import os

import yaml

from jobpilot.sources import search

URLS = [
    "https://jobs.ashbyhq.com/newco/1b2c3d",
    "https://boards.greenhouse.io/acmepay/jobs/123",
    "https://www.linkedin.com/jobs/view/999",          # hard rule: never used
    "https://www.indeed.com/viewjob?jk=abc",
    "https://jobs.lever.co/ghostco/xyz",
    "https://jobs.ashbyhq.com/newco/another-role",     # same board twice
    "https://example.com/careers",                     # not an ATS
]


class FakeClient:
    """Answers search-API and ATS-API URLs from canned payloads; records every request."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def get_json(self, url, headers=None, reuse=False):
        self.calls.append((url, headers))
        for prefix, reply in self.routes.items():
            if url.startswith(prefix):
                return reply
        return 404, None


ATS_ROUTES = {
    "https://api.ashbyhq.com/posting-api/job-board/newco": (200, {"jobs": [
        {"id": "1", "title": "Backend Engineer", "isListed": True, "jobUrl": "u", "applyUrl": "a", "location": "NYC"},
    ]}),
    "https://boards-api.greenhouse.io/v1/boards/acmepay/jobs": (200, {"jobs": [
        {"id": 1, "title": "SWE", "location": {"name": "NYC"}, "content": "", "absolute_url": "u"},
        {"id": 2, "title": "SRE", "location": {"name": "NYC"}, "content": "", "absolute_url": "u"},
    ], "meta": {}}),
    "https://boards-api.greenhouse.io/v1/boards/acmepay": (200, {"name": "AcmePay Inc."}),
}


def test_boards_from_urls_skips_blocked_and_non_ats_and_dedupes():
    assert search.boards_from_urls(URLS) == [("ashby", "newco"), ("greenhouse", "acmepay"), ("lever", "ghostco")]


def test_brave_results_are_parsed_and_key_goes_in_header(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    client = FakeClient({"https://api.search.brave.com/": (200, {"web": {"results": [{"url": u} for u in URLS]}})})
    urls = asyncio.run(search.search(client, {"provider": "brave", "max_results_per_query": 40}, "q"))
    assert urls == URLS
    url, headers = client.calls[0]
    assert headers["X-Subscription-Token"] == "k" and "k" not in url.split("?")[0]


def test_google_cse_and_serpapi_google_jobs_parsing(monkeypatch):
    monkeypatch.setenv("GOOGLE_CSE_KEY", "k")
    monkeypatch.setenv("GOOGLE_CSE_CX", "cx")
    cse = FakeClient({"https://www.googleapis.com/customsearch/v1": (200, {"items": [{"link": URLS[0]}]})})
    assert asyncio.run(search.search(cse, {"provider": "google_cse"}, "q")) == [URLS[0]]

    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    jobs = {"jobs_results": [{"title": "SWE", "apply_options": [
        {"title": "LinkedIn", "link": URLS[2]}, {"title": "Greenhouse", "link": URLS[1]}]}]}
    serp = FakeClient({"https://serpapi.com/search.json": (200, jobs)})
    urls = asyncio.run(search.search(serp, {"provider": "serpapi", "serpapi_engine": "google_jobs"}, "q"))
    assert search.boards_from_urls(urls) == [("greenhouse", "acmepay")]  # LinkedIn option dropped


def test_missing_key_is_a_clear_error_and_stops_early(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    report = asyncio.run(search.discover(FakeClient({}), {"provider": "brave", "queries": ["a", "b"]}, []))
    assert report.queries == 1 and "BRAVE_API_KEY is not set" in report.errors[0]


def test_discover_verifies_skips_known_and_rejects_missing_boards(temp_root, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    routes = {"https://api.search.brave.com/": (200, {"web": {"results": [{"url": u} for u in URLS]}}), **ATS_ROUTES}
    existing = [{"name": "Old", "ats": "lever", "token": "oldco"}]
    report = asyncio.run(search.discover(FakeClient(routes), {"provider": "brave", "queries": ["q1"]}, existing))
    added = {(f.ats, f.token): f for f in report.added}
    assert set(added) == {("ashby", "newco"), ("greenhouse", "acmepay")}
    assert added[("greenhouse", "acmepay")].name == "AcmePay Inc." and added[("greenhouse", "acmepay")].open_roles == 2
    assert added[("ashby", "newco")].name == "Newco"
    assert report.rejected == ["lever/ghostco: no such board"]

    again = asyncio.run(search.discover(
        FakeClient(routes), {"provider": "brave", "queries": ["q1"]},
        existing + [{"name": "Newco", "ats": "ashby", "token": "newco"}],
    ))
    assert ("ashby", "newco") not in {(f.ats, f.token) for f in again.added}


def test_append_keeps_companies_yaml_valid(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text("companies:\n  # a comment that must survive\n  - name: Old\n    ats: lever\n    token: oldco\n")
    search.append_to_companies(path, [search.Found("Acme: Pay", "greenhouse", "acmepay", 2, 'site:x "y"')])
    text = path.read_text()
    assert "# a comment that must survive" in text
    assert yaml.safe_load(text)["companies"][-1] == {"name": "Acme: Pay", "ats": "greenhouse", "token": "acmepay"}


def test_env_file_is_loaded_without_overriding(tmp_path, monkeypatch):
    from jobpilot import config

    (tmp_path / ".env").write_text("# c\nBRAVE_API_KEY=fromfile\nexport GOOGLE_CSE_CX='cx1'\nALREADY=file\n")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CSE_CX", raising=False)
    monkeypatch.setenv("ALREADY", "shell")
    config.load_env(tmp_path / ".env")
    assert os.environ["BRAVE_API_KEY"] == "fromfile" and os.environ["GOOGLE_CSE_CX"] == "cx1"
    assert os.environ["ALREADY"] == "shell"
    monkeypatch.delenv("BRAVE_API_KEY")
    monkeypatch.delenv("GOOGLE_CSE_CX")
