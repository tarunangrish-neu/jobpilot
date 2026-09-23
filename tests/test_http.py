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
