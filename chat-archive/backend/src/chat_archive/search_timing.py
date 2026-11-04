"""Request-scoped search timings. No query, identity, URL, SQL or record data."""

from __future__ import annotations

import json
import logging
import secrets
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_logger = logging.getLogger(__name__)
_clock = time.perf_counter
_STAGES = frozenset({
    "handler", "auth", "account", "schema", "index", "browse", "counts", "page",
    "index_status", "schema_upgrade", "hydrate", "records", "avatars", "transcripts", "replies",
    "upstream_headers", "upstream_body", "upstream_decode",
})


def _read_bytes() -> int | None:
    """Linux per-thread physical reads; absent on other platforms."""
    if sys.platform != "linux":
        return None
    try:
        for line in Path("/proc/thread-self/io").read_text().splitlines():
            if line.startswith("read_bytes:"):
                return int(line.split(":", 1)[1])
    except (OSError, ValueError):
        pass
    return None


@dataclass
class _Stage:
    wall_ms: float = 0
    cpu_ms: float = 0
    read_bytes: int | None = None
    calls: int = 0


class SearchTrace:
    def __init__(self, deployment: str) -> None:
        self.deployment = deployment
        self.trace_id = secrets.token_hex(6)
        self.started = _clock()
        self.handler_started: float | None = None
        self.handler_finished: float | None = None
        self.headers_at: float | None = None
        self.status = 500
        self.stages: dict[str, _Stage] = {}

    def header(self) -> str:
        at = self.headers_at if self.headers_at is not None else _clock()
        metrics = {"server": (at - self.started) * 1000}
        if self.handler_started is not None:
            # Dispatch includes framework/dependency work and worker-pool waiting.
            metrics["dispatch"] = (self.handler_started - self.started) * 1000
        if self.handler_finished is not None:
            metrics["encode"] = (at - self.handler_finished) * 1000
        metrics.update({name: stage.wall_ms for name, stage in self.stages.items()})
        return ", ".join(f"{name};dur={max(0, duration):.3f}" for name, duration in metrics.items())

    def log(self, elapsed_ms: float) -> None:
        _logger.warning(json.dumps({
            "event": "slow_search", "trace_id": self.trace_id,
            "deployment": self.deployment, "status": self.status,
            "wall_ms": round(elapsed_ms, 3),
            "dispatch_ms": None if self.handler_started is None else round((self.handler_started - self.started) * 1000, 3),
            "stages": {name: {"wall_ms": round(stage.wall_ms, 3),
                "cpu_ms": round(stage.cpu_ms, 3), "read_bytes": stage.read_bytes,
                "calls": stage.calls} for name, stage in self.stages.items()},
        }, separators=(",", ":")))


_current: ContextVar[SearchTrace | None] = ContextVar("search_trace", default=None)


def current_search_trace() -> SearchTrace | None:
    return _current.get()


@contextmanager
def search_stage(name: str) -> Generator[None, None, None]:
    trace = _current.get()
    if trace is None:
        yield
        return
    if name not in _STAGES:
        raise ValueError("unknown search timing stage")
    if name == "handler" and trace.handler_started is None:
        trace.handler_started = _clock()
    before_io = _read_bytes()
    started, cpu_started = _clock(), time.thread_time()
    try:
        yield
    finally:
        elapsed, cpu = (_clock() - started) * 1000, (time.thread_time() - cpu_started) * 1000
        after_io = _read_bytes()
        stage = trace.stages.setdefault(name, _Stage())
        stage.wall_ms += elapsed
        stage.cpu_ms += cpu
        stage.calls += 1
        if before_io is not None and after_io is not None:
            stage.read_bytes = (stage.read_bytes or 0) + max(0, after_io - before_io)
        if name == "handler":
            trace.handler_finished = _clock()


class SearchTimingMiddleware:
    def __init__(self, app: ASGIApp, *, deployment: str, slow_after_ms: float = 2000) -> None:
        self.app = app
        self.deployment = deployment
        self.slow_after_ms = slow_after_ms

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path in {"/api/v1/search", "/api/v1/remote/search"}
            or (path.startswith("/api/v1/archives/") and path.endswith("/query/search"))
        ):
            await self.app(scope, receive, send)
            return
        trace = SearchTrace(self.deployment)
        token = _current.set(trace)

        async def timed_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                trace.headers_at = _clock()
                trace.status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"server-timing", trace.header().encode("ascii")))
                headers.append((b"x-search-trace", trace.trace_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        finally:
            elapsed_ms = (_clock() - trace.started) * 1000
            if elapsed_ms >= self.slow_after_ms:
                trace.log(elapsed_ms)
            _current.reset(token)
