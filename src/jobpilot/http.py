"""Shared async HTTP client.

Enforces the politeness rules from the spec: a descriptive User-Agent, at
least one second between requests to the same host, bounded concurrency, and
an on-disk response cache so re-runs are cheap and don't re-hammer boards.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import httpx

from . import config

log = logging.getLogger(__name__)


def _retry_after(value: Optional[str], default: float) -> float:
    """Seconds to wait from a Retry-After header (seconds form), capped at a minute."""
    try:
        return min(max(float(value), 0.0), 60.0) if value is not None else min(default, 60.0)
    except ValueError:  # the HTTP-date form: rare, just use the default
        return min(default, 60.0)


class PoliteClient:
    def __init__(self, use_cache: bool = True, fresh: bool = False) -> None:
        """`fresh`: never answer from the cache unless the call says the response is reusable
        (`reuse=True`, for per-posting detail pages); responses are still written to it."""
        cfg = config.settings().get("http", {})
        self.user_agent: str = cfg.get("user_agent", "JobPilot/0.1")
        self.min_interval: float = float(cfg.get("min_seconds_between_requests_per_host", 1.0))
        self.timeout: float = float(cfg.get("timeout_seconds", 30))
        self.cache_ttl: float = float(cfg.get("cache_ttl_hours", 6)) * 3600.0
        self.use_cache = use_cache
        self.fresh = fresh

        self._sem = asyncio.Semaphore(int(cfg.get("concurrency", 4)))
        self._host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request: dict[str, float] = {}
        # A host that answered 429/503 gets a longer spacing for the rest of the run.
        self._host_interval: dict[str, float] = {}
        self.max_retries: int = int(cfg.get("rate_limit_retries", 3))
        self._client: Optional[httpx.AsyncClient] = None

        self.cache_dir: Path = config.project_root() / ".cache" / "http"

    async def __aenter__(self) -> "PoliteClient":
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- cache ---------------------------------------------------------

    def _cache_file(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()}.json"

    def _read_cache(self, url: str, reuse: bool = False) -> Optional[dict[str, Any]]:
        if not self.use_cache or (self.fresh and not reuse):
            return None
        f = self._cache_file(url)
        if not f.exists():
            return None
        # cache_ttl_hours <= 0 means "never serve from cache", not "cache forever".
        if self.cache_ttl <= 0 or (time.time() - f.stat().st_mtime) > self.cache_ttl:
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write_cache(self, url: str, payload: dict[str, Any]) -> None:
        if not self.use_cache:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._cache_file(url).write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            log.warning("could not cache %s: %s", url, exc)

    # --- fetching ------------------------------------------------------

    async def _wait_turn(self, host: str) -> None:
        """Sleep until >= min_interval seconds since the last hit on this host."""
        last = self._last_request.get(host)
        if last is not None:
            wait = self._host_interval.get(host, self.min_interval) - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)

    @asynccontextmanager
    async def _slot(self, host: str):
        """One request's turn on `host`: its spacing, then a concurrency slot.

        The spacing is waited out *before* taking a slot. Sleeping inside one
        let the 144 Ashby boards (all on api.ashbyhq.com) fill every slot while
        they queued for their host, which cut the whole fetch to ~1 request/s.
        The host lock is held until the slot is taken, so the recorded time is
        when the request really starts and same-host spacing still holds.
        """
        async with self._host_locks[host]:
            await self._wait_turn(host)
            await self._sem.acquire()
            self._last_request[host] = time.monotonic()
        try:
            yield
        finally:
            self._sem.release()

    async def _send(self, host: str, call) -> httpx.Response:
        """Run one request in its slot; on 429/503, back off as the site asks and retry.

        Another process hitting the same site (the UI's fetch while a terminal fetch
        runs) doubles the rate the site sees, so a 429 also slows this host down for
        the rest of the run instead of just retrying at the same pace.
        """
        for attempt in range(self.max_retries + 1):
            async with self._slot(host):
                resp = await call()
            if resp.status_code not in (429, 503) or attempt == self.max_retries:
                return resp
            delay = _retry_after(resp.headers.get("retry-after"), default=5.0 * 2**attempt)
            self._host_interval[host] = min(max(self._host_interval.get(host, self.min_interval) * 2, 2.0), 30.0)
            log.info("%s answered %s; waiting %.0fs, then %.0fs between requests",
                     host, resp.status_code, delay, self._host_interval[host])
            await asyncio.sleep(delay)
        return resp

    async def get_text(
        self, url: str, headers: Optional[dict[str, str]] = None, reuse: bool = False
    ) -> tuple[int, str]:
        """GET a URL, returning (status_code, body). Cached and rate-limited.

        `headers` are extra request headers (e.g. an API key); they are not part
        of the cache key, so never vary the response by header alone. `reuse`
        marks a response that rarely changes (one posting's details), which a
        `fresh` client may still serve from the cache.
        """
        cached = self._read_cache(url, reuse)
        if cached is not None:
            return cached["status"], cached["body"]

        if self._client is None:
            raise RuntimeError("PoliteClient must be used as an async context manager")

        host = httpx.URL(url).host or url
        resp = await self._send(host, lambda: self._client.get(url, headers=headers))

        body = resp.text
        # Only cache successes; a transient 5xx should not be sticky.
        if resp.status_code == 200:
            self._write_cache(url, {"status": resp.status_code, "body": body})
        return resp.status_code, body

    async def post_json(
        self, url: str, payload: Any, headers: Optional[dict[str, str]] = None
    ) -> tuple[int, Any]:
        """POST JSON and parse the JSON reply. Rate-limited, never disk-cached.

        Used by the OpenAI-compatible LLM provider; the LLM layer keeps its
        own cache keyed by prompt hash.
        """
        if self._client is None:
            raise RuntimeError("PoliteClient must be used as an async context manager")

        host = httpx.URL(url).host or url
        resp = await self._send(host, lambda: self._client.post(url, json=payload, headers=headers))
        try:
            return resp.status_code, resp.json()
        except json.JSONDecodeError:
            return resp.status_code, None

    async def get_json(
        self, url: str, headers: Optional[dict[str, str]] = None, reuse: bool = False
    ) -> tuple[int, Any]:
        """GET a URL and parse JSON. Returns (status, parsed-or-None)."""
        status, body = await self.get_text(url, headers=headers, reuse=reuse)
        if status != 200:
            return status, None
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            log.warning("non-JSON body from %s", url)
            return status, None
