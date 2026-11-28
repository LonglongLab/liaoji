"""Shared client-source normalization, proxy trust checks, sliding-window rate limits, and bounded concurrency gates."""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import deque
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager

from starlette.requests import Request

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

DEFAULT_TRUSTED_PROXY_NETWORKS: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)

_MAX_SOURCE_LENGTH = 256


class ConcurrencyLimitExceeded(Exception):
    """Raised when bounded concurrent execution slots and waiter queue are saturated."""


def parse_trusted_proxies(raw: str | None) -> tuple[IPNetwork, ...] | None:
    """Parse a comma-separated list of IP addresses or CIDR networks."""
    if raw is None or not raw.strip():
        return None
    networks: list[IPNetwork] = []
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        networks.append(ipaddress.ip_network(token, strict=False))
    return tuple(networks) if networks else None


def _parse_ip(candidate: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = candidate.strip()
    if not text:
        return None
    if text.startswith("[") and "]" in text:
        bracket_end = text.find("]")
        text = text[1:bracket_end]
    elif text.count(":") == 1 and "." in text:
        host, _port = text.split(":", 1)
        text = host
    if "%" in text:
        text = text.split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def _ip_in_networks(peer: str, networks: Sequence[IPNetwork]) -> bool:
    addr = _parse_ip(peer)
    if addr is None:
        return False
    return any(addr in network for network in networks)


def extract_client_source(
    request: Request,
    trusted_proxies: Sequence[IPNetwork] | None = None,
) -> str:
    """Return the client source address, trusting X-Forwarded-For only from trusted proxies."""
    client = request.client
    peer = client.host.strip() if client is not None and client.host else ""
    trust_xff = (
        (peer == "testclient" or _ip_in_networks(peer, DEFAULT_TRUSTED_PROXY_NETWORKS))
        if trusted_proxies is None
        else _ip_in_networks(peer, trusted_proxies)
    )
    if trust_xff:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
            if hops:
                return hops[-1]
    return peer or "unknown"


def normalize_source_bucket(source: str) -> str:
    """Normalize client source into a rate-limit bucket (aggregating IPv6 addresses to /64)."""
    cleaned = source.strip()[:_MAX_SOURCE_LENGTH]
    if not cleaned:
        return "unknown"
    addr = _parse_ip(cleaned)
    if addr is None:
        return cleaned
    if isinstance(addr, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network((addr, 64), strict=False))
    return str(addr)


class SlidingWindowLimiter:
    """Thread-safe in-memory sliding-window rate limiter with bounded key cardinality."""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int,
        now: Callable[[], float] | None = None,
        max_keys: int = 4096,
    ) -> None:
        if limit <= 0 or window_seconds <= 0 or max_keys <= 0:
            raise ValueError("rate limiter limits must be positive")
        self._limit = limit
        self._window = float(window_seconds)
        self._now = now if now is not None else time.monotonic
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}

    def _prune_locked(self, key: str, current: float) -> deque[float] | None:
        bucket = self._events.get(key)
        if bucket is None:
            return None
        cutoff = current - self._window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            del self._events[key]
            return None
        return bucket

    def _evict_if_full_locked(self, current: float) -> None:
        if len(self._events) < self._max_keys:
            return
        cutoff = current - self._window
        stale_keys = [
            key
            for key, bucket in self._events.items()
            if not bucket or bucket[-1] <= cutoff
        ]
        for key in stale_keys:
            del self._events[key]
        while len(self._events) >= self._max_keys:
            oldest_key = min(
                self._events,
                key=lambda k: self._events[k][-1] if self._events[k] else 0.0,
            )
            del self._events[oldest_key]

    def retry_after(self, key: str) -> int | None:
        """Return seconds until next allowed attempt, or None when currently below limit."""
        current = float(self._now())
        with self._lock:
            bucket = self._prune_locked(key, current)
            if bucket is None or len(bucket) < self._limit:
                return None
            remaining = (bucket[0] + self._window) - current
            return max(1, int(remaining) + (1 if remaining > int(remaining) else 0))

    def record(self, key: str) -> None:
        """Record one event in the sliding window for key."""
        current = float(self._now())
        with self._lock:
            bucket = self._prune_locked(key, current)
            if bucket is None:
                self._evict_if_full_locked(current)
                bucket = deque[float]()
                self._events[key] = bucket
            bucket.append(current)

    def hit(self, key: str) -> int | None:
        """Check and record atomically: returns retry_after if throttled, else None."""
        current = float(self._now())
        with self._lock:
            bucket = self._prune_locked(key, current)
            if bucket is not None and len(bucket) >= self._limit:
                remaining = (bucket[0] + self._window) - current
                return max(1, int(remaining) + (1 if remaining > int(remaining) else 0))
            if bucket is None:
                self._evict_if_full_locked(current)
                bucket = deque[float]()
                self._events[key] = bucket
            bucket.append(current)
            return None


class BoundedConcurrencyGate:
    """Caps simultaneous CPU/memory-heavy operations and bounds queued waiters."""

    def __init__(
        self,
        *,
        max_concurrent: int = 2,
        max_waiters: int = 8,
        acquire_timeout: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_concurrent <= 0 or max_waiters < 0 or acquire_timeout <= 0:
            raise ValueError("concurrency gate bounds are invalid")
        self._max_concurrent = max_concurrent
        self._max_waiters = max_waiters
        self._acquire_timeout = acquire_timeout
        self._monotonic = monotonic
        self._cond = threading.Condition()
        self._active = 0
        self._waiters = 0

    @contextmanager
    def acquire(self) -> Generator[None]:
        deadline = self._monotonic() + self._acquire_timeout
        with self._cond:
            if self._active >= self._max_concurrent:
                if self._waiters >= self._max_waiters:
                    raise ConcurrencyLimitExceeded()
                self._waiters += 1
                try:
                    while self._active >= self._max_concurrent:
                        remaining = deadline - self._monotonic()
                        if remaining <= 0:
                            raise ConcurrencyLimitExceeded()
                        self._cond.wait(timeout=remaining)
                finally:
                    self._waiters -= 1
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify()


__all__ = [
    "DEFAULT_TRUSTED_PROXY_NETWORKS",
    "BoundedConcurrencyGate",
    "ConcurrencyLimitExceeded",
    "IPNetwork",
    "SlidingWindowLimiter",
    "extract_client_source",
    "normalize_source_bucket",
    "parse_trusted_proxies",
]
