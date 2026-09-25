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
            async with client._slot("boards-api.greenhouse.io"):
                pass
        return time.monotonic() - start

    assert asyncio.run(run()) >= 0.5   # two gaps of 0.25s between three requests


def test_throttle_does_not_penalise_different_hosts(temp_root):
    from jobpilot.http import PoliteClient

    async def run() -> float:
        client = PoliteClient()
        client.min_interval = 0.3
        start = time.monotonic()

        async def hit(host: str) -> None:
            async with client._slot(host):
                pass

        await asyncio.gather(hit("api.lever.co"), hit("api.ashbyhq.com"))
        return time.monotonic() - start

    assert asyncio.run(run()) < 0.2


def test_a_queued_host_does_not_hold_slots_other_hosts_need(temp_root):
    """Requests waiting out one host's spacing must not starve every other host.

    Regression: the wait used to happen inside the concurrency semaphore, so the
    many boards on api.ashbyhq.com filled every slot and the whole fetch ran at
    ~1 request/s.
    """
    from jobpilot.http import PoliteClient

    async def run() -> tuple[float, list[float]]:
        client = PoliteClient()
        client.min_interval = 0.3
        client._sem = asyncio.Semaphore(1)
        start = time.monotonic()
        same_host: list[float] = []
        other: list[float] = []

        async def hit(host: str, record: list[float]) -> None:
            async with client._slot(host):
                record.append(time.monotonic() - start)

        await asyncio.gather(*(hit("api.ashbyhq.com", same_host) for _ in range(4)), hit("api.lever.co", other))
        return other[0], same_host

    other_at, same_host = asyncio.run(run())
    assert other_at < 0.2
    gaps = [b - a for a, b in zip(same_host, same_host[1:])]
    assert all(g >= 0.29 for g in gaps)


def test_fresh_client_refetches_listings_but_reuses_detail_pages(temp_root):
    """Every fetch must see new postings; a posting's own detail page may come from the cache."""
    from jobpilot import config
    from jobpilot.http import PoliteClient

    config.settings()["http"]["cache_ttl_hours"] = 6
    client = PoliteClient(fresh=True)
    client.cache_dir.mkdir(parents=True, exist_ok=True)
    client._write_cache("https://example.com/board", {"status": 200, "body": "old listing"})

    assert client._read_cache("https://example.com/board") is None
    assert client._read_cache("https://example.com/board", reuse=True)["body"] == "old listing"
    assert PoliteClient()._read_cache("https://example.com/board")["body"] == "old listing"


def test_429_waits_as_asked_retries_and_slows_that_host(temp_root):
    """A 429 must not drop the board: wait out Retry-After, retry, and space that host wider."""
    import httpx

    from jobpilot.http import PoliteClient

    calls = []

    def handler(request):
        calls.append(request.url.host)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, text="ok")

    async def run():
        client = PoliteClient(use_cache=False)
        client.min_interval = 0.0
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await client.get_text("https://apply.workable.com/api/v1/widget/accounts/x"), client
        finally:
            await client._client.aclose()

    (status, body), client = asyncio.run(run())
    assert (status, body) == (200, "ok")
    assert calls == ["apply.workable.com"] * 2
    assert client._host_interval["apply.workable.com"] >= 2.0
