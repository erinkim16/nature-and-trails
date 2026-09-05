"""A polite, cached, retrying async HTTP client.

Why this file exists
--------------------
Every data source in this project is free and community-run. That comes with rules:

* **Nominatim** (OSM geocoding) allows *at most 1 request/second* and requires a
  `User-Agent` identifying the app and a contact address. Violating either gets the
  caller blocked — not throttled, blocked.
* **Overpass** (OSM query engine) is frequently overloaded and answers with 429 or
  504 under load. Retrying without backoff makes it worse for everyone.
* Development runs the *same* query dozens of times while a prompt is tweaked.
  Re-fetching each time is slow and rude.

So this module gives every outbound call three properties:

1. **Cached** — SQLite, keyed by a hash of the full request, with a per-call TTL.
   Trail geometry gets 30 days (mountains do not move); weather gets 1 hour.
2. **Rate-limited** — a per-host minimum interval, enforced with an async lock.
3. **Retried** — exponential backoff with jitter on transient failures, honouring
   `Retry-After` when the server sends it.

A note on SQLite and asyncio: the cache calls are synchronous. That is deliberate.
A local SQLite read is measured in microseconds, so the event loop stall is far
smaller than the overhead of shipping the work to a thread pool. If the cache ever
moved to a network store, this is the seam at which it would become truly async.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import get_settings

# Per-host minimum seconds between requests. Conservative on purpose.
_HOST_MIN_INTERVAL: dict[str, float] = {
    "nominatim.openstreetmap.org": 1.1,  # policy is 1/s; leave headroom
    "overpass-api.de": 1.5,
    "overpass.kumi.systems": 1.0,
    "overpass.private.coffee": 1.0,
    # Open-Meteo's free tier returns 429 under concurrent bursts — chunked
    # elevation profiles fire several requests at once, so keep some distance.
    "api.open-meteo.com": 0.4,
    "archive-api.open-meteo.com": 0.4,
    "customer-archive-api.open-meteo.com": 0.4,
    "en.wikipedia.org": 0.2,
    "en.wikivoyage.org": 0.2,
}
_DEFAULT_MIN_INTERVAL = 0.5

# Status codes worth retrying. 429 = rate limited, 5xx = server having a bad day.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ResponseCache:
    """A tiny SQLite key/value store with per-read TTL semantics.

    TTL is applied at *read* time rather than write time, so the same cached body
    can be considered fresh by one caller (30-day TTL) and stale by another
    (1-hour TTL) without storing anything twice.
    """

    def __init__(self, path, disabled: bool = False) -> None:
        self.disabled = disabled
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if disabled:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because httpx/asyncio may touch this from a
        # different thread; we serialise all access with self._lock ourselves.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS responses ("
            "  key TEXT PRIMARY KEY,"
            "  body TEXT NOT NULL,"
            "  created_at REAL NOT NULL"
            ")"
        )
        self._conn.commit()

    def get(self, key: str, ttl_seconds: float) -> str | None:
        if self.disabled or self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT body, created_at FROM responses WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        body, created_at = row
        if time.time() - created_at > ttl_seconds:
            return None
        return body

    def set(self, key: str, body: str) -> None:
        if self.disabled or self._conn is None:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO responses (key, body, created_at) VALUES (?, ?, ?)",
                (key, body, time.time()),
            )
            self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class _HostThrottle:
    """Enforces a minimum interval between requests to the same host."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_call: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        interval = _HOST_MIN_INTERVAL.get(host, _DEFAULT_MIN_INTERVAL)
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last_call.get(host)
            if last is not None:
                delay = interval - (time.monotonic() - last)
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last_call[host] = time.monotonic()


def _cache_key(method: str, url: str, params: Mapping[str, Any] | None, body: str | None) -> str:
    payload = json.dumps(
        {
            "m": method.upper(),
            "u": url,
            # Sorting matters: {"a":1,"b":2} and {"b":2,"a":1} are the same request,
            # and an unsorted key would silently halve the cache hit rate.
            "p": sorted((k, str(v)) for k, v in (params or {}).items()),
            "b": body,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ApiClient:
    """Shared HTTP client for all external data sources."""

    def __init__(self) -> None:
        settings = get_settings()
        self._cache = ResponseCache(settings.absolute_cache_path, settings.cache_disabled)
        self._throttle = _HostThrottle()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json",
            },
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
        self._cache.close()

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        ttl_seconds: float = 3600.0,
        max_attempts: int = 4,
    ) -> Any:
        """Perform a request and return parsed JSON, with cache/throttle/retry.

        Raises `httpx.HTTPError` if every attempt fails.
        """
        body = None
        if data is not None:
            body = json.dumps(sorted(data.items()), separators=(",", ":"))

        key = _cache_key(method, url, params, body)
        cached = self._cache.get(key, ttl_seconds)
        if cached is not None:
            return json.loads(cached)

        host = urlparse(url).netloc
        last_error: Exception | None = None

        for attempt in range(max_attempts):
            await self._throttle.wait(host)
            try:
                response = await self._client.request(method, url, params=params, data=data)
            except httpx.TransportError as exc:  # DNS, connect, read timeouts
                last_error = exc
                await self._backoff(attempt)
                continue

            if response.status_code in _RETRYABLE_STATUS:
                last_error = httpx.HTTPStatusError(
                    f"{response.status_code} from {host}",
                    request=response.request,
                    response=response,
                )
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue

            response.raise_for_status()
            text = response.text
            self._cache.set(key, text)
            return json.loads(text)

        assert last_error is not None
        raise last_error

    @staticmethod
    async def _backoff(attempt: int, retry_after: str | None = None) -> None:
        """Exponential backoff with jitter; obey Retry-After when provided.

        Jitter matters: without it, N concurrent tool calls that all get 429 will
        retry in lockstep and hammer the server at exactly the same moment.
        """
        if retry_after:
            try:
                await asyncio.sleep(min(float(retry_after), 30.0))
                return
            except ValueError:
                pass  # header was an HTTP-date, not seconds; fall through
        delay = min(2.0**attempt, 16.0) * (0.5 + random.random())
        await asyncio.sleep(delay)

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return await self.request_json("GET", url, **kwargs)

    async def post_json(self, url: str, **kwargs: Any) -> Any:
        return await self.request_json("POST", url, **kwargs)


_client: ApiClient | None = None


def get_client() -> ApiClient:
    """Process-wide shared client. Reusing connections is most of the speed."""
    global _client
    if _client is None:
        _client = ApiClient()
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
