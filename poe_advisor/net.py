"""Shared HTTP client: OS trust store, disk cache, and GGG rate-limit handling."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import truststore

# Use the operating system's certificate store everywhere (requests to poe.ninja,
# pathofexile.com, reddit and the Anthropic API). Machines with antivirus HTTPS
# scanning or a corporate proxy otherwise fail certifi-based verification.
truststore.inject_into_ssl()

import httpx  # noqa: E402  (must come after inject_into_ssl)

from .config import settings  # noqa: E402

MAX_RATE_LIMIT_WAIT = 60.0  # seconds; longer waits are surfaced as an error instead of hanging the chat


class RateLimited(RuntimeError):
    """Raised when a remote API asks us to wait longer than we are willing to block."""


class RateLimiter:
    """Client-side tracker for GGG's X-Rate-Limit-* headers.

    GGG publishes rules as ``max:period:penalty`` triples per policy
    (e.g. ``5:10:60,15:60:300``). We record our own request timestamps per policy
    and wait before a request that would hit ``max`` inside any ``period``.
    """

    def __init__(self) -> None:
        self._rules: dict[str, list[tuple[int, int]]] = {}
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def wait_time(self, policy: str, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        rules = self._rules.get(policy)
        if not rules:
            return 0.0
        hits = self._hits[policy]
        longest = max(period for _, period in rules)
        while hits and now - hits[0] > longest:
            hits.popleft()
        wait = 0.0
        for limit, period in rules:
            recent = [t for t in hits if now - t < period]
            # Leave one request of headroom: other tools (browser, Awakened Trade) share the IP budget.
            if len(recent) >= max(1, limit - 1):
                wait = max(wait, period - (now - recent[0]) + 0.25)
        return wait

    def before_request(self, policy: str) -> None:
        with self._lock:
            wait = self.wait_time(policy)
        if wait > MAX_RATE_LIMIT_WAIT:
            raise RateLimited(f"{policy}: would need to wait {wait:.0f}s for the rate limit window")
        if wait > 0:
            time.sleep(wait)
        with self._lock:
            self._hits[policy].append(time.monotonic())

    def update(self, policy: str, headers: httpx.Headers) -> None:
        rules: list[tuple[int, int]] = []
        for rule_name in (headers.get("x-rate-limit-rules") or "").split(","):
            rule_name = rule_name.strip()
            if not rule_name:
                continue
            for triple in (headers.get(f"x-rate-limit-{rule_name}") or "").split(","):
                parts = triple.split(":")
                if len(parts) == 3:
                    rules.append((int(parts[0]), int(parts[1])))
        if rules:
            with self._lock:
                self._rules[policy] = rules


_limiter = RateLimiter()
_client_lock = threading.Lock()
_client: httpx.Client | None = None


def client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
            )
        return _client


def _cache_path(key: str) -> Path:
    digest = hashlib.sha1(key.encode()).hexdigest()
    return settings.cache_dir / f"{digest}.json"


def _read_cache(key: str, ttl: float) -> Any | None:
    path = _cache_path(key)
    try:
        if time.time() - path.stat().st_mtime < ttl:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _write_cache(key: str, value: Any) -> None:
    path = _cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    tmp.replace(path)


def request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    cache_ttl: float = 0,
    policy: str | None = None,
    parse: str = "json",
) -> Any:
    """Perform a request with optional caching (``cache_ttl`` seconds) and GGG rate limiting (``policy``)."""
    cache_key = json.dumps([method, url, params, json_body], sort_keys=True)
    if cache_ttl:
        cached = _read_cache(cache_key, cache_ttl)
        if cached is not None:
            return cached

    for attempt in range(3):
        if policy:
            _limiter.before_request(policy)
        resp = client().request(method, url, params=params, json=json_body, headers=headers, cookies=cookies)
        if policy:
            _limiter.update(policy, resp.headers)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after") or 5 * (attempt + 1))
            if retry_after > MAX_RATE_LIMIT_WAIT or attempt == 2:
                raise RateLimited(f"{url} rate limited; retry after {retry_after:.0f}s")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        value = resp.json() if parse == "json" else resp.text
        if cache_ttl:
            _write_cache(cache_key, value)
        return value
    raise RateLimited(f"{url} kept returning 429")


def get(url: str, **kwargs: Any) -> Any:
    return request("GET", url, **kwargs)


def post(url: str, json_body: Any, **kwargs: Any) -> Any:
    return request("POST", url, json_body=json_body, **kwargs)
