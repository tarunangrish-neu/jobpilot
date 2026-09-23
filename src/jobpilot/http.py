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
from pathlib import Path
from typing import Any, Optional

import httpx

from . import config

log = logging.getLogger(__name__)


class PoliteClient:
    def __init__(self, use_cache: bool = True) -> None:
        cfg = config.settings().get("http", {})
        self.user_agent: str = cfg.get("user_agent", "JobPilot/0.1")
        self.min_interval: float = float(cfg.get("min_seconds_between_requests_per_host", 1.0))
        self.timeout: float = float(cfg.get("timeout_seconds", 30))
        self.cache_ttl: float = float(cfg.get("cache_ttl_hours", 6)) * 3600.0
        self.use_cache = use_cache

        self._sem = asyncio.Semaphore(int(cfg.get("concurrency", 4)))
        self._host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request: dict[str, float] = {}
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

    def _read_cache(self, url: str) -> Optional[dict[str, Any]]:
        if not self.use_cache:
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

    async def _throttle(self, host: str) -> None:
        """Ensure >= min_interval seconds since the last hit on this host."""
        async with self._host_locks[host]:
            last = self._last_request.get(host)
            if last is not None:
                wait = self.min_interval - (time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request[host] = time.monotonic()

    async def get_text(self, url: str) -> tuple[int, str]:
        """GET a URL, returning (status_code, body). Cached and rate-limited."""
        cached = self._read_cache(url)
        if cached is not None:
            return cached["status"], cached["body"]

        if self._client is None:
            raise RuntimeError("PoliteClient must be used as an async context manager")

        host = httpx.URL(url).host or url
        async with self._sem:
            await self._throttle(host)
            resp = await self._client.get(url)

        body = resp.text
        # Only cache successes; a transient 5xx should not be sticky.
        if resp.status_code == 200:
            self._write_cache(url, {"status": resp.status_code, "body": body})
        return resp.status_code, body

    async def get_json(self, url: str) -> tuple[int, Any]:
        """GET a URL and parse JSON. Returns (status, parsed-or-None)."""
        status, body = await self.get_text(url)
        if status != 200:
            return status, None
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            log.warning("non-JSON body from %s", url)
            return status, None
