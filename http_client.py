"""Resilient HTTP transport shared by all external-site scrapers.

The parser layer (``aoty.py``) should not know how to retry, rate-limit or
cache requests.  It only asks this module for a page.  Keeping all network
policy here makes future scraper changes much easier and prevents one command
from accidentally hammering AOTY while another command is already active.

Design goals:
- one request at a time to AOTY;
- interactive Discord actions have priority over the background monitor;
- global minimum delay between requests;
- Retry-After + exponential backoff for 429/5xx/network errors;
- a small in-memory stale cache so repeated button clicks do not refetch pages;
- a circuit breaker so an outage does not turn into an endless retry storm.
- a global challenge cooldown shared by commands, monitor and maintenance.
"""

from __future__ import annotations

import heapq
import random
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Iterator
from urllib.parse import urlparse

import requests

from settings import (
    AOTY_CACHE_MAX_ENTRIES,
    AOTY_CHALLENGE_COOLDOWN,
    AOTY_CIRCUIT_COOLDOWN,
    AOTY_CIRCUIT_FAILURES,
    AOTY_MAX_RETRIES,
    AOTY_MAINTENANCE_MIN_REQUEST_INTERVAL,
    AOTY_MIN_REQUEST_INTERVAL,
    AOTY_REQUEST_TIMEOUT_CONNECT,
    AOTY_REQUEST_TIMEOUT_READ,
)


PRIORITY_INTERACTIVE = 0
PRIORITY_NORMAL = 10
PRIORITY_BACKGROUND = 20
# Najniższy priorytet: pełne archiwum i enrichment. Interaktywne komendy oraz
# regularny monitor mogą wejść przed następny request wielostronicowego joba.
PRIORITY_MAINTENANCE = 30


class ExternalRateLimit(RuntimeError):
    """Remote site explicitly rejected us for sending requests too quickly."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ExternalUnavailable(RuntimeError):
    """Remote site is temporarily unavailable and no usable stale page exists."""


class ExternalChallenge(ExternalUnavailable):
    """AOTY returned an anti-bot interstitial instead of requested content."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after))


@dataclass(slots=True)
class PageResult:
    text: str
    url: str
    status_code: int
    stale: bool = False
    from_cache: bool = False


@dataclass(slots=True)
class _CacheEntry:
    result: PageResult
    stored_at: float
    fresh_until: float
    stale_until: float


_thread_context = threading.local()


@contextmanager
def request_priority(priority: int) -> Iterator[None]:
    """Temporarily set the priority used by nested synchronous scraper calls.

    The value is thread-local.  This matters because most scraper functions run
    inside ``asyncio.to_thread`` and can make several nested ``fetch_page`` calls.
    """

    previous = getattr(_thread_context, "priority", PRIORITY_NORMAL)
    _thread_context.priority = int(priority)

    try:
        yield
    finally:
        _thread_context.priority = previous


def current_priority() -> int:
    return int(getattr(_thread_context, "priority", PRIORITY_NORMAL))


def call_with_priority(priority: int, func, /, *args, **kwargs):
    """Run a synchronous scraper function under one network priority."""

    with request_priority(priority):
        return func(*args, **kwargs)


