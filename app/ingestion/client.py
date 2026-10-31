"""A resilient client for the Elanco Tick Sightings API.

Failure modes handled (all covered by tests in ``tests/test_client.py``):

* **Slow / hanging server** - separate connect and read timeouts.
* **Transient errors** (timeouts, connection resets, 408/429/5xx, truncated JSON) -
  retried with exponential backoff and *full jitter*, honouring ``Retry-After``.
* **Permanent errors** (other 4xx, wrong payload shape, oversized payload) -
  fail fast, no pointless retries.
* **Sustained outage** - a circuit breaker opens after N consecutive failed attempts
  and short-circuits further calls until a cool-down passes, then lets a single
  trial request through (half-open). This protects both us and the upstream.
* **Huge payloads** - ``iter_batches`` streams and parses the JSON array
  incrementally (ijson), yielding fixed-size batches so memory stays flat no
  matter how large the dataset grows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx
import ijson

from app.config import Settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class UpstreamError(Exception):
    """Base class for upstream failures."""

    retryable = False

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class UpstreamUnavailable(UpstreamError):
    retryable = True


class UpstreamClientError(UpstreamError):
    """4xx that will not succeed on retry (e.g. 404, 400)."""


class UpstreamDataError(UpstreamError):
    """The response arrived but is not usable."""


class TruncatedPayload(UpstreamDataError):
    retryable = True


class CircuitOpenError(UpstreamError):
    """Raised without making a request while the circuit breaker is open."""


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Classic three-state breaker: closed -> open -> half-open -> closed/open.

    While half-open exactly one trial request is let through; everyone else keeps
    failing fast until that trial succeeds (close) or fails (re-open).
    """

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(
        self, failure_threshold: int, recovery_s: float, clock: Callable[[], float] = time.monotonic
    ):
        self.failure_threshold = failure_threshold
        self.recovery_s = recovery_s
        self._clock = clock
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False

    @property
    def state(self) -> str:
        if (
            self._state == self.OPEN
            and not self._trial_in_flight
            and self._clock() - self._opened_at >= self.recovery_s
        ):
            return self.HALF_OPEN
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def retry_in(self) -> float:
        if self._state != self.OPEN or self._trial_in_flight:
            return 0.0
        return max(0.0, self.recovery_s - (self._clock() - self._opened_at))

    def before_call(self) -> None:
        state = self.state
        if state == self.HALF_OPEN:
            self._trial_in_flight = True  # this caller is the trial; others still see OPEN
            return
        if state == self.OPEN:
            raise CircuitOpenError(f"circuit open; upstream calls paused for {self.retry_in():.0f}s more")

    def record_success(self) -> None:
        self._state = self.CLOSED
        self._failures = 0
        self._trial_in_flight = False

    def record_failure(self) -> None:
        self._failures += 1
        if self._trial_in_flight or self._failures >= self.failure_threshold:
            self._state = self.OPEN
            self._opened_at = self._clock()
        self._trial_in_flight = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "consecutive_failures": self._failures,
            "failure_threshold": self.failure_threshold,
            "retry_in_s": round(self.retry_in(), 1),
        }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class ClientStats:
    requests: int = 0
    failures: int = 0
    retries: int = 0
    last_success_at: float | None = None
    last_error: str | None = None
    last_latency_ms: float | None = None
    recent_latencies_ms: list[float] = field(default_factory=list)

    def observe_latency(self, ms: float) -> None:
        self.last_latency_ms = round(ms, 1)
        self.recent_latencies_ms = (self.recent_latencies_ms + [ms])[-50:]


