"""Politeness guarantees: rate limiting and cache semantics."""

from __future__ import annotations

import asyncio
import time


def test_zero_ttl_disables_cache_rather_than_making_it_permanent(temp_root):
    """A cache_ttl_hours of 0 must mean 'never reuse', not 'reuse forever'."""
    from jobpilot import config
    from jobpilot.http import PoliteClient

    config.settings()["http"]["cache_ttl_hours"] = 0
    client = PoliteClient()
    client.cache_dir.mkdir(parents=True, exist_ok=True)
    client._write_cache("https://example.com/x", {"status": 200, "body": "cached"})

    assert client._read_cache("https://example.com/x") is None


def test_positive_ttl_serves_from_cache(temp_root):
    from jobpilot import config
    from jobpilot.http import PoliteClient

    config.settings()["http"]["cache_ttl_hours"] = 6
    client = PoliteClient()
    client.cache_dir.mkdir(parents=True, exist_ok=True)
    client._write_cache("https://example.com/y", {"status": 200, "body": "cached"})

    assert client._read_cache("https://example.com/y")["body"] == "cached"


def test_throttle_spaces_requests_to_the_same_host(temp_root):
    """Back-to-back hits on one host must be >= min_interval apart."""
    from jobpilot.http import PoliteClient

    async def run() -> float:
        client = PoliteClient()
        client.min_interval = 0.25
        start = time.monotonic()
        for _ in range(3):
            await client._throttle("boards-api.greenhouse.io")
        return time.monotonic() - start

    assert asyncio.run(run()) >= 0.5   # two gaps of 0.25s between three requests


def test_throttle_does_not_penalise_different_hosts(temp_root):
    from jobpilot.http import PoliteClient

    async def run() -> float:
        client = PoliteClient()
        client.min_interval = 0.3
        start = time.monotonic()
        await asyncio.gather(
            client._throttle("api.lever.co"),
            client._throttle("api.ashbyhq.com"),
        )
        return time.monotonic() - start

    assert asyncio.run(run()) < 0.2


def test_per_call_max_age_outlives_the_board_ttl_but_not_a_disabled_cache(temp_root):
    """Posting details are kept for days; cache_ttl_hours <= 0 still means no cache at all."""
    import os

    from jobpilot import config
    from jobpilot.http import PoliteClient

    config.settings()["http"]["cache_ttl_hours"] = 6
    client = PoliteClient()
    client._write_cache("https://api.smartrecruiters.com/d/1", {"status": 200, "body": "detail"})
    ten_hours_ago = time.time() - 10 * 3600
    os.utime(client._cache_file("https://api.smartrecruiters.com/d/1"), (ten_hours_ago, ten_hours_ago))

    assert client._read_cache("https://api.smartrecruiters.com/d/1") is None
    assert client._read_cache("https://api.smartrecruiters.com/d/1", max_age_hours=72)["body"] == "detail"
    client.cache_ttl = 0
    assert client._read_cache("https://api.smartrecruiters.com/d/1", max_age_hours=72) is None


def _recording_client(concurrency: int, interval: float):
    """A PoliteClient on a mock transport that records when each request started."""
    import httpx

    from jobpilot.http import PoliteClient

    starts: list[tuple[str, float]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        starts.append((request.url.host, time.monotonic()))
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={})

    client = PoliteClient(use_cache=False)
    client.min_interval = interval
    client._sem = asyncio.Semaphore(concurrency)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, starts


def test_a_busy_host_does_not_starve_other_hosts(temp_root):
    """Requests queued for one host's spacing must not hold the concurrency slots."""

    async def run():
        client, starts = _recording_client(concurrency=2, interval=0.2)
        t0 = time.monotonic()
        urls = [f"https://boards-api.greenhouse.io/v1/boards/b{i}/jobs" for i in range(8)]
        await asyncio.gather(*(client.get_text(u) for u in [*urls, "https://api.lever.co/v0/postings/x"]))
        await client._client.aclose()
        return t0, starts

    t0, starts = asyncio.run(run())
    lever = [t for host, t in starts if host == "api.lever.co"]
    assert lever[0] - t0 < 0.15   # previously ~1.4s: it waited behind the Greenhouse queue


def test_same_host_spacing_holds_under_concurrency(temp_root):
    async def run():
        client, starts = _recording_client(concurrency=8, interval=0.1)
        await asyncio.gather(*(client.get_text(f"https://api.ashbyhq.com/b/{i}") for i in range(5)))
        await client._client.aclose()
        return [t for _, t in starts]

    times = sorted(asyncio.run(run()))
    assert all(b - a >= 0.095 for a, b in zip(times, times[1:]))