class _PriorityGate:
    """Thread-safe one-at-a-time request gate with a priority queue.

    Maintenance crawling has a larger spacing than normal traffic. This keeps
    a full-profile bootstrap polite without making Discord interactions wait
    for that same conservative delay.
    """

    def __init__(self, min_interval: float, maintenance_interval: float):
        self.min_interval = max(0.0, float(min_interval))
        self.maintenance_interval = max(
            self.min_interval,
            float(maintenance_interval),
        )
        self._condition = threading.Condition()
        self._queue: list[tuple[int, int, object]] = []
        self._sequence = 0
        self._in_flight = False
        self._next_allowed = 0.0
        self._blocked_until = 0.0

    def block_for(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        with self._condition:
            self._blocked_until = max(
                self._blocked_until,
                time.monotonic() + seconds,
            )
            self._condition.notify_all()

    @property
    def blocked_seconds(self) -> float:
        with self._condition:
            return max(0.0, self._blocked_until - time.monotonic())

    @contextmanager
    def slot(self, priority: int) -> Iterator[None]:
        ticket = object()

        with self._condition:
            self._sequence += 1
            sequence = self._sequence
            heapq.heappush(
                self._queue,
                (int(priority), sequence, ticket),
            )

            while True:
                now = time.monotonic()
                first = self._queue and self._queue[0][2] is ticket
                allowed_at = max(self._next_allowed, self._blocked_until)

                if first and not self._in_flight and now >= allowed_at:
                    heapq.heappop(self._queue)
                    self._in_flight = True
                    break

                timeout = 0.25
                if first and not self._in_flight and allowed_at > now:
                    timeout = min(1.0, max(0.05, allowed_at - now))

                self._condition.wait(timeout=timeout)

        try:
            yield
        finally:
            with self._condition:
                self._in_flight = False
                interval = (
                    self.maintenance_interval
                    if int(priority) >= PRIORITY_MAINTENANCE
                    else self.min_interval
                )
                self._next_allowed = time.monotonic() + interval
                self._condition.notify_all()


class ResilientHTTPClient:
    """Small, synchronous HTTP client designed for scraper workloads."""

    def __init__(self):
        self.session = requests.Session()
        self._gate = _PriorityGate(
            AOTY_MIN_REQUEST_INTERVAL,
            AOTY_MAINTENANCE_MIN_REQUEST_INTERVAL,
        )
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._cache_lock = threading.RLock()
        self._circuit_lock = threading.RLock()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._challenge_open_until = 0.0
        self._challenge_last_error: str | None = None
        self._last_error: str | None = None
        self._request_count = 0
        self._cache_hits = 0

    def configure_headers(self, headers: dict[str, str]) -> None:
        self.session.headers.update(headers)

    # ------------------------------------------------------------------
    # Cache policy
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_policy(url: str) -> tuple[float, float]:
        """Return (fresh_seconds, stale_seconds) based on URL semantics."""

        path = urlparse(url).path.casefold()

        if "/search/" in path or path.rstrip("/") == "/search":
            return 45.0, 10 * 60.0

        if "/user/" in path and "/ratings" in path:
            return 20.0, 30 * 60.0

        if "/user/" in path and "/album/" in path:
            return 60.0, 6 * 60 * 60.0

        if "/user/" in path:
            return 90.0, 6 * 60 * 60.0

        if "/album/" in path:
            return 10 * 60.0, 24 * 60 * 60.0

        if "/artist/" in path:
            return 10 * 60.0, 24 * 60 * 60.0

        return 60.0, 30 * 60.0

    def _cache_get(self, url: str, *, stale: bool) -> PageResult | None:
        now = time.monotonic()

        with self._cache_lock:
            entry = self._cache.get(url)

            if entry is None:
                return None

            deadline = entry.stale_until if stale else entry.fresh_until
            if now > deadline:
                if now > entry.stale_until:
                    self._cache.pop(url, None)
                return None

            self._cache.move_to_end(url)
            self._cache_hits += 1

            return PageResult(
                text=entry.result.text,
                url=entry.result.url,
                status_code=entry.result.status_code,
                stale=stale and now > entry.fresh_until,
                from_cache=True,
            )

    def _cache_put(self, url: str, result: PageResult) -> None:
        fresh_seconds, stale_seconds = self._cache_policy(url)
        now = time.monotonic()

        with self._cache_lock:
            self._cache[url] = _CacheEntry(
                result=PageResult(
                    text=result.text,
                    url=result.url,
                    status_code=result.status_code,
                ),
                stored_at=now,
                fresh_until=now + fresh_seconds,
                stale_until=now + max(fresh_seconds, stale_seconds),
            )
            self._cache.move_to_end(url)

            while len(self._cache) > AOTY_CACHE_MAX_ENTRIES:
                self._cache.popitem(last=False)

    # ------------------------------------------------------------------
    # Circuit breaker / retry helpers
    # ------------------------------------------------------------------

    def _circuit_open(self) -> bool:
        with self._circuit_lock:
            return time.monotonic() < self._circuit_open_until

    def _challenge_seconds(self) -> float:
        with self._circuit_lock:
            return max(0.0, self._challenge_open_until - time.monotonic())

    @staticmethod
    def _challenge_issue(text: str) -> str | None:
        """Return a reason when a response is an anti-bot interstitial."""

        body = str(text or "").casefold()
        markers = (
            "cf-chl-",
            "challenge-platform",
            "verify you are human",
            "checking your browser before accessing",
            "attention required! | cloudflare",
        )
        if any(marker in body for marker in markers):
            return "interstitial/challenge page"
        return None

    def _record_challenge(self, issue: str) -> ExternalChallenge:
        seconds = max(0.0, float(AOTY_CHALLENGE_COOLDOWN))
        message = (
            f"AOTY zwróciło {issue}; globalny cooldown "
            f"{seconds / 3600:.1f} h."
        )
        with self._circuit_lock:
            self._challenge_open_until = max(
                self._challenge_open_until,
                time.monotonic() + seconds,
            )
            self._challenge_last_error = message
        return ExternalChallenge(message, retry_after=seconds)

    def _active_challenge(self) -> ExternalChallenge | None:
        seconds = self._challenge_seconds()
        if seconds <= 0:
            return None
        with self._circuit_lock:
            message = self._challenge_last_error or (
                "AOTY challenge cooldown jest nadal aktywny."
            )
        return ExternalChallenge(message, retry_after=seconds)

    def _record_success(self) -> None:
        with self._circuit_lock:
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0
            self._challenge_open_until = 0.0
            self._challenge_last_error = None
            self._last_error = None

    def _record_failure(self, message: str) -> None:
        with self._circuit_lock:
            self._consecutive_failures += 1
            self._last_error = message

            if self._consecutive_failures >= AOTY_CIRCUIT_FAILURES:
                self._circuit_open_until = max(
                    self._circuit_open_until,
                    time.monotonic() + AOTY_CIRCUIT_COOLDOWN,
                )

    @staticmethod
    def _retry_after_seconds(response: requests.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None

        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass

        try:
            target = parsedate_to_datetime(raw)
            seconds = target.timestamp() - time.time()
            return max(0.0, seconds)
        except Exception:
            return None

    @staticmethod
    def _backoff(attempt: int, *, rate_limited: bool = False) -> float:
        base = 4.0 if rate_limited else 1.2
        return min(90.0, base * (2 ** attempt) + random.uniform(0.0, 0.8))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        use_cache: bool = True,
        allow_stale: bool = True,
        priority: int | None = None,
    ) -> PageResult:
        """GET one page with rate limiting, retries and stale fallback."""

        url = str(url or "").strip()
        parsed_url = urlparse(url)
        if (
            parsed_url.scheme.casefold() not in {"http", "https"}
            or not parsed_url.netloc
        ):
            raise ValueError("Nieprawidłowy URL HTTP")

        if use_cache:
            fresh = self._cache_get(url, stale=False)
            if fresh is not None:
                return fresh

        stale_result = self._cache_get(url, stale=True) if allow_stale else None

        active_challenge = self._active_challenge()
        if active_challenge is not None:
            if stale_result is not None:
                return stale_result
            raise active_challenge

        if self._circuit_open():
            if stale_result is not None:
                return stale_result
            raise ExternalUnavailable(
                "Obwód ochronny HTTP jest chwilowo otwarty po serii błędów."
            )

        request_priority_value = current_priority() if priority is None else int(priority)
        last_exception: Exception | None = None

        for attempt in range(AOTY_MAX_RETRIES + 1):
            try:
                with self._gate.slot(request_priority_value):
                    # The circuit may have opened while this request was
                    # waiting behind another worker.  Re-check it after the
                    # priority gate as well, otherwise every already-queued
                    # request would still hit AOTY during the outage.
                    if self._circuit_open():
                        if stale_result is not None:
                            return stale_result
                        raise ExternalUnavailable(
                            "Obwód ochronny HTTP jest chwilowo otwarty po serii błędów."
                        )

                    active_challenge = self._active_challenge()
                    if active_challenge is not None:
                        if stale_result is not None:
                            return stale_result
                        raise active_challenge

                    # Another worker may have fetched the same URL while this
                    # request waited in the priority queue. Re-checking here
                    # gives us single-flight-like behavior without a second
                    # coordination system.
                    if use_cache:
                        refreshed = self._cache_get(url, stale=False)
                        if refreshed is not None:
                            return refreshed

                    self._request_count += 1
                    response = self.session.get(
                        url,
                        timeout=(
                            AOTY_REQUEST_TIMEOUT_CONNECT,
                            AOTY_REQUEST_TIMEOUT_READ,
                        ),
                    )

                    challenge_issue = self._challenge_issue(response.text)
                    if challenge_issue is not None:
                        challenge = self._record_challenge(challenge_issue)
                        if stale_result is not None:
                            return stale_result
                        raise challenge

                if response.status_code == 429:
                    retry_after = self._retry_after_seconds(response)
                    wait = retry_after if retry_after is not None else self._backoff(
                        attempt,
                        rate_limited=True,
                    )
                    wait = min(max(wait, 2.0), 15 * 60.0)
                    self._gate.block_for(wait)
                    message = f"HTTP 429 - za dużo zapytań; cooldown {wait:.0f}s"
                    self._record_failure(message)

                    if attempt < AOTY_MAX_RETRIES:
                        continue

                    if stale_result is not None:
                        return stale_result

                    raise ExternalRateLimit(message, retry_after=wait)

                if response.status_code >= 500:
                    message = f"HTTP {response.status_code} z serwera zewnętrznego"
                    self._record_failure(message)

                    if attempt < AOTY_MAX_RETRIES:
                        self._gate.block_for(self._backoff(attempt))
                        continue

                    if stale_result is not None:
                        return stale_result

                    response.raise_for_status()

                # Stable 4xx responses (especially the expected 404s used
                # while probing canonical user-release URLs) are not an AOTY
                # outage.  They must reach the caller immediately, without
                # retries and without contributing to the circuit breaker.
                response.raise_for_status()

                result = PageResult(
                    text=response.text,
                    url=str(response.url),
                    status_code=response.status_code,
                )

                self._record_success()

                if use_cache:
                    self._cache_put(url, result)

                return result

            except ExternalRateLimit:
                raise
            except ExternalChallenge:
                raise
            except requests.HTTPError:
                # 429 has its dedicated branch above and 5xx retries were
                # already handled before raise_for_status().  What remains is
                # a stable HTTP response, not a transport failure.
                raise
            except requests.RequestException as exc:
                last_exception = exc
                message = f"{type(exc).__name__}: {exc}"
                self._record_failure(message)

                if attempt < AOTY_MAX_RETRIES:
                    self._gate.block_for(self._backoff(attempt))
                    continue

                if stale_result is not None:
                    return stale_result

                raise

        if stale_result is not None:
            return stale_result

        raise ExternalUnavailable(
            str(last_exception or "Nie udało się pobrać strony")
        )

    def invalidate(self, url: str) -> None:
        with self._cache_lock:
            self._cache.pop(str(url), None)

    def close(self) -> None:
        """Close pooled sockets during a graceful Railway shutdown."""
        try:
            self.session.close()
        except Exception:
            pass

    def status(self) -> dict:
        # Snapshot independent locks separately. Request code may hold the
        # priority gate while updating circuit/challenge state, so acquiring
        # the gate again under ``_circuit_lock`` would invert that order.
        with self._circuit_lock:
            now = time.monotonic()
            circuit_open = now < self._circuit_open_until
            circuit_seconds = max(0.0, self._circuit_open_until - now)
            challenge_seconds = max(
                0.0,
                self._challenge_open_until - now,
            )
            challenge_last_error = self._challenge_last_error
            consecutive_failures = self._consecutive_failures
            last_error = self._last_error
            requests_count = self._request_count

        with self._cache_lock:
            cache_entries = len(self._cache)
            cache_hits = self._cache_hits

        return {
            "circuit_open": circuit_open,
            "circuit_seconds": circuit_seconds,
            "blocked_seconds": self._gate.blocked_seconds,
            "challenge_open": challenge_seconds > 0,
            "challenge_seconds": challenge_seconds,
            "challenge_last_error": challenge_last_error,
            "consecutive_failures": consecutive_failures,
            "last_error": last_error,
            "cache_entries": cache_entries,
            "requests": requests_count,
            "cache_hits": cache_hits,
        }


HTTP = ResilientHTTPClient()