class _AsyncByteReader:
    """Adapts httpx's async byte iterator to the ``async read(n)`` interface ijson expects."""

    def __init__(self, chunks: AsyncIterator[bytes], max_bytes: int):
        self._chunks = chunks
        self._buf = b""
        self._done = False
        self.total = 0
        self._max = max_bytes

    async def read(self, n: int = -1) -> bytes:
        while not self._done and (n < 0 or len(self._buf) < n):
            try:
                chunk = await self._chunks.__anext__()
            except StopAsyncIteration:
                self._done = True
                break
            self.total += len(chunk)
            if self.total > self._max:
                raise UpstreamDataError(f"payload exceeds {self._max // 1_000_000} MB limit")
            self._buf += chunk
        if n < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out


Sleep = Callable[[float], Awaitable[None]]


class UpstreamClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        breaker: CircuitBreaker | None = None,
        sleep: Sleep = asyncio.sleep,
    ):
        self.settings = settings
        self.base_url = settings.upstream_base_url.rstrip("/")
        self.breaker = breaker or CircuitBreaker(
            settings.circuit_failure_threshold, settings.circuit_recovery_s
        )
        self.stats = ClientStats()
        self._sleep = sleep
        self._transport = transport
        self._max_bytes = settings.http_max_payload_mb * 1_000_000

    def _client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=self.settings.http_connect_timeout_s,
            read=self.settings.http_read_timeout_s,
            write=10.0,
            pool=10.0,
        )
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=self._transport,
            headers={"User-Agent": "uk-tick-tracker/1.0", "Accept": "application/json"},
            follow_redirects=True,
        )

    # -- retry machinery ----------------------------------------------------

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.settings.http_backoff_cap_s * 4)
        ceiling = min(
            self.settings.http_backoff_cap_s, self.settings.http_backoff_base_s * 2 ** (attempt - 1)
        )
        return random.uniform(0, ceiling)  # "full jitter" avoids synchronised retry storms

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        value = resp.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError):
                return None

    def _classify(self, resp: httpx.Response) -> UpstreamError | None:
        if resp.status_code < 400:
            return None
        message = f"upstream returned HTTP {resp.status_code} for {resp.request.url.path}"
        if resp.status_code in RETRYABLE_STATUS:
            return UpstreamUnavailable(message, status=resp.status_code)
        return UpstreamClientError(message, status=resp.status_code)

    async def _with_retries(self, op: Callable[[int], Awaitable[Any]], what: str) -> Any:
        attempts = self.settings.http_max_attempts
        last_exc: UpstreamError | None = None
        for attempt in range(1, attempts + 1):
            self.breaker.before_call()  # may raise CircuitOpenError (not retried)
            started = time.perf_counter()
            self.stats.requests += 1
            try:
                result = await op(attempt)
            except UpstreamError as exc:
                last_exc = exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = UpstreamUnavailable(f"{type(exc).__name__} calling {what}: {exc}")
            else:
                self.stats.observe_latency((time.perf_counter() - started) * 1000)
                self.stats.last_success_at = time.time()
                self.breaker.record_success()
                return result

            self.stats.failures += 1
            self.stats.last_error = str(last_exc)
            if not last_exc.retryable:
                # the server answered (e.g. 404 / wrong shape) - it is up, so don't trip the breaker
                self.breaker.record_success()
                raise last_exc
            self.breaker.record_failure()
            if attempt == attempts:
                break
            delay = self._backoff(attempt, last_exc.retry_after)
            self.stats.retries += 1
            log.warning(
                "upstream call failed; retrying",
                extra={"what": what, "attempt": attempt, "delay_s": round(delay, 2), "error": str(last_exc)},
            )
            await self._sleep(delay)
        assert last_exc is not None
        raise UpstreamUnavailable(
            f"{what} failed after {attempts} attempts: {last_exc}", status=last_exc.status
        )

    # -- public API ---------------------------------------------------------

    async def get_json(self, path: str) -> Any:
        async def op(_attempt: int) -> Any:
            async with self._client() as client:
                resp = await client.get(path)
                err = self._classify(resp)
                if err:
                    err.retry_after = self._retry_after(resp)
                    raise err
                length = int(resp.headers.get("Content-Length") or 0)
                if length > self._max_bytes:
                    raise UpstreamDataError(f"payload of {length} bytes exceeds limit")
                try:
                    return resp.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    raise TruncatedPayload(f"invalid JSON from {path}: {exc}") from exc

        return await self._with_retries(op, f"GET {path}")

    async def fetch_city(self, city: str) -> list[Any]:
        data = await self.get_json(f"/tick-sightings/city/{quote(city, safe='')}")
        return _as_list(data)

    async def ping(self) -> bool:
        try:
            async with self._client() as client:
                resp = await client.get("/health", timeout=5.0)
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def iter_batches(self, path: str, batch_size: int) -> AsyncIterator[tuple[int, list[Any]]]:
        """Stream a (possibly huge) JSON array and yield ``(attempt, batch)`` tuples.

        If the connection drops mid-stream the whole request is retried; batches
        may then be re-delivered, which is safe because the pipeline's upserts are
        idempotent and it ignores ids it has already processed in the run.
        """
        attempts = self.settings.http_max_attempts
        for attempt in range(1, attempts + 1):
            self.breaker.before_call()
            self.stats.requests += 1
            started = time.perf_counter()
            batch: list[Any] = []
            try:
                async with self._client() as client, client.stream("GET", path) as resp:
                    err = self._classify(resp)
                    if err:
                        err.retry_after = self._retry_after(resp)
                        raise err
                    ctype = resp.headers.get("content-type", "")
                    if "json" not in ctype:
                        raise TruncatedPayload(f"expected JSON but got content-type {ctype!r}")
                    reader = _AsyncByteReader(resp.aiter_bytes(), self._max_bytes)
                    try:
                        async for item in ijson.items_async(reader, "item", use_float=True):
                            batch.append(item)
                            if len(batch) >= batch_size:
                                yield attempt, batch
                                batch = []
                    except ijson.common.IncompleteJSONError as exc:
                        raise TruncatedPayload(f"stream ended early or invalid JSON: {exc}") from exc
                    if reader.total == 0:
                        raise TruncatedPayload("empty response body")
                if batch:
                    yield attempt, batch
                self.stats.observe_latency((time.perf_counter() - started) * 1000)
                self.stats.last_success_at = time.time()
                self.breaker.record_success()
                return
            except UpstreamError as exc:
                last_exc: UpstreamError = exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = UpstreamUnavailable(f"{type(exc).__name__} streaming {path}: {exc}")

            self.stats.failures += 1
            self.stats.last_error = str(last_exc)
            if not last_exc.retryable:
                self.breaker.record_success()
                raise last_exc
            self.breaker.record_failure()
            if attempt == attempts:
                raise UpstreamUnavailable(f"stream {path} failed after {attempts} attempts: {last_exc}")
            delay = self._backoff(attempt, last_exc.retry_after)
            self.stats.retries += 1
            log.warning(
                "upstream stream failed; retrying",
                extra={"what": path, "attempt": attempt, "delay_s": round(delay, 2), "error": str(last_exc)},
            )
            await self._sleep(delay)

    def health_snapshot(self) -> dict[str, Any]:
        lat = sorted(self.stats.recent_latencies_ms)

        def pct(q: float) -> float | None:  # nearest-rank percentile
            return round(lat[max(0, math.ceil(q * len(lat)) - 1)], 1) if lat else None

        return {
            "base_url": self.base_url,
            "circuit_breaker": self.breaker.snapshot(),
            "requests": self.stats.requests,
            "failures": self.stats.failures,
            "retries": self.stats.retries,
            "last_success_at": self.stats.last_success_at,
            "last_error": self.stats.last_error,
            "latency_ms_p50": pct(0.5),
            "latency_ms_p95": pct(0.95),
        }


def _as_list(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "items", "results", "sightings"):
            if isinstance(data.get(key), list):
                return data[key]
    raise UpstreamDataError(f"expected a JSON array of sightings, got {type(data).__name__}")
